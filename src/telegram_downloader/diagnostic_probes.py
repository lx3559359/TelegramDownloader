from __future__ import annotations

import asyncio
import importlib
import os
import secrets
import shutil
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from telegram_downloader.diagnostics import DiagnosticResult, DiagnosticStatus
from telegram_downloader.gateway import SessionExpiredError, TransientNetworkError
from telegram_downloader.paths import PortablePaths
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


def managed_writable_paths(paths: PortablePaths) -> dict[str, Path]:
    return {
        "settings": paths.settings,
        "secrets": paths.secrets,
        "database": paths.database,
        "catalogDatabase": paths.catalog_database,
        "log": paths.log,
        "cache": paths.cache,
        "thumbnailCache": paths.thumbnail_cache,
        "temp": paths.temp,
        "downloads": paths.downloads,
        "updateStaging": paths.update_staging,
        "updateBackup": paths.update_backup,
        "updateHelper": paths.update_helper,
        "updateJournal": paths.update_journal,
        "diagnostics": paths.diagnostics,
        "diagnosticTemp": paths.diagnostic_temp,
    }


def probe_environment(
    paths: PortablePaths,
    *,
    frozen: bool,
    windows_x64: bool,
    system_drive: str,
) -> DiagnosticResult:
    try:
        guarded = {
            name: paths.guard(path) for name, path in managed_writable_paths(paths).items()
        }
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
        return _result(
            "project-write",
            "项目内写入",
            DiagnosticStatus.PASSED,
            "project-write-ok",
            "项目内临时写入和读取正常",
        )
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


def probe_disk(
    paths: PortablePaths,
    usage_provider: Callable[[Path], DiskUsage] = shutil.disk_usage,
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
    metrics = {"totalBytes": total, "freeBytes": free}
    if free < 256 * MIB:
        return _result(
            "disk",
            "磁盘空间",
            DiagnosticStatus.FAILED,
            "disk-space-critical",
            "磁盘可用空间低于 256 MiB",
            metrics,
        )
    if free < GIB:
        return _result(
            "disk",
            "磁盘空间",
            DiagnosticStatus.WARNING,
            "disk-space-low",
            "磁盘可用空间低于 1 GiB",
            metrics,
        )
    return _result(
        "disk",
        "磁盘空间",
        DiagnosticStatus.PASSED,
        "disk-space-ok",
        "磁盘可用空间正常",
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
        if not _database_integrity_ok(connection):
            return _database_failure("task-database", title, "database-corrupt")
        compatible = _schema_contains(
            connection,
            {
                "tasks": {"id", "status"},
                "media_items": {"id", "status", "integrity_status"},
            },
        )
        if not compatible:
            return _result(
                "task-database",
                title,
                DiagnosticStatus.FAILED,
                "database-schema-incompatible",
                "下载任务数据库结构不兼容",
                {"schemaCompatible": False},
            )
        metrics: dict[str, bool | int] = {
            "taskCount": _row_count(connection, "tasks"),
            "mediaCount": _row_count(connection, "media_items"),
            "schemaCompatible": True,
        }
        metrics.update(
            _grouped_state_counts(
                connection,
                "tasks",
                "status",
                "taskStatus",
                (
                    "queued",
                    "scanning",
                    "downloading",
                    "waiting_retry",
                    "paused",
                    "completed",
                    "partial_failure",
                ),
            )
        )
        metrics.update(
            _grouped_state_counts(
                connection,
                "media_items",
                "status",
                "itemStatus",
                ("queued", "downloading", "waiting_retry", "paused", "completed", "failed"),
            )
        )
        metrics.update(
            _grouped_state_counts(
                connection,
                "media_items",
                "integrity_status",
                "integrityStatus",
                (
                    "unverified",
                    "verified",
                    "missing",
                    "size_mismatch",
                    "hash_mismatch",
                    "read_error",
                ),
            )
        )
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return _database_failure("task-database", title, "database-unreadable")
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
        if not _database_integrity_ok(connection):
            return _database_failure("content-database", title, "database-corrupt")
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            "accounts": {"account_id"},
            "dialogs": {"account_id", "peer_ref"},
            "search_sessions": {"id"},
            "search_results": {"id"},
            "subscription_rules": {"id"},
            "subscription_runs": {"id", "keyword_hits"},
        }
        compatible = schema_version == 3 and _schema_contains(connection, tables)
        if not compatible:
            return _result(
                "content-database",
                title,
                DiagnosticStatus.FAILED,
                "database-schema-incompatible",
                "账号内容数据库结构不兼容",
                {"schemaVersion": schema_version, "schemaCompatible": False},
            )
        metrics = {
            "schemaVersion": schema_version,
            "schemaCompatible": True,
            "accountCount": _row_count(connection, "accounts"),
            "dialogCount": _row_count(connection, "dialogs"),
            "searchCount": _row_count(connection, "search_sessions"),
            "searchResultCount": _row_count(connection, "search_results"),
            "subscriptionCount": _row_count(connection, "subscription_rules"),
            "subscriptionRunCount": _row_count(connection, "subscription_runs"),
        }
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return _database_failure("content-database", title, "database-unreadable")
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
) -> DiagnosticResult:
    metrics = {
        "settingsReadable": bool(settings_readable),
        "secretsPresent": bool(secrets_present),
        "secretsDecryptable": bool(secrets_decrypted),
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
    return _result(
        "credentials",
        "登录凭据",
        DiagnosticStatus.PASSED,
        "credentials-ok",
        "Telegram 登录凭据可用",
        metrics,
    )


async def probe_telegram(gateway: ConnectionProbe | None) -> DiagnosticResult:
    if gateway is None:
        return _result(
            "telegram",
            "Telegram 连接",
            DiagnosticStatus.SKIPPED,
            "telegram-not-configured",
            "尚未建立可检查的 Telegram 会话",
        )
    try:
        await gateway.test_connection()
    except asyncio.CancelledError:
        raise
    except SessionExpiredError:
        return _result(
            "telegram",
            "Telegram 连接",
            DiagnosticStatus.FAILED,
            "telegram-session-expired",
            "Telegram 登录会话已失效",
        )
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


async def probe_update_sources(coordinator: UpdateSourceProbe | None) -> DiagnosticResult:
    if coordinator is None:
        return _result(
            "updates",
            "签名更新源",
            DiagnosticStatus.SKIPPED,
            "update-check-unavailable",
            "当前未配置签名更新检查",
        )
    try:
        checks = await coordinator.check_sources()
    except asyncio.CancelledError:
        raise
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


def _open_read_only_database(database: Path) -> sqlite3.Connection | DiagnosticResult:
    if not database.is_file():
        return _result(
            "database",
            "数据库",
            DiagnosticStatus.FAILED,
            "database-missing",
            "数据库文件不存在",
        )
    try:
        uri = f"{database.resolve().as_uri()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        return connection
    except (OSError, sqlite3.DatabaseError, ValueError):
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


def _result(
    result_id: str,
    title: str,
    status: DiagnosticStatus,
    code: str,
    summary: str,
    metrics: Mapping[str, bool | int | float | str] | None = None,
) -> DiagnosticResult:
    return DiagnosticResult(result_id, title, status, code, summary, 0, metrics or {})
