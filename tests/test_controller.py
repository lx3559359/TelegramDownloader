import asyncio
import logging
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import telegram_downloader.controller as controller_module
from telegram_downloader.connectivity import ConnectionRecovery
from telegram_downloader.content import (
    AccountProfile,
    ContentSearchQuery,
    SearchSession,
    SearchStatus,
)
from telegram_downloader.content_progress import DialogSyncProgress, SearchProgress
from telegram_downloader.controller import AppController
from telegram_downloader.domain import ItemStatus, MediaKind, ScanFilters, TaskStatus
from telegram_downloader.gateway import (
    AccessDeniedError,
    AuthState,
    GatewayError,
    QrLoginInfo,
    SessionExpiredError,
    TransientNetworkError,
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


class ConnectedGateway:
    async def connect(self):
        pass

    async def disconnect(self):
        pass


@pytest.mark.asyncio
async def test_startup_error_does_not_expose_unknown_exception_text() -> None:
    class Gateway:
        async def connect(self):
            raise RuntimeError("proxy-password-secret")

        async def disconnect(self):
            pass

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
    assert window.message == ""
    assert controller._session_restore_task is not None
    await controller._session_restore_task

    assert "RuntimeError" in window.message
    assert "proxy-password-secret" not in window.message
    await controller.shutdown()


@pytest.mark.asyncio
async def test_transient_offline_state_keeps_session_and_never_opens_login() -> None:
    class Gateway:
        def is_connected(self) -> bool:
            return False

        async def connect(self) -> None:
            raise TransientNetworkError("offline")

    vault = Vault()
    vault.value = {"session": "saved", "api_hash": "hash"}
    controller = AppController.for_test(
        gateway=Gateway(),
        vault=vault,
        secrets=vault.load(),
        connection_recovery=ConnectionRecovery(delays=(0.0,)),
    )
    shown: list[str] = []
    controller.show_login = lambda: shown.append("login")

    assert await controller.ensure_telegram_online() is False
    assert controller.secrets["session"] == "saved"
    assert vault.load()["session"] == "saved"
    assert shown == []


@pytest.mark.asyncio
async def test_connection_monitor_waits_30_seconds_and_shutdown_cancels_it() -> None:
    sleeping = asyncio.Event()
    blocker = asyncio.Event()
    intervals: list[float] = []

    async def sleep(value: float) -> None:
        intervals.append(value)
        sleeping.set()
        await blocker.wait()

    class Gateway:
        def is_connected(self) -> bool:
            return False

        async def disconnect(self) -> None:
            pass

    controller = AppController.for_test(
        gateway=Gateway(),
        connection_monitor_interval=30.0,
        connection_sleeper=sleep,
    )
    task = asyncio.create_task(controller._monitor_connection())
    controller._connection_monitor_task = task
    await sleeping.wait()

    assert intervals == [30.0]
    await controller.shutdown()
    assert task.cancelled() is True


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


def test_show_login_prefills_saved_credentials_before_show() -> None:
    calls = []
    proxy = ProxySettings("http", "127.0.0.1", 8080, "alice")

    class Dialog:
        def set_saved_credentials(self, *values):
            calls.append(("prefill", values))

        def show(self):
            calls.append("show")

        def raise_(self):
            calls.append("raise")

        def activateWindow(self):
            calls.append("activate")

        def show_page(self, page):
            calls.append(("page", page))

    controller = AppController.for_test(
        login_dialog=Dialog(),
        settings=AppSettings(api_id=12345, proxy=proxy),
        secrets={
            "api_hash": "saved-hash",
            "proxy_password": "saved-password",
        },
    )

    controller.show_login()

    assert calls[0] == (
        "prefill",
        (12345, "saved-hash", proxy, "saved-password"),
    )
    assert calls[1:4] == ["show", "raise", "activate"]
    assert calls[4] == ("page", LoginPage.CREDENTIALS)


@pytest.mark.asyncio
async def test_qr_network_failure_keeps_prefill_and_returns_to_credentials() -> None:
    calls = []
    proxy = ProxySettings("socks5", "127.0.0.1", 1080, "alice")

    class Gateway:
        async def begin_qr_login(self):
            raise TransientNetworkError("Telegram 网络连接失败")

    class Dialog:
        def set_saved_credentials(self, *values):
            calls.append(("prefill", values))

        def show_page(self, page):
            calls.append(("page", page))

        def show_error(self, message):
            calls.append(("error", message))

    controller = AppController.for_test(
        gateway=Gateway(),
        login_dialog=Dialog(),
        settings=AppSettings(api_id=12345, proxy=proxy),
        secrets={
            "api_hash": "saved-hash",
            "proxy_password": "saved-password",
        },
    )

    await controller.begin_qr_login()

    assert calls[0][0] == "prefill"
    assert calls[1] == ("page", LoginPage.CREDENTIALS)
    assert calls[2] == ("error", "Telegram 网络连接失败")
    assert controller.secrets["api_hash"] == "saved-hash"


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
        gateway=ConnectedGateway(),
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
        gateway=ConnectedGateway(),
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


class ContentPageFake:
    def __init__(self):
        self.logged_in = None
        self.dialogs = []
        self.sessions = []
        self.results = []
        self.active_search_id = None
        self.busy = []
        self.search_progress = []
        self.sync_states = []
        self.connection_states = []
        self.connection_retryable = []
        self.thumbnails = {}
        self.previews = []
        self.errors = []

    def set_logged_in(self, value):
        self.logged_in = value

    def set_dialogs(self, value):
        self.dialogs = value

    def set_sessions(self, value):
        self.sessions = value

    def set_active_search(self, value):
        self.active_search_id = value.id if value else None

    def set_results(self, value):
        self.results = value

    def set_search_busy(self, value):
        self.busy.append(value)

    def set_search_progress(self, progress):
        self.search_progress.append(progress)

    def set_sync_state(self, text, *, busy=False, count=0):
        self.sync_states.append((text, busy, count))

    def set_connection_state(self, text, *, retryable=False):
        self.connection_states.append(text)
        self.connection_retryable.append(retryable)

    def set_thumbnail(self, result_id, path):
        self.thumbnails[result_id] = path

    def show_preview(self, result, path):
        self.previews.append((result, path))

    def show_error(self, message):
        self.errors.append(message)


class ContentWindowFake:
    def __init__(self):
        self.content_page = ContentPageFake()
        self.account = None
        self.message = ""

    def set_task_summaries(self, _tasks):
        pass

    def set_account(self, value):
        self.account = value
        self.content_page.set_logged_in(bool(value))

    def statusBar(self):
        return self

    def showMessage(self, message, _timeout):
        self.message = message


@pytest.mark.asyncio
async def test_manual_refresh_reports_counts_and_keeps_one_task() -> None:
    dialogs = [SimpleNamespace(id="d1"), SimpleNamespace(id="d2")]

    class Gateway:
        def is_connected(self) -> bool:
            return True

    class ContentService:
        async def sync_dialogs(self, *, on_progress=None):
            on_progress(DialogSyncProgress(1))
            on_progress(DialogSyncProgress(2))
            return dialogs

    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=Gateway(),
        content_browser=ContentService(),
        window=window,
    )

    await controller.refresh_content_dialogs()

    assert window.content_page.sync_states == [
        ("正在刷新群组…", True, 0),
        ("正在刷新，已发现 1 个群组/频道", True, 1),
        ("正在刷新，已发现 2 个群组/频道", True, 2),
        ("刚刚同步，共 2 个", False, 2),
    ]
    assert controller._dialog_sync_task is None


