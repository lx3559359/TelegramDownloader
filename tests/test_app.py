import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime
from inspect import getsource, isawaitable
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox, QWidget

from telegram_downloader import app
from telegram_downloader.account_access import OnlineServices
from telegram_downloader.branding import APP_NAME
from telegram_downloader.catalog import CatalogRepository
from telegram_downloader.connectivity import ConnectionRecovery
from telegram_downloader.content import (
    ALL_DIALOGS_SCOPE_REF,
    AccountProfile,
    ContentDialog,
    DialogKind,
    SearchScope,
    SearchSelectionIntent,
    SelectionCommit,
    SelectionMode,
)
from telegram_downloader.content_browser import ContentBrowserService
from telegram_downloader.domain import MediaKind
from telegram_downloader.download_persistence import DownloadPersistenceCoordinator
from telegram_downloader.download_schedule import DownloadScheduleController
from telegram_downloader.file_integrity import FileIntegrityService
from telegram_downloader.files import DownloadNamingSettings
from telegram_downloader.gateway import (
    AuthorizationFailureReason,
    SessionExpiredError,
)
from telegram_downloader.maintenance_activity import OperationActivityRegistry
from telegram_downloader.paths import PortablePaths
from telegram_downloader.settings import (
    AppSettings,
    DownloadStorageSettings,
    SettingsStore,
)
from telegram_downloader.subscription_matching import SubscriptionCriteria
from telegram_downloader.subscription_scheduler import SubscriptionScheduler
from telegram_downloader.subscription_service import SubscriptionService
from telegram_downloader.subscriptions import SubscriptionRule, SubscriptionState
from telegram_downloader.ui.async_actions import ActionPolicy
from telegram_downloader.update import UpdateStartupResult

EXPECTED_POLICIES = {
    "account.access.open": ActionPolicy.DEDUPLICATE,
    "account.reauthenticate": ActionPolicy.DEDUPLICATE,
    "account.reconnect": ActionPolicy.DEDUPLICATE,
    "content.activate": ActionPolicy.REPLACE_LATEST,
    "content.history.open": ActionPolicy.REPLACE_LATEST,
    "content.search": ActionPolicy.REPLACE_LATEST,
    "content.load_more": ActionPolicy.REPLACE_LATEST,
    "content.queue": ActionPolicy.DEDUPLICATE,
    "task.details": ActionPolicy.REPLACE_LATEST,
    "task.page": ActionPolicy.DEDUPLICATE,
    "dialogs.refresh": ActionPolicy.DEDUPLICATE,
    "telegram.retry": ActionPolicy.DEDUPLICATE,
    "diagnostics.run": ActionPolicy.DEDUPLICATE,
    "diagnostics.export": ActionPolicy.DEDUPLICATE,
    "login.qr.refresh": ActionPolicy.DEDUPLICATE,
    "login.qr.expired": ActionPolicy.DEDUPLICATE,
    "login.phone": ActionPolicy.DEDUPLICATE,
    "settings.save": ActionPolicy.DEDUPLICATE,
    "settings.update.check": ActionPolicy.DEDUPLICATE,
    "settings.thumbnail_cache.clear": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.activate": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.scan": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.prepare-safe": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.execute-safe": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.scan-downloads": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.prepare-manual": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.execute-manual": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.automatic": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.cancel": ActionPolicy.DEDUPLICATE,
}


