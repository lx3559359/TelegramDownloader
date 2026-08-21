import asyncio
import logging
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import telegram_downloader.controller as controller_module
from telegram_downloader.connectivity import ConnectionRecovery
from telegram_downloader.content import (
    ALL_DIALOGS_SCOPE_REF,
    ALL_DIALOGS_TITLE,
    AccountProfile,
    ContentSearchQuery,
    SearchScope,
    SearchSession,
    SearchStatus,
)
from telegram_downloader.content_progress import DialogSyncProgress, SearchProgress
from telegram_downloader.controller import AppController
from telegram_downloader.diagnostics import (
    DiagnosticProgress,
    DiagnosticReport,
    DiagnosticResult,
    DiagnosticStatus,
)
from telegram_downloader.domain import (
    IntegrityStatus,
    ItemStatus,
    MediaKind,
    ScanFilters,
    SourceKind,
    TaskStatus,
)
from telegram_downloader.file_integrity import (
    IntegrityProgress,
    IntegritySummary,
    RepairPreparation,
)
from telegram_downloader.gateway import (
    AccessDeniedError,
    AuthorizationFailureReason,
    AuthState,
    GatewayError,
    QrLoginInfo,
    SessionExpiredError,
    TransientNetworkError,
)
from telegram_downloader.paths import PortablePaths
from telegram_downloader.scheduler import SchedulerSnapshot
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
async def test_connected_transport_requires_authorized_account() -> None:
    class Gateway:
        def is_connected(self) -> bool:
            return True

        async def test_connection(self) -> None:
            raise SessionExpiredError(
                reason=AuthorizationFailureReason.AUTH_KEY_INVALID
            )

        async def disconnect(self) -> None:
            pass

    vault = Vault()
    vault.value = {"session": "saved", "api_hash": "hash"}
    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=Gateway(),
        vault=vault,
        secrets=vault.load(),
        window=window,
    )
    shown: list[str] = []
    controller.show_login = lambda: shown.append("login")

    assert await controller.ensure_telegram_online() is False
    assert "连接正常" not in window.content_page.connection_states
    assert window.content_page.logged_in is False
    assert "session" not in controller.secrets
    assert shown == ["login"]


@pytest.mark.asyncio
async def test_authorization_check_network_failure_keeps_saved_session() -> None:
    class Gateway:
        def is_connected(self) -> bool:
            return True

        async def test_connection(self) -> None:
            raise TransientNetworkError("offline")

    vault = Vault()
    vault.value = {"session": "saved", "api_hash": "hash"}
    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=Gateway(),
        vault=vault,
        secrets=vault.load(),
        window=window,
    )

    assert await controller.ensure_telegram_online() is False
    assert controller.secrets["session"] == "saved"
    assert vault.load()["session"] == "saved"
    assert window.content_page.connection_retryable[-1] is True


