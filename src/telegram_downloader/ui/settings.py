from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from telegram_downloader.settings import AppSettings, ProxySettings, SettingsError
from telegram_downloader.ui.theme import DARK_STYLESHEET, ensure_cjk_font


class SettingsDialog(QDialog):
    test_proxy_requested = Signal(object, str)
    thumbnail_cache_clear_requested = Signal()

    def __init__(
        self,
        settings: AppSettings,
        proxy_password: str = "",
        parent: QWidget | None = None,
        *,
        thumbnail_cache_bytes: int = 0,
    ) -> None:
        super().__init__(parent)
        ensure_cjk_font()
        self.setStyleSheet(DARK_STYLESHEET)
        self.setWindowTitle("设置")
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(13)
        title = QLabel("应用设置")
        title.setObjectName("pageTitle")
        description = QLabel("配置会保存在应用目录；代理密码单独使用 DPAPI 加密。")
        description.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(description)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.api_id = QSpinBox()
        self.api_id.setRange(0, 2_147_483_647)
        self.api_id.setValue(settings.api_id)
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 5)
        self.concurrency.setValue(settings.concurrency)
        self.check_updates = QCheckBox("启动后自动检查正式版更新")
        self.check_updates.setChecked(settings.check_updates_on_startup)
        self.proxy_kind = QComboBox()
        self.proxy_kind.addItem("不使用代理", "none")
        self.proxy_kind.addItem("SOCKS5", "socks5")
        self.proxy_kind.addItem("HTTP", "http")
        self.proxy_kind.setCurrentIndex(self.proxy_kind.findData(settings.proxy.kind))
        self.proxy_host = QLineEdit(settings.proxy.host)
        self.proxy_port = QSpinBox()
        self.proxy_port.setRange(0, 65535)
        self.proxy_port.setValue(settings.proxy.port)
        self.proxy_username = QLineEdit(settings.proxy.username)
        self.proxy_password = QLineEdit(proxy_password)
        self.proxy_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API ID", self.api_id)
        form.addRow("并发下载", self.concurrency)
        form.addRow("在线更新", self.check_updates)
        form.addRow("代理类型", self.proxy_kind)
        form.addRow("代理地址", self.proxy_host)
        form.addRow("代理端口", self.proxy_port)
        form.addRow("代理用户名", self.proxy_username)
        form.addRow("代理密码", self.proxy_password)
        layout.addLayout(form)

        cache_row = QHBoxLayout()
        self.thumbnail_cache_size = QLabel(
            self._format_bytes(thumbnail_cache_bytes)
        )
        self.thumbnail_cache_size.setObjectName("muted")
        self.thumbnail_cache_clear_button = QPushButton("清理缩略图缓存")
        cache_row.addWidget(self.thumbnail_cache_size)
        cache_row.addStretch()
        cache_row.addWidget(self.thumbnail_cache_clear_button)
        form.addRow("缩略图缓存", cache_row)

        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #fb923c;")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        actions = QHBoxLayout()
        self.test_button = QPushButton("测试代理")
        cancel_button = QPushButton("取消")
        self.save_button = QPushButton("保存")
        self.save_button.setObjectName("primaryButton")
        actions.addWidget(self.test_button)
        actions.addStretch()
        actions.addWidget(cancel_button)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)

        self.proxy_kind.currentIndexChanged.connect(self._update_proxy_fields)
        self.test_button.clicked.connect(self._test_proxy)
        self.thumbnail_cache_clear_button.clicked.connect(
            self.thumbnail_cache_clear_requested.emit
        )
        cancel_button.clicked.connect(self.reject)
        self.save_button.clicked.connect(self._save)
        self._update_proxy_fields()

    def proxy_values(self) -> ProxySettings:
        kind = str(self.proxy_kind.currentData())
        if kind == "none":
            return ProxySettings()
        return ProxySettings(
            kind,
            self.proxy_host.text().strip(),
            self.proxy_port.value(),
            self.proxy_username.text().strip(),
        )

    def values(self) -> AppSettings:
        return AppSettings(
            self.api_id.value(),
            self.concurrency.value(),
            self.proxy_values(),
            self.check_updates.isChecked(),
        )

    def _update_proxy_fields(self) -> None:
        enabled = self.proxy_kind.currentData() != "none"
        for widget in (
            self.proxy_host,
            self.proxy_port,
            self.proxy_username,
            self.proxy_password,
        ):
            widget.setEnabled(enabled)
        self.test_button.setEnabled(enabled)

    def _test_proxy(self) -> None:
        try:
            proxy = self.proxy_values()
        except SettingsError as error:
            self._show_error(str(error))
            return
        self.error_label.setVisible(False)
        self.test_proxy_requested.emit(proxy, self.proxy_password.text())

    def _save(self) -> None:
        try:
            self.values()
        except SettingsError as error:
            self._show_error(str(error))
            return
        self.accept()

    def _show_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.setVisible(True)

    def set_thumbnail_cache_bytes(self, value: int) -> None:
        self.thumbnail_cache_size.setText(self._format_bytes(value))

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(max(0, value))
        units = ("B", "KB", "MB", "GB", "TB")
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                return (
                    f"{amount:.0f} {unit}"
                    if unit == "B"
                    else f"{amount:.1f} {unit}"
                )
            amount /= 1024
        return f"{value} B"
