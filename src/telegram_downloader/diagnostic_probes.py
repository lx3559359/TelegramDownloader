from __future__ import annotations

import asyncio
import ctypes
import importlib
import math
import os
import secrets
import shutil
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from telegram_downloader.catalog import CATALOG_SCHEMA_VERSION
from telegram_downloader.content import DialogKind, SearchScope, SearchStatus
from telegram_downloader.diagnostics import DiagnosticResult, DiagnosticStatus
from telegram_downloader.domain import (
    IntegrityStatus,
    ItemStatus,
    MediaKind,
    PauseReason,
    SourceKind,
    TaskStatus,
)
from telegram_downloader.download_paths import DownloadPathError, DownloadPathPolicy
from telegram_downloader.gateway import (
    AuthorizationFailureReason,
    SessionExpiredError,
    TransientNetworkError,
)
from telegram_downloader.paths import PortablePaths
from telegram_downloader.subscriptions import SubscriptionRunStatus, SubscriptionState
from telegram_downloader.update_sources import SourceCheck, SourceStatus, UpdateSourceId

MIB = 1024 * 1024
GIB = 1024 * MIB


class DiskUsage(Protocol):
    total: int
    free: int


class ConnectionProbe(Protocol):
    async def test_connection(self) -> None: ...


class UpdateSourceProbe(Protocol):
    async def check_sources(self) -> tuple[SourceCheck, SourceCheck]: ...


def managed_writable_paths(
    paths: PortablePaths,
    *,
    download_paths: DownloadPathPolicy | None = None,
) -> dict[str, Path]:
    return {
        "settings": paths.settings,
        "secrets": paths.secrets,
        "database": paths.database,
        "catalogDatabase": paths.catalog_database,
        "log": paths.log,
        "cache": paths.cache,
        "thumbnailCache": paths.thumbnail_cache,
        "temp": paths.temp,
        "downloads": (
            download_paths.current_root
            if download_paths is not None
            else paths.downloads
        ),
        "updateStaging": paths.update_staging,
        "updateBackup": paths.update_backup,
        "updateHelper": paths.update_helper,
        "updateJournal": paths.update_journal,
        "diagnostics": paths.diagnostics,
        "diagnosticTemp": paths.diagnostic_temp,
        "maintenanceState": paths.storage_maintenance_state,
    }


def probe_environment(
    paths: PortablePaths,
    *,
    download_paths: DownloadPathPolicy | None = None,
    frozen: bool,
    windows_x64: bool,
    system_drive: str,
) -> DiagnosticResult:
    try:
        managed = managed_writable_paths(paths, download_paths=download_paths)
        guarded = {}
        for name, path in managed.items():
            guarded[name] = (
                download_paths.guard(path, allow_root=True)
                if name == "downloads" and download_paths is not None
                else paths.guard(path)
            )
    except (OSError, ValueError):
        return _result(
            "environment",
            "运行环境与路径",
            DiagnosticStatus.FAILED,
            "runtime-path-invalid",
            "应用写入路径未通过安全边界检查",
        )
    root_drive = paths.root.drive.casefold()
    system_volume = Path(system_drive).drive.casefold()
    non_system_volume = bool(root_drive and system_volume and root_drive != system_volume)
    metrics = {
        "frozen": bool(frozen),
        "windowsX64": bool(windows_x64),
        "nonSystemVolume": non_system_volume,
        "guardedPathCount": len(guarded),
    }
    if not windows_x64:
        return _result(
            "environment",
            "运行环境与路径",
            DiagnosticStatus.FAILED,
            "runtime-unsupported",
            "当前运行环境不是受支持的 Windows x64",
            metrics,
        )
    if not non_system_volume:
        status = DiagnosticStatus.FAILED if frozen else DiagnosticStatus.WARNING
        code = "runtime-system-volume" if frozen else "source-system-volume"
        summary = (
            "正式程序位于系统盘，必须迁移到非系统盘"
            if frozen
            else "源码开发环境位于系统盘"
        )
        return _result("environment", "运行环境与路径", status, code, summary, metrics)
    return _result(
        "environment",
        "运行环境与路径",
        DiagnosticStatus.PASSED,
        "runtime-paths-ok",
        "运行环境和项目内路径正常",
        metrics,
    )


