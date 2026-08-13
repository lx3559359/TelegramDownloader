from __future__ import annotations

import re
from enum import IntEnum

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from telegram_downloader.settings import ProxySettings, SettingsError
from telegram_downloader.ui.theme import DARK_STYLESHEET, ensure_cjk_font

_PHONE = re.compile(r"^\+\d{5,15}$")


class LoginPage(IntEnum):
    CREDENTIALS = 0
    PHONE = 1
    CODE = 2
    PASSWORD = 3
    READY = 4


class LoginDialog(QDialog):
    credentials_submitted = Signal(int, str, object, str)
    phone_submitted = Signal(str)
    code_submitted = Signal(str)
    password_submitted = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        ensure_cjk_font()
        self.setStyleSheet(DARK_STYLESHEET)
        self.setWindowTitle("登录 Telegram")
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(13)
        title = QLabel("连接你的 Telegram 账号")
        title.setObjectName("pageTitle")
        subtitle = QLabel("凭据和授权会话将使用 Windows DPAPI 加密后保存在应用目录")
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.stack = QStackedWidget()
        self.credentials_page = self._build_credentials_page()
        self.phone_page = self._build_phone_page()
        self.code_page = self._build_code_page()
        self.password_page = self._build_password_page()
        self.ready_page = self._build_ready_page()
        for page in (
            self.credentials_page,
            self.phone_page,
            self.code_page,
            self.password_page,
            self.ready_page,
        ):
            self.stack.addWidget(page)
        layout.addWidget(self.stack)

        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #fb923c;")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)
        self.show_page(LoginPage.CREDENTIALS)

    def _build_credentials_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._step_label("步骤 1 / 4 · API 凭据与网络"))

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.api_id = QSpinBox()
        self.api_id.setRange(0, 2_147_483_647)
        self.api_id.setSpecialValueText("请输入 API ID")
        self.api_hash = QLineEdit()
        self.api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_hash.setPlaceholderText("从 my.telegram.org 获取")
        self.proxy_kind = QComboBox()
        self.proxy_kind.addItem("不使用代理", "none")
        self.proxy_kind.addItem("SOCKS5", "socks5")
        self.proxy_kind.addItem("HTTP", "http")
        self.proxy_host = QLineEdit()
        self.proxy_host.setPlaceholderText("127.0.0.1")
        self.proxy_port = QSpinBox()
        self.proxy_port.setRange(0, 65535)
        self.proxy_port.setSpecialValueText("端口")
        self.proxy_username = QLineEdit()
        self.proxy_password = QLineEdit()
        self.proxy_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API ID", self.api_id)
        form.addRow("API Hash", self.api_hash)
        form.addRow("代理类型", self.proxy_kind)
        form.addRow("代理地址", self.proxy_host)
        form.addRow("代理端口", self.proxy_port)
        form.addRow("代理用户名", self.proxy_username)
        form.addRow("代理密码", self.proxy_password)
        layout.addLayout(form)

        self.credentials_next = QPushButton("连接并继续")
        self.credentials_next.setObjectName("primaryButton")
        self.credentials_next.clicked.connect(self._submit_credentials)
        layout.addWidget(self.credentials_next, 0, Qt.AlignmentFlag.AlignRight)
        self.proxy_kind.currentIndexChanged.connect(self._update_proxy_fields)
        self._update_proxy_fields()
        return page

    def _build_phone_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._step_label("步骤 2 / 4 · 手机号"))
        description = QLabel("请输入带国家区号的手机号，验证码将由 Telegram 发送。")
        description.setObjectName("muted")
        layout.addWidget(description)
        self.phone = QLineEdit()
        self.phone.setPlaceholderText("例如：+8613800000000")
        layout.addWidget(self.phone)
        self.phone_next = QPushButton("发送验证码")
        self.phone_next.setObjectName("primaryButton")
        self.phone_next.clicked.connect(self._submit_phone)
        layout.addWidget(self.phone_next, 0, Qt.AlignmentFlag.AlignRight)
        return page

    def _build_code_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._step_label("步骤 3 / 4 · 验证码"))
        description = QLabel("输入 Telegram 客户端或短信中收到的验证码。")
        description.setObjectName("muted")
        layout.addWidget(description)
        self.code = QLineEdit()
        self.code.setPlaceholderText("验证码")
        self.code.setMaxLength(12)
        layout.addWidget(self.code)
        self.code_next = QPushButton("验证")
        self.code_next.setObjectName("primaryButton")
        self.code_next.clicked.connect(self._submit_code)
        layout.addWidget(self.code_next, 0, Qt.AlignmentFlag.AlignRight)
        return page

    def _build_password_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._step_label("步骤 4 / 4 · 两步验证"))
        description = QLabel("该账号启用了两步验证，请输入 Telegram 云密码。")
        description.setObjectName("muted")
        layout.addWidget(description)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("两步验证密码")
        layout.addWidget(self.password)
        self.password_next = QPushButton("完成登录")
        self.password_next.setObjectName("primaryButton")
        self.password_next.clicked.connect(self._submit_password)
        layout.addWidget(self.password_next, 0, Qt.AlignmentFlag.AlignRight)
        return page

    def _build_ready_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)
        self.ready_label = QLabel("登录成功")
        self.ready_label.setObjectName("pageTitle")
        self.ready_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.ready_label)
        note = QLabel("授权会话已加密保存在当前应用目录。")
        note.setObjectName("muted")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(note)
        close_button = QPushButton("进入任务中心")
        close_button.setObjectName("primaryButton")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignCenter)
        return page

    @staticmethod
    def _step_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        return label

    def show_page(self, page: LoginPage) -> None:
        self.error_label.clear()
        self.error_label.setVisible(False)
        self.stack.setCurrentIndex(int(page))

    def show_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.setVisible(True)

    def show_ready(self, display_name: str) -> None:
        self.api_hash.clear()
        self.proxy_password.clear()
        self.code.clear()
        self.password.clear()
        self.ready_label.setText(f"登录成功 · {display_name}")
        self.show_page(LoginPage.READY)

    def _update_proxy_fields(self) -> None:
        enabled = self.proxy_kind.currentData() != "none"
        for widget in (
            self.proxy_host,
            self.proxy_port,
            self.proxy_username,
            self.proxy_password,
        ):
            widget.setEnabled(enabled)

    def _proxy_values(self) -> ProxySettings:
        kind = str(self.proxy_kind.currentData())
        if kind == "none":
            return ProxySettings()
        return ProxySettings(
            kind,
            self.proxy_host.text().strip(),
            self.proxy_port.value(),
            self.proxy_username.text().strip(),
        )

    def _submit_credentials(self) -> None:
        api_hash = self.api_hash.text().strip()
        if self.api_id.value() <= 0:
            self.show_error("请输入有效的 API ID")
            return
        if not api_hash:
            self.show_error("请输入 API Hash")
            return
        try:
            proxy = self._proxy_values()
        except SettingsError as error:
            self.show_error(str(error))
            return
        self.credentials_submitted.emit(
            self.api_id.value(),
            api_hash,
            proxy,
            self.proxy_password.text(),
        )

    def _submit_phone(self) -> None:
        phone = self.phone.text().strip().replace(" ", "")
        if _PHONE.fullmatch(phone) is None:
            self.show_error("手机号必须以 + 开头并包含国家区号")
            return
        self.phone_submitted.emit(phone)

    def _submit_code(self) -> None:
        code = self.code.text().strip()
        if not code:
            self.show_error("请输入验证码")
            return
        self.code_submitted.emit(code)

    def _submit_password(self) -> None:
        password = self.password.text()
        if not password:
            self.show_error("请输入两步验证密码")
            return
        self.password_submitted.emit(password)
