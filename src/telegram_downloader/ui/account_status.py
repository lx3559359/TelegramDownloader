from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from telegram_downloader.account_access import (
    AccountStatusSnapshot,
    AuthorizationState,
    ConnectionState,
)
from telegram_downloader.ui.effects import ElevationLevel, apply_elevation
from telegram_downloader.ui.theme import APP_STYLESHEET, ensure_cjk_font


class AccountStatusDialog(QDialog):
    reconnect_requested = Signal()
    reauthenticate_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        ensure_cjk_font()
        self.setStyleSheet(APP_STYLESHEET)
        self.setWindowTitle("账号状态")
        self.setModal(True)
        self.setMinimumWidth(500)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 18)
        surface = QFrame(self)
        surface.setObjectName("dialogSurface")
        apply_elevation(surface, ElevationLevel.MAJOR)
        outer.addWidget(surface)
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(10)

        title = QLabel("Telegram 账号")
        title.setObjectName("pageTitle")
        description = QLabel("查看当前账号和连接状态；此页面不会发起登录。")
        description.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(description)

        self.account_name = QLabel("未登录")
        self.account_name.setObjectName("sectionTitle")
        self.authorization_label = QLabel()
        self.connection_label = QLabel()
        self.session_label = QLabel()
        self.services_label = QLabel()
        self.download_label = QLabel()
        for label in (
            self.authorization_label,
            self.connection_label,
            self.session_label,
            self.services_label,
            self.download_label,
        ):
            label.setObjectName("muted")
            layout.addWidget(label)
        layout.insertWidget(2, self.account_name)

        self.guard_label = QLabel()
        self.guard_label.setObjectName("accountGuard")
        self.guard_label.setWordWrap(True)
        self.guard_label.hide()
        layout.addWidget(self.guard_label)
        self.error_label = QLabel()
        self.error_label.setObjectName("errorText")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        actions = QHBoxLayout()
        self.reconnect_button = QPushButton("重新连接")
        self.reauthenticate_button = QPushButton("重新登录 / 切换账号")
        self.reauthenticate_button.setObjectName("primaryButton")
        close_button = QPushButton("关闭")
        actions.addWidget(self.reconnect_button)
        actions.addStretch()
        actions.addWidget(close_button)
        actions.addWidget(self.reauthenticate_button)
        layout.addLayout(actions)

        self.reconnect_button.clicked.connect(self.reconnect_requested.emit)
        self.reauthenticate_button.clicked.connect(
            self.reauthenticate_requested.emit
        )
        close_button.clicked.connect(self.accept)

    def set_snapshot(self, snapshot: AccountStatusSnapshot) -> None:
        authorization_labels = {
            AuthorizationState.MISSING: "授权状态：尚未配置",
            AuthorizationState.AUTHORIZED: "授权状态：已授权",
            AuthorizationState.EXPIRED: "授权状态：登录已失效",
            AuthorizationState.UNKNOWN: "授权状态：暂时无法确认",
        }
        connection_labels = {
            ConnectionState.OFFLINE: "连接状态：离线",
            ConnectionState.ONLINE: "连接状态：连接正常",
            ConnectionState.DEGRADED: "连接状态：连接异常",
        }
        self.account_name.setText(snapshot.display_name or "账号信息不可用")
        self.authorization_label.setText(
            authorization_labels[snapshot.authorization]
        )
        self.connection_label.setText(connection_labels[snapshot.connection])
        self.session_label.setText(
            "授权会话：已加密保存"
            if snapshot.session_encrypted
            else "授权会话：未保存"
        )
        self.services_label.setText(
            "账号内容：{} · 自动订阅：{}".format(
                "可用" if snapshot.content_available else "不可用",
                "可用" if snapshot.subscriptions_available else "不可用",
            )
        )
        self.download_label.setText(
            f"活动下载：{snapshot.active_download_count} 个"
        )
        self.reconnect_button.setVisible(snapshot.can_reconnect)
        guarded = snapshot.active_download_count > 0
        self.reauthenticate_button.setEnabled(
            snapshot.can_reauthenticate and not guarded
        )
        self.guard_label.setText(
            "请先暂停或等待活动下载完成" if guarded else ""
        )
        self.guard_label.setVisible(guarded)
        self.error_label.hide()

    def show_error(self, text: str) -> None:
        self.error_label.setText(str(text))
        self.error_label.setVisible(bool(text))