def probe_project_write(
    paths: PortablePaths,
    *,
    download_paths: DownloadPathPolicy | None = None,
    marker: bytes | None = None,
    token_factory: Callable[[], str] | None = None,
    writer: Callable[[Path, bytes], None] | None = None,
) -> DiagnosticResult:
    marker = marker if marker is not None else secrets.token_bytes(32)
    token = (token_factory or (lambda: uuid4().hex))()
    target = paths.guard(paths.diagnostic_temp / f"write-probe-{token}.tmp")
    write = writer or _durable_write
    try:
        paths.guard(paths.diagnostic_temp).mkdir(parents=True, exist_ok=True)
        write(target, marker)
        if target.read_bytes() != marker:
            raise OSError("write verification failed")
    except Exception:
        return _result(
            "project-write",
            "项目内写入",
            DiagnosticStatus.FAILED,
            "project-write-failed",
            "项目内临时写入检查失败",
        )
    finally:
        with suppress(OSError):
            target.unlink(missing_ok=True)
    if download_paths is not None:
        try:
            download_paths.require_current_writable()
        except (DownloadPathError, OSError, ValueError):
            return _result(
                "project-write",
                "项目内写入",
                DiagnosticStatus.FAILED,
                "download-write-failed",
                "当前下载目录写入检查失败",
                {"downloadWritable": False},
            )
        return _result(
            "project-write",
            "项目内写入",
            DiagnosticStatus.PASSED,
            "project-write-ok",
            "项目内临时写入和当前下载目录写入正常",
            {"downloadWritable": True},
        )
    return _result(
        "project-write",
        "项目内写入",
        DiagnosticStatus.PASSED,
        "project-write-ok",
        "项目内临时写入和读取正常",
    )


def probe_disk(
    paths: PortablePaths,
    usage_provider: Callable[[Path], DiskUsage] = shutil.disk_usage,
    *,
    download_root: Path | None = None,
    volume_identity_provider: Callable[[Path], str] | None = None,
) -> DiagnosticResult:
    try:
        usage = usage_provider(paths.root)
        total = _safe_size(usage.total)
        free = _safe_size(usage.free)
    except (OSError, TypeError, ValueError):
        return _result(
            "disk",
            "磁盘空间",
            DiagnosticStatus.FAILED,
            "disk-unavailable",
            "无法读取应用所在磁盘的空间信息",
        )
    metrics: dict[str, bool | int] = {"totalBytes": total, "freeBytes": free}
    download_total = total
    download_free = free
    if download_root is not None:
        same_volume = _same_volume(
            paths.root,
            download_root,
            volume_identity_provider or _volume_identity,
        )
        metrics["downloadSameVolume"] = same_volume
        if not same_volume:
            try:
                download_usage = usage_provider(download_root)
                download_total = _safe_size(download_usage.total)
                download_free = _safe_size(download_usage.free)
            except (OSError, TypeError, ValueError):
                return _result(
                    "disk",
                    "磁盘空间",
                    DiagnosticStatus.FAILED,
                    "download-disk-unavailable",
                    "无法读取下载所在磁盘的空间信息",
                    metrics,
                )
        metrics["downloadTotalBytes"] = download_total
        metrics["downloadFreeBytes"] = download_free
    if free < 256 * MIB:
        return _result(
            "disk",
            "磁盘空间",
            DiagnosticStatus.FAILED,
            "disk-space-critical",
            (
                "应用所在磁盘可用空间低于 256 MiB"
                if download_root is not None
                else "磁盘可用空间低于 256 MiB"
            ),
            metrics,
        )
    if download_root is not None and download_free < 256 * MIB:
        return _result(
            "disk",
            "磁盘空间",
            DiagnosticStatus.FAILED,
            "download-disk-space-critical",
            "下载所在磁盘可用空间低于 256 MiB",
            metrics,
        )
    if free < GIB:
        return _result(
            "disk",
            "磁盘空间",
            DiagnosticStatus.WARNING,
            "disk-space-low",
            (
                "应用所在磁盘可用空间低于 1 GiB"
                if download_root is not None
                else "磁盘可用空间低于 1 GiB"
            ),
            metrics,
        )
    if download_root is not None and download_free < GIB:
        return _result(
            "disk",
            "磁盘空间",
            DiagnosticStatus.WARNING,
            "download-disk-space-low",
            "下载所在磁盘可用空间低于 1 GiB",
            metrics,
        )
    return _result(
        "disk",
        "磁盘空间",
        DiagnosticStatus.PASSED,
        "disk-space-ok",
        (
            "应用和下载所在磁盘可用空间正常"
            if download_root is not None
            else "磁盘可用空间正常"
        ),
        metrics,
    )