@pytest.mark.asyncio
async def test_concurrent_session_expiry_runs_one_relogin_flow(monkeypatch) -> None:
    class Gateway:
        def __init__(self) -> None:
            self.disconnects = 0

        async def disconnect(self) -> None:
            self.disconnects += 1

    class Logger:
        def __init__(self) -> None:
            self.calls = []

        def warning(self, template, *args) -> None:
            self.calls.append((template, args))

    logger = Logger()
    monkeypatch.setattr(controller_module, "_LOGGER", logger)
    gateway = Gateway()
    vault = Vault()
    vault.value = {"session": "saved", "api_hash": "hash"}
    window = ContentWindowFake()
    subscription_login_states: list[bool] = []
    window.subscriptions_page = SimpleNamespace(
        set_logged_in=subscription_login_states.append
    )
    controller = AppController.for_test(
        gateway=gateway,
        vault=vault,
        secrets=vault.load(),
        window=window,
    )
    shown: list[str] = []
    controller.show_login = lambda: shown.append("login")
    error = SessionExpiredError(
        "private server text",
        reason=AuthorizationFailureReason.AUTH_KEY_DUPLICATED,
    )

    await asyncio.gather(
        controller._handle_session_expired(error),
        controller._handle_session_expired(error),
    )

    assert gateway.disconnects == 1
    assert shown == ["login"]
    assert subscription_login_states == [False]
    assert controller.last_authorization_failure_reason is (
        AuthorizationFailureReason.AUTH_KEY_DUPLICATED
    )
    serialized = repr(logger.calls)
    assert "auth-key-duplicated" in serialized
    assert "private server text" not in serialized
    assert "private server text" not in repr(window.content_page.errors)


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
async def test_successful_login_starts_connection_monitor_after_logged_out_start() -> None:
    sleeping = asyncio.Event()
    blocker = asyncio.Event()

    async def sleep(_value: float) -> None:
        sleeping.set()
        await blocker.wait()

    class Gateway:
        def export_session(self) -> str:
            return "new-session"

        async def account_name(self) -> str:
            return "New User"

        def is_connected(self) -> bool:
            return True

        async def disconnect(self) -> None:
            pass

    controller = AppController.for_test(
        gateway=Gateway(),
        connection_sleeper=sleep,
    )
    controller._session_expiry_handled = True
    controller._last_authorization_failure_reason = (
        AuthorizationFailureReason.SESSION_REVOKED
    )

    assert controller._connection_monitor_task is None
    await controller._finish_login()

    task = controller._connection_monitor_task
    assert task is not None
    assert controller._session_expiry_handled is False
    assert controller.last_authorization_failure_reason is None
    await sleeping.wait()
    assert task.done() is False
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
async def test_scan_waits_for_async_user_confirmation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class Planner:
        committed = False

        async def scan(self, source, filters):
            return "preview"

        def commit(self, preview):
            self.committed = True

    async def confirm_preview(_preview):
        entered.set()
        await release.wait()
        return False

    planner = Planner()
    controller = AppController.for_test(
        gateway=ConnectedGateway(),
        planner=planner,
        confirm_preview=confirm_preview,
    )

    scan = asyncio.create_task(
        controller.scan_link(
            "https://t.me/example/42",
            controller.default_filters(datetime(2026, 8, 13, tzinfo=UTC)),
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)

    assert scan.done() is False
    assert planner.committed is False

    release.set()
    await scan

    assert planner.committed is False


@pytest.mark.asyncio
async def test_confirmed_scan_starts_persisted_task() -> None:
    class Planner:
        async def scan(self, source, filters):
            return "preview"

        def commit(self, preview):
            return SimpleNamespace(
                task=SimpleNamespace(id="task-1", status=TaskStatus.QUEUED),
                accepted_keys=frozenset({("peer", 42, "media")}),
                skipped_count=2,
            )

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

    assert controller.window.message.last_message == (
        "加入 1 项，跳过重复 2 项；任务已开始下载"
    )


@pytest.mark.asyncio
async def test_running_task_refreshes_window_before_download_finishes() -> None:
    release = asyncio.Event()
    started = asyncio.Event()
    task = SimpleNamespace(
        id="task-1",
        source_title="示例频道",
        display_title=None,
        status=TaskStatus.QUEUED,
        last_error=None,
        archived_at=None,
    )
    item = SimpleNamespace(
        status=ItemStatus.QUEUED,
        expected_size=100,
        downloaded_bytes=0,
        last_error=None,
    )

    class Repository:
        def list_task_snapshots(self, *, include_archived=False):
            assert include_archived is True
            return [
                SimpleNamespace(
                    task=task,
                    total_items=1,
                    completed_items=int(item.status is ItemStatus.COMPLETED),
                    downloaded_bytes=item.downloaded_bytes,
                    known_size=item.expected_size,
                    unknown_size_count=0,
                    item_error=item.last_error,
                )
            ]

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
        display_title=None,
        status=TaskStatus.DOWNLOADING,
        last_error=None,
        archived_at=None,
    )
    item = SimpleNamespace(
        status=ItemStatus.DOWNLOADING,
        expected_size=1024,
        downloaded_bytes=0,
        last_error=None,
    )

    class Repository:
        def list_task_snapshots(self, *, include_archived=False):
            assert include_archived is True
            return [
                SimpleNamespace(
                    task=task,
                    total_items=1,
                    completed_items=0,
                    downloaded_bytes=item.downloaded_bytes,
                    known_size=item.expected_size,
                    unknown_size_count=0,
                    item_error=item.last_error,
                )
            ]

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


def test_search_task_uses_display_title_but_opens_source_directory(tmp_path, monkeypatch) -> None:
    task = SimpleNamespace(
        id="task-1",
        source_kind=SourceKind.CHANNEL_OR_GROUP,
        source_title="资料群",
        display_title="资料群（搜索：安装）",
        status=TaskStatus.QUEUED,
        last_error=None,
        archived_at=None,
    )
    item = SimpleNamespace(
        status=ItemStatus.QUEUED,
        expected_size=100,
        downloaded_bytes=0,
        last_error=None,
    )

    class Repository:
        def list_task_snapshots(self, *, include_archived=False):
            assert include_archived is True
            return [
                SimpleNamespace(
                    task=task,
                    total_items=1,
                    completed_items=0,
                    downloaded_bytes=item.downloaded_bytes,
                    known_size=item.expected_size,
                    unknown_size_count=0,
                    item_error=item.last_error,
                )
            ]

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


def test_account_search_task_opens_download_root(tmp_path, monkeypatch) -> None:
    task = SimpleNamespace(
        id="account-search",
        source_kind=SourceKind.ACCOUNT_SEARCH,
        source_title=ALL_DIALOGS_TITLE,
    )

    class Repository:
        def get_task(self, task_id):
            assert task_id == task.id
            return task

    opened = []
    monkeypatch.setattr(
        controller_module.os,
        "startfile",
        lambda directory: opened.append(directory),
        raising=False,
    )
    paths = PortablePaths(tmp_path)
    controller = AppController.for_test(repository=Repository(), paths=paths)

    controller.open_task_directory(task.id)

    assert opened == [paths.downloads.resolve()]
    assert not (paths.downloads / ALL_DIALOGS_TITLE).exists()


