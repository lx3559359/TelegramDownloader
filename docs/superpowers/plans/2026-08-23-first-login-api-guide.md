# TG 快取首次登录 API 获取指南 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在首次登录凭据页增加默认展开、可折叠的 Telegram API 获取指南，并安全调用系统浏览器打开唯一官方申请地址。

**Architecture:** 新建独立 `ApiCredentialGuide` Qt 组件，封装静态教程、折叠状态、系统浏览器与复制网址的可替换边界；`LoginDialog` 只组合该组件、根据凭据完整性设置初始状态，并把凭据表单放入仅纵向滚动的区域。现有登录信号、控制器、Telegram 网关和 DPAPI 存储协议保持不变。

**Tech Stack:** Python 3.12、PySide6、pytest、pytest-qt、Ruff、现有 PyInstaller/Inno Setup 构建脚本

---

## 文件职责

- Create: `src/telegram_downloader/ui/api_guide.py` — 指南内容、折叠状态、官方网址打开和复制失败反馈。
- Create: `tests/ui/test_api_guide.py` — 指南行为、安全边界、主题合同与可访问性测试。
- Modify: `src/telegram_downloader/ui/login.py` — 凭据页组合指南、纵向滚动和已有凭据初始状态。
- Modify: `tests/ui/test_login_dialog.py` — 登录集成、字段保留、滚动和回车提交回归测试。
- Modify: `src/telegram_downloader/ui/theme.py` — 指南卡片、编号、安全提醒和局部错误样式。
- Modify: `README.md` — 同步详细申请步骤与常见问题。
- Modify: `tests/test_packaging_contract.py` — 锁定中文指南关键内容。

## Task 1: 建立指南模块和官方 URL 合同

**Files:**
- Create: `tests/ui/test_api_guide.py`
- Create: `src/telegram_downloader/ui/api_guide.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/ui/test_api_guide.py`：

```python
from __future__ import annotations

import importlib
import importlib.util


def test_api_guide_module_exposes_only_official_portal_url() -> None:
    spec = importlib.util.find_spec("telegram_downloader.ui.api_guide")

    assert spec is not None
    module = importlib.import_module("telegram_downloader.ui.api_guide")
    assert module.API_PORTAL_URL == "https://my.telegram.org/apps"
```

- [ ] **Step 2: 运行并观察正确的 RED**

```powershell
& '.venv\Scripts\python.exe' -m pytest -q tests/ui/test_api_guide.py
```

Expected: FAIL at `assert spec is not None`; no import-time side effect occurs.

- [ ] **Step 3: 添加最小生产代码**

创建 `src/telegram_downloader/ui/api_guide.py`：

```python
from __future__ import annotations

API_PORTAL_URL = "https://my.telegram.org/apps"
```

- [ ] **Step 4: 运行并观察 GREEN**

```powershell
& '.venv\Scripts\python.exe' -m pytest -q tests/ui/test_api_guide.py
```

Expected: `1 passed`.

- [ ] **Step 5: 提交**

```powershell
git add -- src/telegram_downloader/ui/api_guide.py tests/ui/test_api_guide.py
git commit -m "test: establish API guide portal contract"
```

## Task 2: 测试驱动实现可折叠指南组件

**Files:**
- Modify: `tests/ui/test_api_guide.py`
- Modify: `src/telegram_downloader/ui/api_guide.py`

- [ ] **Step 1: 添加组件行为失败测试**

在 `tests/ui/test_api_guide.py` 的标准库导入后加入：

```python
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel
```

并追加：

