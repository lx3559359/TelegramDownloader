from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from telegram_downloader.content import SearchResult
from telegram_downloader.domain import MediaKind

_MEDIA_LABELS = {
    MediaKind.PHOTO: "图片",
    MediaKind.VIDEO: "视频",
    MediaKind.AUDIO: "音频",
    MediaKind.VOICE: "语音",
    MediaKind.DOCUMENT: "文档",
    MediaKind.ARCHIVE: "压缩包",
}


class MediaPreviewDialog(QDialog):
    def __init__(
        self,
        result: SearchResult,
        path: Path | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"媒体预览 · {result.original_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(780, 620)
        self._fit_to_window = True
        self._source_pixmap = self._load_pixmap(path)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self.preview_scroll = QScrollArea()
        self.preview_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_scroll.setWidgetResizable(True)
        self.preview_label = QLabel()
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(480, 320)
        self.preview_scroll.setWidget(self.preview_label)
        layout.addWidget(self.preview_scroll, 1)

        self.metadata_label = QLabel(self._metadata_text(result))
        self.metadata_label.setObjectName("muted")
        self.metadata_label.setWordWrap(True)
        layout.addWidget(self.metadata_label)

        actions = QHBoxLayout()
        actions.addStretch()
        self.size_button = QPushButton("原始尺寸")
        self.size_button.setCheckable(True)
        self.close_button = QPushButton("关闭")
        actions.addWidget(self.size_button)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)

        self.size_button.toggled.connect(self._toggle_size)
        self.close_button.clicked.connect(self.reject)
        layout.activate()
        self._render_preview(result)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._fit_to_window and self._source_pixmap is not None:
            self._apply_pixmap()

    def _toggle_size(self, original_size: bool) -> None:
        self._fit_to_window = not original_size
        self.size_button.setText("适应窗口" if original_size else "原始尺寸")
        self.preview_scroll.setWidgetResizable(not original_size)
        self._apply_pixmap()

    def _render_preview(self, result: SearchResult) -> None:
        if self._source_pixmap is None:
            self.preview_label.setText(
                f"暂无可用预览\n\n{_MEDIA_LABELS[result.media_kind]} · "
                f"{result.original_name}"
            )
            self.size_button.setEnabled(False)
            return
        self._apply_pixmap()

    def _apply_pixmap(self) -> None:
        source = self._source_pixmap
        if source is None:
            return
        if self._fit_to_window:
            viewport = self.preview_scroll.viewport().size()
            target = QSize(
                max(640, viewport.width() - 24),
                max(420, viewport.height() - 24),
            )
            pixmap = source.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        else:
            pixmap = source
            self.preview_label.resize(source.size())
        self.preview_label.setPixmap(pixmap)

    @staticmethod
    def _load_pixmap(path: Path | None) -> QPixmap | None:
        if path is None or not path.is_file():
            return None
        pixmap = QPixmap(str(path))
        return None if pixmap.isNull() else pixmap

    @classmethod
    def _metadata_text(cls, result: SearchResult) -> str:
        return (
            f"文件：{result.original_name}\n"
            f"类型：{_MEDIA_LABELS[result.media_kind]} · "
            f"大小：{cls._format_bytes(result.expected_size)} · "
            f"时间：{result.message_date_utc:%Y-%m-%d %H:%M:%S}\n"
            f"消息 ID：{result.message_id}"
        )

    @staticmethod
    def _format_bytes(value: int | None) -> str:
        if value is None:
            return "未知"
        amount = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if amount < 1024 or unit == "TB":
                return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
            amount /= 1024
        return f"{value} B"