def test_task_detail_selection_loads_only_one_selected_task() -> None:
    item = SimpleNamespace(
        id="item-1",
        original_name="video.mp4",
        media_kind=MediaKind.VIDEO,
        status=ItemStatus.DOWNLOADING,
        downloaded_bytes=5,
        expected_size=10,
        retry_count=2,
        last_error="safe-error",
        integrity_status=IntegrityStatus.HASH_MISMATCH,
        verified_at=datetime(2026, 8, 16, tzinfo=UTC),
    )

    class Repository:
        def __init__(self):
            self.calls = []

        def list_items(self, task_id):
            self.calls.append(task_id)
            return [item]

    class Window:
        def __init__(self):
            self.details = []

        def set_task_items(self, task_id, items):
            self.details.append((task_id, items))

    repository = Repository()
    window = Window()
    controller = AppController.for_test(repository=repository, window=window)

    controller.select_task_details([])
    controller.select_task_details(["task-1", "task-2"])
    controller.select_task_details(["task-1"])

    assert repository.calls == ["task-1"]
    task_id, summaries = window.details[-1]
    assert task_id == "task-1"
    assert len(summaries) == 1
    assert summaries[0].id == item.id
    assert summaries[0].error_text == "safe-error"
    assert summaries[0].integrity_status is IntegrityStatus.HASH_MISMATCH
    assert summaries[0].verified_at == item.verified_at


def _integrity_item(
    item_id: str,
    task_id: str = "task",
    *,
    status: ItemStatus = ItemStatus.COMPLETED,
    integrity_status: IntegrityStatus = IntegrityStatus.UNVERIFIED,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=item_id,
        task_id=task_id,
        original_name=f"{item_id}.bin",
        media_kind=MediaKind.DOCUMENT,
        status=status,
        downloaded_bytes=4,
        expected_size=4,
        retry_count=0,
        last_error=None,
        integrity_status=integrity_status,
        verified_at=None,
    )


class _IntegrityWindow:
    def __init__(self) -> None:
        self.busy: list[bool] = []
        self.progress = []
        self.task_refreshes = 0
        self.details = []
        self.message = ""

    def set_integrity_busy(self, value):
        self.busy.append(value)

    def set_integrity_progress(self, value):
        self.progress.append(value)

    def set_task_summaries(self, _summaries):
        self.task_refreshes += 1

    def set_task_items(self, task_id, items):
        self.details.append((task_id, items))

    def statusBar(self):
        return self

    def showMessage(self, message, _timeout=0):
        self.message = message


@pytest.mark.asyncio
async def test_verify_media_forwards_progress_suppresses_duplicate_and_refreshes() -> None:
    item = _integrity_item("media")
    entered = asyncio.Event()
    release = asyncio.Event()

    class Repository:
        def get_item(self, item_id):
            assert item_id == item.id
            return item

        def list_items(self, task_id, statuses=None):
            assert task_id == item.task_id
            return [item]

        def list_task_snapshots(self, *, include_archived=False):
            assert include_archived is True
            return []

    class Integrity:
        def __init__(self):
            self.calls = []

        async def verify(self, item_ids, *, progress, cancelled):
            self.calls.append(item_ids)
            entered.set()
            await release.wait()
            progress(
                IntegrityProgress(
                    1,
                    1,
                    item.id,
                    item.original_name,
                    IntegrityStatus.VERIFIED,
                )
            )
            return IntegritySummary(baselined=1)

    window = _IntegrityWindow()
    integrity = Integrity()
    controller = AppController.for_test(
        repository=Repository(),
        window=window,
        integrity_service=integrity,
    )
    controller.select_task_details([item.task_id])

    active = asyncio.create_task(controller.verify_media([item.id, item.id]))
    await entered.wait()
    await controller.verify_media([item.id])
    release.set()
    await active

    assert integrity.calls == [[item.id]]
    assert window.busy == [True, False]
    assert next(value for value in window.progress if value is not None).item_id == item.id
    assert window.progress[-1] is None
    assert window.task_refreshes == 1
    assert window.details[-1][0] == item.task_id
    assert "建立基线 1" in window.message


@pytest.mark.asyncio
async def test_verify_tasks_expands_media_and_cancel_stops_operation() -> None:
    items = [_integrity_item("one", "a"), _integrity_item("two", "b")]
    entered = asyncio.Event()

    class Repository:
        def list_items(self, task_id, statuses=None):
            return [item for item in items if item.task_id == task_id]

        def list_task_snapshots(self, *, include_archived=False):
            return []

    class Integrity:
        def __init__(self):
            self.ids = []

        async def verify(self, item_ids, *, progress, cancelled):
            self.ids = item_ids
            entered.set()
            while not cancelled.is_set():
                await asyncio.sleep(0)
            return IntegritySummary(cancelled=len(item_ids))

    integrity = Integrity()
    window = _IntegrityWindow()
    controller = AppController.for_test(
        repository=Repository(),
        window=window,
        integrity_service=integrity,
    )

    operation = asyncio.create_task(controller.verify_tasks(["a", "b", "a"]))
    await entered.wait()
    controller.cancel_integrity()
    await operation

    assert integrity.ids == ["one", "two"]
    assert window.busy == [True, False]
    assert "取消" in window.message


@pytest.mark.asyncio
async def test_shutdown_cancels_active_integrity_operation() -> None:
    entered = asyncio.Event()

    class Integrity:
        async def verify(self, item_ids, *, progress, cancelled):
            entered.set()
            await asyncio.Event().wait()

    class Repository:
        def list_task_snapshots(self, *, include_archived=False):
            return []

    controller = AppController.for_test(
        repository=Repository(),
        window=_IntegrityWindow(),
        integrity_service=Integrity(),
    )
    operation = asyncio.create_task(controller.verify_media(["media"]))
    await entered.wait()

    await controller.shutdown()

    try:
        assert operation.cancelled() is True
    finally:
        if not operation.done():
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation


