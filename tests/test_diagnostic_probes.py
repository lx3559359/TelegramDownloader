from __future__ import annotations

import sqlite3
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_downloader.catalog import CATALOG_SCHEMA_VERSION, CatalogRepository
from telegram_downloader.content import AccountProfile, ContentDialog, DialogKind
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
from telegram_downloader.download_paths import DownloadPathPolicy
from telegram_downloader.gateway import (
    AuthorizationFailureReason,
    SessionExpiredError,
    TransientNetworkError,
)
from telegram_downloader.paths import PortablePaths
from telegram_downloader.repository import TaskRepository
from telegram_downloader.settings import DownloadStorageSettings
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


def test_credentials_probe_exposes_only_boolean_state() -> None:
    ready = probe_credentials(
        settings_readable=True,
        secrets_present=True,
        secrets_decrypted=True,
    )
    absent = probe_credentials(
        settings_readable=True,
        secrets_present=False,
        secrets_decrypted=False,
    )
    unreadable = probe_credentials(
        settings_readable=True,
        secrets_present=True,
        secrets_decrypted=False,
    )
    settings_error = probe_credentials(
        settings_readable=False,
        secrets_present=True,
        secrets_decrypted=True,
    )

    assert ready.status is DiagnosticStatus.PASSED
    assert ready.metrics == {
        "settingsReadable": True,
        "secretsPresent": True,
        "secretsDecryptable": True,
    }
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
