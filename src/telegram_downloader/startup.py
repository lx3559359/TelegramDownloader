from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen, QWidget


class StartupIndicator:
    def __init__(self, application: QApplication, widget: QSplashScreen) -> None:
        self.application = application
        self.widget = widget
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
    application = QApplication.instance() or QApplication(sys.argv[:1])
    pixmap = QPixmap(520, 240)
    pixmap.fill(QColor("#0b111b"))

    painter = QPainter(pixmap)
    painter.setPen(QColor("#22b8cf"))
    painter.setFont(QFont("Microsoft YaHei UI", 24, QFont.Weight.Bold))
    painter.drawText(36, 82, "Telegram 下载器")
    painter.setPen(QColor("#e6edf7"))
    painter.setFont(QFont("Microsoft YaHei UI", 12, QFont.Weight.DemiBold))
    painter.drawText(38, 118, "安全、可恢复的 Telegram 媒体下载工作台")
    painter.setPen(QColor("#25354a"))
    painter.drawLine(38, 148, 482, 148)
    painter.end()

    widget = QSplashScreen(pixmap)
    widget.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    widget.show()
    indicator = StartupIndicator(application, widget)
    indicator.set_status("正在启动 Telegram 下载器…")
    return indicator