@pytest.mark.asyncio
async def test_repeated_manual_refresh_does_not_stack_sync_tasks() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class Gateway:
        def is_connected(self) -> bool:
            return True

    class ContentService:
        def __init__(self) -> None:
            self.calls = 0

        async def sync_dialogs(self, *, on_progress=None):
            self.calls += 1
            started.set()
            await release.wait()
            return []

    content = ContentService()
    controller = AppController.for_test(
        gateway=Gateway(),
        content_browser=content,
        window=ContentWindowFake(),
    )
    first = asyncio.create_task(controller.refresh_content_dialogs())
    await started.wait()

    await controller.refresh_content_dialogs()

    assert content.calls == 1
    release.set()
    await first


@pytest.mark.asyncio
async def test_search_progress_is_forwarded_and_always_stops() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 500),
    )
    session = SearchSession(
        "search-1",
        "a1",
        "-1001",
        "资料群",
        query,
        SearchStatus.COMPLETED,
        1,
        None,
        True,
        3,
        now,
        now,
    )

    class Gateway:
        def is_connected(self) -> bool:
            return True

    class Browser:
        async def start_search(self, _peer_ref, _query, *, on_progress=None):
            on_progress(SearchProgress(20, 3, "正在整理结果"))
            return session, []

        def list_sessions(self):
            return [session]

        def list_results(self, _search_id):
            return []

    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=Gateway(),
        content_browser=Browser(),
        window=window,
    )

    await controller.search_content("-1001", query)

    assert window.content_page.busy == [True, False]
    assert window.content_page.search_progress[0] == SearchProgress(
        0, 0, "正在连接 Telegram"
    )
    assert window.content_page.search_progress[-2].inspected == 20
    assert window.content_page.search_progress[-1] is None