def component_availability(
    importer: Callable[[str], object] = importlib.import_module,
    *,
    dpapi_available: bool | None = None,
) -> dict[str, bool]:
    values: dict[str, bool] = {}
    for key, module in (
        ("pyside6", "PySide6"),
        ("telethon", "telethon"),
        ("qasync", "qasync"),
        ("qrcode", "qrcode"),
        ("sqlite", "sqlite3"),
    ):
        try:
            importer(module)
        except (ImportError, OSError):
            values[key] = False
        else:
            values[key] = True
    values["dpapi"] = os.name == "nt" if dpapi_available is None else dpapi_available
    return values


def probe_components(availability: Mapping[str, bool]) -> DiagnosticResult:
    required = ("pyside6", "telethon", "qasync", "qrcode", "sqlite", "dpapi")
    metrics = {name: bool(availability.get(name, False)) for name in required}
    if not all(metrics.values()):
        return _result(
            "components",
            "运行组件",
            DiagnosticStatus.FAILED,
            "component-missing",
            "一个或多个必要运行组件不可用",
            metrics,
        )
    return _result(
        "components",
        "运行组件",
        DiagnosticStatus.PASSED,
        "components-ok",
        "必要运行组件全部可用",
        metrics,
    )


def probe_task_database(database: Path) -> DiagnosticResult:
    title = "下载任务数据库"
    connection = _open_read_only_database(database)
    if isinstance(connection, DiagnosticResult):
        return _database_failure("task-database", title, connection.code)
    try:
        try:
            if not _database_integrity_ok(connection):
                return _database_failure("task-database", title, "database-corrupt")
            compatible = _schema_contains(
                connection,
                {
                    "tasks": {"id", "source_kind", "status", "pause_reason"},
                    "media_items": {
                        "id",
                        "media_kind",
                        "status",
                        "integrity_status",
                    },
                },
            )
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            return _database_failure("task-database", title, "database-unreadable")
        if not compatible:
            return _result(
                "task-database",
                title,
                DiagnosticStatus.FAILED,
                "database-schema-incompatible",
                "下载任务数据库结构不兼容",
                {"schemaCompatible": False},
            )
        try:
            foreign_keys_valid = _foreign_keys_valid(connection)
            state_values_valid = all(
                (
                    _column_values_valid(
                        connection,
                        "tasks",
                        "source_kind",
                        tuple(value.value for value in SourceKind),
                    ),
                    _column_values_valid(
                        connection,
                        "tasks",
                        "status",
                        tuple(value.value for value in TaskStatus),
                    ),
                    _column_values_valid(
                        connection,
                        "tasks",
                        "pause_reason",
                        tuple(value.value for value in PauseReason),
                        nullable=True,
                    ),
                    _column_values_valid(
                        connection,
                        "media_items",
                        "media_kind",
                        tuple(value.value for value in MediaKind),
                    ),
                    _column_values_valid(
                        connection,
                        "media_items",
                        "status",
                        tuple(value.value for value in ItemStatus),
                    ),
                    _column_values_valid(
                        connection,
                        "media_items",
                        "integrity_status",
                        tuple(value.value for value in IntegrityStatus),
                    ),
                )
            )
            metrics: dict[str, bool | int] = {
                "taskCount": _row_count(connection, "tasks"),
                "mediaCount": _row_count(connection, "media_items"),
                "schemaCompatible": True,
                "foreignKeysValid": foreign_keys_valid,
                "stateValuesValid": state_values_valid,
            }
            metrics.update(
                _grouped_state_counts(
                    connection,
                    "tasks",
                    "status",
                    "taskStatus",
                    tuple(value.value for value in TaskStatus),
                )
            )
            metrics.update(
                _grouped_state_counts(
                    connection,
                    "media_items",
                    "status",
                    "itemStatus",
                    tuple(value.value for value in ItemStatus),
                )
            )
            metrics.update(
                _grouped_state_counts(
                    connection,
                    "media_items",
                    "integrity_status",
                    "integrityStatus",
                    tuple(value.value for value in IntegrityStatus),
                )
            )
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            return _database_failure(
                "task-database", title, "database-semantics-invalid"
            )
        if (
            not foreign_keys_valid
            or not state_values_valid
            or any(key.endswith("Other") for key in metrics)
        ):
            return _result(
                "task-database",
                title,
                DiagnosticStatus.FAILED,
                "database-semantics-invalid",
                "下载任务数据库包含无效关系或状态",
                metrics,
            )
    finally:
        connection.close()
    return _result(
        "task-database",
        title,
        DiagnosticStatus.PASSED,
        "task-database-ok",
        "下载任务数据库结构和聚合状态正常",
        metrics,
    )


