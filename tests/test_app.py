from PySide6.QtWidgets import QMessageBox

from telegram_downloader import app
from telegram_downloader.connectivity import ConnectionRecovery
from telegram_downloader.content_browser import ContentBrowserService


def test_standard_button_selection_accepts_pyside_integer_result() -> None:
    yes = QMessageBox.StandardButton.Yes

    assert app._standard_button_selected(yes.value, yes) is True


def test_duplicate_instance_exits_before_application_construction(
    tmp_path, monkeypatch
) -> None:
    class Guard:
        def acquire(self) -> bool:
            return False

        def notify_already_running(self) -> None:
            self.notified = True

        def release(self) -> None:
            raise AssertionError("unowned guard must not be released")

    guard = Guard()
    monkeypatch.setattr(
        app,
        "create_application",
        lambda _root: (_ for _ in ()).throw(AssertionError()),
    )

    assert app.run(tmp_path, instance_guard=guard) == 2
    assert guard.notified is True


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
        assert isinstance(controller.connection_recovery, ConnectionRecovery)
        slot_names = {
            getattr(slot, "__name__", "") for slot in controller._ui_slots
        }
        assert "content_preview_requested" in slot_names
        assert controller._async_actions.active_keys == frozenset()
        assert len(controller._async_actions._slots) == 7
        controller.window.content_page.link_requested.emit(
            "https://t.me/example/1#fragment"
        )
        assert controller.window.content_page.error_label.text() == (
            "请输入有效的 t.me 链接"
        )

        report = app.run_self_test(tmp_path)
        for value in report["writable_paths"].values():
            assert str(value).startswith(str(tmp_path.resolve()))
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