```python
def _guide_type():
    module = importlib.import_module("telegram_downloader.ui.api_guide")
    guide_type = getattr(module, "ApiCredentialGuide", None)
    assert guide_type is not None
    return module, guide_type


def _all_label_text(guide) -> str:
    return "\n".join(label.text() for label in guide.findChildren(QLabel))


def test_guide_defaults_expanded_and_contains_complete_process(qtbot) -> None:
    _module, guide_type = _guide_type()
    guide = guide_type(open_url=lambda _url: True, copy_text=lambda _text: None)
    qtbot.addWidget(guide)
    guide.show()

    assert guide.is_expanded() is True
    text = _all_label_text(guide)
    for required in (
        "如何获取 API ID / Hash",
        "准备 Telegram 账号",
        "系统默认浏览器",
        "国际格式",
        "确认码会发送到 Telegram 消息，而不是短信",
        "API development tools",
        "TG Quick Fetch Personal",
        "tgquickfetch",
        "Desktop",
        "Personal media download manager",
        "api_id 是纯数字",
        "每个手机号只能关联一个 API ID",
        "API Hash 与密码类似",
        "垃圾消息",
    ):
        assert required in text


def test_complete_credentials_collapse_until_user_overrides(qtbot) -> None:
    _module, guide_type = _guide_type()
    guide = guide_type(open_url=lambda _url: True, copy_text=lambda _text: None)
    qtbot.addWidget(guide)

    guide.set_credentials_present(True)
    assert guide.is_expanded() is False
    assert guide.toggle_button.text() == "展开指南"

    qtbot.mouseClick(guide.toggle_button, Qt.MouseButton.LeftButton)
    assert guide.is_expanded() is True
    guide.set_credentials_present(True)
    assert guide.is_expanded() is True


def test_open_button_uses_only_official_https_url(qtbot) -> None:
    module, guide_type = _guide_type()
    opened: list[str] = []
    guide = guide_type(
        open_url=lambda url: opened.append(url.toString()) or True,
        copy_text=lambda _text: None,
    )
    qtbot.addWidget(guide)

    qtbot.mouseClick(guide.open_button, Qt.MouseButton.LeftButton)

    assert opened == [module.API_PORTAL_URL]
    assert guide.fallback_widget.isHidden() is True


def _raise_open(_url) -> bool:
    raise RuntimeError("browser unavailable")


@pytest.mark.parametrize("open_url", (lambda _url: False, _raise_open))
def test_open_failure_exposes_selectable_copy_fallback(qtbot, open_url) -> None:
    module, guide_type = _guide_type()
    copied: list[str] = []
    guide = guide_type(open_url=open_url, copy_text=copied.append)
    qtbot.addWidget(guide)

    qtbot.mouseClick(guide.open_button, Qt.MouseButton.LeftButton)
    assert guide.fallback_widget.isHidden() is False
    assert guide.url_label.text() == module.API_PORTAL_URL
    assert guide.url_label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse

    qtbot.mouseClick(guide.copy_button, Qt.MouseButton.LeftButton)
    assert copied == [module.API_PORTAL_URL]
    assert guide.status_label.text() == "官方网址已复制"


def test_copy_failure_keeps_manual_url_available(qtbot) -> None:
    module, guide_type = _guide_type()

    def fail_copy(_text: str) -> None:
        raise RuntimeError("clipboard unavailable")

    guide = guide_type(open_url=lambda _url: False, copy_text=fail_copy)
    qtbot.addWidget(guide)
    qtbot.mouseClick(guide.open_button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(guide.copy_button, Qt.MouseButton.LeftButton)

    assert guide.url_label.text() == module.API_PORTAL_URL
    assert guide.status_label.text() == "复制失败，请手动选择网址"


def test_collapsed_details_and_accessible_actions(qtbot) -> None:
    _module, guide_type = _guide_type()
    guide = guide_type(open_url=lambda _url: True, copy_text=lambda _text: None)
    qtbot.addWidget(guide)
    guide.show()

    guide.set_credentials_present(True)
    assert guide.details.isHidden() is True
    assert guide.open_button.isVisible() is False
    assert guide.toggle_button.accessibleName() == "展开 Telegram API 获取指南"

    guide.set_expanded(True)
    assert guide.toggle_button.accessibleName() == "收起 Telegram API 获取指南"
    assert guide.open_button.accessibleName() == "在系统浏览器打开 Telegram API 申请页面"
    assert guide.status_label.accessibleName() == "Telegram API 官方网址操作状态"


def test_guide_exposes_theme_object_names(qtbot) -> None:
    _module, guide_type = _guide_type()
    guide = guide_type(open_url=lambda _url: True, copy_text=lambda _text: None)
    qtbot.addWidget(guide)

    assert guide.objectName() == "apiGuide"
    assert len(guide.findChildren(QFrame, "apiGuideStep")) == 5
    assert len(guide.findChildren(QLabel, "apiGuideNumber")) == 5
    assert len(guide.findChildren(QLabel, "apiGuideWarning")) == 1
```