def probe_content_database(database: Path) -> DiagnosticResult:
    title = "账号内容数据库"
    connection = _open_read_only_database(database)
    if isinstance(connection, DiagnosticResult):
        return _database_failure("content-database", title, connection.code)
    try:
        try:
            if not _database_integrity_ok(connection):
                return _database_failure("content-database", title, "database-corrupt")
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                "accounts": {"account_id"},
                "dialogs": {"account_id", "peer_ref", "kind"},
                "search_sessions": {"id", "status", "scope"},
                "search_results": {"id", "media_kind"},
                "subscription_rules": {"id", "state"},
                "subscription_runs": {"id", "keyword_hits", "status"},
            }
            compatible = (
                schema_version == CATALOG_SCHEMA_VERSION
                and _schema_contains(connection, tables)
            )
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            return _database_failure("content-database", title, "database-unreadable")
        if not compatible:
            return _result(
                "content-database",
                title,
                DiagnosticStatus.FAILED,
                "database-schema-incompatible",
                "账号内容数据库结构不兼容",
                {"schemaVersion": schema_version, "schemaCompatible": False},
            )
        try:
            foreign_keys_valid = _foreign_keys_valid(connection)
            state_values_valid = all(
                (
                    _column_values_valid(
                        connection,
                        "dialogs",
                        "kind",
                        tuple(value.value for value in DialogKind),
                    ),
                    _column_values_valid(
                        connection,
                        "search_sessions",
                        "status",
                        tuple(value.value for value in SearchStatus),
                    ),
                    _column_values_valid(
                        connection,
                        "search_sessions",
                        "scope",
                        tuple(value.value for value in SearchScope),
                    ),
                    _column_values_valid(
                        connection,
                        "search_results",
                        "media_kind",
                        tuple(value.value for value in MediaKind),
                    ),
                    _column_values_valid(
                        connection,
                        "subscription_rules",
                        "state",
                        tuple(value.value for value in SubscriptionState),
                    ),
                    _column_values_valid(
                        connection,
                        "subscription_runs",
                        "status",
                        tuple(value.value for value in SubscriptionRunStatus),
                    ),
                )
            )
            metrics = {
                "schemaVersion": schema_version,
                "schemaCompatible": True,
                "foreignKeysValid": foreign_keys_valid,
                "stateValuesValid": state_values_valid,
                "accountCount": _row_count(connection, "accounts"),
                "dialogCount": _row_count(connection, "dialogs"),
                "searchCount": _row_count(connection, "search_sessions"),
                "searchResultCount": _row_count(connection, "search_results"),
                "subscriptionCount": _row_count(connection, "subscription_rules"),
                "subscriptionRunCount": _row_count(connection, "subscription_runs"),
            }
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            return _database_failure(
                "content-database", title, "database-semantics-invalid"
            )
        if not foreign_keys_valid or not state_values_valid:
            return _result(
                "content-database",
                title,
                DiagnosticStatus.FAILED,
                "database-semantics-invalid",
                "账号内容数据库包含无效关系或状态",
                metrics,
            )
    finally:
        connection.close()
    return _result(
        "content-database",
        title,
        DiagnosticStatus.PASSED,
        "content-database-ok",
        "账号内容数据库结构和聚合状态正常",
        metrics,
    )