@pytest.mark.asyncio
async def test_repair_selected_media_runs_only_prepared_ids() -> None:
    broken = _integrity_item(
        "broken",
        status=ItemStatus.FAILED,
        integrity_status=IntegrityStatus.MISSING,
    )
    healthy = _integrity_item(
        "healthy",
        status=ItemStatus.COMPLETED,
        integrity_status=IntegrityStatus.VERIFIED,
    )
    items = {item.id: item for item in (broken, healthy)}

    class Repository:
        def get_item(self, item_id):
            return items[item_id]

        def list_items(self, task_id, statuses=None):
            return list(items.values())

        def list_task_snapshots(self, *, include_archived=False):
            return []

    class Integrity:
        def prepare_repairs(self, item_ids):
            assert item_ids == [broken.id, healthy.id]
            broken.status = ItemStatus.QUEUED
            broken.integrity_status = IntegrityStatus.UNVERIFIED
            return RepairPreparation((broken.id,), skipped=1)

    class Scheduler:
        def __init__(self):
            self.selected_runs = []

        async def run_items(self, task_id, item_ids):
            self.selected_runs.append((task_id, item_ids))
            items[item_ids[0]].status = ItemStatus.COMPLETED

    scheduler = Scheduler()
    window = _IntegrityWindow()
    controller = AppController.for_test(
        repository=Repository(),
        scheduler=scheduler,
        window=window,
        integrity_service=Integrity(),
    )
    controller.select_task_details([broken.task_id])

    await controller.repair_media([broken.id, healthy.id])

    assert scheduler.selected_runs == [(broken.task_id, [broken.id])]
    assert "成功 1" in window.message
    assert "跳过 1" in window.message


