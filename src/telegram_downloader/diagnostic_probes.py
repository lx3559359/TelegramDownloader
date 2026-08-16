from __future__ import annotations

import importlib
import os
import secrets
import shutil
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from telegram_downloader.diagnostics import DiagnosticResult, DiagnosticStatus
from telegram_downloader.paths import PortablePaths

MIB = 1024 * 1024
GIB = 1024 * MIB


class DiskUsage(Protocol):
    total: int
    free: int


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