- [ ] **Step 2: 运行并观察正确的 RED**

```powershell
& '.venv\Scripts\python.exe' -m pytest -q tests/ui/test_api_guide.py
```

Expected: URL contract passes; eight new test cases fail at `guide_type is not None`.

- [ ] **Step 3: 实现完整指南组件**

用以下内容替换 `src/telegram_downloader/ui/api_guide.py`：

```python
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
```

- [ ] **Step 4: 运行组件测试、Ruff 并提交**

```powershell
& '.venv\Scripts\python.exe' -m pytest -q tests/ui/test_api_guide.py
& '.venv\Scripts\python.exe' -m ruff check src/telegram_downloader/ui/api_guide.py tests/ui/test_api_guide.py
git add -- src/telegram_downloader/ui/api_guide.py tests/ui/test_api_guide.py
git commit -m "feat: add first-login API guide component"
```

Expected: component tests pass; Ruff prints `All checks passed!`.

## Task 3: 集成登录凭据页并保持现有契约

**Files:**
- Modify: `tests/ui/test_login_dialog.py`
- Modify: `src/telegram_downloader/ui/login.py`

- [ ] **Step 1: 添加登录集成失败测试**

在 `tests/ui/test_login_dialog.py` 的导入中加入：

```python
import pytest
from PySide6.QtWidgets import QScrollArea
```

并追加：

```python
@pytest.mark.parametrize(
    ("api_id", "api_hash", "expanded"),
    ((0, "", True), (12345, "", True), (0, "saved-hash", True), (12345, "saved-hash", False)),
)
def test_api_guide_initial_state_requires_complete_credentials(
    qtbot, api_id: int, api_hash: str, expanded: bool
) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)

    dialog.set_saved_credentials(api_id, api_hash, ProxySettings(), "")

    assert dialog.api_guide.is_expanded() is expanded


def test_user_guide_choice_and_values_survive_repeated_prefill(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)
    dialog.set_saved_credentials(12345, "saved-hash", ProxySettings(), "")
    qtbot.mouseClick(dialog.api_guide.toggle_button, Qt.MouseButton.LeftButton)
    dialog.set_saved_credentials(67890, "edited-hash", ProxySettings(), "")

    assert dialog.api_guide.is_expanded() is True
    assert dialog.api_id.value() == 67890
    assert dialog.api_hash.text() == "edited-hash"


def test_credentials_page_uses_vertical_only_scroll_area(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    QApplication.processEvents()

    assert isinstance(dialog.credentials_scroll, QScrollArea)
    assert dialog.credentials_scroll.widgetResizable() is True
    assert dialog.credentials_scroll.maximumHeight() == 500
    assert dialog.minimumWidth() == 600
    assert (
        dialog.credentials_scroll.horizontalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    assert dialog.credentials_scroll.horizontalScrollBar().maximum() == 0


def test_enter_still_submits_credentials_after_guide_integration(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    QApplication.processEvents()
    dialog.api_id.setValue(12345)
    dialog.api_hash.setText("secret-hash")
    dialog.api_hash.setFocus()

    with qtbot.waitSignal(dialog.credentials_submitted, timeout=500) as signal:
        qtbot.keyPress(dialog.api_hash, Qt.Key.Key_Return)

    assert signal.args[0:2] == [12345, "secret-hash"]


def test_expanded_guide_dialog_fits_screen_and_submit_is_reachable(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    QApplication.processEvents()
    available = dialog.screen().availableGeometry()

    assert dialog.frameGeometry().width() <= available.width()
    assert dialog.frameGeometry().height() <= available.height()
    dialog.credentials_scroll.ensureWidgetVisible(dialog.credentials_next)
    QApplication.processEvents()
    assert dialog.credentials_next.isVisible() is True
```

