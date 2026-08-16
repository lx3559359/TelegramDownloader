from __future__ import annotations

import hashlib
import inspect
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from telegram_downloader.catalog import CatalogRepository
from telegram_downloader.content import AccountProfile, ContentDialog, DialogKind
from telegram_downloader.diagnostic_probes import (
    probe_components,
    probe_content_database,
    probe_credentials,
    probe_disk,
    probe_environment,
    probe_project_write,
    probe_task_database,
    probe_telegram,
    probe_update_sources,
)
from telegram_downloader.diagnostic_store import DiagnosticReportStore
from telegram_downloader.diagnostics import DiagnosticsService
from telegram_downloader.domain import (
    IntegrityStatus,
    ItemStatus,
    MediaItem,
    MediaKind,
    ScanFilters,
    SourceKind,
    TaskRecord,
    TaskStatus,
)
from telegram_downloader.paths import PortablePaths
from telegram_downloader.repository import TaskRepository
from telegram_downloader.update_sources import SourceCheck, SourceStatus, UpdateSourceId

NOW = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)


class Probe:
    def __init__(self, probe_id: str, title: str, action) -> None:
        self.id = probe_id
        self.title = title
        self.action = action

    async def run(self, _cancel_event):
        value = self.action()
        return await value if inspect.isawaitable(value) else value


class Gateway:
    async def test_connection(self) -> None:
        return None


class Updates:
    async def check_sources(self):
        verified = SimpleNamespace(
            manifest=SimpleNamespace(version="0.10.0"),
            canonical=b"same",
            signature=b"same",
        )
        return (
            SourceCheck(
                UpdateSourceId.GITHUB,
                SourceStatus.VALID,
                10.0,
                verified=verified,
            ),
            SourceCheck(
                UpdateSourceId.MODELSCOPE,
                SourceStatus.VALID,
                20.0,
                verified=verified,
            ),
        )


def digest_files(paths: PortablePaths) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths.data.rglob("*"):
        if not path.is_file():
            continue
        if path.is_relative_to(paths.diagnostics) or path.is_relative_to(
            paths.diagnostic_temp
        ):
            continue
        values[path.relative_to(paths.root).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return values


@pytest.mark.asyncio
async def test_diagnostic_bundle_is_structured_private_and_read_only(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    task_repository = TaskRepository(paths.database)
    task_repository.initialize()
    filters = ScanFilters(NOW, NOW, frozenset({MediaKind.VIDEO}), 10)
    task = TaskRecord(
        "private-task",
        SourceKind.CHANNEL_OR_GROUP,
        "private-peer",
        "private-group",
        "https://t.me/private-group/42",
        filters,
        TaskStatus.COMPLETED,
        NOW,
        NOW,
    )
    task_repository.create_task(
        task,
        [
            MediaItem(
                "private-item",
                task.id,
                "private-peer",
                42,
                None,
                "private-media",
                MediaKind.VIDEO,
                "private-video.mp4",
                paths.downloads / "private-group" / "private-video.mp4",
                10,
                NOW,
                10,
                ItemStatus.COMPLETED,
                integrity_status=IntegrityStatus.VERIFIED,
                content_sha256="a" * 64,
                verified_at=NOW,
            )
        ],
    )
    catalog = CatalogRepository(paths.catalog_database)
    catalog.initialize()
    catalog.upsert_account(AccountProfile("private-account", "private-name"), NOW)
    catalog.replace_dialogs(
        "private-account",
        [
            ContentDialog(
                "private-account",
                "private-peer",
                "private-group",
                "private-user",
                DialogKind.GROUP,
                False,
                True,
                NOW,
            )
        ],
        NOW,
    )
    private_values = {
        "private-task",
        "private-peer",
        "private-group",
        "private-video.mp4",
        "private-account",
        "private-name",
        "api-hash-secret",
        "session-secret",
        "+8613812345678",
        "https://t.me/private-group/42",
        str(paths.root),
    }
    paths.settings.write_text("api-hash-secret", encoding="utf-8")
    paths.secrets.write_bytes(b"session-secret")
    before = digest_files(paths)

    service = DiagnosticsService(
        (
            Probe(
                "environment",
                "运行环境与路径",
                lambda: probe_environment(
                    paths,
                    frozen=True,
                    windows_x64=True,
                    system_drive="C:",
                ),
            ),
            Probe("project-write", "项目内写入", lambda: probe_project_write(paths)),
            Probe("disk", "磁盘空间", lambda: probe_disk(paths)),
            Probe(
                "components",
                "运行组件",
                lambda: probe_components(
                    {
                        "pyside6": True,
                        "telethon": True,
                        "qasync": True,
                        "qrcode": True,
                        "sqlite": True,
                        "dpapi": True,
                    }
                ),
            ),
            Probe(
                "task-database",
                "下载任务数据库",
                lambda: probe_task_database(paths.database),
            ),
            Probe(
                "content-database",
                "账号内容数据库",
                lambda: probe_content_database(paths.catalog_database),
            ),
            Probe(
                "credentials",
                "登录凭据",
                lambda: probe_credentials(
                    settings_readable=True,
                    secrets_present=True,
                    secrets_decrypted=True,
                ),
            ),
            Probe("telegram", "Telegram 连接", lambda: probe_telegram(Gateway())),
            Probe("updates", "签名更新源", lambda: probe_update_sources(Updates())),
        ),
        app_version="0.10.0",
    )
    report = await service.run()
    store = DiagnosticReportStore(paths, secrets=private_values)
    store.save(report)
    package = store.export(report)

    with ZipFile(package) as archive:
        assert sorted(archive.namelist()) == [
            "diagnostic-report.json",
            "diagnostic-summary.txt",
        ]
        payload = b"\n".join(archive.read(name) for name in archive.namelist())
    assert len(report.results) == 9
    assert report.results[4].metrics["taskCount"] == 1
    assert report.results[5].metrics["accountCount"] == 1
    for private in private_values:
        assert private.encode("utf-8") not in payload
    assert digest_files(paths) == before
