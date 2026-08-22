import ctypes
from types import SimpleNamespace

from telegram_downloader.instance_guard import WindowsInstanceGuard, WindowsKernelApi


class KernelStub:
    def __init__(self, *, last_error: int = 0) -> None:
        self.last_error = last_error
        self.closed: list[int] = []

    def create_mutex(self, name: str) -> tuple[int, bool]:
        assert name == r"Local\TelegramDownloader.SingleInstance"
        return 41, self.last_error == WindowsInstanceGuard.ERROR_ALREADY_EXISTS

    def close_handle(self, handle: int) -> None:
        self.closed.append(handle)


def test_first_instance_owns_mutex_until_release() -> None:
    kernel = KernelStub()
    guard = WindowsInstanceGuard(kernel=kernel)

    assert guard.acquire() is True

    guard.release()
    assert kernel.closed == [41]


def test_duplicate_instance_closes_unowned_handle() -> None:
    kernel = KernelStub(last_error=183)
    guard = WindowsInstanceGuard(kernel=kernel)

    assert guard.acquire() is False

    assert kernel.closed == [41]


def test_windows_api_clears_stale_last_error_before_creating_mutex() -> None:
    api = WindowsKernelApi.__new__(WindowsKernelApi)
    api.kernel32 = SimpleNamespace(CreateMutexW=lambda *_args: 42)
    ctypes.set_last_error(WindowsInstanceGuard.ERROR_ALREADY_EXISTS)
    try:
        assert api.create_mutex("probe") == (42, False)
    finally:
        ctypes.set_last_error(0)
