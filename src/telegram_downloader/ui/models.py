from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from telegram_downloader.domain import TaskStatus


@dataclass(frozen=True, slots=True)
class TaskSummary:
    id: str
    title: str
    status: TaskStatus
    progress_text: str
    size_text: str
    speed_text: str
    remaining_text: str


_STATUS_LABELS = {
    TaskStatus.DRAFT: "待确认",
    TaskStatus.SCANNING: "扫描中",
    TaskStatus.QUEUED: "等待中",
    TaskStatus.DOWNLOADING: "下载中",
    TaskStatus.PAUSED: "已暂停",
    TaskStatus.WAITING_RETRY: "等待重试",
    TaskStatus.COMPLETED: "已完成",
    TaskStatus.PARTIAL_FAILURE: "部分失败",
}

_STATUS_COLORS = {
    TaskStatus.DOWNLOADING: QColor("#67e8f9"),
    TaskStatus.COMPLETED: QColor("#5eead4"),
    TaskStatus.WAITING_RETRY: QColor("#fbbf24"),
    TaskStatus.PAUSED: QColor("#c4b5fd"),
    TaskStatus.PARTIAL_FAILURE: QColor("#fb923c"),
}
_INVALID_INDEX = QModelIndex()


class TaskTableModel(QAbstractTableModel):
    HEADERS = ("任务", "状态", "进度", "大小", "速度", "剩余时间")

    def __init__(self) -> None:
        super().__init__()
        self._tasks: list[TaskSummary] = []

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._tasks)

    def columnCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._tasks):
            return None
        task = self._tasks[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return task.id
        if role == Qt.ItemDataRole.DisplayRole:
            values = (
                task.title,
                _STATUS_LABELS[task.status],
                task.progress_text,
                task.size_text,
                task.speed_text,
                task.remaining_text,
            )
            return values[index.column()]
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 1:
            return _STATUS_COLORS.get(task.status)
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() > 0:
            return int(Qt.AlignmentFlag.AlignCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{task.title} · {_STATUS_LABELS[task.status]}"
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(self.HEADERS)
        ):
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def set_tasks(self, tasks: list[TaskSummary]) -> None:
        self.beginResetModel()
        self._tasks = list(tasks)
        self.endResetModel()

    def task_at(self, row: int) -> TaskSummary | None:
        return self._tasks[row] if 0 <= row < len(self._tasks) else None