@pytest.mark.asyncio
async def test_cancel_integrity_pauses_active_repair_download() -> None:
    broken = _integrity_item(
        "broken",
        status=ItemStatus.FAILED,
        integrity_status=IntegrityStatus.MISSING,
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    class Repository:
        def get_item(self, _item_id):
            return broken

        def list_items(self, task_id, statuses=None):
            return [broken]

        def list_task_snapshots(self, *, include_archived=False):
            return []

    class Integrity:
        def prepare_repairs(self, item_ids):
            broken.status = ItemStatus.QUEUED
            broken.integrity_status = IntegrityStatus.UNVERIFIED
            return RepairPreparation((broken.id,), 0)

    class Scheduler:
        def __init__(self):
            self.paused = []

        async def run_items(self, task_id, item_ids):
            entered.set()
            await release.wait()

        def pause_task(self, task_id):
            self.paused.append(task_id)
            broken.status = ItemStatus.PAUSED
            release.set()

    scheduler = Scheduler()
    controller = AppController.for_test(
        repository=Repository(),
        scheduler=scheduler,
        window=_IntegrityWindow(),
        integrity_service=Integrity(),
    )
    operation = asyncio.create_task(controller.repair_media([broken.id]))
    await entered.wait()

    controller.cancel_integrity()
    await asyncio.sleep(0)

    try:
        assert scheduler.paused == [broken.task_id]
    finally:
        release.set()
        await operation


@pytest.mark.asyncio
async def test_task_batch_actions_deduplicate_and_skip_ineligible_tasks() -> None:
    tasks = {
        "run": SimpleNamespace(
            id="run",
            status=TaskStatus.DOWNLOADING,
            archived_at=None,
        ),
        "pause": SimpleNamespace(
            id="pause",
            status=TaskStatus.PAUSED,
            archived_at=None,
        ),
        "fail": SimpleNamespace(
            id="fail",
            status=TaskStatus.PARTIAL_FAILURE,
            archived_at=None,
        ),
    }

    class Repository:
        def get_task(self, task_id):
            return tasks[task_id]

        def list_task_snapshots(self, *, include_archived=False):
            assert include_archived is True
            return []

    class Scheduler:
        def __init__(self):
            self.paused = []
            self.resumed = []

        def pause_task(self, task_id):
            self.paused.append(task_id)

        async def resume_task(self, task_id):
            self.resumed.append(task_id)

    scheduler = Scheduler()
    controller = AppController.for_test(repository=Repository(), scheduler=scheduler)

    controller.pause_tasks(["run", "pause", "run"])
    await controller.resume_tasks(["pause", "run", "pause"])
    assert scheduler.paused == ["run"]
    assert scheduler.resumed == ["pause"]

    await controller.retry_failed_tasks(["fail", "run", "fail"])

    assert scheduler.resumed == ["pause", "fail"]


def test_archive_and_restore_tasks_report_repository_results() -> None:
    class Repository:
        def __init__(self):
            self.archived = []
            self.restored = []

        def archive_tasks(self, task_ids):
            self.archived.append(task_ids)
            return {"done"}

        def restore_tasks(self, task_ids):
            self.restored.append(task_ids)
            return {"done"}

        def list_task_snapshots(self, *, include_archived=False):
            assert include_archived is True
            return []

    repository = Repository()
    controller = AppController.for_test(repository=repository)

    controller.archive_tasks(["done", "done", "active"])
    controller.restore_tasks(["done", "done"])

    assert repository.archived == [["done", "active"]]
    assert repository.restored == [["done"]]
    assert "已恢复 1 个" in controller.window.message.last_message


def test_open_media_file_requires_completed_local_existing_file(
    tmp_path,
    monkeypatch,
) -> None:
    paths = PortablePaths(tmp_path / "app")
    paths.ensure_layout()
    target = paths.downloads / "channel" / "video.mp4"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")

    class Repository:
        def __init__(self):
            self.item = SimpleNamespace(
                id="item",
                status=ItemStatus.COMPLETED,
                target_path=target,
            )

        def get_item(self, item_id):
            assert item_id == self.item.id
            return self.item

    repository = Repository()
    opened = []
    monkeypatch.setattr(
        controller_module.os,
        "startfile",
        lambda path: opened.append(path),
        raising=False,
    )
    controller = AppController.for_test(repository=repository, paths=paths)

    controller.open_media_file("item")
    assert opened == [target.resolve()]

    target.unlink()
    controller.open_media_file("item")
    assert opened == [target.resolve()]
    assert "本地文件不存在" in controller.window.message.last_message

    repository.item = SimpleNamespace(
        id="item",
        status=ItemStatus.COMPLETED,
        target_path=tmp_path / "outside.bin",
    )
    controller.open_media_file("item")
    assert opened == [target.resolve()]
    assert "安全" in controller.window.message.last_message


@pytest.mark.asyncio
async def test_progress_refresh_is_throttled_across_concurrent_callers() -> None:
    controller = AppController.for_test(progress_refresh_interval=0.5)
    refreshes: list[float | None] = []

    async def refresh(*, now=None) -> None:
        refreshes.append(now)

    controller.refresh_tasks_async = refresh

    await controller._refresh_tasks_if_due(20.0)
    await controller._refresh_tasks_if_due(20.1)
    await controller._refresh_tasks_if_due(20.5)

    assert refreshes == [20.0, 20.5]


def test_progress_refresh_interval_must_be_positive() -> None:
    with pytest.raises(ValueError, match="进度刷新间隔必须大于零"):
        AppController.for_test(progress_refresh_interval=0)


class ContentPageFake:
    def __init__(self):
        self.logged_in = None
        self.dialogs = []
        self.sessions = []
        self.results = []
        self.batches = []
        self.active_search_id = None
        self.busy = []
        self.search_progress = []
        self.sync_states = []
        self.connection_states = []
        self.connection_retryable = []
        self.queue_busy = []
        self.thumbnails = {}
        self.previews = []
        self.preview_updates = []
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

    def apply_search_batch(self, value):
        self.batches.append(value)
        self.results = list(value.results)

    def set_search_busy(self, value):
        self.busy.append(value)

    def set_search_progress(self, progress):
        self.search_progress.append(progress)

    def set_sync_state(self, text, *, busy=False, count=0):
        self.sync_states.append((text, busy, count))

    def set_connection_state(self, text, *, retryable=False):
        self.connection_states.append(text)
        self.connection_retryable.append(retryable)

    def set_queue_busy(self, busy):
        self.queue_busy.append(busy)

    def set_thumbnail(self, result_id, path):
        self.thumbnails[result_id] = path

    def show_preview(self, result, path):
        self.previews.append((result, path))

    def update_preview(self, result_id, path):
        self.preview_updates.append((result_id, path))

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
        async def start_search(
            self,
            _peer_ref,
            _query,
            *,
            scope=SearchScope.SINGLE_DIALOG,
            on_progress=None,
            on_results=None,
        ):
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
    assert window.content_page.search_progress[0] == SearchProgress(0, 0, "正在连接 Telegram")
    assert window.content_page.search_progress[-2].inspected == 20
    assert window.content_page.search_progress[-1] is None


@pytest.mark.asyncio
async def test_controller_forwards_global_scope_to_content_service() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 500),
    )
    session = SearchSession(
        "global-1",
        "a1",
        ALL_DIALOGS_SCOPE_REF,
        ALL_DIALOGS_TITLE,
        query,
        SearchStatus.COMPLETED,
        1,
        None,
        True,
        0,
        now,
        now,
        scope=SearchScope.ALL_DIALOGS,
    )
    calls = []

    class Browser:
        async def start_search(
            self,
            peer_ref,
            query,
            *,
            scope,
            on_progress=None,
            on_results=None,
        ):
            calls.append((scope, peer_ref, query.keyword))
            return session, []

        def list_sessions(self):
            return [session]

        def list_results(self, _search_id):
            return []

    controller = AppController.for_test(
        gateway=ConnectedGateway(),
        content_browser=Browser(),
        window=ContentWindowFake(),
    )

    await controller.search_content(
        ALL_DIALOGS_SCOPE_REF,
        query,
        scope=SearchScope.ALL_DIALOGS,
    )

    assert calls == [(SearchScope.ALL_DIALOGS, ALL_DIALOGS_SCOPE_REF, "安装")]


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
        async def start_search(
            self,
            _peer_ref,
            query,
            *,
            scope=SearchScope.SINGLE_DIALOG,
            on_progress=None,
            on_results=None,
        ):
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
    first = asyncio.create_task(controller.search_content("-1001", make_query("first")))
    await first_started.wait()

    await controller.search_content("-1001", make_query("second"))

    with pytest.raises(asyncio.CancelledError):
        await first
    assert calls == ["first", "first-cancelled", "second"]


