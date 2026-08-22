import logging

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QSystemTrayIcon, QWidget

from telegram_downloader.background import BackgroundModeController, QtTrayAdapter
from telegram_downloader.notifications import NotificationPayload, NotificationRoute


class FakeWindow:
    def __init__(self) -> None:
        self.visible = True
        self.raised = 0
        self.activated = 0
        self.routes: list[NotificationRoute] = []

    def hide(self) -> None:
        self.visible = False

    def show(self) -> None:
        self.visible = True

    def raise_(self) -> None:
        self.raised += 1

    def activateWindow(self) -> None:
        self.activated += 1

    def show_route(self, route: NotificationRoute) -> None:
        self.routes.append(route)


class FakeTray:
    def __init__(self, *, available: bool = True, raises: bool = False) -> None:
        self.available = available
        self.raises = raises
        self.hints = 0
        self.notifications: list[NotificationPayload] = []
        self.hidden = 0

    def show_close_hint(self) -> None:
        self.hints += 1

    def show_notification(self, payload: NotificationPayload) -> None:
        if self.raises:
            raise RuntimeError("private tray failure")
        self.notifications.append(payload)

    def hide(self) -> None:
        self.hidden += 1


def runtime(
    *,
    tray: FakeTray | None = None,
    tray_hint_shown: bool = False,
    persist_tray_hint=None,
):
    exits: list[str] = []
    controller = BackgroundModeController(
        FakeWindow(),
        tray or FakeTray(),
        lambda: exits.append("exit"),
        tray_hint_shown=tray_hint_shown,
        persist_tray_hint=persist_tray_hint,
    )
    return controller, exits


def test_close_hides_without_shutting_down_when_tray_is_available() -> None:
    controller, exits = runtime()
    controller.configure(close_to_tray=True, notifications_enabled=True)

    assert controller.handle_window_close() is True

    assert controller.window.visible is False
    assert exits == []


def test_explicit_exit_is_idempotent_and_bypasses_tray() -> None:
    controller, exits = runtime()

    controller.request_exit()
    controller.request_exit()

    assert exits == ["exit"]
    assert controller.tray.hidden == 1


def test_no_tray_falls_back_to_true_exit() -> None:
    controller, exits = runtime(tray=FakeTray(available=False))

    assert controller.handle_window_close() is False

    assert exits == ["exit"]


def test_first_close_hint_is_persisted_and_not_repeated() -> None:
    persisted: list[bool] = []
    controller, _exits = runtime(
        persist_tray_hint=lambda: persisted.append(True),
    )

    controller.handle_window_close()
    controller.show_window()
    controller.handle_window_close()

    assert persisted == [True]
    assert controller.tray.hints == 1


def test_show_window_raises_activates_and_routes() -> None:
    controller, _exits = runtime(tray_hint_shown=True)
    controller.window.hide()

    controller.show_window(NotificationRoute.SUBSCRIPTIONS)

    assert controller.window.visible is True
    assert controller.window.raised == 1
    assert controller.window.activated == 1
    assert controller.window.routes == [NotificationRoute.SUBSCRIPTIONS]


def test_notification_failure_is_redacted_and_does_not_escape(caplog) -> None:
    controller, exits = runtime(tray=FakeTray(raises=True))
    payload = NotificationPayload(
        "下载完成",
        "1 个下载任务已完成",
        NotificationRoute.TASKS,
    )
    caplog.set_level(logging.ERROR, logger="telegram_downloader.background")

    controller.show_notification(payload)

    assert exits == []
    assert [record.message for record in caplog.records] == ["系统通知不可用"]
    assert "private tray failure" not in caplog.text


def test_disabled_notifications_are_not_forwarded() -> None:
    controller, _exits = runtime()
    controller.configure(close_to_tray=True, notifications_enabled=False)

    controller.show_notification(
        NotificationPayload("title", "body", NotificationRoute.TASKS)
    )

    assert controller.tray.notifications == []


def test_tray_menu_uses_explicit_readable_palette(qapp, qtbot, monkeypatch) -> None:
    original = qapp.palette()
    dark = QPalette(original)
    dark.setColor(QPalette.ColorRole.Window, QColor("#111827"))
    dark.setColor(QPalette.ColorRole.WindowText, QColor("#111827"))
    qapp.setPalette(dark)
    window = QWidget()
    qtbot.addWidget(window)
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: True)

    try:
        adapter = QtTrayAdapter(window)
        palette = adapter.menu.palette()

        assert palette.color(QPalette.ColorRole.Window).name().lower() == "#ffffff"
        assert palette.color(QPalette.ColorRole.WindowText).name().lower() == "#22394a"
        assert palette.color(QPalette.ColorRole.Text).name().lower() == "#22394a"
        assert "QMenu::item:selected" in adapter.menu.styleSheet()
        assert "#E8F9FC" in adapter.menu.styleSheet()
        assert "QMenu::item:disabled" in adapter.menu.styleSheet()
        assert "QMenu::separator" in adapter.menu.styleSheet()
        assert adapter.menu.objectName() == "trayMenu"
        assert (
            qapp.palette().color(QPalette.ColorRole.Window).name().lower()
            == "#111827"
        )
    finally:
        qapp.setPalette(original)
