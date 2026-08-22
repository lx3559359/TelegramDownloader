from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem, QWidget


class WrappedSummaryDelegate(QStyledItemDelegate):
    """Measure and paint complete, wrapped result summaries."""

    MINIMUM_HEIGHT = 78
    HORIZONTAL_PADDING = 16
    VERTICAL_PADDING = 16

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size_cache: dict[tuple[object, int, str, str], QSize] = {}
        self.measurement_count = 0

    def clear_cache(self) -> None:
        self._size_cache.clear()

    def initStyleOption(
        self,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        super().initStyleOption(option, index)
        option.features |= QStyleOptionViewItem.ViewItemFeature.WrapText
        option.textElideMode = Qt.TextElideMode.ElideNone

    def sizeHint(
        self,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> QSize:
        prepared = QStyleOptionViewItem(option)
        self.initStyleOption(prepared, index)
        width = max(1, prepared.rect.width())
        identity = index.data(Qt.ItemDataRole.UserRole)
        text = prepared.text
        key = (identity, width, prepared.font.toString(), text)
        cached = self._size_cache.get(key)
        if cached is not None:
            return cached

        content_width = max(1, width - self.HORIZONTAL_PADDING)
        flags = Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextWrapAnywhere | Qt.TextFlag.TextExpandTabs
        bounds = QFontMetrics(prepared.font).boundingRect(
            QRect(0, 0, content_width, 1_000_000),
            flags,
            text,
        )
        size = QSize(
            width,
            max(self.MINIMUM_HEIGHT, bounds.height() + self.VERTICAL_PADDING),
        )
        self._size_cache[key] = size
        self.measurement_count += 1
        return size
