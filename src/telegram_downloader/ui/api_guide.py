from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

API_PORTAL_URL = "https://my.telegram.org/apps"
OpenUrl = Callable[[QUrl], bool]
CopyText = Callable[[str], None]


class ApiCredentialGuide(QFrame):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        open_url: OpenUrl | None = None,
        copy_text: CopyText | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("apiGuide")
        self._open_url = open_url or QDesktopServices.openUrl
        self._copy_text = copy_text or self._copy_to_clipboard
        self._expanded = True
        self._user_changed = False

        root = QVBoxLayout(self)
        root.setContentsMargins(13, 11, 13, 12)
        root.setSpacing(9)
        header = QHBoxLayout()
        title = QLabel("如何获取 API ID / Hash（首次约 3–5 分钟）")
        title.setObjectName("sectionTitle")
        header.addWidget(title, 1)
        self.toggle_button = QPushButton("收起指南")
        self.toggle_button.setAutoDefault(False)
        self.toggle_button.clicked.connect(self._toggle_from_user)
        header.addWidget(self.toggle_button)
        root.addLayout(header)

        self.details = QWidget(self)
        details_layout = QVBoxLayout(self.details)
        details_layout.setContentsMargins(0, 2, 0, 0)
        details_layout.setSpacing(8)
        self._add_step(
            details_layout,
            1,
            "准备 Telegram 账号",
            "先在官方 Telegram App 注册并保持登录，使用当前有效手机号。",
        )
        self._add_step(
            details_layout,
            2,
            "在系统默认浏览器打开官方申请页",
            "页面属于 Telegram；本程序不会读取你在网页输入的手机号或确认码。",
        )
        self.open_button = QPushButton("打开 my.telegram.org/apps ↗")
        self.open_button.setObjectName("primaryButton")
        self.open_button.setAutoDefault(False)
        self.open_button.setAccessibleName("在系统浏览器打开 Telegram API 申请页面")
        self.open_button.clicked.connect(self._open_official_site)
        details_layout.addWidget(self.open_button, 0, Qt.AlignmentFlag.AlignLeft)
        self._add_step(
            details_layout,
            3,
            "登录并进入 API development tools",
            "手机号使用国际格式（中国大陆格式为 +86…）；"
            "确认码会发送到 Telegram 消息，而不是短信。",
        )
        self._add_step(
            details_layout,
            4,
            "按示例创建或查看个人应用",
            "App title：TG Quick Fetch Personal\n"
            "Short name：tgquickfetch\n"
            "Platform：Desktop\n"
            "Description：Personal media download manager\n"
            "如果官网显示其他字段，请按网页当前要求填写。",
            selectable=True,
        )
        self._add_step(
            details_layout,
            5,
            "复制 api_id 和 api_hash",
            "api_id 是纯数字；api_hash 是较长字符串，分别粘贴到下方对应字段。",
        )
        common = QLabel(
            "常见问题：确认码在 Telegram 服务消息中；页面打不开时检查网络或代理；"
            "已经创建过 API 时返回 API development tools 查看现有凭据。"
            "Telegram 当前说明每个手机号只能关联一个 API ID。"
        )
        common.setObjectName("apiGuideHint")
        common.setWordWrap(True)
        details_layout.addWidget(common)
        warning = QLabel(
            "安全提醒：API Hash 与密码类似，请勿截图、分享或提交到公开网站。"
            "请遵守 Telegram API 条款，不用于刷量、垃圾消息或其他滥用行为。"
        )
        warning.setObjectName("apiGuideWarning")
        warning.setWordWrap(True)
        details_layout.addWidget(warning)

        self.fallback_widget = QWidget(self.details)
        fallback = QVBoxLayout(self.fallback_widget)
        fallback.setContentsMargins(0, 0, 0, 0)
        fallback_title = QLabel("无法打开系统浏览器，请复制官方网址后手动打开")
        fallback_title.setObjectName("apiGuideError")
        fallback_title.setWordWrap(True)
        fallback.addWidget(fallback_title)
        fallback_actions = QHBoxLayout()
        self.url_label = QLabel(API_PORTAL_URL)
        self.url_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        fallback_actions.addWidget(self.url_label, 1)
        self.copy_button = QPushButton("复制官方网址")
        self.copy_button.setAutoDefault(False)
        self.copy_button.setAccessibleName("复制 Telegram API 官方网址")
        self.copy_button.clicked.connect(self._copy_official_url)
        fallback_actions.addWidget(self.copy_button)
        fallback.addLayout(fallback_actions)
        self.status_label = QLabel()
        self.status_label.setObjectName("muted")
        self.status_label.setAccessibleName("Telegram API 官方网址操作状态")
        fallback.addWidget(self.status_label)
        self.fallback_widget.hide()
        details_layout.addWidget(self.fallback_widget)
        root.addWidget(self.details)
        self.set_expanded(True)

    @staticmethod
    def _copy_to_clipboard(text: str) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is None:
            raise RuntimeError("clipboard unavailable")
        clipboard.setText(text)

    @staticmethod
    def _add_step(
        layout: QVBoxLayout,
        number: int,
        title: str,
        body: str,
        *,
        selectable: bool = False,
    ) -> None:
        frame = QFrame()
        frame.setObjectName("apiGuideStep")
        row = QHBoxLayout(frame)
        row.setContentsMargins(9, 8, 9, 8)
        row.setSpacing(9)
        marker = QLabel(str(number))
        marker.setObjectName("apiGuideNumber")
        marker.setAlignment(Qt.AlignmentFlag.AlignCenter)
        marker.setFixedSize(25, 25)
        row.addWidget(marker, 0, Qt.AlignmentFlag.AlignTop)
        copy = QVBoxLayout()
        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        copy.addWidget(heading)
        description = QLabel(body)
        description.setObjectName("muted")
        description.setWordWrap(True)
        if selectable:
            description.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
        copy.addWidget(description)
        row.addLayout(copy, 1)
        layout.addWidget(frame)

    def set_credentials_present(self, present: bool) -> None:
        if not self._user_changed:
            self.set_expanded(not present)

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        self.details.setVisible(self._expanded)
        self.toggle_button.setText("收起指南" if self._expanded else "展开指南")
        self.toggle_button.setAccessibleName(
            "收起 Telegram API 获取指南"
            if self._expanded
            else "展开 Telegram API 获取指南"
        )

    def _toggle_from_user(self) -> None:
        self._user_changed = True
        self.set_expanded(not self._expanded)

    def _open_official_site(self) -> None:
        try:
            opened = bool(self._open_url(QUrl(API_PORTAL_URL)))
        except Exception:
            opened = False
        self.fallback_widget.setVisible(not opened)
        if opened:
            self.status_label.clear()

    def _copy_official_url(self) -> None:
        try:
            self._copy_text(API_PORTAL_URL)
        except Exception:
            self.status_label.setText("复制失败，请手动选择网址")
            return
        self.status_label.setText("官方网址已复制")
