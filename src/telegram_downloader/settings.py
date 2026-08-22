from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from telegram_downloader.files import DownloadNamingSettings
from telegram_downloader.resource_control import validate_speed_limit_kib


class SettingsError(ValueError):
    """Raised when project-local settings are malformed or unsafe."""


def _validate_update_check_time(value: str) -> None:
    if value == "":
        return
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SettingsError("最近更新检查时间必须是 UTC ISO 8601 文本")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise SettingsError("最近更新检查时间格式无效") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise SettingsError("最近更新检查时间必须使用 UTC")


@dataclass(frozen=True, slots=True)
class DownloadStorageSettings:
    root: str = ""
    trusted_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.root, str):
            raise SettingsError("下载根目录必须是文本")
        roots = self.trusted_roots
        if not isinstance(roots, (tuple, list)) or any(
            not isinstance(value, str) or not value.strip() for value in roots
        ):
            raise SettingsError("历史下载根目录格式无效")
        normalized = tuple(value.strip() for value in roots)
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise SettingsError("历史下载根目录不能重复")
        object.__setattr__(self, "root", self.root.strip())
        object.__setattr__(self, "trusted_roots", normalized)


@dataclass(frozen=True, slots=True)
class DownloadScheduleSettings:
    enabled: bool = False
    weekdays: tuple[int, ...] = tuple(range(7))
    start_minute: int = 0
    end_minute: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise SettingsError("下载时段开关必须是布尔值")
        if not isinstance(self.weekdays, (tuple, list)) or not self.weekdays or any(
            not isinstance(day, int) or isinstance(day, bool) or not 0 <= day <= 6
            for day in self.weekdays
        ):
            raise SettingsError("下载星期必须是周一到周日的非空集合")
        normalized = tuple(dict.fromkeys(self.weekdays))
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 1439
            for value in (self.start_minute, self.end_minute)
        ):
            raise SettingsError("下载时段分钟必须在 0 到 1439 之间")
        object.__setattr__(self, "weekdays", normalized)


@dataclass(frozen=True, slots=True)
class StorageMaintenanceSettings:
    automatic_enabled: bool = False
    temp_retention_days: int = 7
    log_retention_days: int = 30
    thumbnail_limit_bytes: int = 1024**3
    thumbnail_target_bytes: int = 900 * 1024**2
    update_staging_retention_days: int = 7
    update_backup_keep_count: int = 1
    check_interval_seconds: int = 86400
    startup_delay_seconds: int = 300
    idle_required_seconds: int = 60
    busy_retry_seconds: int = 900

    def __post_init__(self) -> None:
        if not isinstance(self.automatic_enabled, bool):
            raise SettingsError("自动清理开关必须是布尔值")
        expected = {
            "temp_retention_days": 7,
            "log_retention_days": 30,
            "thumbnail_limit_bytes": 1024**3,
            "thumbnail_target_bytes": 900 * 1024**2,
            "update_staging_retention_days": 7,
            "update_backup_keep_count": 1,
            "check_interval_seconds": 86400,
            "startup_delay_seconds": 300,
            "idle_required_seconds": 60,
            "busy_retry_seconds": 900,
        }
        if any(
            not isinstance(getattr(self, name), int)
            or isinstance(getattr(self, name), bool)
            or getattr(self, name) != value
            for name, value in expected.items()
        ):
            raise SettingsError("存储维护策略不是受支持的固定策略")


@dataclass(frozen=True, slots=True)
class ProxySettings:
    kind: str = "none"
    host: str = ""
    port: int = 0
    username: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"none", "socks5", "http"}:
            raise SettingsError("代理类型必须是 none、socks5 或 http")
        if not isinstance(self.host, str) or not isinstance(self.username, str):
            raise SettingsError("代理地址和用户名必须是文本")
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise SettingsError("代理端口必须是整数")
        if self.kind != "none" and (not self.host.strip() or not 1 <= self.port <= 65535):
            raise SettingsError("启用代理时必须提供有效地址和端口")


