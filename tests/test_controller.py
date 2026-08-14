import asyncio
import logging
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import telegram_downloader.controller as controller_module
from telegram_downloader.controller import AppController
from telegram_downloader.domain import ItemStatus, MediaKind, TaskStatus
from telegram_downloader.gateway import (
    AccessDeniedError,
    AuthState,
    GatewayError,
    QrLoginInfo,
)
from telegram_downloader.paths import PortablePaths
from telegram_downloader.settings import AppSettings, ProxySettings
from telegram_downloader.ui.login import LoginPage


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
async def test_credentials_start_qr_login_instead_of_phone_flow() -> None:
    expires = datetime(2026, 8, 14, 1, tzinfo=UTC)

    class Gateway:
        async def connect(self):
            return None

        async def begin_qr_login(self):
            return QrLoginInfo("tg://login?token=first", expires)

        async def wait_qr_login(self):
            return AuthState.PASSWORD_REQUIRED

    class Dialog:
        def __init__(self):
            self.pages = []
            self.qr = None

        def show_qr(self, url, expires_at):
            self.qr = (url, expires_at)

        def show_qr_status(self, _text):
            pass

        def show_page(self, page):
            self.pages.append(page)

        def show_error(self, _message):
            pass

    dialog = Dialog()
    controller = AppController.for_test(
        login_dialog=dialog,
        gateway_factory=lambda *args: Gateway(),
    )

    await controller.submit_credentials(123, "api-secret", ProxySettings(), "")
    await asyncio.sleep(0)

    assert dialog.qr == ("tg://login?token=first", expires)
    assert LoginPage.PHONE not in dialog.pages
    assert dialog.pages[-1] is LoginPage.PASSWORD
    assert controller._qr_wait_task is None


@pytest.mark.asyncio
async def test_show_login_uses_saved_credentials_for_qr() -> None:
    expires = datetime(2026, 8, 14, 1, tzinfo=UTC)

    class Gateway:
        async def begin_qr_login(self):
            return QrLoginInfo("tg://login?token=saved", expires)

        async def wait_qr_login(self):
            return AuthState.PASSWORD_REQUIRED

    class Dialog:
        def __init__(self):
            self.shown = False
            self.qr = None
            self.pages = []

        def show(self):
            self.shown = True

        def raise_(self):
            pass

        def activateWindow(self):
            pass

        def show_qr(self, url, expires_at):
            self.qr = (url, expires_at)

        def show_qr_status(self, _text):
            pass

        def show_page(self, page):
            self.pages.append(page)

        def show_error(self, _message):
            pass

    dialog = Dialog()
    settings = AppSettings(api_id=123)
    controller = AppController.for_test(
        gateway=Gateway(),
        login_dialog=dialog,
        settings=settings,
        secrets={"api_hash": "saved-hash"},
    )

    controller.show_login()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert dialog.shown is True
    assert dialog.qr == ("tg://login?token=saved", expires)
    assert dialog.pages[-1] is LoginPage.PASSWORD


@pytest.mark.asyncio
async def test_manual_qr_refresh_cancels_old_wait_before_starting_new() -> None:
    expires = datetime(2026, 8, 14, 1, tzinfo=UTC)

    class Gateway:
        def __init__(self):
            self.wait_calls = 0
            self.active = 0
            self.peak = 0
            self.cancelled = 0

        async def begin_qr_login(self):
            return QrLoginInfo("tg://login?token=first", expires)

        async def refresh_qr_login(self):
            return QrLoginInfo("tg://login?token=second", expires)

        async def wait_qr_login(self):
            self.wait_calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            finally:
                self.active -= 1

    class Dialog:
        def __init__(self):
            self.urls = []

        def show_qr(self, url, _expires_at):
            self.urls.append(url)

        def show_qr_status(self, _text):
            pass

        def show_error(self, _text):
            pass

    gateway = Gateway()
    controller = AppController.for_test(gateway=gateway, login_dialog=Dialog())

    await controller.begin_qr_login()
    await asyncio.sleep(0)
    try:
        await controller.refresh_qr_login()
        await asyncio.sleep(0)

        assert gateway.wait_calls == 2
        assert gateway.cancelled == 1
        assert gateway.peak == 1
        assert controller.login_dialog.urls == [
            "tg://login?token=first",
            "tg://login?token=second",
        ]
    finally:
        if controller._qr_wait_task is not None:
            controller._qr_wait_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await controller._qr_wait_task