- [ ] **Step 2: 运行并观察正确的 RED**

```powershell
& '.venv\Scripts\python.exe' -m pytest -q tests/ui/test_login_dialog.py
```

Expected: new tests fail because `api_guide` and `credentials_scroll` are absent and Return does not submit.

- [ ] **Step 3: 修改登录页**

在 `src/telegram_downloader/ui/login.py`：

- 将 `QScrollArea` 加入 `PySide6.QtWidgets` 导入。
- 加入 `from telegram_downloader.ui.api_guide import ApiCredentialGuide`。
- 把 `self.setMinimumWidth(520)` 改为 `self.setMinimumWidth(600)`。
- 用下面的完整方法替换 `_build_credentials_page()`：

```python
def _build_credentials_page(self) -> QWidget:
    page = QWidget()
    page_layout = QVBoxLayout(page)
    page_layout.setContentsMargins(0, 0, 0, 0)
    self.credentials_scroll = QScrollArea(page)
    self.credentials_scroll.setFrameShape(QFrame.Shape.NoFrame)
    self.credentials_scroll.setWidgetResizable(True)
    self.credentials_scroll.setHorizontalScrollBarPolicy(
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    self.credentials_scroll.setMinimumHeight(360)
    self.credentials_scroll.setMaximumHeight(500)

    content = QWidget()
    layout = QVBoxLayout(content)
    layout.setContentsMargins(0, 8, 8, 0)
    layout.setSpacing(12)
    layout.addWidget(self._step_label("步骤 1 / 4 · API 凭据与网络"))
    self.api_guide = ApiCredentialGuide(content)
    layout.addWidget(self.api_guide)

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

    self.credentials_next = QPushButton("保存并生成二维码")
    self.credentials_next.setObjectName("primaryButton")
    self.credentials_next.setDefault(True)
    self.credentials_next.clicked.connect(self._submit_credentials)
    layout.addWidget(self.credentials_next, 0, Qt.AlignmentFlag.AlignRight)
    self.proxy_kind.currentIndexChanged.connect(self._update_proxy_fields)
    self._update_proxy_fields()
    self.credentials_scroll.setWidget(content)
    page_layout.addWidget(self.credentials_scroll)
    return page
```

在 `set_saved_credentials()` 的 `self._update_proxy_fields()` 后追加：

```python
self.api_guide.set_credentials_present(api_id > 0 and bool(api_hash.strip()))
```

- [ ] **Step 4: 运行登录与指南测试、Ruff 并提交**

```powershell
& '.venv\Scripts\python.exe' -m pytest -q tests/ui/test_api_guide.py tests/ui/test_login_dialog.py
& '.venv\Scripts\python.exe' -m ruff check src/telegram_downloader/ui/login.py tests/ui/test_login_dialog.py
git add -- src/telegram_downloader/ui/login.py tests/ui/test_login_dialog.py
git commit -m "feat: embed API guide in first-login flow"
```

Expected: 原有和新增登录测试全部通过；Ruff 通过。

## Task 4: 添加 TG 快取主题样式

**Files:**
- Modify: `tests/ui/test_api_guide.py`
- Modify: `src/telegram_downloader/ui/theme.py`

- [ ] **Step 1: 写主题失败测试**

在 `tests/ui/test_api_guide.py` 导入并追加：

```python
from telegram_downloader.ui.theme import APP_STYLESHEET


def test_theme_styles_api_guide_objects() -> None:
    for selector in (
        "QFrame#apiGuide",
        "QFrame#apiGuideStep",
        "QLabel#apiGuideNumber",
        "QLabel#apiGuideHint",
        "QLabel#apiGuideWarning",
        "QLabel#apiGuideError",
    ):
        assert selector in APP_STYLESHEET
```

