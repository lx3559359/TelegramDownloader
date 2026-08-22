from __future__ import annotations

import ctypes
from typing import Protocol


class KernelApi(Protocol):
    def create_mutex(self, name: str) -> tuple[int, bool]: ...

    def close_handle(self, handle: int) -> None: ...


class WindowsKernelApi:
    def __init__(self) -> None:
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateMutexW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        )
        self.kernel32.CreateMutexW.restype = ctypes.c_void_p
        self.kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        self.kernel32.CloseHandle.restype = ctypes.c_bool

    def create_mutex(self, name: str) -> tuple[int, bool]:
        ctypes.set_last_error(0)
        handle = int(self.kernel32.CreateMutexW(None, False, name) or 0)
        already_exists = ctypes.get_last_error() == WindowsInstanceGuard.ERROR_ALREADY_EXISTS
        return handle, already_exists

    def close_handle(self, handle: int) -> None:
        self.kernel32.CloseHandle(ctypes.c_void_p(handle))


class WindowsInstanceGuard:
    ERROR_ALREADY_EXISTS = 183
    MUTEX_NAME = r"Local\TelegramDownloader.SingleInstance"

    def __init__(self, kernel: KernelApi | None = None) -> None:
        self.kernel = kernel or WindowsKernelApi()
        self.handle = 0

    def acquire(self) -> bool:
        handle, already_exists = self.kernel.create_mutex(self.MUTEX_NAME)
        if not handle:
            raise OSError("无法创建程序单实例保护")
        if already_exists:
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