@pytest.mark.asyncio
async def test_search_displays_current_batch_and_rejects_replaced_task() -> None:
    window = ContentWindowFake()
    page = window.content_page
    page.batches = []
    page.apply_search_batch = page.batches.append

    class Service:
        def __init__(self) -> None:
            self.calls = 0

        async def start_search(self, _peer, _query, **kwargs):
            self.calls += 1
            search_id = "old" if self.calls == 1 else "new"
            kwargs["on_results"](
                SimpleNamespace(
                    search_id=search_id,
                    generation=self.calls,
                    results=(),
                    stable=False,
                )
            )
            if self.calls == 1:
                await asyncio.Event().wait()
            return SimpleNamespace(id=search_id), []

        def list_sessions(self):
            return [SimpleNamespace(id="new")]

    class Gateway:
        def is_connected(self) -> bool:
            return True

    controller = AppController.for_test(
        gateway=Gateway(),
        content_browser=Service(),
        window=window,
    )
    first = asyncio.create_task(controller.search_content("peer", object()))
    await asyncio.sleep(0)
    second = asyncio.create_task(controller.search_content("peer", object()))
    await asyncio.sleep(0)
    await asyncio.gather(first, second, return_exceptions=True)
    assert page.batches[-1].search_id == "new"


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

    assert window.content_page.previews == [(result, None)]
    assert window.content_page.thumbnails == {result.id: path}
    assert window.content_page.preview_updates == [(result.id, path)]


@pytest.mark.asyncio
async def test_content_preview_opens_before_network_thumbnail_finishes(
    tmp_path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    result = SimpleNamespace(id="result-1")
    path = tmp_path / "preview.jpg"

    class Browser:
        def get_result(self, result_id):
            assert result_id == result.id
            return result

        async def load_thumbnail(self, result_id):
            assert result_id == result.id
            entered.set()
            await release.wait()
            return path

    window = ContentWindowFake()
    controller = AppController.for_test(
        content_browser=Browser(),
        window=window,
    )

    opening = asyncio.create_task(controller.open_content_preview(result.id))
    await asyncio.wait_for(entered.wait(), timeout=1)

    assert opening.done() is False
    assert window.content_page.previews == [(result, None)]

    release.set()
    await opening

    assert window.content_page.preview_updates == [(result.id, path)]


def test_thumbnail_task_cleanup_is_safe_without_running_event_loop() -> None:
    controller = AppController.for_test()
    task = SimpleNamespace()
    controller._thumbnail_tasks["result-1"] = task

    controller._forget_thumbnail_task("result-1", task)

    assert controller._thumbnail_tasks == {}


@pytest.mark.asyncio
async def test_offline_search_reconnects_then_continues() -> None:
    calls = []
    active = SimpleNamespace(id="search-1")

    class Gateway:
        async def connect(self):
            calls.append("connect")

    class ContentService:
        async def start_search(
            self,
            peer_ref,
            query,
            *,
            scope=SearchScope.SINGLE_DIALOG,
            on_progress=None,
            on_results=None,
        ):
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

        async def start_search(
            self,
            _peer_ref,
            _query,
            *,
            scope=SearchScope.SINGLE_DIALOG,
            on_progress=None,
            on_results=None,
        ):
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
    assert window.content_page.connection_states[-1] == ("重连失败，请检查网络或代理后重试")
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

    controller.route_content_link("https://t.me/Zhangzhoulao66/56156?single")

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
    first_page = [SimpleNamespace(id="result-1", search_id="search-1")]
    calls = []

    class ContentService:
        async def start_search(
            self,
            peer_ref,
            received_query,
            *,
            scope=SearchScope.SINGLE_DIALOG,
            on_progress=None,
            on_results=None,
        ):
            calls.append(("search", peer_ref, received_query))
            return active, first_page

        async def load_more(
            self,
            search_id,
            *,
            on_progress=None,
            on_results=None,
        ):
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
    assert window.content_page.queue_busy == [True, False]


@pytest.mark.asyncio
async def test_cancelled_queue_confirmation_restores_action_state() -> None:
    class ContentService:
        def prepare_download(self, search_id):
            return SimpleNamespace(preview="preview")

    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=ConnectedGateway(),
        content_browser=ContentService(),
        planner=object(),
        window=window,
        confirm_preview=lambda _preview: False,
    )

    await controller.queue_content_selection("search-1")

    assert window.content_page.queue_busy == [True, False]
    assert window.message == "已取消创建任务"


@pytest.mark.asyncio
async def test_queue_selection_waits_for_async_user_confirmation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class ContentService:
        def prepare_download(self, search_id):
            return SimpleNamespace(preview="preview")

    async def confirm_preview(_preview):
        entered.set()
        await release.wait()
        return False

    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=ConnectedGateway(),
        content_browser=ContentService(),
        planner=object(),
        window=window,
        confirm_preview=confirm_preview,
    )

    queue = asyncio.create_task(controller.queue_content_selection("search-1"))
    await asyncio.wait_for(entered.wait(), timeout=1)

    assert queue.done() is False
    assert window.content_page.queue_busy == [True]

    release.set()
    await queue

    assert window.content_page.queue_busy == [True, False]
    assert window.message == "已取消创建任务"


