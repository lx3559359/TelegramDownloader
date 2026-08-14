from telegram_downloader.instance_guard import WindowsInstanceGuard


class KernelStub:
    def __init__(self, *, last_error: int = 0) -> None:
        self.last_error = last_error
        self.closed: list[int] = []

    def create_mutex(self, name: str) -> int:
        assert name == r"Local\TelegramDownloader.SingleInstance"
        return 41

    def get_last_error(self) -> int:
        return self.last_error

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