@pytest.mark.asyncio
async def test_expired_qr_refreshes_in_same_wait_task() -> None:
    expires = datetime(2026, 8, 14, 1, tzinfo=UTC)

    class Gateway:
        def __init__(self):
            self.wait_calls = 0
            self.refresh_calls = 0

        async def begin_qr_login(self):
            return QrLoginInfo("tg://login?token=first", expires)

        async def refresh_qr_login(self):
            self.refresh_calls += 1
            return QrLoginInfo("tg://login?token=second", expires)

        async def wait_qr_login(self):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise TimeoutError
            return AuthState.PASSWORD_REQUIRED

    class Dialog:
        def __init__(self):
            self.urls = []
            self.pages = []

        def show_qr(self, url, _expires_at):
            self.urls.append(url)

        def show_qr_status(self, _text):
            pass

        def show_page(self, page):
            self.pages.append(page)

        def show_error(self, _text):
            pass

    gateway = Gateway()
    dialog = Dialog()
    controller = AppController.for_test(gateway=gateway, login_dialog=dialog)

    await controller.begin_qr_login()
    task = controller._qr_wait_task
    assert task is not None
    await task

    assert gateway.wait_calls == 2
    assert gateway.refresh_calls == 1
    assert dialog.urls == [
        "tg://login?token=first",
        "tg://login?token=second",
    ]
    assert dialog.pages[-1] is LoginPage.PASSWORD


@pytest.mark.asyncio
async def test_successful_qr_login_saves_session_through_common_finish_path() -> None:
    expires = datetime(2026, 8, 14, 1, tzinfo=UTC)

    class Gateway:
        async def begin_qr_login(self):
            return QrLoginInfo("tg://login?token=first", expires)

        async def wait_qr_login(self):
            return AuthState.READY

        def export_session(self):
            return "qr-portable-session"

        async def account_name(self):
            return "QR User"

    class Dialog:
        def __init__(self):
            self.ready = None
            self.accepted = False

        def show_qr(self, _url, _expires_at):
            pass

        def show_qr_status(self, _text):
            pass

        def show_ready(self, name):
            self.ready = name

        def accept(self):
            self.accepted = True

        def show_error(self, _text):
            pass

    vault = Vault()
    dialog = Dialog()
    controller = AppController.for_test(
        gateway=Gateway(),
        login_dialog=dialog,
        vault=vault,
    )

    await controller.begin_qr_login()
    task = controller._qr_wait_task
    assert task is not None
    await task

    assert vault.value["session"] == "qr-portable-session"
    assert controller.window.account == "QR User"
    assert dialog.ready == "QR User"
    assert dialog.accepted is True