@pytest.mark.asyncio
async def test_cancel_content_search_restores_page_busy_state() -> None:
    started = asyncio.Event()

    class ContentService:
        async def start_search(
            self,
            peer_ref,
            query,
            *,
            scope=SearchScope.SINGLE_DIALOG,
            on_progress=None,
            on_results=None,
        ):
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
        async def start_search(
            self,
            _peer_ref,
            _query,
            *,
            scope=SearchScope.SINGLE_DIALOG,
            on_progress=None,
            on_results=None,
        ):
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

    def service_builder(gateway, resource_settings):
        assert isinstance(resource_settings, AppSettings)
        calls.append(("services", gateway, resource_settings))
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
        ("services", fresh, AppSettings(api_id=12345)),
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

        async def start_search(
            self,
            peer_ref,
            query,
            *,
            scope=SearchScope.SINGLE_DIALOG,
            on_progress=None,
            on_results=None,
        ):
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
async def test_async_task_refresh_does_not_block_event_loop() -> None:
    import time

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    heartbeat = asyncio.Event()

    class Repository:
        def list_task_snapshots(self, *, include_archived=False):
            assert include_archived is True
            loop.call_soon_threadsafe(entered.set)
            time.sleep(0.05)
            return []

    controller = AppController.for_test(repository=Repository())
    refresh = asyncio.create_task(controller.refresh_tasks_async())
    await entered.wait()
    loop.call_soon(heartbeat.set)

    await asyncio.wait_for(heartbeat.wait(), timeout=0.02)
    await refresh


@pytest.mark.asyncio
async def test_concurrent_task_refreshes_are_coalesced() -> None:
    from threading import Event

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = Event()
    calls = 0

    class Repository:
        def list_task_snapshots(self, *, include_archived=False):
            nonlocal calls
            assert include_archived is True
            calls += 1
            if calls == 1:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(timeout=1)
            return []

    controller = AppController.for_test(repository=Repository())
    first = asyncio.create_task(controller.refresh_tasks_async())
    await entered.wait()
    followers = [
        asyncio.create_task(controller.refresh_tasks_async()) for _ in range(2)
    ]
    await asyncio.sleep(0)
    release.set()

    await asyncio.gather(first, *followers)

    assert calls == 2


def test_refresh_tasks_exposes_queue_positions_and_scheduler_summary() -> None:
    queued_task = SimpleNamespace(
        id="queued",
        source_title="Queued",
        display_title=None,
        status=TaskStatus.QUEUED,
        last_error=None,
        archived_at=None,
    )
    active_task = SimpleNamespace(
        id="active",
        source_title="Active",
        display_title=None,
        status=TaskStatus.DOWNLOADING,
        last_error=None,
        archived_at=None,
    )

    class Repository:
        def list_task_snapshots(self, *, include_archived=False):
            assert include_archived is True
            return [
                SimpleNamespace(
                    task=task,
                    total_items=1,
                    completed_items=0,
                    downloaded_bytes=0,
                    known_size=1,
                    unknown_size_count=0,
                    item_error=None,
                )
                for task in (queued_task, active_task)
            ]

    class Scheduler:
        def snapshot(self):
            return SchedulerSnapshot("active", ("queued",), 4, 2048)

        def queue_positions(self):
            return {"queued": 1}

    class Window:
        def __init__(self):
            self.tasks = []
            self.scheduler_summary = None

        def set_task_summaries(self, summaries):
            self.tasks = summaries

        def set_scheduler_summary(self, **summary):
            self.scheduler_summary = summary

    window = Window()
    controller = AppController.for_test(
        repository=Repository(),
        scheduler=Scheduler(),
        window=window,
    )

    controller.refresh_tasks(now=1.0)

    assert window.tasks[0].queue_position == 1
    assert window.tasks[1].queue_position is None
    assert window.scheduler_summary == {
        "active": 1,
        "queued": 1,
        "concurrency": 4,
        "speed_limit_kib": 2048,
    }


def test_prioritize_task_persists_before_reordering_and_reports_position() -> None:
    events: list[str] = []
    task = SimpleNamespace(
        id="queued",
        status=TaskStatus.QUEUED,
        archived_at=None,
    )

    class Repository:
        def get_task(self, task_id):
            assert task_id == task.id
            return task

        def prioritize_task(self, task_id):
            events.append(f"repository:{task_id}")
            return True

        def list_task_snapshots(self, *, include_archived=False):
            return []

    class Scheduler:
        def prioritize_task(self, task_id):
            events.append(f"scheduler:{task_id}")
            return True

        def queue_positions(self):
            return {task.id: 1}

        def snapshot(self):
            return SchedulerSnapshot(None, (task.id,), 3, 0)

    controller = AppController.for_test(
        repository=Repository(),
        scheduler=Scheduler(),
    )

    controller.prioritize_task(task.id)

    assert events == ["repository:queued", "scheduler:queued"]
    assert "第 1 位" in controller.window.message.last_message


def test_prioritize_task_handles_state_race_without_duplicate_start() -> None:
    task = SimpleNamespace(
        id="queued",
        status=TaskStatus.QUEUED,
        archived_at=None,
    )

    class Repository:
        def get_task(self, _task_id):
            return task

        def prioritize_task(self, _task_id):
            return True

        def list_task_snapshots(self, *, include_archived=False):
            return []

    class Scheduler:
        def prioritize_task(self, _task_id):
            return False

        def queue_positions(self):
            return {}

        def snapshot(self):
            return SchedulerSnapshot(task.id, (), 3, 0)

    controller = AppController.for_test(
        repository=Repository(),
        scheduler=Scheduler(),
    )

    controller.prioritize_task(task.id)

    assert "已经开始下载" in controller.window.message.last_message


