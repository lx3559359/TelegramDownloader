import asyncio
import logging
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from telegram_downloader.controller import AppController
from telegram_downloader.domain import MediaKind, TaskStatus
from telegram_downloader.gateway import AccessDeniedError, AuthState
from telegram_downloader.settings import ProxySettings


class Vault:
    def __init__(self):
        self.value = {}

    def save(self, value):
        self.value = value

    def load(self):
        return dict(self.value)


@pytest.mark.asyncio
async def test_startup_error_does_not_expose_unknown_exception_text() -> None:
    class Gateway:
        async def connect(self):
            raise RuntimeError("proxy-password-secret")

    class Window:
        def __init__(self):
            self.message = ""

        def set_task_summaries(self, _tasks):
            pass

        def statusBar(self):
            return self

        def showMessage(self, message, _timeout):
            self.message = message

    window = Window()
    controller = AppController.for_test(gateway=Gateway(), window=window)

    await controller.start()

    assert "RuntimeError" in window.message
    assert "proxy-password-secret" not in window.message


@pytest.mark.asyncio
async def test_code_login_saves_exported_session() -> None:
    class Gateway:
        async def sign_in(self, phone, code, phone_code_hash):
            return AuthState.READY

        def export_session(self):
            return "portable-session"

    vault = Vault()
    controller = AppController.for_test(gateway=Gateway(), vault=vault)
    controller.phone, controller.phone_code_hash = "+8613800000000", "hash"

    await controller.submit_code("12345")

    assert vault.value["session"] == "portable-session"
    assert controller.phone == ""
    assert controller.phone_code_hash == ""


@pytest.mark.asyncio
async def test_credentials_store_secrets_separately_from_settings() -> None:
    class Gateway:
        async def connect(self):
            return None

    created = []
    vault = Vault()
    controller = AppController.for_test(
        vault=vault,
        gateway_factory=lambda *args: created.append(args) or Gateway(),
    )

    await controller.submit_credentials(
        123,
        "api-secret",
        ProxySettings("http", "127.0.0.1", 8080, "user"),
        "proxy-secret",
    )

    assert controller.settings_store.value.api_id == 123
    assert vault.value == {"api_hash": "api-secret", "proxy_password": "proxy-secret"}
    assert created[0][0] == 123


@pytest.mark.asyncio
async def test_scan_requires_user_confirmation_before_commit() -> None:
    class Planner:
        committed = False

        async def scan(self, source, filters):
            return "preview"

        def commit(self, preview):
            self.committed = True

    planner = Planner()
    controller = AppController.for_test(
        planner=planner,
        confirm_preview=lambda preview: False,
    )

    await controller.scan_link(
        "https://t.me/example/42",
        controller.default_filters(datetime(2026, 8, 13, tzinfo=UTC)),
    )

    assert planner.committed is False


@pytest.mark.asyncio
async def test_confirmed_scan_starts_persisted_task() -> None:
    class Planner:
        async def scan(self, source, filters):
            return "preview"

        def commit(self, preview):
            return type("Task", (), {"id": "task-1", "status": TaskStatus.QUEUED})()

    class Scheduler:
        def __init__(self):
            self.started = asyncio.Event()

        async def run_task(self, task_id):
            assert task_id == "task-1"
            self.started.set()

    scheduler = Scheduler()
    controller = AppController.for_test(
        planner=Planner(),
        scheduler=scheduler,
        confirm_preview=lambda preview: True,
    )

    await controller.scan_link(
        "https://t.me/example/42",
        controller.default_filters(datetime(2026, 8, 13, tzinfo=UTC)),
    )
    await asyncio.wait_for(scheduler.started.wait(), timeout=1)


@pytest.mark.asyncio
async def test_scan_failure_is_persistent_and_releases_busy_state() -> None:
    class Planner:
        async def scan(self, source, filters):
            raise AccessDeniedError("当前账号未加入该私有频道或群组")

    class Window:
        def __init__(self):
            self.message = ""
            self.timeout = -1
            self.busy_states = []

        def set_task_summaries(self, _tasks):
            pass

        def set_scan_busy(self, busy):
            self.busy_states.append(busy)

        def statusBar(self):
            return self

        def showMessage(self, message, timeout):
            self.message = message
            self.timeout = timeout

    window = Window()
    controller = AppController.for_test(planner=Planner(), window=window)

    await controller.scan_link(
        "https://t.me/c/123456/7",
        controller.default_filters(datetime(2026, 8, 14, tzinfo=UTC)),
    )

    assert window.message == "当前账号未加入该私有频道或群组"
    assert window.timeout == 0
    assert window.busy_states == [True, False]


@pytest.mark.asyncio
async def test_unexpected_scan_failure_is_logged_without_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Planner:
        async def scan(self, source, filters):
            raise RuntimeError("api-secret-in-library-error")

    class Window:
        def __init__(self):
            self.message = ""
            self.timeout = -1
            self.busy = False

        def set_task_summaries(self, _tasks):
            pass

        def set_scan_busy(self, busy):
            self.busy = busy

        def statusBar(self):
            return self

        def showMessage(self, message, timeout):
            self.message = message
            self.timeout = timeout

    caplog.set_level(logging.ERROR, logger="telegram_downloader.controller")
    window = Window()
    controller = AppController.for_test(planner=Planner(), window=window)

    await controller.scan_link(
        "https://t.me/example/7",
        controller.default_filters(datetime(2026, 8, 14, tzinfo=UTC)),
    )

    assert window.message == "操作失败（RuntimeError）"
    assert window.timeout == 0
    assert window.busy is False
    assert "scan failed (RuntimeError)" in caplog.text
    assert "api-secret" not in caplog.text


def test_local_dates_become_inclusive_utc_boundaries() -> None:
    filters = AppController.filters_from_dates(
        date(2026, 8, 1),
        date(2026, 8, 2),
        frozenset(MediaKind),
        500,
        timezone(timedelta(hours=8)),
    )

    assert filters.date_from_utc.isoformat() == "2026-07-31T16:00:00+00:00"
    assert filters.date_to_utc.isoformat() == "2026-08-02T15:59:59.999999+00:00"