@pytest.mark.asyncio
async def test_phone_fallback_cancels_qr_wait_before_switching_page() -> None:
    expires = datetime(2026, 8, 14, 1, tzinfo=UTC)

    class Gateway:
        def __init__(self):
            self.cancelled = False

        async def begin_qr_login(self):
            return QrLoginInfo("tg://login?token=first", expires)

        async def wait_qr_login(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    class Dialog:
        def __init__(self):
            self.page = None

        def show_qr(self, _url, _expires_at):
            pass

        def show_qr_status(self, _text):
            pass

        def show_page(self, page):
            self.page = page

        def show_error(self, _text):
            pass

    gateway = Gateway()
    dialog = Dialog()
    controller = AppController.for_test(gateway=gateway, login_dialog=dialog)
    await controller.begin_qr_login()
    await asyncio.sleep(0)

    await controller.use_phone_fallback()

    assert gateway.cancelled is True
    assert dialog.page is LoginPage.PHONE
    assert controller._qr_wait_task is None


@pytest.mark.asyncio
async def test_edit_credentials_cancels_qr_and_disconnects_old_gateway() -> None:
    expires = datetime(2026, 8, 14, 1, tzinfo=UTC)

    class Gateway:
        def __init__(self):
            self.cancelled = False
            self.disconnected = 0

        async def begin_qr_login(self):
            return QrLoginInfo("tg://login?token=first", expires)

        async def wait_qr_login(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def disconnect(self):
            self.disconnected += 1

    class Dialog:
        def __init__(self):
            self.page = None

        def show_qr(self, _url, _expires_at):
            pass

        def show_qr_status(self, _text):
            pass

        def show_page(self, page):
            self.page = page

        def show_error(self, _text):
            pass

    gateway = Gateway()
    dialog = Dialog()
    controller = AppController.for_test(gateway=gateway, login_dialog=dialog)
    await controller.begin_qr_login()
    await asyncio.sleep(0)

    await controller.edit_credentials()

    assert gateway.cancelled is True
    assert gateway.disconnected == 1
    assert dialog.page is LoginPage.CREDENTIALS


@pytest.mark.asyncio
async def test_new_credentials_replace_old_gateway_in_safe_order() -> None:
    expires = datetime(2026, 8, 14, 1, tzinfo=UTC)
    order = []

    class OldGateway:
        async def begin_qr_login(self):
            return QrLoginInfo("tg://login?token=old", expires)

        async def wait_qr_login(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                order.append("old-qr-cancelled")
                raise

        async def disconnect(self):
            order.append("old-disconnected")

    class NewGateway:
        async def connect(self):
            order.append("new-connected")

        async def begin_qr_login(self):
            return QrLoginInfo("tg://login?token=new", expires)

        async def wait_qr_login(self):
            return AuthState.PASSWORD_REQUIRED

    class Dialog:
        def show_qr(self, _url, _expires_at):
            pass

        def show_qr_status(self, _text):
            pass

        def show_page(self, _page):
            pass

        def show_error(self, _text):
            pass

    old = OldGateway()
    controller = AppController.for_test(
        gateway=old,
        login_dialog=Dialog(),
        gateway_factory=lambda *args: NewGateway(),
    )
    await controller.begin_qr_login()
    await asyncio.sleep(0)

    await controller.submit_credentials(456, "new-api-secret", ProxySettings(), "")
    await asyncio.sleep(0)

    assert order == ["old-qr-cancelled", "old-disconnected", "new-connected"]


@pytest.mark.asyncio
async def test_cancel_login_stops_qr_wait_and_disconnects_client() -> None:
    expires = datetime(2026, 8, 14, 1, tzinfo=UTC)

    class Gateway:
        def __init__(self):
            self.cancelled = False
            self.disconnected = 0

        async def begin_qr_login(self):
            return QrLoginInfo("tg://login?token=first", expires)

        async def wait_qr_login(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def disconnect(self):
            self.disconnected += 1

    gateway = Gateway()
    controller = AppController.for_test(gateway=gateway)
    await controller.begin_qr_login()
    await asyncio.sleep(0)

    await controller.cancel_login()

    assert gateway.cancelled is True
    assert gateway.disconnected == 1
    assert controller._qr_wait_task is None


@pytest.mark.asyncio
async def test_shutdown_cancels_qr_wait_before_disconnecting_gateway() -> None:
    expires = datetime(2026, 8, 14, 1, tzinfo=UTC)
    order = []

    class Gateway:
        async def begin_qr_login(self):
            return QrLoginInfo("tg://login?token=first", expires)

        async def wait_qr_login(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                order.append("qr-cancelled")
                raise

        async def disconnect(self):
            order.append("gateway-disconnected")

    class Scheduler:
        async def shutdown(self):
            order.append("scheduler-stopped")

    controller = AppController.for_test(gateway=Gateway(), scheduler=Scheduler())
    await controller.begin_qr_login()
    await asyncio.sleep(0)

    await controller.shutdown()

    assert order == ["qr-cancelled", "scheduler-stopped", "gateway-disconnected"]
    assert controller._qr_wait_task is None


@pytest.mark.asyncio
async def test_qr_wait_failure_is_consumed_and_shown_safely() -> None:
    expires = datetime(2026, 8, 14, 1, tzinfo=UTC)

    class Gateway:
        async def begin_qr_login(self):
            return QrLoginInfo("tg://login?token=first", expires)

        async def wait_qr_login(self):
            raise GatewayError("Telegram 网络连接失败")

    class Dialog:
        def __init__(self):
            self.error = None

        def show_qr(self, _url, _expires_at):
            pass

        def show_qr_status(self, _text):
            pass

        def show_error(self, text):
            self.error = text

    dialog = Dialog()
    controller = AppController.for_test(gateway=Gateway(), login_dialog=dialog)

    await controller.begin_qr_login()
    task = controller._qr_wait_task
    assert task is not None
    await task

    assert dialog.error == "Telegram 网络连接失败"


def test_show_login_without_credentials_returns_to_credentials_page() -> None:
    class Dialog:
        def __init__(self):
            self.page = LoginPage.PHONE

        def show(self):
            pass

        def raise_(self):
            pass

        def activateWindow(self):
            pass

        def show_page(self, page):
            self.page = page

    dialog = Dialog()
    controller = AppController.for_test(login_dialog=dialog)

    controller.show_login()

    assert dialog.page is LoginPage.CREDENTIALS


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
async def test_running_task_refreshes_window_before_download_finishes() -> None:
    release = asyncio.Event()
    started = asyncio.Event()
    task = SimpleNamespace(
        id="task-1",
        source_title="示例频道",
        status=TaskStatus.QUEUED,
        last_error=None,
    )
    item = SimpleNamespace(
        status=ItemStatus.QUEUED,
        expected_size=100,
        downloaded_bytes=0,
        last_error=None,
    )

    class Repository:
        def list_tasks(self):
            return [task]

        def list_items(self, task_id):
            assert task_id == "task-1"
            return [item]

    class Scheduler:
        async def run_task(self, task_id):
            assert task_id == "task-1"
            task.status = TaskStatus.DOWNLOADING
            item.status = ItemStatus.DOWNLOADING
            item.downloaded_bytes = 25
            started.set()
            await release.wait()
            item.downloaded_bytes = 100
            item.status = ItemStatus.COMPLETED
            task.status = TaskStatus.COMPLETED

    class Window:
        def __init__(self):
            self.snapshots = []
            self.downloading = asyncio.Event()

        def set_task_summaries(self, summaries):
            self.snapshots.append(summaries)
            if summaries and summaries[0].status is TaskStatus.DOWNLOADING:
                self.downloading.set()

    window = Window()
    controller = AppController.for_test(
        repository=Repository(),
        scheduler=Scheduler(),
        window=window,
        progress_refresh_interval=0.01,
    )

    running = asyncio.create_task(controller._run_and_refresh("task-1"))
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(window.downloading.wait(), timeout=1)

    assert running.done() is False
    assert window.snapshots[-1][0].downloaded_bytes == 25

    release.set()
    await running

    assert window.snapshots[-1][0].status is TaskStatus.COMPLETED
    assert window.snapshots[-1][0].completed_items == 1


def test_refresh_tasks_calculates_speed_and_remaining_time() -> None:
    task = SimpleNamespace(
        id="task-1",
        source_title="频道",
        status=TaskStatus.DOWNLOADING,
        last_error=None,
    )
    item = SimpleNamespace(
        status=ItemStatus.DOWNLOADING,
        expected_size=1024,
        downloaded_bytes=0,
        last_error=None,
    )

    class Repository:
        def list_tasks(self):
            return [task]

        def list_items(self, _task_id):
            return [item]

    class Window:
        def __init__(self):
            self.tasks = []

        def set_task_summaries(self, summaries):
            self.tasks = summaries

    window = Window()
    controller = AppController.for_test(repository=Repository(), window=window)
    controller.refresh_tasks(now=10.0)
    item.downloaded_bytes = 512
    controller.refresh_tasks(now=11.0)

    summary = window.tasks[0]
    assert summary.speed_bps == 512
    assert summary.speed_text == "512 B/s"
    assert summary.remaining_seconds == 1
    assert summary.remaining_text == "1 秒"


def test_search_task_uses_display_title_but_opens_source_directory(
    tmp_path, monkeypatch
) -> None:
    task = SimpleNamespace(
        id="task-1",
        source_title="资料群",
        display_title="资料群（搜索：安装）",
        status=TaskStatus.QUEUED,
        last_error=None,
    )
    item = SimpleNamespace(
        status=ItemStatus.QUEUED,
        expected_size=100,
        downloaded_bytes=0,
        last_error=None,
    )

    class Repository:
        def list_tasks(self):
            return [task]

        def list_items(self, _task_id):
            return [item]

        def get_task(self, task_id):
            assert task_id == task.id
            return task

    class Window:
        def __init__(self):
            self.tasks = []

        def set_task_summaries(self, summaries):
            self.tasks = summaries

    opened = []
    monkeypatch.setattr(
        controller_module.os,
        "startfile",
        lambda directory: opened.append(directory),
        raising=False,
    )
    window = Window()
    paths = PortablePaths(tmp_path)
    controller = AppController.for_test(
        repository=Repository(),
        window=window,
        paths=paths,
    )

    controller.refresh_tasks(now=10.0)
    controller.open_task_directory(task.id)

    assert window.tasks[0].title == "资料群（搜索：安装）"
    assert opened == [(paths.downloads / "资料群").resolve()]
    assert not (paths.downloads / "资料群（搜索：安装）").exists()


def test_progress_refresh_is_throttled_across_concurrent_callers() -> None:
    class Window:
        def __init__(self):
            self.refreshes = 0

        def set_task_summaries(self, _summaries):
            self.refreshes += 1

    window = Window()
    controller = AppController.for_test(window=window, progress_refresh_interval=0.5)

    controller._refresh_tasks_if_due(20.0)
    controller._refresh_tasks_if_due(20.1)
    controller._refresh_tasks_if_due(20.5)

    assert window.refreshes == 2


def test_progress_refresh_interval_must_be_positive() -> None:
    with pytest.raises(ValueError, match="进度刷新间隔必须大于零"):
        AppController.for_test(progress_refresh_interval=0)


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


@pytest.mark.asyncio
async def test_background_failure_is_consumed_without_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Window:
        def __init__(self):
            self.message = ""
            self.timeout = -1

        def set_task_summaries(self, _tasks):
            pass

        def statusBar(self):
            return self

        def showMessage(self, message, timeout):
            self.message = message
            self.timeout = timeout

    async def fail():
        raise RuntimeError("api-secret-in-background-error")

    caplog.set_level(logging.ERROR, logger="telegram_downloader.controller")
    window = Window()
    controller = AppController.for_test(window=window)

    controller._spawn_background(fail())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert controller._background == set()
    assert window.message == "操作失败（RuntimeError）"
    assert window.timeout == 0
    assert "background task failed (RuntimeError)" in caplog.text
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
