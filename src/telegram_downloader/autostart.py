from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol

try:
    import winreg
except ImportError:  # pragma: no cover - Windows is the supported runtime
    winreg = None  # type: ignore[assignment]


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "TelegramDownloader"


class AutostartError(RuntimeError):
    pass


class AutostartUnavailableError(AutostartError):
    pass


class RegistryPort(Protocol):
    def set_value(self, key: str, name: str, value: str) -> None: ...

    def delete_value(self, key: str, name: str) -> None: ...

    def get_value(self, key: str, name: str) -> str | None: ...


class WindowsCurrentUserRegistry:
    @staticmethod
    def _require_windows() -> None:
        if winreg is None:
            raise AutostartUnavailableError("当前系统不支持 Windows 开机启动")

    def set_value(self, key: str, name: str, value: str) -> None:
        self._require_windows()
        try:
            with winreg.CreateKeyEx(  # type: ignore[union-attr]
                winreg.HKEY_CURRENT_USER,  # type: ignore[union-attr]
                key,
                0,
                winreg.KEY_SET_VALUE,  # type: ignore[union-attr]
            ) as handle:
                winreg.SetValueEx(  # type: ignore[union-attr]
                    handle,
                    name,
                    0,
                    winreg.REG_SZ,  # type: ignore[union-attr]
                    value,
                )
        except OSError as exc:
            raise AutostartError("无法更新当前用户的开机启动设置") from exc

    def delete_value(self, key: str, name: str) -> None:
        self._require_windows()
        try:
            with winreg.OpenKey(  # type: ignore[union-attr]
                winreg.HKEY_CURRENT_USER,  # type: ignore[union-attr]
                key,
                0,
                winreg.KEY_SET_VALUE,  # type: ignore[union-attr]
            ) as handle:
                winreg.DeleteValue(handle, name)  # type: ignore[union-attr]
        except FileNotFoundError:
            return
        except OSError as exc:
            raise AutostartError("无法更新当前用户的开机启动设置") from exc

    def get_value(self, key: str, name: str) -> str | None:
        self._require_windows()
        try:
            with winreg.OpenKey(  # type: ignore[union-attr]
                winreg.HKEY_CURRENT_USER,  # type: ignore[union-attr]
                key,
                0,
                winreg.KEY_QUERY_VALUE,  # type: ignore[union-attr]
            ) as handle:
                value, _kind = winreg.QueryValueEx(handle, name)  # type: ignore[union-attr]
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AutostartError("无法读取当前用户的开机启动设置") from exc
        return str(value)


class CurrentUserAutostart:
    def __init__(
        self,
        registry: RegistryPort,
        executable: Path,
        *,
        frozen: bool,
    ) -> None:
        self.registry = registry
        self.executable = executable.resolve()
        self.frozen = frozen

    @property
    def available(self) -> bool:
        return self.frozen and self.executable.is_file()

    def command(self) -> str:
        return subprocess.list2cmdline([str(self.executable), "--background"])

    def enabled(self) -> bool:
        return self.registry.get_value(RUN_KEY, RUN_VALUE_NAME) == self.command()

    def reconcile(self, enabled: bool) -> None:
        if enabled:
            if not self.available:
                raise AutostartUnavailableError("开机启动只支持正式打包程序")
            self.registry.set_value(RUN_KEY, RUN_VALUE_NAME, self.command())
        else:
            self.registry.delete_value(RUN_KEY, RUN_VALUE_NAME)
