from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit

from telegram_downloader.settings import AppSettings, ProxySettings
from telegram_downloader.ui.settings import SettingsDialog


def test_round_trip_manual_proxy_form(qtbot) -> None:
    settings = AppSettings(
        123,
        4,
        ProxySettings("http", "127.0.0.1", 8080, "u"),
        False,
    )
    dialog = SettingsDialog(settings, proxy_password="secret")
    qtbot.addWidget(dialog)

    assert dialog.values() == settings
    assert dialog.concurrency.minimum() == 1
    assert dialog.concurrency.maximum() == 5
    assert dialog.proxy_password.echoMode() is QLineEdit.EchoMode.Password
    assert dialog.proxy_password.text() == "secret"


def test_proxy_test_emits_without_accepting_dialog(qtbot) -> None:
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)
    dialog.proxy_kind.setCurrentIndex(dialog.proxy_kind.findData("socks5"))
    dialog.proxy_host.setText("127.0.0.1")
    dialog.proxy_port.setValue(1080)

    with qtbot.waitSignal(dialog.test_proxy_requested, timeout=500) as signal:
        qtbot.mouseClick(dialog.test_button, Qt.MouseButton.LeftButton)

    assert signal.args[0] == ProxySettings("socks5", "127.0.0.1", 1080)
    assert dialog.result() == 0


def test_invalid_proxy_shows_error_and_does_not_accept(qtbot) -> None:
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)
    dialog.proxy_kind.setCurrentIndex(dialog.proxy_kind.findData("http"))
    dialog.proxy_host.clear()
    dialog.proxy_port.setValue(8080)

    qtbot.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)

    assert dialog.result() == 0
    assert "代理" in dialog.error_label.text()