def test_apply_settings_reconfigures_active_scheduler_after_persistence() -> None:
    events: list[object] = []

    class Store:
        def load(self):
            return AppSettings(api_id=1)

        def save(self, value):
            events.append(("settings", value))

    class SecretStore:
        def load(self):
            return {}

        def save(self, value):
            events.append(("secrets", value))

    class Scheduler:
        def configure_resources(self, concurrency, speed_limit_kib):
            events.append(("scheduler", concurrency, speed_limit_kib))

    updated = AppSettings(api_id=1, concurrency=5, speed_limit_kib=2048)
    controller = AppController.for_test(
        settings_store=Store(),
        vault=SecretStore(),
        scheduler=Scheduler(),
    )

    controller.apply_settings(updated, "")

    assert events == [
        ("settings", updated),
        ("secrets", {}),
        ("scheduler", 5, 2048),
    ]
    assert controller.settings == updated
    assert "即时应用" in controller.window.message.last_message


@pytest.mark.asyncio
async def test_restore_uses_persistent_dispatch_order() -> None:
    ordered = [SimpleNamespace(id="priority"), SimpleNamespace(id="oldest")]

    class Repository:
        def list_queued_for_dispatch(self):
            return ordered

    controller = AppController.for_test(
        gateway=ConnectedGateway(),
        repository=Repository(),
    )
    started: list[str] = []

    async def online() -> bool:
        return True

    async def account_name() -> str:
        return "Synthetic account"

    async def activate() -> None:
        return None

    controller.ensure_telegram_online = online
    controller._account_name = account_name
    controller.activate_content_account = activate
    controller._start_task = started.append

    await controller._restore_saved_session()

    assert started == ["priority", "oldest"]


@pytest.mark.asyncio
async def test_waiting_run_does_not_poll_database_until_completion() -> None:
    release = asyncio.Event()

    class Scheduler:
        async def run_task(self, _task_id):
            await release.wait()

        def is_active(self, _task_id):
            return False

    controller = AppController.for_test(
        scheduler=Scheduler(),
        progress_refresh_interval=0.01,
    )
    refreshes = 0

    async def refresh() -> None:
        nonlocal refreshes
        refreshes += 1

    controller.refresh_tasks_async = refresh
    operation = asyncio.create_task(controller._run_and_refresh("waiting"))
    await asyncio.sleep(0.03)
    assert refreshes == 0

    release.set()
    await operation
    assert refreshes == 1


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


def diagnostic_report() -> DiagnosticReport:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    return DiagnosticReport.build(
        "0.10.0",
        now,
        now,
        (
            DiagnosticResult(
                "environment",
                "运行环境与路径",
                DiagnosticStatus.PASSED,
                "runtime-paths-ok",
                "检查完成",
                1,
            ),
        ),
    )


class DiagnosticPage:
    def __init__(self) -> None:
        self.report = None
        self.progress = None
        self.running: list[bool] = []
        self.errors: list[str] = []
        self.historical = None

    def set_report(self, report, *, historical: bool) -> None:
        self.report = report
        self.historical = historical

    def set_progress(self, progress) -> None:
        self.progress = progress

    def set_running(self, running: bool) -> None:
        self.running.append(running)

    def show_error(self, message: str) -> None:
        self.errors.append(message)


@pytest.mark.asyncio
async def test_controller_loads_history_runs_persists_exports_and_opens_directory(
    tmp_path,
    monkeypatch,
) -> None:
    completed = diagnostic_report()
    progress = DiagnosticProgress(
        0,
        1,
        "environment",
        "运行环境与路径",
        DiagnosticStatus.RUNNING,
    )

    class Diagnostics:
        def __init__(self) -> None:
            self.runs = 0
            self.cancelled = 0

        async def run(self, callback):
            self.runs += 1
            callback(progress)
            return completed

        async def cancel(self) -> None:
            self.cancelled += 1

    class Store:
        def __init__(self) -> None:
            self.saved = []
            self.exported = []

        def load_latest(self):
            return completed

        def save(self, value):
            self.saved.append(value)

        def export(self, value):
            self.exported.append(value)
            return tmp_path / "data" / "diagnostics" / "diagnostics.zip"

    class StatusBar:
        def __init__(self) -> None:
            self.message = ""

        def showMessage(self, message, _timeout=0):
            self.message = message

    page = DiagnosticPage()
    status = StatusBar()
    window = SimpleNamespace(diagnostics_page=page, statusBar=lambda: status)
    diagnostics = Diagnostics()
    store = Store()
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    opened = []
    monkeypatch.setattr(controller_module.os, "startfile", opened.append)
    controller = AppController.for_test(
        window=window,
        paths=paths,
        diagnostics=diagnostics,
        diagnostic_store=store,
    )

    controller.activate_diagnostics()
    assert diagnostics.runs == 0
    assert page.report is completed
    assert page.historical is True

    await controller.run_diagnostics()
    controller.export_diagnostics()
    controller.open_diagnostics_directory()

    assert diagnostics.runs == 1
    assert page.progress is progress
    assert page.running == [True, False]
    assert page.historical is False
    assert store.saved == [completed]
    assert store.exported == [completed]
    assert status.message == "诊断目录已打开"
    assert opened == [paths.diagnostics]

    await controller.shutdown()
    assert diagnostics.cancelled == 1
