from __future__ import annotations

from PySide6.QtCore import QAbstractItemModel, QEvent, QModelIndex, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem


class FullCellCheckDelegate(QStyledItemDelegate):
    def editorEvent(
        self,
        event: QEvent,
        model: QAbstractItemModel,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> bool:
        if not index.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            return False
        if event.type() == QEvent.Type.MouseButtonRelease:
            if not isinstance(event, QMouseEvent):
                return False
            if event.button() != Qt.MouseButton.LeftButton:
                return False
            if not option.rect.contains(event.position().toPoint()):
                return False
        elif event.type() == QEvent.Type.KeyPress:
            if not isinstance(event, QKeyEvent):
                return False
            if event.key() not in (Qt.Key.Key_Space, Qt.Key.Key_Select):
                return False
        else:
            return False

        current = Qt.CheckState(
            model.data(index, Qt.ItemDataRole.CheckStateRole)
        )
        requested = (
            Qt.CheckState.Unchecked
            if current == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
        return model.setData(
            index,
            requested,
            Qt.ItemDataRole.CheckStateRole,
        )
