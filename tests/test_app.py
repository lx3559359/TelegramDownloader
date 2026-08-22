import asyncio
from datetime import UTC, date, datetime
from inspect import getsource, isawaitable
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox, QWidget

from telegram_downloader import app
from telegram_downloader.catalog import CatalogRepository
from telegram_downloader.connectivity import ConnectionRecovery
from telegram_downloader.content import (
    ALL_DIALOGS_SCOPE_REF,
    AccountProfile,
    ContentDialog,
    DialogKind,
    SearchScope,
)
from telegram_downloader.content_browser import ContentBrowserService
from telegram_downloader.domain import MediaKind
from telegram_downloader.download_schedule import DownloadScheduleController
from telegram_downloader.file_integrity import FileIntegrityService
from telegram_downloader.gateway import (
    AuthorizationFailureReason,
    SessionExpiredError,
)
from telegram_downloader.paths import PortablePaths
from telegram_downloader.settings import AppSettings
from telegram_downloader.subscription_scheduler import SubscriptionScheduler
from telegram_downloader.subscription_service import SubscriptionService
from telegram_downloader.subscriptions import SubscriptionRule, SubscriptionState
from telegram_downloader.ui.async_actions import ActionPolicy

EXPECTED_POLICIES = {
    "content.activate": ActionPolicy.REPLACE_LATEST,
    "content.search": ActionPolicy.REPLACE_LATEST,
    "content.load_more": ActionPolicy.REPLACE_LATEST,
    "dialogs.refresh": ActionPolicy.DEDUPLICATE,
    "telegram.retry": ActionPolicy.DEDUPLICATE,
    "diagnostics.run": ActionPolicy.DEDUPLICATE,
    "diagnostics.export": ActionPolicy.DEDUPLICATE,
    "login.qr.refresh": ActionPolicy.DEDUPLICATE,
    "login.phone": ActionPolicy.DEDUPLICATE,
    "settings.save": ActionPolicy.DEDUPLICATE,
    "settings.thumbnail_cache.clear": ActionPolicy.DEDUPLICATE,
}


def test_responsive_action_policy_map_is_complete() -> None:
    from telegram_downloader.ui import async_actions

    assert {
        key: async_actions.ACTION_POLICIES[key] for key in EXPECTED_POLICIES
    } == EXPECTED_POLICIES


def test_standard_button_selection_accepts_pyside_integer_result() -> None:
    yes = QMessageBox.StandardButton.Yes

    assert app._standard_button_selected(yes.value, yes) is True


