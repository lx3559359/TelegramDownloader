from __future__ import annotations

import asyncio
import sqlite3
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import telegram_downloader.diagnostic_probes as diagnostic_probes
from telegram_downloader.catalog import CATALOG_SCHEMA_VERSION, CatalogRepository
from telegram_downloader.content import (
    AccountProfile,
    ContentDialog,
    ContentSearchQuery,
    DialogKind,
    SearchResult,
)
from telegram_downloader.diagnostic_probes import (
    GIB,
    MIB,
    component_availability,
    managed_writable_paths,
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
from telegram_downloader.diagnostics import DiagnosticStatus
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
from telegram_downloader.download_paths import DownloadPathError, DownloadPathPolicy
from telegram_downloader.gateway import (
    AuthorizationFailureReason,
    SessionExpiredError,
    TransientNetworkError,
)
from telegram_downloader.paths import PortablePaths
from telegram_downloader.repository import TaskRepository
from telegram_downloader.settings import DownloadStorageSettings
from telegram_downloader.subscription_matching import SubscriptionCriteria
from telegram_downloader.subscriptions import (
    SubscriptionRule,
    SubscriptionRun,
    SubscriptionRunStatus,
    SubscriptionState,
)
from telegram_downloader.update_sources import SourceCheck, SourceStatus, UpdateSourceId

DiskUsage = namedtuple("DiskUsage", "total used free")


def usage(total: int, free: int) -> DiskUsage:
    return DiskUsage(total, total - free, free)


def test_managed_writable_paths_are_guarded_and_include_diagnostics(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    values = managed_writable_paths(paths)

    assert len(values) == 16
    assert values["diagnostics"] == paths.diagnostics
    assert values["diagnosticTemp"] == paths.diagnostic_temp
    assert values["maintenanceState"] == paths.storage_maintenance_state
    assert all(paths.guard(path) == path.resolve() for path in values.values())


def test_managed_writable_paths_uses_current_external_download_root(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path / "app")
    paths.ensure_layout()
    external = tmp_path / "external"
    external.mkdir()
    policy = DownloadPathPolicy(paths, DownloadStorageSettings())
    prepared = policy.prepare(DownloadStorageSettings(str(external)))
    policy.apply(prepared)

    values = managed_writable_paths(paths, download_paths=policy)

    assert values["downloads"] == external.resolve()
    assert policy.guard(values["downloads"], allow_root=True) == external.resolve()
    assert all(
        paths.guard(path) == path.resolve()
        for name, path in values.items()
        if name != "downloads"
    )


def test_environment_probe_guards_external_downloads_with_media_policy(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path / "app")
    paths.ensure_layout()
    external = tmp_path / "external"
    external.mkdir()
    policy = DownloadPathPolicy(paths, DownloadStorageSettings())
    prepared = policy.prepare(DownloadStorageSettings(str(external)))
    policy.apply(prepared)

    result = probe_environment(
        paths,
        download_paths=policy,
        frozen=False,
        windows_x64=True,
        system_drive=f"{tmp_path.drive}\\",
    )

    assert result.code != "runtime-path-invalid"
    assert result.metrics["guardedPathCount"] == 16


def test_environment_probe_requires_non_system_volume_for_frozen_runtime(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path)
    system_drive = f"{tmp_path.drive}\\"

    failed = probe_environment(
        paths,
        frozen=True,
        windows_x64=True,
        system_drive=system_drive,
    )
    source_warning = probe_environment(
        paths,
        frozen=False,
        windows_x64=True,
        system_drive=system_drive,
    )
    passed = probe_environment(
        paths,
        frozen=True,
        windows_x64=True,
        system_drive="C:\\" if tmp_path.drive.casefold() != "c:" else "Z:\\",
    )

    assert failed.status is DiagnosticStatus.FAILED
    assert failed.code == "runtime-system-volume"
    assert source_warning.status is DiagnosticStatus.WARNING
    assert source_warning.code == "source-system-volume"
    assert passed.status is DiagnosticStatus.PASSED
    assert passed.metrics == {
        "frozen": True,
        "windowsX64": True,
        "nonSystemVolume": True,
        "guardedPathCount": 16,
    }


def test_environment_probe_fails_unsupported_runtime(tmp_path: Path) -> None:
    result = probe_environment(
        PortablePaths(tmp_path),
        frozen=True,
        windows_x64=False,
        system_drive="C:\\",
    )

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "runtime-unsupported"


def test_disk_probe_uses_fixed_thresholds(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)

    passed = probe_disk(paths, lambda _: usage(10 * GIB, 2 * GIB))
    warning = probe_disk(paths, lambda _: usage(10 * GIB, 512 * MIB))
    failed = probe_disk(paths, lambda _: usage(10 * GIB, 128 * MIB))

    assert (passed.status, passed.code) == (
        DiagnosticStatus.PASSED,
        "disk-space-ok",
    )
    assert (warning.status, warning.code) == (
        DiagnosticStatus.WARNING,
        "disk-space-low",
    )
    assert (failed.status, failed.code) == (
        DiagnosticStatus.FAILED,
        "disk-space-critical",
    )
    assert warning.metrics == {
        "totalBytes": 10 * GIB,
        "freeBytes": 512 * MIB,
    }


def test_disk_probe_maps_provider_failure_to_fixed_safe_result(tmp_path: Path) -> None:
    def fail(_path: Path) -> DiskUsage:
        raise OSError(r"D:\\private\\disk")

    result = probe_disk(PortablePaths(tmp_path), fail)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "disk-unavailable"
    assert "private" not in result.summary
    assert result.metrics == {}


def test_disk_probe_checks_same_volume_once_and_different_volumes_twice(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path)
    calls: list[Path] = []

    def record(path: Path) -> DiskUsage:
        calls.append(path)
        return usage(10 * GIB, 2 * GIB)

    same = probe_disk(paths, record, download_root=paths.downloads)

    assert len(calls) == 1
    assert same.metrics == {
        "totalBytes": 10 * GIB,
        "freeBytes": 2 * GIB,
        "downloadSameVolume": True,
        "downloadTotalBytes": 10 * GIB,
        "downloadFreeBytes": 2 * GIB,
    }

    calls.clear()
    other_drive = Path("Z:/Media" if tmp_path.drive.casefold() != "z:" else "Y:/Media")
    different = probe_disk(paths, record, download_root=other_drive)

    assert len(calls) == 2
    assert different.metrics["downloadSameVolume"] is False
    assert different.metrics["downloadTotalBytes"] == 10 * GIB
    assert different.metrics["downloadFreeBytes"] == 2 * GIB


def test_disk_probe_distinguishes_directory_mounted_volume_with_same_anchor(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path)
    download_root = tmp_path / "mounted-download"
    download_root.mkdir()
    calls: list[Path] = []

    def disk_usage(path: Path) -> DiskUsage:
        calls.append(path)
        free = 2 * GIB if path == paths.root else 128 * MIB
        return usage(8 * GIB, free)

    result = probe_disk(
        paths,
        disk_usage,
        download_root=download_root,
        volume_identity_provider=(
            lambda path: "app-volume" if path == paths.root else "download-volume"
        ),
    )

    assert calls == [paths.root, download_root]
    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "download-disk-space-critical"
    assert result.metrics["downloadSameVolume"] is False
    assert result.metrics["downloadFreeBytes"] == 128 * MIB


@pytest.mark.parametrize(
    ("app_free", "download_free", "expected_status", "expected_code"),
    [
        (128 * MIB, 2 * GIB, DiagnosticStatus.FAILED, "disk-space-critical"),
        (512 * MIB, 2 * GIB, DiagnosticStatus.WARNING, "disk-space-low"),
        (
            2 * GIB,
            128 * MIB,
            DiagnosticStatus.FAILED,
            "download-disk-space-critical",
        ),
        (
            2 * GIB,
            512 * MIB,
            DiagnosticStatus.WARNING,
            "download-disk-space-low",
        ),
    ],
)
def test_disk_probe_distinguishes_app_and_download_thresholds(
    tmp_path: Path,
    app_free: int,
    download_free: int,
    expected_status: DiagnosticStatus,
    expected_code: str,
) -> None:
    paths = PortablePaths(tmp_path)
    other_drive = Path("Z:/Media" if tmp_path.drive.casefold() != "z:" else "Y:/Media")

    def disk_usage(path: Path) -> DiskUsage:
        free = app_free if path.drive.casefold() == tmp_path.drive.casefold() else download_free
        return usage(8 * GIB, free)

    result = probe_disk(paths, disk_usage, download_root=other_drive)

    assert (result.status, result.code) == (expected_status, expected_code)
    assert result.metrics["downloadFreeBytes"] == download_free
    assert str(other_drive) not in result.summary


def test_disk_probe_maps_download_volume_failure_to_fixed_safe_result(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path)
    other_drive = Path("Z:/Media" if tmp_path.drive.casefold() != "z:" else "Y:/Media")

    def fail_download(path: Path) -> DiskUsage:
        if path.drive.casefold() != tmp_path.drive.casefold():
            raise OSError(r"Z:\\private\\media")
        return usage(8 * GIB, 2 * GIB)

    result = probe_disk(paths, fail_download, download_root=other_drive)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "download-disk-unavailable"
    assert result.metrics == {
        "totalBytes": 8 * GIB,
        "freeBytes": 2 * GIB,
        "downloadSameVolume": False,
    }
    assert "private" not in result.summary


def test_write_probe_cleans_project_local_marker(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()

    result = probe_project_write(paths, marker=b"diagnostic", token_factory=lambda: "fixed")

    assert result.status is DiagnosticStatus.PASSED
    assert result.code == "project-write-ok"
    assert list(paths.diagnostic_temp.iterdir()) == []


def test_write_probe_cleans_partial_marker_after_failure(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()

    def failed_writer(path: Path, content: bytes) -> None:
        path.write_bytes(content[:1])
        raise OSError(r"D:\\private\\failed")

    result = probe_project_write(
        paths,
        marker=b"diagnostic",
        token_factory=lambda: "fixed",
        writer=failed_writer,
    )

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "project-write-failed"
    assert "private" not in result.summary
    assert list(paths.diagnostic_temp.iterdir()) == []


def test_write_probe_checks_active_download_root(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    checked: list[Path] = []
    policy = DownloadPathPolicy(
        paths,
        DownloadStorageSettings(),
        probe=checked.append,
    )

    result = probe_project_write(paths, download_paths=policy)

    assert result.status is DiagnosticStatus.PASSED
    assert result.metrics["downloadWritable"] is True
    assert checked == [policy.current_root]


def test_write_probe_maps_active_download_failure_to_fixed_safe_result(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()

    def fail(_root: Path) -> None:
        raise DownloadPathError(r"Z:\\private\\media")

    policy = DownloadPathPolicy(
        paths,
        DownloadStorageSettings(),
        probe=fail,
    )

    result = probe_project_write(paths, download_paths=policy)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "download-write-failed"
    assert result.metrics == {"downloadWritable": False}
    assert "private" not in result.summary


def test_component_availability_uses_six_fixed_keys() -> None:
    imported: list[str] = []

    def importer(name: str) -> object:
        imported.append(name)
        if name == "qrcode":
            raise ImportError(name)
        return object()

    availability = component_availability(importer, dpapi_available=True)

    assert availability == {
        "pyside6": True,
        "telethon": True,
        "qasync": True,
        "qrcode": False,
        "sqlite": True,
        "dpapi": True,
    }
    assert imported == ["PySide6", "telethon", "qasync", "qrcode", "sqlite3"]


@pytest.mark.parametrize("missing", ["pyside6", "telethon", "qasync", "qrcode", "sqlite", "dpapi"])
def test_components_probe_fails_when_any_required_component_is_missing(
    missing: str,
) -> None:
    availability = {
        "pyside6": True,
        "telethon": True,
        "qasync": True,
        "qrcode": True,
        "sqlite": True,
        "dpapi": True,
    }
    availability[missing] = False

    result = probe_components(availability)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "component-missing"
    assert result.metrics == availability


def test_task_database_probe_reports_schema_and_aggregate_counts_only(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tasks.sqlite3"
    repository = TaskRepository(database)
    repository.initialize()
    now = datetime(2026, 8, 16, tzinfo=UTC)
    filters = ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 10)
    task = TaskRecord(
        "task-1",
        SourceKind.CHANNEL_OR_GROUP,
        "private-peer",
        "private-group",
        "https://t.me/private/1",
        filters,
        TaskStatus.COMPLETED,
        now,
        now,
    )
    items = [
        MediaItem(
            f"item-{index}",
            task.id,
            "private-peer",
            index,
            None,
            f"media-{index}",
            MediaKind.VIDEO,
            f"private-{index}.mp4",
            tmp_path / f"private-{index}.mp4",
            10,
            now,
            10,
            ItemStatus.COMPLETED,
            integrity_status=(
                IntegrityStatus.VERIFIED if index == 1 else IntegrityStatus.UNVERIFIED
            ),
            content_sha256=("a" * 64 if index == 1 else None),
            verified_at=(now if index == 1 else None),
        )
        for index in (1, 2)
    ]
    repository.create_task(task, items)

    result = probe_task_database(database)

    assert result.status is DiagnosticStatus.PASSED
    assert result.code == "task-database-ok"
    assert result.metrics == {
        "taskCount": 1,
        "mediaCount": 2,
        "schemaCompatible": True,
        "foreignKeysValid": True,
        "stateValuesValid": True,
        "taskStatusCompleted": 1,
        "itemStatusCompleted": 2,
        "integrityStatusUnverified": 1,
        "integrityStatusVerified": 1,
    }
    serialized = repr(dict(result.metrics)) + result.summary
    assert "private" not in serialized
    assert str(tmp_path) not in serialized


def test_task_database_probe_reads_committed_wal_state(tmp_path: Path) -> None:
    database = tmp_path / "tasks.sqlite3"
    TaskRepository(database).initialize()
    now = datetime(2026, 8, 16, tzinfo=UTC).isoformat()
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO tasks("
            "id, source_kind, source_ref, source_title, source_url, date_from_utc, "
            "date_to_utc, media_kinds, item_limit, status, created_at, updated_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task-wal",
                SourceKind.CHANNEL_OR_GROUP.value,
                "private-peer",
                "private-title",
                "https://t.me/private",
                now,
                now,
                MediaKind.VIDEO.value,
                1,
                TaskStatus.QUEUED.value,
                now,
                now,
            ),
        )
        writer.commit()
        assert database.with_name(f"{database.name}-wal").is_file()

        result = probe_task_database(database)

        assert result.status is DiagnosticStatus.PASSED
        assert result.metrics["taskCount"] == 1
    finally:
        writer.close()


def test_content_database_probe_reads_committed_wal_state(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    CatalogRepository(database).initialize()
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO accounts(account_id, display_name, last_used_at) "
            "VALUES(?, ?, ?)",
            ("private-account", "private-name", "2026-08-16T00:00:00+00:00"),
        )
        writer.commit()
        assert database.with_name(f"{database.name}-wal").is_file()

        result = probe_content_database(database)

        assert result.status is DiagnosticStatus.PASSED
        assert result.metrics["accountCount"] == 1
    finally:
        writer.close()


def test_task_database_probe_rejects_dangling_foreign_key(tmp_path: Path) -> None:
    database = tmp_path / "tasks.sqlite3"
    TaskRepository(database).initialize()
    now = datetime(2026, 8, 16, tzinfo=UTC).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO media_items("
            "id, task_id, peer_ref, message_id, media_id, media_kind, original_name, "
            "target_path, message_date_utc, status"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "item-orphan",
                "private-missing-task",
                "private-peer",
                7,
                "private-media",
                MediaKind.VIDEO.value,
                "private.mp4",
                str(tmp_path / "private.mp4"),
                now,
                ItemStatus.QUEUED.value,
            ),
        )

    result = probe_task_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-semantics-invalid"
    assert result.metrics["foreignKeysValid"] is False
    assert "private" not in result.summary + repr(dict(result.metrics))


def _create_task_probe_schema(
    database: Path,
    *,
    missing_column: str | None = None,
    task_foreign_key: str | None = "REFERENCES tasks(id) ON DELETE CASCADE",
    duplicate_task_foreign_key: bool = False,
    task_media_kinds_declaration: str = "TEXT NOT NULL",
) -> None:
    task_columns = [
        "id TEXT PRIMARY KEY",
        "source_kind TEXT NOT NULL",
        "source_ref TEXT",
        "source_title TEXT",
        "source_url TEXT",
        "date_from_utc TEXT",
        "date_to_utc TEXT",
        f"media_kinds {task_media_kinds_declaration}",
        "item_limit INTEGER",
        "status TEXT NOT NULL",
        "created_at TEXT",
        "updated_at TEXT",
        "last_error TEXT",
        "display_title TEXT",
        "archived_at TEXT",
        "queue_priority INTEGER",
        "pause_reason TEXT",
    ]
    item_columns = [
        "id TEXT PRIMARY KEY",
        "task_id TEXT NOT NULL",
        "peer_ref TEXT",
        "message_id INTEGER",
        "grouped_id INTEGER",
        "media_id TEXT",
        "media_kind TEXT NOT NULL",
        "original_name TEXT",
        "target_path TEXT",
        "expected_size INTEGER",
        "message_date_utc TEXT",
        "downloaded_bytes INTEGER",
        "status TEXT NOT NULL",
        "retry_count INTEGER",
        "last_error TEXT",
        "integrity_status TEXT NOT NULL",
        "content_sha256 TEXT",
        "verified_at TEXT",
    ]
    if missing_column is not None:
        table, column = missing_column.split(".", 1)
        selected = task_columns if table == "tasks" else item_columns
        selected[:] = [value for value in selected if not value.startswith(f"{column} ")]
    if task_foreign_key is not None and missing_column != "media_items.task_id":
        foreign_key = f"FOREIGN KEY(task_id) {task_foreign_key}"
        item_columns.extend(
            [foreign_key] * (2 if duplicate_task_foreign_key else 1)
        )
    with sqlite3.connect(database) as connection:
        connection.executescript(
            f"CREATE TABLE tasks ({','.join(task_columns)});"
            f"CREATE TABLE media_items ({','.join(item_columns)});"
        )


@pytest.mark.parametrize(
    "missing_column",
    ["tasks.media_kinds", "media_items.task_id"],
)
def test_task_database_probe_rejects_missing_runtime_column(
    tmp_path: Path,
    missing_column: str,
) -> None:
    database = tmp_path / "tasks-missing-column.sqlite3"
    _create_task_probe_schema(database, missing_column=missing_column)

    result = probe_task_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-schema-incompatible"
    assert result.metrics == {"schemaCompatible": False}


@pytest.mark.parametrize(
    "task_foreign_key",
    [None, "REFERENCES tasks(id) ON DELETE SET NULL"],
)
def test_task_database_probe_rejects_missing_or_wrong_foreign_key_definition(
    tmp_path: Path,
    task_foreign_key: str | None,
) -> None:
    database = tmp_path / "tasks-wrong-foreign-key.sqlite3"
    _create_task_probe_schema(database, task_foreign_key=task_foreign_key)

    result = probe_task_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-semantics-invalid"
    assert result.metrics["foreignKeysValid"] is False


def test_task_database_probe_rejects_duplicate_foreign_key_definition(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tasks-duplicate-foreign-key.sqlite3"
    _create_task_probe_schema(database, duplicate_task_foreign_key=True)

    result = probe_task_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-semantics-invalid"
    assert result.metrics["foreignKeysValid"] is False


def test_task_database_probe_rejects_private_unknown_media_kind_set(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tasks.sqlite3"
    repository = TaskRepository(database)
    repository.initialize()
    now = datetime(2026, 8, 16, tzinfo=UTC)
    repository.create_task(
        TaskRecord(
            "task-media-kinds",
            SourceKind.CHANNEL_OR_GROUP,
            "private-peer",
            "private-title",
            "https://t.me/private",
            ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 1),
            TaskStatus.QUEUED,
            now,
            now,
        ),
        [],
    )
    private_token = "private-invalid-media-kind"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE tasks SET media_kinds=?", (private_token,))

    result = probe_task_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-semantics-invalid"
    assert result.metrics["stateValuesValid"] is False
    assert private_token not in result.summary + result.code + repr(dict(result.metrics))


def test_task_database_probe_rejects_oversized_repeated_valid_media_kinds(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tasks.sqlite3"
    _create_task_probe_schema(database)
    oversized = ",".join([MediaKind.VIDEO.value] * 1024)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO tasks(id, source_kind, media_kinds, status) "
            "VALUES(?, ?, ?, ?)",
            (
                "task-oversized-media-kinds",
                SourceKind.CHANNEL_OR_GROUP.value,
                oversized,
                TaskStatus.QUEUED.value,
            ),
        )

    result = probe_task_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-semantics-invalid"
    assert result.metrics["stateValuesValid"] is False
    assert oversized not in result.summary + result.code + repr(dict(result.metrics))


@pytest.mark.parametrize(
    ("raw_value", "declaration", "private_marker"),
    [
        (None, "TEXT", None),
        (sqlite3.Binary(b"private-media-kinds-blob"), "TEXT NOT NULL", "private-media-kinds-blob"),
    ],
    ids=["null", "blob"],
)
def test_task_database_probe_rejects_non_text_media_kind_set(
    tmp_path: Path,
    raw_value: object,
    declaration: str,
    private_marker: str | None,
) -> None:
    database = tmp_path / "tasks-non-text-media-kinds.sqlite3"
    _create_task_probe_schema(
        database,
        task_media_kinds_declaration=declaration,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO tasks(id, source_kind, media_kinds, status) "
            "VALUES(?, ?, ?, ?)",
            (
                "task-non-text-media-kinds",
                SourceKind.CHANNEL_OR_GROUP.value,
                raw_value,
                TaskStatus.QUEUED.value,
            ),
        )

    result = probe_task_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-semantics-invalid"
    assert result.metrics["stateValuesValid"] is False
    if private_marker is not None:
        assert private_marker not in result.summary + repr(dict(result.metrics))


@pytest.mark.parametrize(
    ("raw_value", "expected_status", "expected_state_values_valid"),
    [
        (",,,", DiagnosticStatus.PASSED, True),
        ("   ", DiagnosticStatus.FAILED, False),
    ],
    ids=["empty-tokens", "whitespace-token"],
)
def test_task_database_probe_handles_empty_and_whitespace_media_kind_tokens(
    tmp_path: Path,
    raw_value: str,
    expected_status: DiagnosticStatus,
    expected_state_values_valid: bool,
) -> None:
    database = tmp_path / "tasks-media-kind-token-boundary.sqlite3"
    _create_task_probe_schema(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO tasks(id, source_kind, media_kinds, status) "
            "VALUES(?, ?, ?, ?)",
            (
                "task-media-kind-token-boundary",
                SourceKind.CHANNEL_OR_GROUP.value,
                raw_value,
                TaskStatus.QUEUED.value,
            ),
        )

    result = probe_task_database(database)

    assert result.status is expected_status
    assert result.metrics["stateValuesValid"] is expected_state_values_valid


def test_task_database_probe_accepts_runtime_readable_empty_media_kind_set(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tasks.sqlite3"
    repository = TaskRepository(database)
    repository.initialize()
    now = datetime(2026, 8, 16, tzinfo=UTC).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO tasks("
            "id, source_kind, source_ref, source_title, source_url, date_from_utc, "
            "date_to_utc, media_kinds, item_limit, status, created_at, updated_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task-empty-media-kinds",
                SourceKind.CHANNEL_OR_GROUP.value,
                "private-peer",
                "private-title",
                "https://t.me/private",
                now,
                now,
                "",
                1,
                TaskStatus.QUEUED.value,
                now,
                now,
            ),
        )

    result = probe_task_database(database)

    assert result.status is DiagnosticStatus.PASSED
    assert result.metrics["stateValuesValid"] is True


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("tasks", "source_kind"),
        ("tasks", "status"),
        ("tasks", "pause_reason"),
        ("media_items", "media_kind"),
        ("media_items", "status"),
        ("media_items", "integrity_status"),
    ],
)
def test_task_database_probe_rejects_unknown_domain_value(
    tmp_path: Path,
    table: str,
    column: str,
) -> None:
    database = tmp_path / "tasks.sqlite3"
    repository = TaskRepository(database)
    repository.initialize()
    now = datetime(2026, 8, 16, tzinfo=UTC)
    task = TaskRecord(
        "task-domain",
        SourceKind.CHANNEL_OR_GROUP,
        "private-peer",
        "private-title",
        "https://t.me/private",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 1),
        TaskStatus.QUEUED,
        now,
        now,
    )
    repository.create_task(
        task,
        [
            MediaItem(
                "item-domain",
                task.id,
                "private-peer",
                7,
                None,
                "private-media",
                MediaKind.VIDEO,
                "private.mp4",
                tmp_path / "private.mp4",
                8,
                now,
            )
        ],
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"UPDATE {table} SET {column} = ?",
            ("private-invalid-value",),
        )

    result = probe_task_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-semantics-invalid"
    assert result.metrics["stateValuesValid"] is False
    assert "private-invalid-value" not in result.summary + repr(dict(result.metrics))


def test_content_database_probe_reports_schema_and_counts_only(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    repository = CatalogRepository(database)
    repository.initialize()
    now = datetime(2026, 8, 16, tzinfo=UTC)
    repository.upsert_account(AccountProfile("private-account", "private-name"), now)
    repository.replace_dialogs(
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
                now,
            )
        ],
        now,
    )

    result = probe_content_database(database)

    assert result.status is DiagnosticStatus.PASSED
    assert result.code == "content-database-ok"
    assert result.metrics == {
        "schemaVersion": CATALOG_SCHEMA_VERSION,
        "schemaCompatible": True,
        "foreignKeysValid": True,
        "stateValuesValid": True,
        "accountCount": 1,
        "dialogCount": 1,
        "searchCount": 0,
        "searchResultCount": 0,
        "subscriptionCount": 0,
        "subscriptionRunCount": 0,
    }
    serialized = repr(dict(result.metrics)) + result.summary
    assert "private" not in serialized
    assert str(tmp_path) not in serialized


def _seed_content_domain_rows(database: Path) -> None:
    repository = CatalogRepository(database)
    repository.initialize()
    now = datetime(2026, 8, 16, tzinfo=UTC)
    account_id = "private-account"
    peer_ref = "private-peer"
    repository.upsert_account(AccountProfile(account_id, "private-name"), now)
    repository.replace_dialogs(
        account_id,
        [
            ContentDialog(
                account_id,
                peer_ref,
                "private-group",
                "private-user",
                DialogKind.GROUP,
                False,
                True,
                now,
            )
        ],
        now,
    )
    session = repository.begin_search(
        "search-domain",
        account_id,
        peer_ref,
        "private-group",
        ContentSearchQuery(
            "private-keyword",
            ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 1),
        ),
        now,
    )
    repository.save_search_page(
        account_id,
        session.id,
        session.generation,
        [
            SearchResult(
                "result-domain",
                session.id,
                account_id,
                peer_ref,
                7,
                None,
                "private-media",
                MediaKind.VIDEO,
                "private.mp4",
                8,
                now,
                "private-excerpt",
                "private-thumbnail",
            )
        ],
    )
    rule = SubscriptionRule(
        id="rule-domain",
        account_id=account_id,
        peer_ref=peer_ref,
        dialog_title="private-group",
        criteria=SubscriptionCriteria(("private-keyword",)),
        media_kinds=frozenset({MediaKind.VIDEO}),
        interval_minutes=30,
        history_days=0,
        enabled=True,
        state=SubscriptionState.WAITING,
        last_message_id=7,
        backfill_from_utc=None,
        backfill_through_id=None,
        next_run_at=now,
        last_run_at=None,
        last_error=None,
        failure_count=0,
        created_at=now,
        updated_at=now,
    )
    repository.save_subscription(rule)
    repository.save_subscription_run(
        SubscriptionRun(
            id="run-domain",
            rule_id=rule.id,
            account_id=account_id,
            started_at=now,
            finished_at=now,
            status=SubscriptionRunStatus.COMPLETED,
            inspected=1,
            keyword_hits=1,
            matched=1,
            queued=1,
            duplicate=0,
        )
    )


