from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class SettingsError(ValueError):
    """Raised when project-local settings are malformed or unsafe."""


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
    check_updates_on_startup: bool = True

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
            values = dict(raw)
            values["proxy"] = ProxySettings(**proxy_raw)
            return AppSettings(**values)
        except SettingsError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise SettingsError("无法读取设置文件") from exc

    def save(self, settings: AppSettings) -> None:
        if not isinstance(settings, AppSettings):
            raise SettingsError("设置对象类型无效")
        content = (
            json.dumps(asdict(settings), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
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
