from __future__ import annotations

import ctypes
from typing import Protocol


class KernelApi(Protocol):
    def create_mutex(self, name: str) -> int: ...

    def get_last_error(self) -> int: ...

    def close_handle(self, handle: int) -> None: ...


class WindowsKernelApi:
    def __init__(self) -> None:
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def create_mutex(self, name: str) -> int:
        return int(self.kernel32.CreateMutexW(None, False, name) or 0)

    def get_last_error(self) -> int:
        return ctypes.get_last_error()

    def close_handle(self, handle: int) -> None:
        self.kernel32.CloseHandle(handle)


class WindowsInstanceGuard:
    ERROR_ALREADY_EXISTS = 183
    MUTEX_NAME = r"Local\TelegramDownloader.SingleInstance"

    def __init__(self, kernel: KernelApi | None = None) -> None:
        self.kernel = kernel or WindowsKernelApi()
        self.handle = 0

    def acquire(self) -> bool:
        handle = self.kernel.create_mutex(self.MUTEX_NAME)
        if not handle:
            raise OSError("无法创建程序单实例保护")
        if self.kernel.get_last_error() == self.ERROR_ALREADY_EXISTS:
            self.kernel.close_handle(handle)
            return False
        self.handle = handle
        return True

    def notify_already_running(self) -> None:
        ctypes.windll.user32.MessageBoxW(
            None,
            "Telegram 下载器已经在运行。",
            "Telegram 下载器",
            0x40,
        )

    def release(self) -> None:
        if self.handle:
            self.kernel.close_handle(self.handle)
            self.handle = 0