def test_download_confirmation_is_nonblocking_and_awaitable(tmp_path) -> None:
    application, loop, controller = app.create_application(tmp_path)

    def reject_confirmation() -> None:
        for widget in application.topLevelWidgets():
            if isinstance(widget, QMessageBox):
                widget.done(QMessageBox.StandardButton.No.value)
                return
        QTimer.singleShot(1, reject_confirmation)

    try:
        QTimer.singleShot(0, reject_confirmation)
        confirmation = controller.confirm_preview(
            SimpleNamespace(items=(), known_bytes=0, unknown_size_count=0)
        )

        assert isawaitable(confirmation)
        assert loop.run_until_complete(confirmation) is False
    finally:
        loop.run_until_complete(controller._async_actions.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()


def test_graceful_shutdown_cleans_async_work_before_quitting() -> None:
    events: list[str] = []

    class AsyncActions:
        async def shutdown(self) -> None:
            events.append("actions")

    class Controller:
        _async_actions = AsyncActions()

        async def shutdown(self) -> None:
            events.append("controller")

    async def exercise() -> None:
        shutdown = app._GracefulShutdown(
            Controller(),
            lambda: events.append("quit"),
        )

        first = shutdown.request()
        second = shutdown.request()
        await shutdown.wait()

        assert first is second
        assert shutdown.completed is True

    asyncio.run(exercise())

    assert events == ["actions", "controller", "quit"]


def test_graceful_shutdown_stops_schedule_before_controller() -> None:
    events: list[str] = []

    class AsyncActions:
        async def shutdown(self) -> None:
            events.append("actions")

    class Schedule:
        async def shutdown(self) -> None:
            events.append("schedule")

    class Controller:
        _async_actions = AsyncActions()

        async def shutdown(self) -> None:
            events.append("controller")

    async def exercise() -> None:
        shutdown = app._GracefulShutdown(
            Controller(),
            lambda: events.append("quit"),
            before_controller_shutdown=Schedule().shutdown,
        )
        shutdown.request()
        await shutdown.wait()

    asyncio.run(exercise())

    assert events == ["actions", "schedule", "controller", "quit"]


def test_window_close_filter_delegates_to_background_without_shutdown(qapp) -> None:
    class Controller:
        window = QWidget()

        async def shutdown(self) -> None:
            raise AssertionError("close-to-tray must not shut down")

    class Background:
        closes = 0

        def handle_window_close(self) -> bool:
            self.closes += 1
            Controller.window.hide()
            return True

        def request_exit(self) -> None:
            raise AssertionError("close-to-tray must not request exit")

    background = Background()
    shutdown, close_filter = app._install_graceful_shutdown(
        qapp,
        Controller(),
        background,
    )
    Controller.window.show()

    Controller.window.close()
    qapp.processEvents()

    assert background.closes == 1
    assert Controller.window.isVisible() is False
    assert shutdown.task is None
    Controller.window.removeEventFilter(close_filter)
    qapp.setQuitOnLastWindowClosed(True)


def test_session_shutdown_requests_true_background_exit() -> None:
    callbacks = []

    class Signal:
        def connect(self, callback) -> None:
            callbacks.append(callback)

    class Application:
        commitDataRequest = Signal()

    class Background:
        exit_requests = 0

        def request_exit(self) -> None:
            self.exit_requests += 1

    background = Background()
    retained = app._install_session_shutdown(Application(), background)

    callbacks[0](object())

    assert retained is callbacks[0]
    assert background.exit_requests == 1


def test_background_launch_falls_back_to_visible_window_without_tray() -> None:
    class Window:
        visible = False

        def show(self) -> None:
            self.visible = True

    class Controller:
        window = Window()
        messages = []

        def _show_status(self, text: str) -> None:
            self.messages.append(text)

    controller = Controller()

    app._show_initial_window(controller, background=True, tray_available=False)

    assert controller.window.visible is True
    assert controller.messages == ["系统托盘不可用，已显示主窗口"]


def test_run_keeps_startup_inside_the_continuous_event_loop() -> None:
    source = getsource(app.run)

    assert "run_until_complete(controller.start())" not in source
    assert "loop.create_task(start_application())" in source
    assert source.index("_show_initial_window(") < source.index(
        "await controller.start("
    )
    assert source.index("await download_schedule.start()") < source.index(
        "await controller.start("
    )


@pytest.mark.asyncio
async def test_download_schedule_starts_without_configured_telegram_gateway() -> None:
    controller = app.AppController.for_test(gateway=None)
    schedule = DownloadScheduleController(
        lambda: controller.scheduler,
        AppSettings().download_schedule,
    )

    await schedule.start()
    await schedule.shutdown()


def test_duplicate_instance_falls_back_before_application_construction(
    tmp_path,
    monkeypatch,
) -> None:
    events: list[str] = []

    class Guard:
        def acquire(self) -> bool:
            return False

        def notify_already_running(self) -> None:
            self.notified = True

        def release(self) -> None:
            raise AssertionError("unowned guard must not be released")

    class StartupIndicator:
        def close(self) -> None:
            events.append("close")

    guard = Guard()
    monkeypatch.setattr(app, "request_activation", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        app,
        "create_application",
        lambda _root: (_ for _ in ()).throw(AssertionError()),
    )

    assert (
        app.run(
            tmp_path,
            instance_guard=guard,
            startup_indicator=StartupIndicator(),
        )
        == 2
    )
    assert guard.notified is True
    assert events == ["close"]


def test_duplicate_instance_requests_activation_without_fallback(tmp_path, monkeypatch) -> None:
    class Guard:
        notified = False

        def acquire(self) -> bool:
            return False

        def notify_already_running(self) -> None:
            self.notified = True

        def release(self) -> None:
            raise AssertionError("unowned guard must not be released")

    guard = Guard()
    monkeypatch.setattr(app, "request_activation", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        app,
        "create_application",
        lambda _root: (_ for _ in ()).throw(AssertionError()),
    )

    assert app.run(tmp_path, instance_guard=guard) == 2
    assert guard.notified is False


def test_create_application_initializes_project_local_content_services(
    tmp_path,
) -> None:
    application, loop, controller = app.create_application(tmp_path)

    try:
        assert isinstance(controller.content_browser, ContentBrowserService)
        assert (
            controller.content_browser.catalog.database
            == (tmp_path / "data" / "database" / "catalog.sqlite3").resolve()
        )
        assert (
            controller.content_browser.thumbnails.root
            == (tmp_path / "data" / "cache" / "thumbnails").resolve()
        )
        assert controller.window.content_page is not None
        assert isinstance(controller.subscriptions, SubscriptionService)
        assert (
            controller.subscriptions.catalog.database
            == (tmp_path / "data" / "database" / "catalog.sqlite3").resolve()
        )
        assert isinstance(controller.subscription_scheduler, SubscriptionScheduler)
        assert controller.window.subscriptions_page is not None
        assert isinstance(controller.connection_recovery, ConnectionRecovery)
        assert isinstance(controller.integrity_service, FileIntegrityService)
        assert controller.integrity_service.paths.root == tmp_path.resolve()
        slot_names = {getattr(slot, "__name__", "") for slot in controller._ui_slots}
        assert "content_preview_requested" in slot_names
        assert "subscription_probe_requested" in slot_names
        assert controller._async_actions.active_keys == frozenset()
        assert len(controller._async_actions._slots) == 29
        assert controller.diagnostics is not None
        assert controller.diagnostic_store.paths.root == tmp_path.resolve()
        controller.window.content_page.link_requested.emit("https://t.me/example/1#fragment")
        assert controller.window.content_page.error_label.text() == ("请输入有效的 t.me 链接")

        probe_calls: list[str] = []

        async def record_probe(rule_id: str) -> None:
            probe_calls.append(rule_id)

        controller.probe_subscription = record_probe

        async def emit_probe() -> None:
            controller.window.subscriptions_page.probe_requested.emit("rule-1")
            await controller._async_actions.wait_idle()

        loop.run_until_complete(emit_probe())
        assert probe_calls == ["rule-1"]
        assert controller._async_actions.active_keys == frozenset()

        auth_events: list[AuthorizationFailureReason] = []

        async def record_expiry(error: SessionExpiredError) -> None:
            auth_events.append(error.reason)

        controller._handle_session_expired = record_expiry
        loop.run_until_complete(
            controller.subscription_scheduler.on_session_expired(
                SessionExpiredError(
                    reason=AuthorizationFailureReason.SESSION_REVOKED
                )
            )
        )
        assert auth_events == [AuthorizationFailureReason.SESSION_REVOKED]

        report = app.run_self_test(tmp_path)
        for value in report["writable_paths"].values():
            assert str(value).startswith(str(tmp_path.resolve()))
    finally:
        loop.run_until_complete(controller._async_actions.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()


def test_account_search_signal_reaches_controller_with_typed_scope(tmp_path) -> None:
    application, loop, controller = app.create_application(tmp_path)
    calls = []
    completed = asyncio.Event()

    async def record(peer_ref, query, *, scope):
        calls.append((scope, peer_ref, query.keyword))
        completed.set()

    controller.search_content = record

    async def emit_and_wait() -> None:
        controller.window.content_page.search_requested.emit(
            SearchScope.ALL_DIALOGS.value,
            ALL_DIALOGS_SCOPE_REF,
            "安装",
            date(2026, 8, 1),
            date(2026, 8, 17),
            frozenset({MediaKind.VIDEO}),
            500,
        )
        await asyncio.wait_for(completed.wait(), timeout=1)

    try:
        loop.run_until_complete(emit_and_wait())
        assert calls == [
            (SearchScope.ALL_DIALOGS, ALL_DIALOGS_SCOPE_REF, "安装")
        ]
    finally:
        loop.run_until_complete(controller._async_actions.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()


@pytest.mark.asyncio
async def test_telegram_health_uses_retained_authorization_reason() -> None:
    controller = SimpleNamespace(
        gateway=None,
        last_authorization_failure_reason=(
            AuthorizationFailureReason.AUTH_KEY_DUPLICATED
        ),
    )

    result = await app._telegram_health(controller)

    assert result.code == "telegram-session-expired"
    assert result.metrics == {
        "authorizationReason": "auth-key-duplicated"
    }


def test_service_builder_shares_runtime_download_resource_settings(tmp_path) -> None:
    application, loop, controller = app.create_application(tmp_path)
    settings = AppSettings(concurrency=4, speed_limit_kib=2048)

    try:
        planner, scheduler, content = controller.service_builder(object(), settings)

        assert planner is not None
        assert content is controller.content_browser
        assert scheduler.snapshot().concurrency == 4
        assert scheduler.snapshot().speed_limit_kib == 2048
        assert scheduler.downloader.bandwidth is scheduler._bandwidth
    finally:
        loop.run_until_complete(controller._async_actions.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()


def test_create_application_recovers_interrupted_subscription(tmp_path) -> None:
    now = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    catalog = CatalogRepository(paths.catalog_database)
    catalog.initialize()
    catalog.upsert_account(AccountProfile("a1", "账号"), now)
    catalog.replace_dialogs(
        "a1",
        [
            ContentDialog(
                "a1",
                "-1001",
                "资料群",
                "",
                DialogKind.GROUP,
                False,
                True,
                now,
            )
        ],
        now,
    )
    catalog.save_subscription(
        SubscriptionRule(
            "rule-1",
            "a1",
            "-1001",
            "资料群",
            "美女",
            frozenset({MediaKind.PHOTO}),
            30,
            True,
            SubscriptionState.RUNNING,
            42,
            None,
            now,
            None,
            0,
            now,
            now,
        )
    )

    application, loop, controller = app.create_application(tmp_path)
    try:
        recovered = catalog.get_subscription("a1", "rule-1")
        assert recovered.state is SubscriptionState.WAITING
        assert recovered.next_run_at is not None
        assert recovered.last_error == "上次自动检查未正常结束"
    finally:
        loop.run_until_complete(controller._async_actions.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()


def test_content_selection_signal_includes_active_search_id(tmp_path) -> None:
    application, loop, controller = app.create_application(tmp_path)
    calls: list[tuple[str, str, bool]] = []

    class ContentBrowser:
        def set_selected(
            self,
            search_id: str,
            result_id: str,
            selected: bool,
        ) -> list[object]:
            calls.append((search_id, result_id, selected))
            return []

        def list_results(self, _search_id: str) -> list[object]:
            return []

    try:
        controller.content_browser = ContentBrowser()
        page = controller.window.content_page
        page.active_search_id = "search-1"

        page._selection_changed("result-1", True)
        application.processEvents()

        assert calls == [("search-1", "result-1", True)]
    finally:
        loop.run_until_complete(controller._async_actions.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()


def test_zero_argument_ui_signals_schedule_each_controller_action_once(
    tmp_path,
    monkeypatch,
) -> None:
    application, loop, controller = app.create_application(tmp_path)
    calls: list[str] = []

    async def record(name: str) -> None:
        calls.append(name)

    actions = {
        "activate_content_page": "content.activate",
        "activate_subscriptions_page": "subscriptions.activate",
        "refresh_content_dialogs": "dialogs.refresh",
        "retry_telegram_connection": "telegram.retry",
        "refresh_qr_login": "login.qr.refresh",
        "use_phone_fallback": "login.phone",
        "edit_credentials": "login.credentials",
        "cancel_login": "login.cancel",
    }
    for method_name, action_name in actions.items():
        monkeypatch.setattr(
            controller,
            method_name,
            lambda name=action_name: record(name),
        )

    async def emit_actions() -> None:
        controller.window.content_activated.emit()
        controller.window.subscriptions_activated.emit()
        controller.window.content_page.refresh_requested.emit()
        controller.window.content_page.connection_retry_requested.emit()
        controller.login_dialog.qr_refresh_requested.emit()
        controller.login_dialog.phone_fallback_requested.emit()
        controller.login_dialog.credentials_edit_requested.emit()
        controller.login_dialog.login_cancelled.emit()
        await controller._async_actions.wait_idle()

    try:
        bridge = getattr(controller, "_async_actions", None)
        assert bridge is not None
        loop.run_until_complete(emit_actions())

        assert calls == list(actions.values())
        assert bridge.active_keys == frozenset()
    finally:
        bridge = getattr(controller, "_async_actions", None)
        if bridge is not None:
            loop.run_until_complete(bridge.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()


def test_task_management_signals_route_sync_and_async_actions(
    tmp_path,
    monkeypatch,
) -> None:
    application, loop, controller = app.create_application(tmp_path)
    calls: list[tuple[str, object]] = []

    def record_sync(name):
        return lambda value: calls.append((name, value))

    async def record_async(name, value):
        calls.append((name, value))

    monkeypatch.setattr(controller, "select_task_details", record_sync("select"))
    monkeypatch.setattr(
        controller,
        "pause_tasks",
        lambda value: record_async("pause", value),
    )
    monkeypatch.setattr(
        controller,
        "prioritize_task",
        lambda value: record_async("priority", value),
    )
    monkeypatch.setattr(
        controller,
        "archive_tasks",
        lambda value: record_async("archive", value),
    )
    monkeypatch.setattr(
        controller,
        "restore_tasks",
        lambda value: record_async("restore", value),
    )
    monkeypatch.setattr(controller, "open_media_file", record_sync("open"))
    monkeypatch.setattr(controller, "cancel_integrity", lambda: calls.append(("cancel", None)))
    monkeypatch.setattr(
        controller,
        "resume_tasks",
        lambda value: record_async("resume", value),
    )
    monkeypatch.setattr(
        controller,
        "retry_failed_tasks",
        lambda value: record_async("retry", value),
    )
    monkeypatch.setattr(
        controller,
        "verify_media",
        lambda value: record_async("verify_media", value),
    )
    monkeypatch.setattr(
        controller,
        "verify_tasks",
        lambda value: record_async("verify_tasks", value),
    )
    monkeypatch.setattr(
        controller,
        "repair_media",
        lambda value: record_async("repair_media", value),
    )

    async def emit_actions() -> None:
        controller.window.task_selection_changed.emit(["one"])
        controller.window.pause_tasks_requested.emit(["one", "two"])
        controller.window.prioritize_task_requested.emit("queued")
        controller.window.archive_tasks_requested.emit(["done"])
        controller.window.restore_tasks_requested.emit(["old"])
        controller.window.open_media_requested.emit("media")
        controller.window.resume_tasks_requested.emit(["paused"])
        controller.window.retry_tasks_requested.emit(["failed"])
        await controller._async_actions.wait_idle()
        controller.window.verify_media_requested.emit(["media"])
        await controller._async_actions.wait_idle()
        controller.window.verify_tasks_requested.emit(["done"])
        await controller._async_actions.wait_idle()
        controller.window.repair_media_requested.emit(["broken"])
        await controller._async_actions.wait_idle()
        controller.window.integrity_cancel_requested.emit()

    try:
        loop.run_until_complete(emit_actions())

        assert calls == [
            ("select", ["one"]),
            ("open", "media"),
            ("pause", ["one", "two"]),
            ("priority", "queued"),
            ("archive", ["done"]),
            ("restore", ["old"]),
            ("resume", ["paused"]),
            ("retry", ["failed"]),
            ("verify_media", ["media"]),
            ("verify_tasks", ["done"]),
            ("repair_media", ["broken"]),
            ("cancel", None),
        ]
        slot_names = {getattr(slot, "__name__", "") for slot in controller._ui_slots}
        assert "task_selection_changed" in slot_names
        assert "pause_tasks_requested" in slot_names
        assert "prioritize_task_requested" in slot_names
        assert "resume_tasks_requested" in slot_names
        assert "retry_tasks_requested" in slot_names
        assert "archive_tasks_requested" in slot_names
        assert "restore_tasks_requested" in slot_names
        assert "verify_media_requested" in slot_names
        assert "verify_tasks_requested" in slot_names
        assert "repair_media_requested" in slot_names
    finally:
        loop.run_until_complete(controller._async_actions.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()


@pytest.mark.parametrize(
    ("signal_name", "method_name", "payload", "action_key"),
    [
        ("pause_tasks_requested", "pause_tasks", ["running"], "tasks.pause"),
        ("prioritize_task_requested", "prioritize_task", "queued", "tasks.prioritize"),
        ("resume_tasks_requested", "resume_tasks", ["paused"], "tasks.resume"),
        ("retry_tasks_requested", "retry_failed_tasks", ["failed"], "tasks.retry"),
        ("archive_tasks_requested", "archive_tasks", ["done"], "tasks.archive"),
        ("restore_tasks_requested", "restore_tasks", ["old"], "tasks.restore"),
    ],
)
def test_repeated_task_command_clicks_share_one_async_action(
    tmp_path,
    monkeypatch,
    signal_name,
    method_name,
    payload,
    action_key,
) -> None:
    application, loop, controller = app.create_application(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[object] = []

    async def command(value):
        calls.append(value)
        started.set()
        await release.wait()

    monkeypatch.setattr(controller, method_name, command)

    async def emit_actions() -> None:
        signal = getattr(controller.window, signal_name)
        signal.emit(payload)
        await started.wait()
        signal.emit(payload)
        assert controller._async_actions.active_keys == frozenset({action_key})
        release.set()
        await controller._async_actions.wait_idle()

    try:
        loop.run_until_complete(emit_actions())
        assert calls == [payload]
        assert controller._async_actions.active_keys == frozenset()
    finally:
        loop.run_until_complete(controller._async_actions.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()


def test_repeated_diagnostics_clicks_share_one_async_action(tmp_path, monkeypatch) -> None:
    application, loop, controller = app.create_application(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def run_diagnostics() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(controller, "run_diagnostics", run_diagnostics)

    async def emit_actions() -> None:
        controller.window.diagnostics_page.run_requested.emit()
        await started.wait()
        controller.window.diagnostics_page.run_requested.emit()
        assert controller._async_actions.active_keys == frozenset({"diagnostics.run"})
        release.set()
        await controller._async_actions.wait_idle()

    try:
        loop.run_until_complete(emit_actions())
        assert calls == 1
    finally:
        loop.run_until_complete(controller._async_actions.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()