@pytest.mark.asyncio
async def test_new_search_cancels_the_running_search_before_replacement() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    first_started = asyncio.Event()
    calls: list[str] = []

    def make_query(keyword: str) -> ContentSearchQuery:
        return ContentSearchQuery(
            keyword,
            ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 500),
        )

    def make_session(query: ContentSearchQuery) -> SearchSession:
        return SearchSession(
            f"session-{query.keyword}",
            "a1",
            "-1001",
            "资料群",
            query,
            SearchStatus.COMPLETED,
            1,
            None,
            True,
            0,
            now,
            now,
        )

    class Gateway:
        def is_connected(self) -> bool:
            return True

    class Browser:
        async def start_search(self, _peer_ref, query, *, on_progress=None):
            calls.append(query.keyword)
            if query.keyword == "first":
                first_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    calls.append("first-cancelled")
                    raise
            return make_session(query), []

        def list_sessions(self):
            return []

    controller = AppController.for_test(
        gateway=Gateway(),
        content_browser=Browser(),
        window=ContentWindowFake(),
    )
    first = asyncio.create_task(
        controller.search_content("-1001", make_query("first"))
    )
    await first_started.wait()

    await controller.search_content("-1001", make_query("second"))

    with pytest.raises(asyncio.CancelledError):
        await first
    assert calls == ["first", "first-cancelled", "second"]


@pytest.mark.asyncio
async def test_manual_connection_retry_shares_the_existing_recovery() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        def is_connected(self) -> bool:
            return False

        async def connect(self) -> None:
            self.calls += 1
            entered.set()
            await release.wait()

    gateway = Gateway()
    recovery = ConnectionRecovery(delays=(0.0,))
    controller = AppController.for_test(
        gateway=gateway,
        connection_recovery=recovery,
        window=ContentWindowFake(),
    )
    first = asyncio.create_task(controller.retry_telegram_connection())
    second = asyncio.create_task(controller.retry_telegram_connection())
    await entered.wait()
    release.set()

    assert await asyncio.gather(first, second) == [True, True]
    assert gateway.calls == 1
    assert controller.connection_recovery is recovery


@pytest.mark.asyncio
async def test_content_preview_uses_scoped_metadata_and_loaded_thumbnail(
    tmp_path,
) -> None:
    result = SimpleNamespace(id="result-1")
    path = tmp_path / "preview.jpg"

    class Browser:
        def get_result(self, result_id):
            assert result_id == result.id
            return result

        async def load_thumbnail(self, result_id):
            assert result_id == result.id
            return path

    window = ContentWindowFake()
    controller = AppController.for_test(
        content_browser=Browser(),
        window=window,
    )

    await controller.open_content_preview(result.id)

    assert window.content_page.previews == [(result, path)]


@pytest.mark.asyncio
async def test_offline_search_reconnects_then_continues() -> None:
    calls = []
    active = SimpleNamespace(id="search-1")

    class Gateway:
        async def connect(self):
            calls.append("connect")

    class ContentService:
        async def start_search(self, peer_ref, query, *, on_progress=None):
            calls.append(("search", peer_ref, query))
            return active, []

        def list_sessions(self):
            return [active]

        def list_results(self, _search_id):
            return []

    query = object()
    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=Gateway(),
        content_browser=ContentService(),
        window=window,
    )

    await controller.search_content("-1001", query)

    assert calls == ["connect", ("search", "-1001", query)]
    assert window.content_page.busy == [True, False]
    assert window.content_page.connection_states[-1] == "连接已恢复"