def _create_content_probe_schema(
    database: Path,
    *,
    missing_column: str | None = None,
    broken_foreign_key: str | None = None,
    search_media_kinds_declaration: str = "TEXT NOT NULL",
    invalid_dialog_parent_column: bool = False,
) -> None:
    columns = {
        "accounts": [
            "account_id TEXT PRIMARY KEY",
            "display_name TEXT",
            "last_used_at TEXT",
        ],
        "dialogs": [
            "account_id TEXT NOT NULL",
            "peer_ref TEXT NOT NULL",
            "title TEXT",
            "username TEXT",
            "kind TEXT NOT NULL",
            "archived INTEGER",
            "available INTEGER",
            "last_synced_at TEXT",
            "PRIMARY KEY(account_id, peer_ref)",
        ],
        "search_sessions": [
            "id TEXT PRIMARY KEY",
            "account_id TEXT NOT NULL",
            "peer_ref TEXT",
            "dialog_title TEXT",
            "keyword TEXT",
            "normalized_keyword TEXT",
            "date_from_utc TEXT",
            "date_to_utc TEXT",
            f"media_kinds {search_media_kinds_declaration}",
            "item_limit INTEGER",
            "filters_fingerprint TEXT",
            "status TEXT NOT NULL",
            "generation INTEGER",
            "next_offset_id INTEGER",
            "exhausted INTEGER",
            "result_count INTEGER",
            "created_at TEXT",
            "updated_at TEXT",
            "last_error TEXT",
            "scope TEXT NOT NULL",
            "cursor_json TEXT",
        ],
        "search_results": [
            "id TEXT PRIMARY KEY",
            "search_id TEXT NOT NULL",
            "account_id TEXT",
            "peer_ref TEXT",
            "message_id INTEGER",
            "grouped_id INTEGER",
            "media_id TEXT",
            "media_kind TEXT NOT NULL",
            "original_name TEXT",
            "expected_size INTEGER",
            "message_date_utc TEXT",
            "excerpt TEXT",
            "thumbnail_key TEXT",
            "selected INTEGER",
            "available INTEGER",
            "queued INTEGER",
            "generation INTEGER",
            "source_title TEXT",
            "source_kind TEXT NOT NULL",
        ],
        "subscription_rules": [
            "id TEXT PRIMARY KEY",
            "account_id TEXT NOT NULL",
            "peer_ref TEXT NOT NULL",
            "dialog_title TEXT",
            "keyword TEXT",
            "normalized_keyword TEXT",
            "media_kinds TEXT NOT NULL",
            "interval_minutes INTEGER",
            "enabled INTEGER",
            "state TEXT NOT NULL",
            "last_message_id INTEGER",
            "next_run_at TEXT",
            "last_run_at TEXT",
            "last_error TEXT",
            "failure_count INTEGER",
            "created_at TEXT",
            "updated_at TEXT",
            "include_keywords_json TEXT",
            "exclude_keywords_json TEXT",
            "match_mode TEXT NOT NULL",
            "matcher_fingerprint TEXT",
            "history_days INTEGER",
            "backfill_from_utc TEXT",
            "backfill_through_id INTEGER",
        ],
        "subscription_runs": [
            "id TEXT PRIMARY KEY",
            "rule_id TEXT NOT NULL",
            "account_id TEXT NOT NULL",
            "started_at TEXT",
            "finished_at TEXT",
            "status TEXT NOT NULL",
            "inspected INTEGER",
            "keyword_hits INTEGER NOT NULL",
            "matched INTEGER",
            "queued INTEGER",
            "duplicate INTEGER",
            "error TEXT",
        ],
    }
    if missing_column is not None:
        table, column = missing_column.split(".", 1)
        columns[table] = [
            value
            for value in columns[table]
            if not value.startswith(f"{column} ")
        ]
    foreign_keys = {
        "dialogs-account": (
            "dialogs",
            "FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE CASCADE",
            "FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE SET NULL",
        ),
        "search-sessions-account": (
            "search_sessions",
            "FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE CASCADE",
            "FOREIGN KEY(account_id) REFERENCES accounts(account_id)",
        ),
        "search-results-session": (
            "search_results",
            "FOREIGN KEY(search_id) REFERENCES search_sessions(id) ON DELETE CASCADE",
            "FOREIGN KEY(search_id) REFERENCES search_sessions(id) ON DELETE SET NULL",
        ),
        "subscription-rules-dialog": (
            "subscription_rules",
            "FOREIGN KEY(account_id, peer_ref) REFERENCES dialogs(account_id, peer_ref)",
            "FOREIGN KEY(account_id, peer_ref) "
            "REFERENCES dialogs(account_id, peer_ref) ON DELETE CASCADE",
        ),
        "subscription-rules-account": (
            "subscription_rules",
            "FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE CASCADE",
            "FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE SET NULL",
        ),
        "subscription-runs-rule": (
            "subscription_runs",
            "FOREIGN KEY(rule_id) REFERENCES subscription_rules(id) ON DELETE CASCADE",
            "FOREIGN KEY(rule_id) REFERENCES subscription_rules(id)",
        ),
        "subscription-runs-account": (
            "subscription_runs",
            "FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE CASCADE",
            "FOREIGN KEY(account_id) REFERENCES accounts(account_id)",
        ),
    }
    if invalid_dialog_parent_column:
        foreign_keys["dialogs-account"] = (
            "dialogs",
            "FOREIGN KEY(account_id) "
            "REFERENCES accounts(missing_parent_key) ON DELETE CASCADE",
            "",
        )
    for key, (table, correct, wrong) in foreign_keys.items():
        columns[table].append(wrong if key == broken_foreign_key else correct)
    with sqlite3.connect(database) as connection:
        for table, definitions in columns.items():
            connection.execute(f"CREATE TABLE {table} ({','.join(definitions)})")
        connection.execute(f"PRAGMA user_version={CATALOG_SCHEMA_VERSION}")