def probe_credentials(
    *,
    settings_readable: bool,
    secrets_present: bool,
    secrets_decrypted: bool,
    credentials_configured: bool,
) -> DiagnosticResult:
    metrics = {
        "settingsReadable": bool(settings_readable),
        "secretsPresent": bool(secrets_present),
        "secretsDecryptable": bool(secrets_decrypted),
        "credentialsConfigured": bool(credentials_configured),
    }
    if not settings_readable:
        return _result(
            "credentials",
            "登录凭据",
            DiagnosticStatus.FAILED,
            "settings-unreadable",
            "应用设置无法读取",
            metrics,
        )
    if not secrets_present:
        return _result(
            "credentials",
            "登录凭据",
            DiagnosticStatus.WARNING,
            "credentials-not-configured",
            "尚未配置 Telegram 登录凭据",
            metrics,
        )
    if not secrets_decrypted:
        return _result(
            "credentials",
            "登录凭据",
            DiagnosticStatus.FAILED,
            "credentials-unreadable",
            "Telegram 登录凭据无法解密",
            metrics,
        )
    if not credentials_configured:
        return _result(
            "credentials",
            "登录凭据",
            DiagnosticStatus.WARNING,
            "credentials-not-configured",
            "尚未配置 Telegram 登录凭据",
            metrics,
        )
    return _result(
        "credentials",
        "登录凭据",
        DiagnosticStatus.PASSED,
        "credentials-ok",
        "Telegram 登录凭据可用",
        metrics,
    )


async def probe_telegram(
    gateway: ConnectionProbe | None,
    *,
    authorization_reason: AuthorizationFailureReason | None = None,
    timeout_seconds: float = 20.0,
) -> DiagnosticResult:
    if authorization_reason is not None:
        return _telegram_authorization_failure(authorization_reason)
    if gateway is None:
        return _result(
            "telegram",
            "Telegram 连接",
            DiagnosticStatus.SKIPPED,
            "telegram-not-configured",
            "尚未建立可检查的 Telegram 会话",
        )
    try:
        async with asyncio.timeout(timeout_seconds):
            await gateway.test_connection()
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return _result(
            "telegram",
            "Telegram 连接",
            DiagnosticStatus.WARNING,
            "telegram-network-timeout",
            "Telegram 连接检查超时",
        )
    except SessionExpiredError as error:
        return _telegram_authorization_failure(error.reason)
    except TransientNetworkError:
        return _result(
            "telegram",
            "Telegram 连接",
            DiagnosticStatus.WARNING,
            "telegram-network-unavailable",
            "暂时无法连接 Telegram 服务",
        )
    except Exception:
        return _result(
            "telegram",
            "Telegram 连接",
            DiagnosticStatus.FAILED,
            "telegram-check-failed",
            "Telegram 连接检查失败",
        )
    return _result(
        "telegram",
        "Telegram 连接",
        DiagnosticStatus.PASSED,
        "telegram-connected",
        "Telegram 登录会话和连接正常",
    )


def _telegram_authorization_failure(
    reason: AuthorizationFailureReason,
) -> DiagnosticResult:
    return _result(
        "telegram",
        "Telegram 连接",
        DiagnosticStatus.FAILED,
        "telegram-session-expired",
        "Telegram 登录会话已失效",
        {"authorizationReason": reason.value},
    )