@pytest.mark.asyncio
async def test_failed_reconnect_keeps_cached_content_and_skips_search() -> None:
    class Gateway:
        def __init__(self):
            self.calls = 0

        async def connect(self):
            self.calls += 1
            raise TransientNetworkError("Telegram 网络连接失败")

    class ContentService:
        def __init__(self):
            self.search_calls = 0

        async def start_search(self, _peer_ref, _query, *, on_progress=None):
            self.search_calls += 1

        def list_sessions(self):
            return []

    gateway = Gateway()
    content = ContentService()
    window = ContentWindowFake()
    cached_dialogs = [SimpleNamespace(peer_ref="-1001")]
    cached_results = [SimpleNamespace(id="result-1")]
    window.content_page.dialogs = cached_dialogs
    window.content_page.results = cached_results
    controller = AppController.for_test(
        gateway=gateway,
        content_browser=content,
        window=window,
        connection_recovery=ConnectionRecovery(delays=(0.0, 0.0, 0.0)),
    )

    await controller.search_content("-1001", object())

    assert gateway.calls == 3
    assert content.search_calls == 0
    assert window.content_page.dialogs is cached_dialogs
    assert window.content_page.results is cached_results
    assert window.content_page.connection_states[-1] == (
        "重连失败，请检查网络或代理后重试"
    )
    assert window.content_page.busy == [True, False]


@pytest.mark.asyncio
async def test_content_page_only_syncs_stale_dialog_cache() -> None:
    class Gateway:
        async def connect(self):
            pass

    class ContentService:
        def __init__(self, stale):
            self.account = AccountProfile("a1", "账号一")
            self.stale = stale
            self.sync_calls = 0

        def list_dialogs(self):
            return []

        def list_sessions(self):
            return []

        def dialog_cache_stale(self, max_age):
            assert max_age == timedelta(seconds=60)
            return self.stale

        async def sync_dialogs(self, *, on_progress=None):
            self.sync_calls += 1
            return []

    fresh = ContentService(False)
    fresh_controller = AppController.for_test(
        gateway=Gateway(),
        content_browser=fresh,
        window=ContentWindowFake(),
    )
    await fresh_controller.activate_content_page()
    assert fresh.sync_calls == 0

    stale = ContentService(True)
    stale_controller = AppController.for_test(
        gateway=Gateway(),
        content_browser=stale,
        window=ContentWindowFake(),
    )
    await stale_controller.activate_content_page()
    assert stale_controller._dialog_sync_task is not None
    await stale_controller._dialog_sync_task
    assert stale.sync_calls == 1


@pytest.mark.asyncio
async def test_manual_dialog_refresh_forces_sync_when_cache_is_fresh() -> None:
    class Gateway:
        async def connect(self):
            pass

    class ContentService:
        def __init__(self):
            self.sync_calls = 0

        async def sync_dialogs(self, *, on_progress=None):
            self.sync_calls += 1
            return []

    content = ContentService()
    controller = AppController.for_test(
        gateway=Gateway(),
        content_browser=content,
        window=ContentWindowFake(),
    )

    await controller.refresh_content_dialogs()

    assert content.sync_calls == 1


@pytest.mark.asyncio
async def test_dialog_selection_restores_history_before_connect_finishes() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    active = SimpleNamespace(id="search-1", peer_ref="-1001")
    results = [SimpleNamespace(id="result-1")]

    class Gateway:
        async def connect(self):
            entered.set()
            await release.wait()

    class ContentService:
        def latest_session(self, peer_ref):
            return active if peer_ref == "-1001" else None

        def list_results(self, search_id):
            assert search_id == "search-1"
            return results

        def dialog_cache_stale(self, _max_age):
            return False

    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=Gateway(),
        content_browser=ContentService(),
        window=window,
    )
    operation = asyncio.create_task(controller.select_content_dialog("-1001"))
    await entered.wait()

    assert window.content_page.active_search_id == "search-1"
    assert window.content_page.results == results

    release.set()
    await operation


