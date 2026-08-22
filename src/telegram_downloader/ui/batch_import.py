from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from telegram_downloader.batch_import import parse_batch_links, read_batch_text_files
from telegram_downloader.ui.theme import APP_STYLESHEET, ensure_cjk_font


class BatchLinkTextEdit(QPlainTextEdit):
    text_files_dropped = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    @staticmethod
    def _text_paths(event: QDragEnterEvent | QDropEvent) -> tuple[Path, ...]:
        paths = tuple(
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile()
        )
        if paths and all(path.suffix.casefold() == ".txt" for path in paths):
            return paths
        return ()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if self._text_paths(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        paths = self._text_paths(event)
        if paths:
            self.text_files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class BatchImportDialog(QDialog):
    submitted = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        ensure_cjk_font()
        self.setStyleSheet(APP_STYLESHEET)
        self.setWindowTitle("批量导入 Telegram 链接")
        self.setMinimumSize(640, 430)

        layout = QVBoxLayout(self)
        title = QLabel("每行粘贴一条 t.me 链接，或拖入一个或多个 TXT 文件")
        title.setWordWrap(True)
        layout.addWidget(title)

        self.link_input = BatchLinkTextEdit()
        self.link_input.setPlaceholderText("https://t.me/channel/123\nhttps://t.me/c/123456/789")
        layout.addWidget(self.link_input, 1)

        self.import_button = QPushButton("选择 TXT 文件…")
        layout.addWidget(self.import_button)
        self.summary_label = QLabel("最多 100 条非空链接，TXT 合计不超过 1 MiB")
        self.summary_label.setObjectName("muted")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        self.error_label = QLabel("")
        self.error_label.setObjectName("errorBanner")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("批量预检")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        layout.addWidget(self.buttons)

        self.import_button.clicked.connect(self._choose_text_files)
        self.link_input.text_files_dropped.connect(self.import_text_files)
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)

    def import_text_files(self, paths: tuple[Path, ...]) -> None:
        try:
            imported = read_batch_text_files(paths)
        except ValueError as error:
            self._show_error(str(error))
            return
        current = self.link_input.toPlainText().rstrip("\r\n")
        combined = "\n".join(part for part in (current, imported) if part)
        self.link_input.setPlainText(combined)
        self.error_label.hide()

    def finish_preflight(self, success: bool, error: str = "") -> None:
        if success:
            self.accept()
            return
        self.link_input.setEnabled(True)
        self.import_button.setEnabled(True)
        self.buttons.setEnabled(True)
        self._show_error(error or "批量预检失败，请稍后重试")

    def _choose_text_files(self) -> None:
        names, _selected = QFileDialog.getOpenFileNames(
            self,
            "选择链接 TXT 文件",
            "",
            "文本文件 (*.txt)",
        )
        if names:
            self.import_text_files(tuple(Path(name) for name in names))

    def _submit(self) -> None:
        lines = tuple(self.link_input.toPlainText().splitlines())
        try:
            parsed = parse_batch_links(lines)
        except ValueError as error:
            self._show_error(str(error))
            return
        details = [
            f"输入 {parsed.input_count} 条",
            f"有效唯一 {len(parsed.links)} 条",
            f"重复 {parsed.duplicate_count} 条",
            f"{len(parsed.issues)} 条无效",
        ]
        self.summary_label.setText(" · ".join(details))
        self.error_label.hide()
        self.link_input.setEnabled(False)
        self.import_button.setEnabled(False)
        self.buttons.setEnabled(False)
        self.submitted.emit(lines)

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()