- [ ] **Step 2: 运行并观察正确的 RED**

```powershell
& '.venv\Scripts\python.exe' -m pytest -q tests/ui/test_api_guide.py::test_theme_styles_api_guide_objects
```

Expected: FAIL because the selectors are absent.

- [ ] **Step 3: 添加样式**

在 `src/telegram_downloader/ui/theme.py` 的卡片规则后加入：

```css
QFrame#apiGuide {
    border: 1px solid #A9D9E4; border-radius: 11px; background: #F2FAFC;
}
QFrame#apiGuideStep {
    border: 1px solid #D8E7ED; border-radius: 8px; background: #FFFFFF;
}
QLabel#apiGuideNumber {
    border: none; border-radius: 12px; background: #17A8C2;
    color: #FFFFFF; font-weight: 750;
}
QLabel#apiGuideHint {
    padding: 8px 10px; border: 1px solid #C7DDE7; border-radius: 7px;
    background: #EDF8FB; color: #376578;
}
QLabel#apiGuideWarning {
    padding: 8px 10px; border: 1px solid #E7C878; border-radius: 7px;
    background: #FFF8E5; color: #76551A;
}
QLabel#apiGuideError {
    padding: 8px 10px; border: 1px solid #E7A8B3; border-radius: 7px;
    background: #FFF0F3; color: #A33C50;
}
```

- [ ] **Step 4: 在 100% 和 125% 缩放运行测试并提交**

```powershell
$env:QT_SCALE_FACTOR='1'
& '.venv\Scripts\python.exe' -m pytest -q tests/ui/test_api_guide.py tests/ui/test_login_dialog.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$env:QT_SCALE_FACTOR='1.25'
& '.venv\Scripts\python.exe' -m pytest -q tests/ui/test_api_guide.py tests/ui/test_login_dialog.py
$result=$LASTEXITCODE
Remove-Item Env:QT_SCALE_FACTOR
if ($result -ne 0) { exit $result }
& '.venv\Scripts\python.exe' -m ruff check src/telegram_downloader/ui tests/ui
git add -- src/telegram_downloader/ui/theme.py tests/ui/test_api_guide.py
git commit -m "style: polish responsive API guidance"
```

Expected: 两种缩放均通过，无横向溢出或屏幕越界；Ruff 通过。

## Task 5: 同步 README 并锁定用户指南合同

**Files:**
- Modify: `tests/test_packaging_contract.py`
- Modify: `README.md`

- [ ] **Step 1: 先扩展合同测试**

在 `test_chinese_guide_documents_portable_data_and_security()` 的 `required` 元组中加入：

```python
"API development tools",
"确认码会发送到 Telegram 消息，而不是短信",
"TG Quick Fetch Personal",
"Short name",
"系统默认浏览器",
"每个手机号只能关联一个 API ID",
"API Hash 与密码类似",
```

- [ ] **Step 2: 运行并观察正确的 RED**

```powershell
& '.venv\Scripts\python.exe' -m pytest -q tests/test_packaging_contract.py::test_chinese_guide_documents_portable_data_and_security
```

Expected: FAIL because README lacks one or more required phrases.

- [ ] **Step 3: 扩写 README 登录步骤**

用下列内容替换 `README.md` 的现有 Telegram 登录 1–6 步，同时保留其后的会话恢复与 DPAPI 说明：