def test_content_link_route_normalizes_single_hint_before_task_preview() -> None:
    class Window(ContentWindowFake):
        def __init__(self):
            super().__init__()
            self.previews = []

        def open_link_preview(self, link):
            self.previews.append(link)

    window = Window()
    controller = AppController.for_test(window=window)

    controller.route_content_link(
        "https://t.me/Zhangzhoulao66/56156?single"
    )

    assert window.previews == ["https://t.me/Zhangzhoulao66/56156"]
    assert window.content_page.errors == []


def test_invalid_content_link_stays_local_and_shows_parser_error() -> None:
    class Window(ContentWindowFake):
        def open_link_preview(self, _link):
            raise AssertionError("invalid link must not leave content page")

    window = Window()
    controller = AppController.for_test(window=window)

    controller.route_content_link("https://t.me/example/1#fragment")

    assert window.content_page.errors == ["请输入有效的 t.me 链接"]


@pytest.mark.asyncio
async def test_start_displays_cached_content_before_network_failure() -> None:
    calls = []
    cached_dialog = SimpleNamespace(title="离线群")

    class ContentService:
        async def activate_cached_account(self):
            calls.append("cached")
            return AccountProfile("a1", "缓存账号"), [cached_dialog]

        def list_sessions(self):
            return []

    class Gateway:
        async def connect(self):
            calls.append("connect")
            raise RuntimeError("offline")

        async def disconnect(self):
            pass

    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=Gateway(),
        content_browser=ContentService(),
        window=window,
    )

    await controller.start()

    assert calls == ["cached"]
    assert window.content_page.dialogs == [cached_dialog]
    assert window.content_page.logged_in is False
    assert controller._session_restore_task is not None

    await controller._session_restore_task

    assert calls == ["cached", "connect"]
    await controller.shutdown()


@pytest.mark.asyncio
async def test_online_activation_starts_dialog_sync_without_blocking_start() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    fresh_dialog = SimpleNamespace(title="新同步群")

    class ContentService:
        async def activate_cached_account(self):
            return None, []

        async def activate_account(self):
            return AccountProfile("a1", "账号一"), [SimpleNamespace(title="缓存群")]

        def list_sessions(self):
            return []

        async def sync_dialogs(self, *, on_progress=None):
            started.set()
            await release.wait()
            return [fresh_dialog]

        def go_offline(self):
            pass

    class Gateway:
        async def connect(self):
            pass

        async def account_name(self):
            return "账号一"

        async def disconnect(self):
            pass

    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=Gateway(),
        content_browser=ContentService(),
        window=window,
    )

    await controller.start()
    await asyncio.wait_for(started.wait(), timeout=1)

    assert window.content_page.dialogs[0].title == "缓存群"
    assert window.account == "账号一"
    assert controller._dialog_sync_task is not None
    assert controller._dialog_sync_task.done() is False

    release.set()
    await controller._dialog_sync_task
    assert window.content_page.dialogs == [fresh_dialog]
    await controller.shutdown()