@dataclass(frozen=True, slots=True)
class AppSettings:
    api_id: int = 0
    concurrency: int = 3
    proxy: ProxySettings = ProxySettings()
    check_updates_on_startup: bool = False
    speed_limit_kib: int = 0
    close_to_tray: bool = True
    notifications_enabled: bool = True
    autostart_enabled: bool = False
    tray_hint_shown: bool = False
    download_schedule: DownloadScheduleSettings = DownloadScheduleSettings()
    download_naming: DownloadNamingSettings = DownloadNamingSettings()
    storage_maintenance: StorageMaintenanceSettings = StorageMaintenanceSettings()
    download_storage: DownloadStorageSettings = DownloadStorageSettings()
    last_successful_update_check_utc: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.api_id, int) or isinstance(self.api_id, bool) or self.api_id < 0:
            raise SettingsError("API ID 必须是非负整数")
        if (
            not isinstance(self.concurrency, int)
            or isinstance(self.concurrency, bool)
            or not 1 <= self.concurrency <= 5
        ):
            raise SettingsError("并发数必须在 1 到 5 之间")
        if not isinstance(self.proxy, ProxySettings):
            raise SettingsError("代理设置格式无效")
        if not isinstance(self.check_updates_on_startup, bool):
            raise SettingsError("自动检查更新必须是布尔值")
        if not all(
            isinstance(value, bool)
            for value in (
                self.close_to_tray,
                self.notifications_enabled,
                self.autostart_enabled,
                self.tray_hint_shown,
            )
        ):
            raise SettingsError("后台设置开关必须是布尔值")
        if not isinstance(self.download_schedule, DownloadScheduleSettings):
            raise SettingsError("下载时段设置格式无效")
        if not isinstance(self.download_naming, DownloadNamingSettings):
            raise SettingsError("下载路径模板设置格式无效")
        if not isinstance(self.storage_maintenance, StorageMaintenanceSettings):
            raise SettingsError("存储维护设置格式无效")
        if not isinstance(self.download_storage, DownloadStorageSettings):
            raise SettingsError("下载存储设置格式无效")
        _validate_update_check_time(self.last_successful_update_check_utc)
        try:
            validate_speed_limit_kib(self.speed_limit_kib)
        except ValueError as exc:
            raise SettingsError("总下载限速设置无效") from exc


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()
        try:
            raw: Any = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise SettingsError("设置文件顶层必须是对象")
            proxy_raw = raw.get("proxy", {})
            if not isinstance(proxy_raw, dict):
                raise SettingsError("代理设置必须是对象")
            schedule_raw = raw.get("download_schedule", {})
            if not isinstance(schedule_raw, dict):
                raise SettingsError("下载时段设置必须是对象")
            naming_raw = raw.get("download_naming", {})
            if not isinstance(naming_raw, dict):
                raise SettingsError("下载路径模板设置必须是对象")
            maintenance_raw = raw.get("storage_maintenance", {})
            if not isinstance(maintenance_raw, dict):
                raise SettingsError("存储维护设置必须是对象")
            storage_raw = raw.get("download_storage", {})
            if not isinstance(storage_raw, dict):
                raise SettingsError("下载存储设置必须是对象")
            values = dict(raw)
            values["check_updates_on_startup"] = False
            values["proxy"] = ProxySettings(**proxy_raw)
            values["download_schedule"] = DownloadScheduleSettings(**schedule_raw)
            values["storage_maintenance"] = StorageMaintenanceSettings(
                **maintenance_raw
            )
            try:
                values["download_naming"] = DownloadNamingSettings(**naming_raw)
            except ValueError as error:
                raise SettingsError(str(error)) from error
            values["download_storage"] = DownloadStorageSettings(**storage_raw)
            return AppSettings(**values)
        except SettingsError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise SettingsError("无法读取设置文件") from exc

    def save(self, settings: AppSettings) -> None:
        if not isinstance(settings, AppSettings):
            raise SettingsError("设置对象类型无效")
        payload = asdict(settings)
        payload.pop("check_updates_on_startup", None)
        content = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        _atomic_write(self.path, content)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