async def probe_update_sources(
    coordinator: UpdateSourceProbe | None,
    *,
    timeout_seconds: float = 20.0,
) -> DiagnosticResult:
    if coordinator is None:
        return _result(
            "updates",
            "签名更新源",
            DiagnosticStatus.SKIPPED,
            "update-check-unavailable",
            "当前未配置签名更新检查",
        )
    try:
        async with asyncio.timeout(timeout_seconds):
            checks = await coordinator.check_sources()
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return _result(
            "updates",
            "签名更新源",
            DiagnosticStatus.WARNING,
            "update-sources-timeout",
            "签名更新源检查超时",
        )
    except Exception:
        return _result(
            "updates",
            "签名更新源",
            DiagnosticStatus.WARNING,
            "update-sources-unavailable",
            "暂时无法检查签名更新源",
        )
    by_source = {item.source: item for item in checks}
    if set(by_source) != {UpdateSourceId.GITHUB, UpdateSourceId.MODELSCOPE}:
        return _result(
            "updates",
            "签名更新源",
            DiagnosticStatus.FAILED,
            "update-source-invalid",
            "签名更新源返回结构无效",
        )
    ordered = (by_source[UpdateSourceId.GITHUB], by_source[UpdateSourceId.MODELSCOPE])
    if not all(_source_check_valid(item) for item in ordered):
        return _result(
            "updates",
            "签名更新源",
            DiagnosticStatus.FAILED,
            "update-source-invalid",
            "签名更新源返回结构无效",
        )
    metrics: dict[str, bool | int | float | str] = {}
    for check in ordered:
        prefix = "github" if check.source is UpdateSourceId.GITHUB else "modelscope"
        metrics[f"{prefix}Status"] = check.status.value
        metrics[f"{prefix}LatencyMs"] = max(0, int(round(check.latency_ms)))
        if check.status is SourceStatus.VALID and check.verified is not None:
            metrics[f"{prefix}Version"] = check.verified.manifest.version
    if any(item.status is SourceStatus.INVALID for item in ordered) or _sources_conflict(
        ordered
    ):
        return _result(
            "updates",
            "签名更新源",
            DiagnosticStatus.FAILED,
            "update-source-invalid",
            "签名更新源验证失败或内容不一致",
            metrics,
        )
    unavailable = sum(item.status is SourceStatus.UNAVAILABLE for item in ordered)
    if unavailable:
        code = "update-sources-unavailable" if unavailable == 2 else "update-source-degraded"
        summary = (
            "两个签名更新源暂时均不可用"
            if unavailable == 2
            else "一个签名更新源暂时不可用"
        )
        return _result(
            "updates",
            "签名更新源",
            DiagnosticStatus.WARNING,
            code,
            summary,
            metrics,
        )
    return _result(
        "updates",
        "签名更新源",
        DiagnosticStatus.PASSED,
        "update-sources-ok",
        "GitHub 与魔搭签名更新源正常",
        metrics,
    )


def _sources_conflict(checks: tuple[SourceCheck, SourceCheck]) -> bool:
    left, right = checks
    if (
        left.status is not SourceStatus.VALID
        or right.status is not SourceStatus.VALID
        or left.verified is None
        or right.verified is None
        or left.verified.manifest.version != right.verified.manifest.version
    ):
        return False
    return (
        left.verified.canonical != right.verified.canonical
        or left.verified.signature != right.verified.signature
    )


def _source_check_valid(check: SourceCheck) -> bool:
    latency = check.latency_ms
    if (
        not isinstance(check.status, SourceStatus)
        or not isinstance(latency, (int, float))
        or isinstance(latency, bool)
        or not math.isfinite(float(latency))
        or latency < 0
    ):
        return False
    if check.status is not SourceStatus.VALID:
        return check.verified is None
    verified = check.verified
    return (
        verified is not None
        and isinstance(getattr(verified, "canonical", None), bytes)
        and isinstance(getattr(verified, "signature", None), bytes)
        and isinstance(getattr(getattr(verified, "manifest", None), "version", None), str)
    )


def _open_read_only_database(database: Path) -> sqlite3.Connection | DiagnosticResult:
    if not database.is_file():
        return _result(
            "database",
            "数据库",
            DiagnosticStatus.FAILED,
            "database-missing",
            "数据库文件不存在",
        )
    connection: sqlite3.Connection | None = None
    try:
        uri = f"{database.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        connection.execute("BEGIN")
        return connection
    except (OSError, sqlite3.DatabaseError, ValueError):
        if connection is not None:
            connection.close()
        return _result(
            "database",
            "数据库",
            DiagnosticStatus.FAILED,
            "database-unreadable",
            "数据库无法读取",
        )