@pytest.mark.asyncio
async def test_content_search_selection_and_queue_flow() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    query = ContentSearchQuery(
        "安装",
        AppController.default_filters(now),
    )
    active = SimpleNamespace(
        id="search-1",
        status=SearchStatus.RUNNING,
        exhausted=False,
    )
    first_page = [SimpleNamespace(id="result-1")]
    calls = []

    class ContentService:
        async def start_search(self, peer_ref, received_query, *, on_progress=None):
            calls.append(("search", peer_ref, received_query))
            return active, first_page

        async def load_more(self, search_id, *, on_progress=None):
            calls.append(("more", search_id))
            return (
                SimpleNamespace(
                    id="search-1",
                    status=SearchStatus.COMPLETED,
                    exhausted=True,
                ),
                first_page,
            )

        def list_sessions(self):
            return [active]

        def list_results(self, search_id):
            return first_page

        def set_selected(self, search_id, result_id, selected):
            calls.append(("select", search_id, result_id, selected))
            return first_page

        def prepare_download(self, search_id):
            calls.append(("prepare", search_id))
            return SimpleNamespace(
                preview="preview",
                selected_count=4,
                duplicate_count=1,
                unavailable_count=1,
                preview_result_ids=("r1", "r2"),
            )

        def finalize_queue(self, search_id, joined_count):
            calls.append(("finalize", search_id, joined_count))
            return SimpleNamespace(
                selected_count=4,
                joined_count=joined_count,
                duplicate_count=1,
                unavailable_count=1,
            )

    class Planner:
        def commit_selected(self, preview):
            calls.append(("commit", preview))
            return SimpleNamespace(
                task=SimpleNamespace(id="task-1"),
                accepted_keys=frozenset({("p", 1, "m1"), ("p", 2, "m2")}),
            )

    class Scheduler:
        def __init__(self):
            self.started = asyncio.Event()

        async def run_task(self, task_id):
            calls.append(("run", task_id))
            self.started.set()

        async def shutdown(self):
            pass

    window = ContentWindowFake()
    scheduler = Scheduler()
    controller = AppController.for_test(
        gateway=ConnectedGateway(),
        content_browser=ContentService(),
        planner=Planner(),
        scheduler=scheduler,
        window=window,
        confirm_preview=lambda preview: True,
    )

    await controller.search_content("-1001", query)
    assert window.content_page.active_search_id == "search-1"
    assert window.content_page.results == first_page

    await controller.load_more_content("search-1")
    controller.set_content_selected("search-1", "result-1", True)
    await controller.queue_content_selection("search-1")
    await asyncio.wait_for(scheduler.started.wait(), timeout=1)

    assert ("commit", "preview") in calls
    assert ("finalize", "search-1", 2) in calls
    assert window.message == "选择 4 项，加入 2 项，跳过重复 1 项，不可用 1 项"


@pytest.mark.asyncio
async def test_cancel_content_search_restores_page_busy_state() -> None:
    started = asyncio.Event()

    class ContentService:
        async def start_search(self, peer_ref, query, *, on_progress=None):
            started.set()
            await asyncio.Event().wait()

        def list_sessions(self):
            return []

    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=ConnectedGateway(),
        content_browser=ContentService(),
        window=window,
    )
    task = asyncio.create_task(controller.search_content("-1001", object()))
    await started.wait()

    controller.cancel_content_search()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert window.content_page.busy[-1] is False


@pytest.mark.asyncio
async def test_content_search_expired_session_rebuilds_gateway_for_qr_login() -> None:
    calls = []

    class ContentService:
        async def start_search(self, _peer_ref, _query, *, on_progress=None):
            raise SessionExpiredError("Telegram 登录已失效，请重新扫码登录")

        def go_offline(self):
            calls.append("offline")

        def list_sessions(self):
            return []

    class OldGateway:
        async def connect(self):
            pass

        async def disconnect(self):
            calls.append("disconnect-old")

    class FreshGateway:
        async def connect(self):
            calls.append("connect-fresh")

    class Scheduler:
        async def shutdown(self):
            calls.append("shutdown-scheduler")

    content = ContentService()
    fresh = FreshGateway()
    vault = Vault()
    vault.value = {"api_hash": "saved-api-hash", "session": "expired-session"}
    window = ContentWindowFake()

    def gateway_factory(api_id, api_hash, session, _proxy, _proxy_password):
        calls.append(("factory", api_id, api_hash, session))
        return fresh

    def service_builder(gateway, concurrency):
        calls.append(("services", gateway, concurrency))
        return "planner", "scheduler", content

    controller = AppController.for_test(
        gateway=OldGateway(),
        scheduler=Scheduler(),
        content_browser=content,
        window=window,
        vault=vault,
        secrets=vault.value,
        settings=AppSettings(api_id=12345),
        gateway_factory=gateway_factory,
        service_builder=service_builder,
    )
    controller.show_login = lambda: calls.append("show-login")

    await controller.search_content("-1001", object())

    assert controller.secrets == {"api_hash": "saved-api-hash"}
    assert vault.value == {"api_hash": "saved-api-hash"}
    assert controller.gateway is fresh
    assert calls == [
        "offline",
        "shutdown-scheduler",
        "disconnect-old",
        ("factory", 12345, "saved-api-hash", ""),
        "connect-fresh",
        ("services", fresh, 3),
        "show-login",
    ]
    assert window.account is None
    assert window.content_page.logged_in is False
    assert window.content_page.busy[-1] is False
    assert window.content_page.errors[-1] == "Telegram 登录已失效，请重新扫码登录"


