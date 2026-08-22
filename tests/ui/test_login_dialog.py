from datetime import UTC, datetime, timedelta

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QLabel,
    QLineEdit,
)

from telegram_downloader.settings import ProxySettings
from telegram_downloader.ui.effects import ElevationLevel
from telegram_downloader.ui.login import LoginDialog, LoginPage
from telegram_downloader.ui.theme import APP_STYLESHEET


def test_login_pages_mask_sensitive_fields(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)

    assert dialog.styleSheet() == APP_STYLESHEET
    assert dialog.dialog_surface.objectName() == "dialogSurface"
    assert dialog.dialog_surface.property("elevation") == ElevationLevel.MAJOR.value
    assert isinstance(dialog.dialog_surface.graphicsEffect(), QGraphicsDropShadowEffect)
    assert dialog.api_hash.echoMode() is QLineEdit.EchoMode.Password
    assert dialog.proxy_password.echoMode() is QLineEdit.EchoMode.Password
    assert dialog.password.echoMode() is QLineEdit.EchoMode.Password
    dialog.show_page(LoginPage.CODE)
    assert dialog.stack.currentWidget() is dialog.code_page


def test_credentials_are_validated_then_emitted(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)
    dialog.api_id.setValue(12345)
    dialog.api_hash.setText("secret-hash")

    with qtbot.waitSignal(dialog.credentials_submitted, timeout=500) as signal:
        qtbot.mouseClick(dialog.credentials_next, Qt.MouseButton.LeftButton)

    assert signal.args[0:2] == [12345, "secret-hash"]
    assert signal.args[2].kind == "none"
    assert signal.args[3] == ""


def test_saved_credentials_and_proxy_are_prefilled_but_masked(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)
    proxy = ProxySettings("socks5", "127.0.0.1", 1080, "alice")

    dialog.set_saved_credentials(
        12345,
        "saved-hash",
        proxy,
        "saved-password",
    )

    assert dialog.api_id.value() == 12345
    assert dialog.api_hash.text() == "saved-hash"
    assert dialog.api_hash.echoMode() is QLineEdit.EchoMode.Password
    assert dialog.proxy_kind.currentData() == "socks5"
    assert dialog.proxy_host.text() == "127.0.0.1"
    assert dialog.proxy_port.value() == 1080
    assert dialog.proxy_username.text() == "alice"
    assert dialog.proxy_password.text() == "saved-password"
    assert dialog.proxy_password.echoMode() is QLineEdit.EchoMode.Password


def test_invalid_phone_stays_on_page_and_shows_error(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)
    dialog.show_page(LoginPage.PHONE)
    dialog.phone.setText("13800000000")

    qtbot.mouseClick(dialog.phone_next, Qt.MouseButton.LeftButton)

    assert dialog.stack.currentWidget() is dialog.phone_page
    assert "+" in dialog.error_label.text()


def test_ready_state_updates_account_label_and_clears_sensitive_values(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)
    dialog.api_hash.setText("hash")
    dialog.password.setText("password")

    dialog.show_ready("Test User")

    assert "Test User" in dialog.ready_label.text()
    assert dialog.api_hash.text() == ""
    assert dialog.password.text() == ""


def test_reset_authentication_clears_attempt_fields(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)
    dialog.phone.setText("+8613800000000")
    dialog.code.setText("12345")
    dialog.password.setText("secret")
    dialog.show_error("old error")

    dialog.reset_authentication()

    assert dialog.phone.text() == ""
    assert dialog.code.text() == ""
    assert dialog.password.text() == ""
    assert dialog.error_label.isHidden()


def test_qr_page_renders_in_memory_and_exposes_login_choices(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)
    expires_at = datetime.now(UTC) + timedelta(seconds=60)

    dialog.show_qr("tg://login?token=abc_123", expires_at)

    assert dialog.stack.currentWidget() is dialog.qr_page
    assert dialog.qr_image.pixmap().isNull() is False
    assert dialog.qr_countdown_timer.isActive() is True
    assert "秒" in dialog.qr_countdown.text()
    assert "tg://login" not in " ".join(
        label.text() for label in dialog.findChildren(QLabel)
    )

    with qtbot.waitSignal(dialog.qr_refresh_requested, timeout=500):
        qtbot.mouseClick(dialog.qr_refresh, Qt.MouseButton.LeftButton)
    with qtbot.waitSignal(dialog.phone_fallback_requested, timeout=500):
        qtbot.mouseClick(dialog.phone_fallback, Qt.MouseButton.LeftButton)
    with qtbot.waitSignal(dialog.credentials_edit_requested, timeout=500):
        qtbot.mouseClick(dialog.credentials_edit, Qt.MouseButton.LeftButton)


def test_qr_actions_show_scoped_busy_state_and_recover(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)

    dialog.set_action_busy("qr.refresh", True)
    assert dialog.qr_refresh.text() == "正在刷新…"
    assert dialog.qr_refresh.isEnabled() is False
    assert dialog.phone_fallback.isEnabled() is False
    assert dialog.credentials_edit.isEnabled() is False

    dialog.set_action_busy("qr.refresh", False)
    assert dialog.qr_refresh.text() == "刷新二维码"
    assert dialog.qr_refresh.isEnabled() is True
    assert dialog.phone_fallback.isEnabled() is True
    assert dialog.credentials_edit.isEnabled() is True


def test_qr_page_uses_fixed_viewport_and_preserves_complete_pixmap(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    expires_at = datetime.now(UTC) + timedelta(seconds=60)

    dialog.show_qr(f"tg://login?token={'a' * 43}", expires_at)
    QApplication.processEvents()

    pixmap = dialog.qr_image.pixmap()
    assert dialog.qr_image.size() == QSize(300, 300)
    assert pixmap.width() <= dialog.qr_image.width()
    assert pixmap.height() <= dialog.qr_image.height()
    assert dialog.qr_image.hasScaledContents() is False


def test_qr_state_is_cleared_on_page_switch_and_reject(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)
    expires_at = datetime.now(UTC) + timedelta(seconds=60)
    dialog.show_qr("tg://login?token=abc_123", expires_at)

    dialog.show_page(LoginPage.PHONE)

    assert dialog.qr_countdown_timer.isActive() is False
    assert dialog.qr_image.pixmap().isNull() is True

    dialog.show_qr("tg://login?token=xyz_789", expires_at)
    with qtbot.waitSignal(dialog.login_cancelled, timeout=500):
        dialog.reject()

    assert dialog.qr_countdown_timer.isActive() is False
    assert dialog.qr_image.pixmap().isNull() is True
