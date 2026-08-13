from PySide6.QtCore import Qt

from telegram_downloader.ui.update_dialog import UpdateDialog
from tests.update.test_update_contract import manifest_value, signed_manifest


def manifest():
    content, signature, keys = signed_manifest(manifest_value("0.2.0"))
    from telegram_downloader.update_contract import verify_manifest

    return verify_manifest(content, signature, keys).manifest


def test_update_dialog_shows_signed_release_details(qtbot) -> None:
    dialog = UpdateDialog(manifest())
    qtbot.addWidget(dialog)

    assert "0.2.0" in dialog.version_label.text()
    assert "10 B" in dialog.size_label.text()
    assert "首个在线更新测试版本" in dialog.notes.toPlainText()


def test_update_dialog_requires_explicit_acceptance(qtbot) -> None:
    dialog = UpdateDialog(manifest())
    qtbot.addWidget(dialog)

    qtbot.mouseClick(dialog.cancel_button, Qt.MouseButton.LeftButton)
    assert dialog.result() == 0

    dialog = UpdateDialog(manifest())
    qtbot.addWidget(dialog)
    qtbot.mouseClick(dialog.update_button, Qt.MouseButton.LeftButton)
    assert dialog.result() == 1