@pytest.mark.parametrize(
    ("database_kind", "table", "column"),
    [
        ("task", "tasks", "source_ref"),
        ("task", "media_items", "original_name"),
        ("content", "search_sessions", "keyword"),
        ("content", "search_results", "original_name"),
    ],
)
def test_database_probe_rejects_missing_repository_read_column(
    tmp_path: Path,
    database_kind: str,
    table: str,
    column: str,
) -> None:
    database = tmp_path / f"{database_kind}.sqlite3"
    if database_kind == "task":
        TaskRepository(database).initialize()
        probe = probe_task_database
        expected_metrics = {"schemaCompatible": False}
    else:
        CatalogRepository(database).initialize()
        probe = probe_content_database
        expected_metrics = {
            "schemaVersion": CATALOG_SCHEMA_VERSION,
            "schemaCompatible": False,
        }
    with sqlite3.connect(database) as connection:
        connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")

    result = probe(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-schema-incompatible"
    assert result.metrics == expected_metrics
    assert column not in result.summary + result.code + repr(dict(result.metrics))


def _schema_columns(database: Path, tables: tuple[str, ...]) -> dict[str, set[str]]:
    with sqlite3.connect(database) as connection:
        return {
            table: {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for table in tables
        }


def test_task_required_column_contract_matches_authoritative_schema(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tasks.sqlite3"
    TaskRepository(database).initialize()

    actual = _schema_columns(database, ("tasks", "media_items"))

    assert actual == diagnostic_probes._TASK_REQUIRED_COLUMNS


def test_content_required_column_contract_matches_authoritative_schema(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    CatalogRepository(database).initialize()
    tables = (
        "accounts",
        "dialogs",
        "search_sessions",
        "search_results",
        "subscription_rules",
        "subscription_runs",
    )

    actual = _schema_columns(database, tables)

    assert actual == diagnostic_probes._CONTENT_REQUIRED_COLUMNS


def test_content_probe_rejects_dangling_foreign_key(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    CatalogRepository(database).initialize()
    now = datetime(2026, 8, 16, tzinfo=UTC).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO dialogs("
            "account_id, peer_ref, title, username, kind, archived, available, "
            "last_synced_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "private-missing-account",
                "private-peer",
                "private-title",
                "private-user",
                DialogKind.GROUP.value,
                0,
                1,
                now,
            ),
        )

    result = probe_content_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-semantics-invalid"
    assert result.metrics["foreignKeysValid"] is False
    assert "private" not in result.summary + repr(dict(result.metrics))


@pytest.mark.parametrize(
    "missing_column",
    [
        "search_sessions.media_kinds",
        "search_results.source_kind",
        "subscription_rules.match_mode",
        "subscription_rules.media_kinds",
    ],
)
def test_content_database_probe_rejects_missing_runtime_column(
    tmp_path: Path,
    missing_column: str,
) -> None:
    database = tmp_path / "content-missing-column.sqlite3"
    _create_content_probe_schema(database, missing_column=missing_column)

    result = probe_content_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-schema-incompatible"
    assert result.metrics == {
        "schemaVersion": CATALOG_SCHEMA_VERSION,
        "schemaCompatible": False,
    }


@pytest.mark.parametrize(
    "broken_foreign_key",
    [
        "dialogs-account",
        "search-sessions-account",
        "search-results-session",
        "subscription-rules-dialog",
        "subscription-rules-account",
        "subscription-runs-rule",
        "subscription-runs-account",
    ],
)
def test_content_database_probe_rejects_wrong_foreign_key_definition(
    tmp_path: Path,
    broken_foreign_key: str,
) -> None:
    database = tmp_path / "content-wrong-foreign-key.sqlite3"
    _create_content_probe_schema(database, broken_foreign_key=broken_foreign_key)

    result = probe_content_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-semantics-invalid"
    assert result.metrics["foreignKeysValid"] is False


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("dialogs", "kind"),
        ("search_sessions", "status"),
        ("search_sessions", "scope"),
        ("search_results", "media_kind"),
        ("subscription_rules", "state"),
        ("subscription_runs", "status"),
    ],
)
def test_content_probe_rejects_unknown_domain_value(
    tmp_path: Path,
    table: str,
    column: str,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    _seed_content_domain_rows(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"UPDATE {table} SET {column} = ?",
            ("private-invalid-value",),
        )

    result = probe_content_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-semantics-invalid"
    assert result.metrics["stateValuesValid"] is False
    assert "private-invalid-value" not in result.summary + repr(dict(result.metrics))


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("search_sessions", "media_kinds"),
        ("search_results", "source_kind"),
        ("subscription_rules", "match_mode"),
        ("subscription_rules", "media_kinds"),
    ],
)
def test_content_probe_rejects_private_unknown_runtime_value(
    tmp_path: Path,
    table: str,
    column: str,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    _seed_content_domain_rows(database)
    private_token = "private-invalid-runtime-value"
    with sqlite3.connect(database) as connection:
        connection.execute(f"UPDATE {table} SET {column}=?", (private_token,))

    result = probe_content_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-semantics-invalid"
    assert result.metrics["stateValuesValid"] is False
    assert private_token not in result.summary + result.code + repr(dict(result.metrics))


def test_content_probe_rejects_oversized_repeated_valid_media_kinds(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    _seed_content_domain_rows(database)
    oversized = ",".join([MediaKind.VIDEO.value] * 1024)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE search_sessions SET media_kinds=?",
            (oversized,),
        )

    result = probe_content_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-semantics-invalid"
    assert result.metrics["stateValuesValid"] is False
    assert oversized not in result.summary + result.code + repr(dict(result.metrics))


@pytest.mark.parametrize(
    ("raw_value", "declaration", "private_marker"),
    [
        (None, "TEXT", None),
        (
            sqlite3.Binary(b"private-search-media-kinds-blob"),
            "TEXT NOT NULL",
            "private-search-media-kinds-blob",
        ),
    ],
    ids=["null", "blob"],
)
def test_content_probe_rejects_non_text_media_kind_set(
    tmp_path: Path,
    raw_value: object,
    declaration: str,
    private_marker: str | None,
) -> None:
    database = tmp_path / "content-non-text-media-kinds.sqlite3"
    _create_content_probe_schema(
        database,
        search_media_kinds_declaration=declaration,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO accounts(account_id) VALUES(?)",
            ("private-account",),
        )
        connection.execute(
            "INSERT INTO search_sessions(id, account_id, status, scope, media_kinds) "
            "VALUES(?, ?, ?, ?, ?)",
            (
                "search-non-text-media-kinds",
                "private-account",
                "running",
                "single_dialog",
                raw_value,
            ),
        )

    result = probe_content_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-semantics-invalid"
    assert result.metrics["stateValuesValid"] is False
    if private_marker is not None:
        assert private_marker not in result.summary + repr(dict(result.metrics))


@pytest.mark.parametrize("raw_value", [",,,", "   "], ids=["empty-tokens", "whitespace-token"])
def test_content_probe_rejects_empty_and_whitespace_media_kind_tokens(
    tmp_path: Path,
    raw_value: str,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    _seed_content_domain_rows(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE search_sessions SET media_kinds=?",
            (raw_value,),
        )

    result = probe_content_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-semantics-invalid"
    assert result.metrics["stateValuesValid"] is False


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("search_sessions", "media_kinds"),
        ("subscription_rules", "media_kinds"),
    ],
)
def test_content_probe_rejects_runtime_unreadable_empty_media_kind_set(
    tmp_path: Path,
    table: str,
    column: str,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    _seed_content_domain_rows(database)
    with sqlite3.connect(database) as connection:
        connection.execute(f"UPDATE {table} SET {column}='' ")

    result = probe_content_database(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-semantics-invalid"
    assert result.metrics["stateValuesValid"] is False


@pytest.mark.parametrize(
    ("probe", "database_name", "expected_first_check"),
    [
        (probe_task_database, "tasks.sqlite3", ("tasks", "source_kind")),
        (probe_content_database, "catalog.sqlite3", ("dialogs", "kind")),
    ],
)
def test_database_state_value_validation_short_circuits_after_first_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe,
    database_name: str,
    expected_first_check: tuple[str, str],
) -> None:
    database = tmp_path / database_name
    if probe is probe_task_database:
        TaskRepository(database).initialize()
    else:
        CatalogRepository(database).initialize()
    checks: list[tuple[str, str]] = []

    def reject_column(
        _connection,
        table: str,
        column: str,
        _allowed,
        *,
        nullable: bool = False,
    ) -> bool:
        del nullable
        checks.append((table, column))
        return False

    def record_enum_sets(_connection, _contracts) -> bool:
        checks.append(("enum-sets", "all"))
        return True

    monkeypatch.setattr(diagnostic_probes, "_column_values_valid", reject_column)
    monkeypatch.setattr(diagnostic_probes, "_enum_sets_valid", record_enum_sets)

    result = probe(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.metrics["stateValuesValid"] is False
    assert checks == [expected_first_check]


@pytest.mark.parametrize("probe", [probe_task_database, probe_content_database])
def test_database_probe_maps_missing_and_corrupt_files_to_safe_codes(
    tmp_path: Path,
    probe,
) -> None:
    missing = probe(tmp_path / "missing.sqlite3")
    corrupt_path = tmp_path / "private-corrupt.sqlite3"
    corrupt_path.write_bytes(b"not-a-sqlite-database private-value")
    corrupt = probe(corrupt_path)

    assert (missing.status, missing.code) == (
        DiagnosticStatus.FAILED,
        "database-missing",
    )
    assert corrupt.status is DiagnosticStatus.FAILED
    assert corrupt.code in {"database-unreadable", "database-corrupt"}
    assert "private" not in corrupt.summary
    assert str(tmp_path) not in corrupt.summary


@pytest.mark.parametrize("database_kind", ["task", "content"])
def test_database_probe_maps_semantic_query_error_to_semantics_invalid(
    tmp_path: Path,
    database_kind: str,
) -> None:
    database = tmp_path / "semantic-query-error.sqlite3"
    if database_kind == "task":
        _create_task_probe_schema(
            database,
            task_foreign_key="REFERENCES tasks(missing_parent_key)",
        )
        probe = probe_task_database
    else:
        _create_content_probe_schema(
            database,
            invalid_dialog_parent_column=True,
        )
        probe = probe_content_database

    result = probe(database)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "database-semantics-invalid"
    assert result.metrics == {}


def test_credentials_probe_exposes_only_boolean_state() -> None:
    ready = probe_credentials(
        settings_readable=True,
        secrets_present=True,
        secrets_decrypted=True,
        credentials_configured=True,
    )
    incomplete = probe_credentials(
        settings_readable=True,
        secrets_present=True,
        secrets_decrypted=True,
        credentials_configured=False,
    )
    absent = probe_credentials(
        settings_readable=True,
        secrets_present=False,
        secrets_decrypted=False,
        credentials_configured=False,
    )
    unreadable = probe_credentials(
        settings_readable=True,
        secrets_present=True,
        secrets_decrypted=False,
        credentials_configured=False,
    )
    settings_error = probe_credentials(
        settings_readable=False,
        secrets_present=True,
        secrets_decrypted=True,
        credentials_configured=False,
    )

    assert ready.status is DiagnosticStatus.PASSED
    assert ready.metrics == {
        "settingsReadable": True,
        "secretsPresent": True,
        "secretsDecryptable": True,
        "credentialsConfigured": True,
    }
    assert (incomplete.status, incomplete.code) == (
        DiagnosticStatus.WARNING,
        "credentials-not-configured",
    )
    assert incomplete.metrics["credentialsConfigured"] is False
    assert (absent.status, absent.code) == (
        DiagnosticStatus.WARNING,
        "credentials-not-configured",
    )
    assert (unreadable.status, unreadable.code) == (
        DiagnosticStatus.FAILED,
        "credentials-unreadable",
    )
    assert (settings_error.status, settings_error.code) == (
        DiagnosticStatus.FAILED,
        "settings-unreadable",
    )


class Gateway:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def test_connection(self) -> None:
        if self.error is not None:
            raise self.error


class BlockingGateway:
    def __init__(self) -> None:
        self.cancelled = False

    async def test_connection(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.mark.asyncio
async def test_telegram_probe_times_out_and_cancels_blocking_gateway() -> None:
    gateway = BlockingGateway()

    result = await probe_telegram(gateway, timeout_seconds=0.001)

    assert (result.status, result.code) == (
        DiagnosticStatus.WARNING,
        "telegram-network-timeout",
    )
    assert gateway.cancelled is True


@pytest.mark.asyncio
async def test_telegram_probe_maps_connection_outcomes_without_error_text() -> None:
    skipped = await probe_telegram(None)
    ready = await probe_telegram(Gateway())
    expired = await probe_telegram(
        Gateway(
            SessionExpiredError(
                "private-session",
                reason=AuthorizationFailureReason.SESSION_REVOKED,
            )
        )
    )
    offline = await probe_telegram(Gateway(TransientNetworkError("private-network")))
    broken = await probe_telegram(Gateway(RuntimeError("private-unknown")))
    retained = await probe_telegram(
        None,
        authorization_reason=AuthorizationFailureReason.AUTH_KEY_DUPLICATED,
    )

    assert (skipped.status, skipped.code) == (
        DiagnosticStatus.SKIPPED,
        "telegram-not-configured",
    )
    assert (ready.status, ready.code) == (
        DiagnosticStatus.PASSED,
        "telegram-connected",
    )
    assert (expired.status, expired.code) == (
        DiagnosticStatus.FAILED,
        "telegram-session-expired",
    )
    assert expired.metrics == {"authorizationReason": "session-revoked"}
    assert "private-session" not in repr(dict(expired.metrics))
    assert (offline.status, offline.code) == (
        DiagnosticStatus.WARNING,
        "telegram-network-unavailable",
    )
    assert (broken.status, broken.code) == (
        DiagnosticStatus.FAILED,
        "telegram-check-failed",
    )
    assert (retained.status, retained.code) == (
        DiagnosticStatus.FAILED,
        "telegram-session-expired",
    )
    assert retained.metrics == {"authorizationReason": "auth-key-duplicated"}
    assert all(
        "private" not in item.summary
        for item in (skipped, ready, expired, offline, broken, retained)
    )


class UpdateChecks:
    def __init__(self, checks: tuple[SourceCheck, SourceCheck]) -> None:
        self.checks = checks

    async def check_sources(self) -> tuple[SourceCheck, SourceCheck]:
        return self.checks


class BlockingUpdateChecks:
    def __init__(self) -> None:
        self.cancelled = False

    async def check_sources(self) -> tuple[SourceCheck, SourceCheck]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_update_probe_times_out_and_cancels_blocking_sources() -> None:
    checks = BlockingUpdateChecks()

    result = await probe_update_sources(checks, timeout_seconds=0.001)

    assert (result.status, result.code) == (
        DiagnosticStatus.WARNING,
        "update-sources-timeout",
    )
    assert checks.cancelled is True


def source_check(
    source: UpdateSourceId,
    status: SourceStatus,
    *,
    version: str = "0.10.0",
    latency_ms: float = 12.6,
) -> SourceCheck:
    verified = (
        SimpleNamespace(
            manifest=SimpleNamespace(version=version),
            canonical=b"same",
            signature=b"same",
        )
        if status is SourceStatus.VALID
        else None
    )
    return SourceCheck(source, status, latency_ms, verified=verified)


@pytest.mark.asyncio
async def test_update_probe_reports_fixed_dual_source_health() -> None:
    valid = (
        source_check(UpdateSourceId.GITHUB, SourceStatus.VALID),
        source_check(UpdateSourceId.MODELSCOPE, SourceStatus.VALID),
    )
    degraded = (valid[0], source_check(UpdateSourceId.MODELSCOPE, SourceStatus.UNAVAILABLE))
    invalid = (valid[0], source_check(UpdateSourceId.MODELSCOPE, SourceStatus.INVALID))

    passed = await probe_update_sources(UpdateChecks(valid))
    warning = await probe_update_sources(UpdateChecks(degraded))
    failed = await probe_update_sources(UpdateChecks(invalid))

    assert (passed.status, passed.code) == (
        DiagnosticStatus.PASSED,
        "update-sources-ok",
    )
    assert passed.metrics == {
        "githubStatus": "valid",
        "githubLatencyMs": 13,
        "githubVersion": "0.10.0",
        "modelscopeStatus": "valid",
        "modelscopeLatencyMs": 13,
        "modelscopeVersion": "0.10.0",
    }
    assert (warning.status, warning.code) == (
        DiagnosticStatus.WARNING,
        "update-source-degraded",
    )
    assert (failed.status, failed.code) == (
        DiagnosticStatus.FAILED,
        "update-source-invalid",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed",
    [
        SourceCheck(UpdateSourceId.GITHUB, SourceStatus.VALID, 1.0),
        source_check(UpdateSourceId.GITHUB, SourceStatus.VALID, latency_ms=-1.0),
        source_check(UpdateSourceId.GITHUB, SourceStatus.VALID, latency_ms=float("inf")),
    ],
)
async def test_update_probe_rejects_structurally_invalid_source_checks(
    malformed: SourceCheck,
) -> None:
    checks = (
        malformed,
        source_check(UpdateSourceId.MODELSCOPE, SourceStatus.VALID),
    )

    result = await probe_update_sources(UpdateChecks(checks))

    assert (result.status, result.code) == (
        DiagnosticStatus.FAILED,
        "update-source-invalid",
    )
