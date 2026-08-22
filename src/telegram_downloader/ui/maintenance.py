from __future__ import annotations

from PySide6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from telegram_downloader.ui.diagnostics import DiagnosticsPage
from telegram_downloader.ui.storage import StoragePage


class MaintenancePage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.diagnostics_page = DiagnosticsPage()
        self.storage_page = StoragePage()
        self.tabs.addTab(self.diagnostics_page, "健康诊断")
        self.tabs.addTab(self.storage_page, "存储空间")
        self.tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(self.tabs)

    def show_health(self) -> None:
        self.tabs.setCurrentWidget(self.diagnostics_page)

    def show_storage(self) -> None:
        self.tabs.setCurrentWidget(self.storage_page)

    def _tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.storage_page:
            self.storage_page.activated.emit()