```markdown
首次登录窗口会默认展开完整指南；已有凭据时默认折叠，但始终可以点击“如何获取 API ID / Hash”重新查看。申请页面只通过系统默认浏览器打开，本程序不内嵌网页，也不会读取官网中的手机号或确认码。

1. 先在官方 Telegram App 注册并保持登录，准备当前有效手机号。
2. 点击登录窗口中的按钮，或在浏览器打开 [my.telegram.org/apps](https://my.telegram.org/apps)。
3. 手机号使用国际格式登录，例如中国大陆号码以 `+86` 开头。确认码会发送到 Telegram 消息，而不是短信。
4. 登录后进入 **API development tools**。如果账号已创建过应用，直接查看现有凭据；Telegram 当前说明每个手机号只能关联一个 API ID。
5. 首次创建时可以参考：App title 填 `TG Quick Fetch Personal`，Short name 填 `tgquickfetch`，Platform 选择 `Desktop`，Description 填 `Personal media download manager`。官网若显示其他字段，以网页当前要求为准。
6. 复制纯数字 `api_id` 和较长字符串 `api_hash`，分别粘贴到程序的 API ID、API Hash 输入框。
7. 点击“保存并生成二维码”，再打开 Telegram App，进入 **设置 → 设备 → 连接桌面设备** 扫码确认；启用两步验证的账号还需输入 Telegram 云密码。

常见问题：收不到官网确认码时检查 Telegram 服务消息，不要只等待短信；页面打不开时检查网络或代理；已经创建过 API 时不要反复创建，返回 **API development tools** 查看即可。API Hash 与密码类似，请勿截图、分享或提交到公开网站。请遵守 Telegram API 条款，不得用于刷量、垃圾消息或其他滥用行为。
```

- [ ] **Step 4: 运行合同测试并提交**

```powershell
& '.venv\Scripts\python.exe' -m pytest -q tests/test_packaging_contract.py::test_chinese_guide_documents_portable_data_and_security
git add -- README.md tests/test_packaging_contract.py
git commit -m "docs: expand Telegram API onboarding guide"
```

Expected: targeted packaging contract passes.

## Task 6: 三轮验证与冻结程序验收

**Files:**
- Verify only. 若发现缺陷，必须先增加能复现问题的失败测试，再修改生产代码并重跑本任务。

- [ ] **Step 1: 第一轮——定向功能回归**

```powershell
& '.venv\Scripts\python.exe' -m pytest -q tests/ui/test_api_guide.py tests/ui/test_login_dialog.py tests/test_account_access.py tests/test_controller.py
& '.venv\Scripts\python.exe' -m ruff check src tests scripts
```

Expected: focused tests and Ruff all pass.

- [ ] **Step 2: 第二轮——完整源码门禁**

```powershell
& '.\scripts\test.ps1'
```

Expected: full pytest、Ruff、编译与资源检查全部通过，退出码 0。

- [ ] **Step 3: 第三轮——打包与冻结运行时**

先确认项目目录内没有活动下载，再执行：

```powershell
& '.\scripts\build.ps1'
```

Expected: build succeeds and prints `PACKAGED_SMOKE_OK`; portable ZIP excludes user `data` and `downloads`.

- [ ] **Step 4: 100%/125% 人工视觉检查**

使用隔离空配置打开 `dist\TelegramDownloader\TelegramDownloader.exe`，分别检查：

- 无凭据时默认展开，五步、示例、安全提醒和常见问题完整可读。
- 只有纵向滚动，能到达 API、代理和提交按钮。
- 折叠、再次展开和打开官网都不改变输入值。
- 系统浏览器只收到 `https://my.telegram.org/apps`；失败回退显示可选择、可复制网址。
- 不输入、截图、打印或记录真实 API ID、API Hash、手机号和确认码。

- [ ] **Step 5: 最终仓库检查**

```powershell
git diff --check
git status --short
git log --oneline -6
```

Expected: diff check has no output; only intentional changes exist；最近提交对应本计划的五个实现提交。

## 完成定义

- 无 API 凭据时默认展开详细指南；完整凭据时默认折叠且始终可重新打开。
- 系统浏览器只接收 `https://my.telegram.org/apps`；失败时提供可选择、可复制的备用网址。
- API/代理输入、凭据提交、二维码、手机号、验证码、两步验证和安全重新登录合同保持通过。
- 100% 与 125% 缩放下无横向滚动、裁切或屏幕溢出。
- README 与应用内指南一致。
- 三轮验证、Ruff、完整测试和冻结程序冒烟全部通过，不读取或泄露真实 Telegram 凭据。
