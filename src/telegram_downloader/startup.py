from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen, QWidget

from telegram_downloader.branding import APP_NAME, APP_SUBTITLE, app_logo_path


class StartupIndicator:
    def __init__(
        self,
        application: QApplication,
        widget: QSplashScreen,
        font_family: str,
    ) -> None:
        self.application = application
        self.widget = widget
        self.font_family = font_family
        self.status = ""
        self._closed = False

    def set_status(self, text: str) -> None:
        if self._closed:
            return
        self.status = text
        self.widget.showMessage(
            text,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
            QColor("#9fb4ca"),
        )
        self.application.processEvents()

    def finish(self, window: QWidget) -> None:
        if self._closed:
            return
        self.widget.finish(window)
        self.application.processEvents()
        self._closed = True

    def close(self) -> None:
        if self._closed:
            return
        self.widget.close()
        self.application.processEvents()
        self._closed = True


def create_startup_indicator() -> StartupIndicator:
    from telegram_downloader.ui.theme import ensure_cjk_font

    application = QApplication.instance() or QApplication(sys.argv[:1])
    font_family = ensure_cjk_font()
    pixmap = QPixmap(520, 240)
    pixmap.fill(QColor("#0b111b"))

    painter = QPainter(pixmap)
    logo = QPixmap(str(app_logo_path())).scaled(
        62,
        62,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    painter.drawPixmap(36, 38, logo)
    painter.setPen(QColor("#22b8cf"))
    painter.setFont(QFont(font_family, 24, QFont.Weight.Bold))
    painter.drawText(116, 78, APP_NAME)
    painter.setPen(QColor("#e6edf7"))
    painter.setFont(QFont(font_family, 12, QFont.Weight.DemiBold))
    painter.drawText(118, 108, APP_SUBTITLE)
    painter.setPen(QColor("#9fb4ca"))
    painter.setFont(QFont(font_family, 10))
    painter.drawText(38, 132, "安全、可恢复的 Telegram 媒体下载工作台")
    painter.setPen(QColor("#25354a"))
    painter.drawLine(38, 148, 482, 148)
    painter.end()

    widget = QSplashScreen(pixmap)
    widget.setFont(QFont(font_family, 10))
    widget.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    widget.show()
    indicator = StartupIndicator(application, widget, font_family)
    indicator.set_status(f"正在启动 {APP_NAME}…")
    return indicator
