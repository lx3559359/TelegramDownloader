from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit

from telegram_downloader.ui.login import LoginDialog, LoginPage


def test_login_pages_mask_sensitive_fields(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)

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