def test_online_service_bundle_build_is_side_effect_free(
    tmp_path,
    monkeypatch,
) -> None:
    application, loop, controller = app.create_application(tmp_path)
    content_bindings = []
    subscription_bindings = []
    monkeypatch.setattr(
        controller.content_browser,
        "bind_online",
        lambda gateway, planner: content_bindings.append((gateway, planner)),
    )
    monkeypatch.setattr(
        controller.subscriptions,
        "bind_online",
        lambda gateway, planner: subscription_bindings.append((gateway, planner)),
    )
    gateway = SimpleNamespace()

    try:
        services = controller.build_online_services(gateway, controller.settings)

        assert isinstance(services, OnlineServices)
        assert services.gateway is gateway
        assert content_bindings == []
        assert subscription_bindings == []

        controller.bind_online_services(services)

        assert content_bindings == [(gateway, services.planner)]
        assert subscription_bindings == [(gateway, services.planner)]
    finally:
        loop.run_until_complete(controller._async_actions.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()


def test_account_navigation_opens_status_without_starting_qr(tmp_path) -> None:
    application, loop, controller = app.create_application(tmp_path)

    class Gateway:
        def __init__(self) -> None:
            self.qr_calls = 0

        def is_connected(self) -> bool:
            return True

        async def account_profile(self):
            return AccountProfile("42", "测试账号")

        async def begin_qr_login(self):
            self.qr_calls += 1
            raise AssertionError("account navigation must be read-only")

    gateway = Gateway()
    controller.gateway = gateway
    controller.settings = replace(controller.settings, api_id=123)
    controller.secrets.update(
        {"api_hash": "saved", "session": "encrypted-session"}
    )

    async def exercise() -> None:
        controller.window.login_requested.emit()
        await controller._async_actions.wait_idle()
        snapshot = controller.account_status_dialog.account_name.text()
        assert "测试账号" in snapshot
        assert gateway.qr_calls == 0

    try:
        loop.run_until_complete(exercise())
    finally:
        controller.account_status_dialog.close()
        loop.run_until_complete(controller._async_actions.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()


def close_created_application(application, loop, controller) -> None:
    loop.run_until_complete(controller._async_actions.shutdown())
    controller.window.close()
    loop.close()
    application.processEvents()


@pytest.mark.parametrize(
    ("api_id", "secret_values", "expected_code", "configured"),
    [
        (0, {}, "credentials-not-configured", False),
        (17, {"api_hash": "configured-hash"}, "credentials-ok", True),
    ],
)
def test_create_application_credential_diagnostic_requires_complete_configuration(
    tmp_path,
    monkeypatch,
    api_id: int,
    secret_values: dict[str, str],
    expected_code: str,
    configured: bool,
) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    SettingsStore(paths.settings).save(AppSettings(api_id=api_id))
    paths.secrets.write_bytes(b"decryptable-test-secrets")

    class VaultStub:
        def __init__(self, path) -> None:
            self.path = path

        def load(self) -> dict[str, str]:
            return dict(secret_values)

        def save(self, _values: dict[str, str]) -> None:
            pass

    monkeypatch.setattr(app, "SecretsVault", VaultStub)

    application, loop, controller = app.create_application(tmp_path)
    try:
        credential_probe = next(
            probe for probe in controller.diagnostics.probes if probe.id == "credentials"
        )
        result = loop.run_until_complete(credential_probe.run(asyncio.Event()))

        assert result.code == expected_code
        assert result.metrics["credentialsConfigured"] is configured
    finally:
        close_created_application(application, loop, controller)


def test_create_application_applies_user_visible_brand(tmp_path) -> None:
    application, loop, controller = app.create_application(tmp_path)

    try:
        assert application.applicationName() == APP_NAME
        assert application.applicationDisplayName() == APP_NAME
        assert application.windowIcon().isNull() is False
        assert controller.window.windowTitle() == APP_NAME
    finally:
        close_created_application(application, loop, controller)


def test_create_application_recovers_only_unsafe_download_setting(tmp_path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    unsafe = AppSettings(
        api_id=17,
        concurrency=4,
        download_storage=DownloadStorageSettings(str(paths.data / "media")),
    )
    SettingsStore(paths.settings).save(unsafe)

    application, loop, controller = app.create_application(tmp_path)

    try:
        assert controller.settings.api_id == 17
        assert controller.settings.concurrency == 4
        assert controller.settings.download_storage == DownloadStorageSettings()
        assert controller.download_paths.current_root == paths.downloads.resolve()
        assert "下载目录设置不安全" in controller.window.statusBar().currentMessage()
        assert SettingsStore(paths.settings).load().download_storage == DownloadStorageSettings()
    finally:
        close_created_application(application, loop, controller)


def test_create_application_keeps_structurally_safe_offline_download_root(tmp_path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    missing = tmp_path / "offline" / "media"
    expected = AppSettings(download_storage=DownloadStorageSettings(str(missing.resolve())))
    SettingsStore(paths.settings).save(expected)

    application, loop, controller = app.create_application(tmp_path)

    try:
        assert controller.settings.download_storage == expected.download_storage
        assert controller.download_paths.current_root == missing.resolve()
        with pytest.raises(ValueError, match="不存在"):
            controller.download_paths.require_current_writable()
    finally:
        close_created_application(application, loop, controller)


def test_create_application_diagnostics_receive_active_download_context(
    tmp_path,
    monkeypatch,
) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    external = tmp_path / "external-media"
    external.mkdir()
    SettingsStore(paths.settings).save(
        AppSettings(download_storage=DownloadStorageSettings(str(external)))
    )
    calls: dict[str, object] = {}

    def project_write(paths_value, *, download_paths):
        calls["projectPaths"] = paths_value
        calls["downloadPolicy"] = download_paths
        return object()

    def disk(paths_value, *, download_root):
        calls["diskPaths"] = paths_value
        calls["downloadRoot"] = download_root
        return object()

    monkeypatch.setattr(app, "probe_project_write", project_write)
    monkeypatch.setattr(app, "probe_disk", disk)

    application, loop, controller = app.create_application(tmp_path)
    try:
        probes = {probe.id: probe for probe in controller.diagnostics.probes}
        probes["project-write"].action()
        probes["disk"].action()

        assert calls["projectPaths"] == paths
        assert calls["downloadPolicy"] is controller.download_paths
        assert calls["diskPaths"] == paths
        assert calls["downloadRoot"] == external.resolve()
    finally:
        close_created_application(application, loop, controller)


def test_responsive_action_policy_map_is_complete() -> None:
    from telegram_downloader.ui import async_actions

    assert {
        key: async_actions.ACTION_POLICIES[key] for key in EXPECTED_POLICIES
    } == EXPECTED_POLICIES


def test_content_history_queue_and_selection_use_responsive_wiring() -> None:
    source = getsource(app.create_application)
    assert '"content.history.open"' in source
    assert '"content.queue"' in source
    assert (
        "selection_intent_requested.connect("
        "controller.submit_content_selection)"
    ) in source.replace("\n", "").replace(" ", "")
    assert "history_open_requested.connect(controller._reload_content_search)" not in source
    assert "selection_changed.connect(controller.set_content_selected)" not in source


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


def test_batch_download_confirmation_summarizes_preflight_duplicates() -> None:
    preview = SimpleNamespace(
        items=(1, 2, 3),
        known_bytes=1024,
        unknown_size_count=1,
        input_count=5,
        unique_link_count=3,
        invalid_link_count=1,
        duplicate_link_count=1,
        scanned_media_count=8,
        internal_duplicate_count=2,
        existing_media_count=3,
    )

    text = app._download_confirmation_text(preview)

    assert "输入 5 条" in text
    assert "有效唯一 3 条" in text
    assert "无效 1 条" in text
    assert "输入重复 1 条" in text
    assert "跨链接重复 2 项" in text
    assert "队列既有 3 项" in text
    assert "最终新增 3 项" in text


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


def test_graceful_shutdown_runs_ordered_lifecycle_callbacks() -> None:
    events: list[str] = []

    async def record(value: str) -> None:
        events.append(value)

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
            before_async_shutdown=lambda: record("storage"),
            after_controller_shutdown=lambda: record("runtime"),
        )
        shutdown.request()
        await shutdown.wait()

    asyncio.run(exercise())

    assert events == ["storage", "actions", "controller", "runtime", "quit"]


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
    assert source.index("_show_initial_window(") < source.index("await controller.start(")
    assert source.index("await download_schedule.start()") < source.index("await controller.start(")


def test_update_notification_opens_settings_without_starting_check() -> None:
    source = getsource(app.run)

    assert (
        "NotificationRoute.UPDATE: controller.window.settings_requested.emit"
        in source
    )
    assert "NotificationRoute.UPDATE: controller.check_for_updates" not in source


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
        assert isinstance(controller.activity, OperationActivityRegistry)
        assert controller.subscription_scheduler.activity is controller.activity
        assert controller.update_coordinator.activity is controller.activity
        assert controller.storage_service.activity is controller.activity
        assert controller.storage_scheduler.activity is controller.activity
        assert controller.storage_scheduler.service is controller.storage_service
        assert controller.window.subscriptions_page is not None
        assert isinstance(controller.connection_recovery, ConnectionRecovery)
        assert isinstance(controller.integrity_service, FileIntegrityService)
        assert controller.integrity_service.paths.root == tmp_path.resolve()
        slot_names = {getattr(slot, "__name__", "") for slot in controller._ui_slots}
        assert "content_preview_requested" in slot_names
        assert "subscription_probe_requested" in slot_names
        assert controller._async_actions.active_keys == frozenset()
        assert len(controller._async_actions._slots) == 46
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
                SessionExpiredError(reason=AuthorizationFailureReason.SESSION_REVOKED)
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


def test_settings_storage_shortcut_navigates_without_direct_cache_delete(
    tmp_path,
    monkeypatch,
) -> None:
    application, loop, controller = app.create_application(tmp_path)
    navigation: list[str] = []
    storage_tabs: list[bool] = []
    delete_calls: list[bool] = []

    async def forbidden_delete() -> None:
        delete_calls.append(True)

    monkeypatch.setattr(controller, "clear_thumbnail_cache", forbidden_delete)
    monkeypatch.setattr(controller.window, "show_page", navigation.append)
    monkeypatch.setattr(
        controller.window.maintenance_page,
        "show_storage",
        lambda: storage_tabs.append(True),
    )

    async def exercise() -> None:
        controller.window.settings_requested.emit()
        await controller._async_actions.wait_idle()
        dialog = controller._settings_dialog
        assert dialog is not None
        dialog.thumbnail_cache_clear_button.click()
        await asyncio.sleep(0)
        assert dialog.isVisible() is False

    try:
        loop.run_until_complete(exercise())
        assert navigation == ["maintenance"]
        assert storage_tabs == [True]
        assert delete_calls == []
    finally:
        loop.run_until_complete(controller._async_actions.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()


def test_settings_manual_update_button_awaits_controller_result(
    tmp_path,
) -> None:
    application, loop, controller = app.create_application(tmp_path)

    class Coordinator:
        async def startup(self, _prompt, _shutdown):
            await asyncio.sleep(0)
            return UpdateStartupResult.NO_UPDATE

    controller.update_coordinator = Coordinator()
    controller._utc_now = lambda: datetime(
        2026,
        8,
        23,
        2,
        20,
        tzinfo=UTC,
    )

    async def exercise() -> None:
        controller.window.settings_requested.emit()
        await controller._async_actions.wait_idle()
        dialog = controller._settings_dialog
        assert dialog is not None
        dialog.concurrency.setValue(5)
        dialog.update_check_button.click()
        await controller._async_actions.wait_idle()
        assert dialog.update_status_label.text() == "当前已是最新正式版"
        assert dialog.update_status_label.property("updateState") == "success"
        assert dialog.update_check_button.isEnabled() is True
        persisted = controller.settings_store.load()
        assert persisted.concurrency == 3
        assert (
            persisted.last_successful_update_check_utc
            == "2026-08-23T02:20:00Z"
        )
        assert "2026-08-23" in dialog.update_last_checked_label.text()

    try:
        loop.run_until_complete(exercise())
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
        assert calls == [(SearchScope.ALL_DIALOGS, ALL_DIALOGS_SCOPE_REF, "安装")]
    finally:
        loop.run_until_complete(controller._async_actions.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()


@pytest.mark.asyncio
async def test_telegram_health_uses_retained_authorization_reason() -> None:
    controller = SimpleNamespace(
        gateway=None,
        last_authorization_failure_reason=(AuthorizationFailureReason.AUTH_KEY_DUPLICATED),
    )

    result = await app._telegram_health(controller)

    assert result.code == "telegram-session-expired"
    assert result.metrics == {"authorizationReason": "auth-key-duplicated"}


@pytest.mark.asyncio
async def test_telegram_health_does_not_use_shared_connection_recovery() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        async def test_connection(self) -> None:
            self.calls += 1

    class Recovery:
        def __init__(self) -> None:
            self.calls = 0

        async def ensure_connected(self, _gateway) -> None:
            self.calls += 1
            raise AssertionError("diagnostics must not mutate shared recovery state")

    gateway = Gateway()
    recovery = Recovery()
    controller = SimpleNamespace(
        gateway=gateway,
        connection_recovery=recovery,
        last_authorization_failure_reason=None,
    )

    result = await app._telegram_health(controller)

    assert result.code == "telegram-connected"
    assert gateway.calls == 1
    assert recovery.calls == 0


def test_service_builder_shares_runtime_download_resource_settings(tmp_path) -> None:
    application, loop, controller = app.create_application(tmp_path)
    naming = DownloadNamingSettings(
        "{year}/{source}/{media_type}",
        "{message_id}_{original_name}",
    )
    settings = AppSettings(
        concurrency=4,
        speed_limit_kib=2048,
        download_naming=naming,
    )

    try:
        services = controller.build_online_services(object(), settings)
        planner = services.planner
        scheduler = services.scheduler

        assert planner is not None
        assert services.gateway is not None
        assert scheduler.snapshot().concurrency == 4
        assert scheduler.snapshot().speed_limit_kib == 2048
        assert scheduler.downloader.bandwidth is scheduler._bandwidth
        assert scheduler.persistence is scheduler.downloader.persistence
        assert isinstance(scheduler.persistence, DownloadPersistenceCoordinator)
        assert planner.naming == naming
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
            id="rule-1",
            account_id="a1",
            peer_ref="-1001",
            dialog_title="资料群",
            criteria=SubscriptionCriteria(("美女",)),
            media_kinds=frozenset({MediaKind.PHOTO}),
            interval_minutes=30,
            history_days=0,
            enabled=True,
            state=SubscriptionState.RUNNING,
            last_message_id=42,
            backfill_from_utc=None,
            backfill_through_id=None,
            next_run_at=None,
            last_run_at=now,
            last_error=None,
            failure_count=0,
            created_at=now,
            updated_at=now,
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
    calls: list[SearchSelectionIntent] = []
    started = asyncio.Event()
    release = asyncio.Event()

    class ContentBrowser:
        async def persist_selection(self, intent):
            calls.append(intent)
            started.set()
            await release.wait()
            return SelectionCommit(
                intent.search_id,
                intent.generation,
                intent.revision,
                1,
            )

    try:
        controller.content_browser = ContentBrowser()
        page = controller.window.content_page
        intent = SearchSelectionIntent(
            "search-1",
            1,
            1,
            SelectionMode.PATCH,
            (("result-1", True),),
        )

        async def exercise() -> None:
            page.selection_intent_requested.emit(intent)
            await started.wait()
            assert controller._selection_persist_task is not None
            task = controller._selection_persist_task
            release.set()
            await task

        loop.run_until_complete(exercise())

        assert calls == [intent]
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

    async def record_expiry(generation: int) -> None:
        calls.append(f"login.qr.expired:{generation}")

    monkeypatch.setattr(controller, "refresh_expired_qr", record_expiry)

    async def emit_actions() -> None:
        controller.window.content_activated.emit()
        controller.window.subscriptions_activated.emit()
        controller.window.content_page.refresh_requested.emit()
        controller.window.content_page.connection_retry_requested.emit()
        controller.login_dialog.qr_refresh_requested.emit()
        controller.login_dialog.qr_expired.emit(42)
        controller.login_dialog.phone_fallback_requested.emit()
        controller.login_dialog.credentials_edit_requested.emit()
        controller.login_dialog.login_cancelled.emit()
        await controller._async_actions.wait_idle()

    try:
        bridge = getattr(controller, "_async_actions", None)
        assert bridge is not None
        loop.run_until_complete(emit_actions())

        assert calls == [
            *list(actions.values())[:5],
            "login.qr.expired:42",
            *list(actions.values())[5:],
        ]
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

    monkeypatch.setattr(
        controller,
        "select_task_details",
        lambda value: record_async("select", value),
    )
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
    async def cancel_integrity():
        calls.append(("cancel", None))

    monkeypatch.setattr(controller, "cancel_integrity", cancel_integrity)
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
        await asyncio.sleep(0)

    try:
        loop.run_until_complete(emit_actions())

        assert calls == [
            ("open", "media"),
            ("select", ["one"]),
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


def test_task_detail_page_and_visibility_signals_use_responsive_routes(
    tmp_path,
    monkeypatch,
) -> None:
    application, loop, controller = app.create_application(tmp_path)
    details_seen = asyncio.Event()
    page_seen = asyncio.Event()
    details: list[list[str]] = []
    pages: list[str] = []
    visibility: list[bool] = []

    async def select_details(task_ids):
        details.append(task_ids)
        details_seen.set()

    async def load_page(task_id):
        pages.append(task_id)
        page_seen.set()

    monkeypatch.setattr(controller, "select_task_details", select_details)
    monkeypatch.setattr(controller, "load_more_task_items", load_page)
    monkeypatch.setattr(
        controller,
        "set_task_center_visible",
        visibility.append,
    )

    async def emit() -> None:
        controller.window.task_selection_changed.emit(["task"])
        controller.window.task_items_page_requested.emit("task")
        controller.window.task_center_visibility_changed.emit(True)
        await asyncio.wait_for(
            asyncio.gather(details_seen.wait(), page_seen.wait()),
            timeout=1,
        )
        await controller._async_actions.wait_idle()

    try:
        loop.run_until_complete(emit())
        assert details == [["task"]]
        assert pages == ["task"]
        assert visibility == [True]
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
