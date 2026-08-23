from __future__ import annotations

import importlib
import importlib.util

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel


def test_api_guide_module_exposes_only_official_portal_url() -> None:
    spec = importlib.util.find_spec("telegram_downloader.ui.api_guide")

    assert spec is not None
    module = importlib.import_module("telegram_downloader.ui.api_guide")
    assert module.API_PORTAL_URL == "https://my.telegram.org/apps"


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
    guide.show()

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
    guide.show()

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
    guide.show()
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
