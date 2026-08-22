from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialogButtonBox

from telegram_downloader.planner import BatchScanProgress
from telegram_downloader.ui.batch_import import BatchImportDialog
from telegram_downloader.ui.main import MainWindow


def test_batch_dialog_imports_txt_and_waits_for_preflight_result(
    qtbot,
    tmp_path: Path,
) -> None:
    text_file = tmp_path / "links.txt"
    text_file.write_text(
        "https://t.me/first_channel\nhttps://t.me/second_channel\n",
        encoding="utf-8",
    )
    dialog = BatchImportDialog()
    qtbot.addWidget(dialog)
    dialog.show()

    assert dialog.link_input.acceptDrops()
    dialog.import_text_files((text_file,))
    assert dialog.link_input.toPlainText().splitlines() == [
        "https://t.me/first_channel",
        "https://t.me/second_channel",
    ]

    with qtbot.waitSignal(dialog.submitted, timeout=500) as submitted:
        qtbot.mouseClick(
            dialog.buttons.button(QDialogButtonBox.StandardButton.Ok),
            Qt.MouseButton.LeftButton,
        )

    assert submitted.args[0] == (
        "https://t.me/first_channel",
        "https://t.me/second_channel",
    )
    assert dialog.isVisible()
    assert dialog.buttons.isEnabled() is False

    dialog.finish_preflight(False, "网络暂不可用")
    assert dialog.isVisible()
    assert dialog.buttons.isEnabled()
    assert "网络暂不可用" in dialog.error_label.text()
    assert "first_channel" in dialog.link_input.toPlainText()

    dialog.finish_preflight(True)
    assert dialog.isVisible() is False


def test_batch_dialog_keeps_valid_lines_when_some_links_are_invalid(qtbot) -> None:
    dialog = BatchImportDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.link_input.setPlainText("invalid\nhttps://t.me/valid_channel")

    with qtbot.waitSignal(dialog.submitted, timeout=500):
        qtbot.mouseClick(
            dialog.buttons.button(QDialogButtonBox.StandardButton.Ok),
            Qt.MouseButton.LeftButton,
        )

    assert "1 条无效" in dialog.summary_label.text()


def test_main_window_batch_entry_reports_progress_and_recovers_editor(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()

    qtbot.mouseClick(window.batch_button, Qt.MouseButton.LeftButton)
    editor = next(iter(window._batch_dialogs))
    editor.link_input.setPlainText("https://t.me/first_channel")
    with qtbot.waitSignal(window.batch_scan_requested, timeout=500):
        qtbot.mouseClick(
            editor.buttons.button(QDialogButtonBox.StandardButton.Ok),
            Qt.MouseButton.LeftButton,
        )

    window.set_scan_busy(True)
    window.set_batch_scan_progress(BatchScanProgress(1, 3))
    assert window.scan_button.isEnabled() is False
    assert window.batch_button.text() == "预检 1/3"

    window.finish_batch_preflight(False, "预检失败")
    window.set_scan_busy(False)
    assert editor.isVisible()
    assert editor.buttons.isEnabled()
    assert window.batch_button.isEnabled()

    with qtbot.waitSignal(window.batch_scan_requested, timeout=500):
        qtbot.mouseClick(
            editor.buttons.button(QDialogButtonBox.StandardButton.Ok),
            Qt.MouseButton.LeftButton,
        )
    window.finish_batch_preflight(True)
    assert editor.isVisible() is False
