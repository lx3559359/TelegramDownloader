from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

from telegram_downloader.branding import APP_NAME, app_icon_path
from telegram_downloader.notifications import NotificationPayload, NotificationRoute

_LOGGER = logging.getLogger(__name__)

TRAY_MENU_STYLESHEET = """
QMenu {
    background: #FFFFFF;
    color: #22394A;
    border: 1px solid #D5DEE7;
    border-radius: 7px;
    padding: 6px;
}
QMenu::item {
    min-height: 28px;
    padding: 3px 24px 3px 10px;
    border-radius: 5px;
    background: transparent;
    color: #22394A;
}
QMenu::item:selected {
    background: #E8F9FC;
    color: #087F96;
}
QMenu::item:disabled {
    background: transparent;
    color: #A8B5BD;
}
QMenu::separator {
    height: 1px;
    margin: 5px 8px;
    background: #E2E8F0;
}
"""


class WindowPort(Protocol):
    def hide(self) -> None: ...

    def show(self) -> None: ...

    def raise_(self) -> None: ...

    def activateWindow(self) -> None: ...

    def show_route(self, route: NotificationRoute) -> None: ...


class TrayPort(Protocol):
    available: bool

    def show_close_hint(self) -> None: ...

    def show_notification(self, payload: NotificationPayload) -> None: ...

    def hide(self) -> None: ...


class BackgroundModeController:
    def __init__(
        self,
        window: WindowPort,
        tray: TrayPort,
        exit_app: Callable[[], None],
        *,
        tray_hint_shown: bool = False,
        persist_tray_hint: Callable[[], None] | None = None,
    ) -> None:
        self.window = window
        self.tray = tray
        self.exit_app = exit_app
        self.close_to_tray = True
        self.notifications_enabled = True
        self.tray_hint_shown = tray_hint_shown
        self.persist_tray_hint = persist_tray_hint or (lambda: None)
        self._exit_requested = False

    def configure(
        self,
        *,
        close_to_tray: bool,
        notifications_enabled: bool,
    ) -> None:
        self.close_to_tray = bool(close_to_tray)
        self.notifications_enabled = bool(notifications_enabled)

    def handle_window_close(self) -> bool:
        if self.close_to_tray and self.tray.available:
            self.window.hide()
            self._show_first_close_hint()
            return True
        self.request_exit()
        return False

    def show_window(self, route: NotificationRoute | None = None) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        if route is not None:
            self.window.show_route(route)

    def show_notification(self, payload: NotificationPayload) -> None:
        if not self.notifications_enabled or not self.tray.available:
            return
        try:
            self.tray.show_notification(payload)
        except Exception:
            _LOGGER.error("系统通知不可用")

    def request_exit(self) -> None:
        if self._exit_requested:
            return
        self._exit_requested = True
        try:
            self.tray.hide()
        except Exception:
            _LOGGER.error("系统托盘不可用")
        self.exit_app()

    def _show_first_close_hint(self) -> None:
        if self.tray_hint_shown:
            return
        try:
            self.tray.show_close_hint()
            self.persist_tray_hint()
        except Exception:
            _LOGGER.error("托盘提示状态无法保存")
        finally:
            self.tray_hint_shown = True


class QtWindowPort:
    def __init__(
        self,
        window: Any,
        route_handlers: Mapping[NotificationRoute, Callable[[], None]],
    ) -> None:
        self.window = window
        self.route_handlers = dict(route_handlers)

    def hide(self) -> None:
        self.window.hide()

    def show(self) -> None:
        if self.window.isMinimized():
            self.window.showNormal()
        else:
            self.window.show()

    def raise_(self) -> None:
        self.window.raise_()

    def activateWindow(self) -> None:
        self.window.activateWindow()

    def show_route(self, route: NotificationRoute) -> None:
        handler = self.route_handlers.get(route)
        if handler is not None:
            handler()


class QtTrayAdapter(QObject):
    show_requested = Signal()
    hide_requested = Signal()
    pause_all_requested = Signal()
    resume_all_requested = Signal()
    subscriptions_requested = Signal()
    downloads_requested = Signal()
    exit_requested = Signal()
    notification_activated = Signal(object)

    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self._notification_route: NotificationRoute | None = None
        icon = QIcon(str(app_icon_path()))
        if icon.isNull():
            icon = window.windowIcon()
        if icon.isNull():
            icon = QApplication.style().standardIcon(
                QStyle.StandardPixmap.SP_ArrowDown
            )
        self.icon = QSystemTrayIcon(QIcon(icon), window)
        self.icon.setToolTip(APP_NAME)
        self.menu = QMenu(window)
        self.menu.setObjectName("trayMenu")
        palette = self.menu.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#FFFFFF"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#FFFFFF"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#22394A"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#22394A"))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#22394A"))
        palette.setColor(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.Text,
            QColor("#A8B5BD"),
        )
        palette.setColor(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.WindowText,
            QColor("#A8B5BD"),
        )
        self.menu.setPalette(palette)
        self.menu.setStyleSheet(TRAY_MENU_STYLESHEET)
        self.show_action = QAction("显示主窗口", self.menu)
        self.hide_action = QAction("隐藏主窗口", self.menu)
        self.pause_action = QAction("暂停全部下载", self.menu)
        self.resume_action = QAction("继续可运行下载", self.menu)
        self.subscriptions_action = QAction("立即检查到期订阅", self.menu)
        self.downloads_action = QAction("打开下载目录", self.menu)
        self.exit_action = QAction("彻底退出", self.menu)
        for action in (
            self.show_action,
            self.hide_action,
            self.pause_action,
            self.resume_action,
            self.subscriptions_action,
            self.downloads_action,
        ):
            self.menu.addAction(action)
        self.menu.addSeparator()
        self.menu.addAction(self.exit_action)
        self.icon.setContextMenu(self.menu)
        self.show_action.triggered.connect(self.show_requested.emit)
        self.hide_action.triggered.connect(self.hide_requested.emit)
        self.pause_action.triggered.connect(self.pause_all_requested.emit)
        self.resume_action.triggered.connect(self.resume_all_requested.emit)
        self.subscriptions_action.triggered.connect(self.subscriptions_requested.emit)
        self.downloads_action.triggered.connect(self.downloads_requested.emit)
        self.exit_action.triggered.connect(self.exit_requested.emit)
        self.icon.messageClicked.connect(self._notification_clicked)
        self.icon.activated.connect(self._activated)

    def show(self) -> None:
        if self.available:
            self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def show_close_hint(self) -> None:
        self.icon.showMessage(
            f"{APP_NAME} 仍在运行",
            "下载和自动订阅将在后台继续；可从托盘菜单彻底退出。",
            QSystemTrayIcon.MessageIcon.Information,
            5000,
        )

    def show_notification(self, payload: NotificationPayload) -> None:
        self._notification_route = payload.route
        self.icon.showMessage(
            payload.title,
            payload.body,
            QSystemTrayIcon.MessageIcon.Information,
            5000,
        )

    def _notification_clicked(self) -> None:
        if self._notification_route is not None:
            self.notification_activated.emit(self._notification_route)

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason is QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_requested.emit()