def _database_integrity_ok(connection: sqlite3.Connection) -> bool:
    row = connection.execute("PRAGMA quick_check").fetchone()
    return row is not None and str(row[0]).casefold() == "ok"


def _foreign_keys_valid(connection: sqlite3.Connection) -> bool:
    return connection.execute("PRAGMA foreign_key_check").fetchone() is None


def _column_values_valid(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    allowed: tuple[str, ...],
    *,
    nullable: bool = False,
) -> bool:
    placeholders = ",".join("?" for _ in allowed)
    predicate = (
        f"{column} IS NOT NULL AND {column} NOT IN ({placeholders})"
        if nullable
        else f"{column} IS NULL OR {column} NOT IN ({placeholders})"
    )
    return (
        connection.execute(
            f"SELECT 1 FROM {table} WHERE {predicate} LIMIT 1",
            allowed,
        ).fetchone()
        is None
    )


def _schema_contains(
    connection: sqlite3.Connection,
    required: Mapping[str, set[str]],
) -> bool:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        ).fetchall()
    }
    if not required.keys() <= tables:
        return False
    for table, columns in required.items():
        actual = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if not columns <= actual:
            return False
    return True


def _row_count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _grouped_state_counts(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    prefix: str,
    allowed: tuple[str, ...],
) -> dict[str, int]:
    rows = connection.execute(
        f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column}"
    ).fetchall()
    known = set(allowed)
    metrics: dict[str, int] = {}
    other = 0
    for state, count in rows:
        value = str(state)
        if value in known:
            suffix = "".join(part.capitalize() for part in value.split("_"))
            metrics[f"{prefix}{suffix}"] = int(count)
        else:
            other += int(count)
    if other:
        metrics[f"{prefix}Other"] = other
    return metrics


def _database_failure(result_id: str, title: str, code: str) -> DiagnosticResult:
    summaries = {
        "database-missing": "数据库文件不存在",
        "database-corrupt": "数据库完整性检查失败",
        "database-unreadable": "数据库无法读取",
        "database-semantics-invalid": "数据库语义检查失败",
    }
    return _result(
        result_id,
        title,
        DiagnosticStatus.FAILED,
        code,
        summaries.get(code, "数据库检查失败"),
    )


def _durable_write(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _safe_size(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("invalid disk size")
    return value


def _same_volume(
    first: Path,
    second: Path,
    identity_provider: Callable[[Path], str],
) -> bool:
    try:
        first_identity = identity_provider(first).strip()
        second_identity = identity_provider(second).strip()
    except (OSError, TypeError, ValueError):
        return False
    return bool(first_identity) and os.path.normcase(first_identity) == os.path.normcase(
        second_identity
    )


def _volume_identity(path: Path) -> str:
    resolved = Path(path).resolve()
    if os.name != "nt":
        return str(resolved.stat().st_dev)

    mount_buffer = ctypes.create_unicode_buffer(32768)
    get_volume_path = ctypes.WinDLL("kernel32", use_last_error=True).GetVolumePathNameW
    get_volume_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_volume_path.restype = wintypes.BOOL
    if not get_volume_path(str(resolved), mount_buffer, len(mount_buffer)):
        raise OSError(ctypes.get_last_error(), "无法识别路径所在卷")

    guid_buffer = ctypes.create_unicode_buffer(64)
    get_volume_name = ctypes.WinDLL(
        "kernel32", use_last_error=True
    ).GetVolumeNameForVolumeMountPointW
    get_volume_name.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_volume_name.restype = wintypes.BOOL
    if get_volume_name(mount_buffer.value, guid_buffer, len(guid_buffer)):
        return guid_buffer.value
    return mount_buffer.value


def _result(
    result_id: str,
    title: str,
    status: DiagnosticStatus,
    code: str,
    summary: str,
    metrics: Mapping[str, bool | int | float | str] | None = None,
) -> DiagnosticResult:
    return DiagnosticResult(result_id, title, status, code, summary, 0, metrics or {})
