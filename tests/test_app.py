from PySide6.QtWidgets import QMessageBox

from telegram_downloader import app


def test_standard_button_selection_accepts_pyside_integer_result() -> None:
    yes = QMessageBox.StandardButton.Yes

    assert app._standard_button_selected(yes.value, yes) is True