@pytest.mark.asyncio
async def test_dialog_sync_expired_session_stops_spinner_and_logs_out() -> None:
    calls = []

    class ContentService:
        async def sync_dialogs(self, *, on_progress=None):
            raise SessionExpiredError("Telegram 登录已失效，请重新扫码登录")

        def go_offline(self):
            calls.append("offline")

        def list_sessions(self):
            return []

    class Gateway:
        async def connect(self):
            pass

        async def disconnect(self):
            calls.append("disconnect")

    vault = Vault()
    vault.value = {"api_hash": "saved-api-hash", "session": "expired-session"}
    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=Gateway(),
        content_browser=ContentService(),
        window=window,
        vault=vault,
        secrets=vault.value,
    )
    controller.show_login = lambda: calls.append("show-login")

    await controller.refresh_content_dialogs()

    assert window.content_page.sync_states[-1] == ("登录已失效", False, 0)
    assert window.content_page.logged_in is False
    assert vault.value == {"api_hash": "saved-api-hash"}
    assert calls == ["offline", "disconnect", "show-login"]


def test_clear_thumbnail_cache_updates_settings_without_touching_history() -> None:
    class Thumbnails:
        def clear(self):
            return 2, 5

        def total_bytes(self):
            return 0

    class Catalog:
        def clear_history(self, _account_id):
            raise AssertionError("thumbnail cleanup must not clear search history")

    class ContentService:
        thumbnails = Thumbnails()
        catalog = Catalog()

    class Dialog:
        def __init__(self):
            self.cache_bytes = None

        def set_thumbnail_cache_bytes(self, value):
            self.cache_bytes = value

    window = ContentWindowFake()
    controller = AppController.for_test(
        content_browser=ContentService(),
        window=window,
    )
    dialog = Dialog()
    controller._settings_dialog = dialog

    controller.clear_thumbnail_cache()

    assert dialog.cache_bytes == 0
    assert window.message == "已清理 2 个缩略图，共 5 B"


@pytest.mark.asyncio
async def test_shutdown_cancels_content_operations_before_services() -> None:
    started = {
        "sync": asyncio.Event(),
        "search": asyncio.Event(),
        "thumbnail": asyncio.Event(),
    }
    order = []

    class ContentService:
        async def sync_dialogs(self, *, on_progress=None):
            started["sync"].set()
            await asyncio.Event().wait()

        async def start_search(self, peer_ref, query, *, on_progress=None):
            started["search"].set()
            await asyncio.Event().wait()

        async def load_thumbnail(self, result_id):
            started["thumbnail"].set()
            await asyncio.Event().wait()

        def list_sessions(self):
            return []

        def go_offline(self):
            order.append("offline")

    class Scheduler:
        async def shutdown(self):
            order.append("scheduler")

    class Gateway:
        async def connect(self):
            pass

        async def disconnect(self):
            order.append("gateway")

    controller = AppController.for_test(
        content_browser=ContentService(),
        scheduler=Scheduler(),
        gateway=Gateway(),
        window=ContentWindowFake(),
    )
    sync_task = asyncio.create_task(controller.refresh_content_dialogs())
    search_task = asyncio.create_task(controller.search_content("-1001", object()))
    controller.request_thumbnail("result-1")
    await asyncio.gather(*(event.wait() for event in started.values()))

    await controller.shutdown()

    assert sync_task.cancelled()
    assert search_task.cancelled()
    assert not controller._thumbnail_tasks
    assert order == ["scheduler", "gateway", "offline"]


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
    controller = AppController.for_test(
        gateway=ConnectedGateway(),
        planner=Planner(),
        window=window,
    )

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
    controller = AppController.for_test(
        gateway=ConnectedGateway(),
        planner=Planner(),
        window=window,
    )

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
