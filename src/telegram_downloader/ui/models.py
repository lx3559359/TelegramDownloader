from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from telegram_downloader.domain import (
    IntegrityStatus,
    ItemStatus,
    MediaKind,
    TaskStatus,
)


@dataclass(frozen=True, slots=True)
class TaskSummary:
    id: str
    title: str
    status: TaskStatus
    progress_text: str
    size_text: str
    speed_text: str
    remaining_text: str
    error_text: str
    completed_items: int = 0
    total_items: int = 0
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed_bps: float = 0.0
    remaining_seconds: int | None = None
    archived: bool = False
    queue_position: int | None = None


class TaskFilter(StrEnum):
    ALL = "all"
    ACTIVE = "active"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class TaskItemSummary:
    id: str
    name: str
    kind: MediaKind
    status: ItemStatus
    downloaded_bytes: int
    expected_size: int | None
    retry_count: int
    error_text: str
    integrity_status: IntegrityStatus = IntegrityStatus.UNVERIFIED
    verified_at: datetime | None = None


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

_ITEM_STATUS_LABELS = {
    ItemStatus.QUEUED: "等待中",
    ItemStatus.DOWNLOADING: "下载中",
    ItemStatus.PAUSED: "已暂停",
    ItemStatus.WAITING_RETRY: "等待重试",
    ItemStatus.COMPLETED: "已完成",
    ItemStatus.FAILED: "失败",
}

_ITEM_STATUS_COLORS = {
    ItemStatus.DOWNLOADING: QColor("#67e8f9"),
    ItemStatus.COMPLETED: QColor("#5eead4"),
    ItemStatus.WAITING_RETRY: QColor("#fbbf24"),
    ItemStatus.PAUSED: QColor("#c4b5fd"),
    ItemStatus.FAILED: QColor("#fb923c"),
}

_INTEGRITY_LABELS = {
    IntegrityStatus.UNVERIFIED: "未校验",
    IntegrityStatus.VERIFIED: "已校验",
    IntegrityStatus.MISSING: "文件缺失",
    IntegrityStatus.SIZE_MISMATCH: "大小异常",
    IntegrityStatus.HASH_MISMATCH: "哈希异常",
    IntegrityStatus.READ_ERROR: "无法读取",
}

_INTEGRITY_COLORS = {
    IntegrityStatus.VERIFIED: QColor("#5eead4"),
    IntegrityStatus.MISSING: QColor("#fb923c"),
    IntegrityStatus.SIZE_MISMATCH: QColor("#fb923c"),
    IntegrityStatus.HASH_MISMATCH: QColor("#fb923c"),
    IntegrityStatus.READ_ERROR: QColor("#fbbf24"),
}

_MEDIA_LABELS = {
    MediaKind.PHOTO: "图片",
    MediaKind.VIDEO: "视频",
    MediaKind.AUDIO: "音频",
    MediaKind.VOICE: "语音",
    MediaKind.DOCUMENT: "文档",
    MediaKind.ARCHIVE: "压缩包",
}
_INVALID_INDEX = QModelIndex()


class TaskTableModel(QAbstractTableModel):
    HEADERS = ("任务", "状态", "进度", "大小", "速度", "剩余时间", "错误")

    def __init__(self) -> None:
        super().__init__()
        self._all_tasks: list[TaskSummary] = []
        self._tasks: list[TaskSummary] = []
        self._filter = TaskFilter.ALL
        self._search = ""

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
                self._status_text(task),
                task.progress_text,
                task.size_text,
                task.speed_text,
                task.remaining_text,
                task.error_text,
            )
            return values[index.column()]
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 1:
            return _STATUS_COLORS.get(task.status)
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() > 0:
            return int(Qt.AlignmentFlag.AlignCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            summary = f"{task.title} · {self._status_text(task)}"
            return summary if task.error_text == "—" else f"{summary} · {task.error_text}"
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
        self._all_tasks = list(tasks)
        self._tasks = self._filtered_tasks()
        self.endResetModel()

    def set_filter(self, selected: TaskFilter, search: str = "") -> None:
        normalized = search.strip().casefold()
        if selected is self._filter and normalized == self._search:
            return
        self.beginResetModel()
        self._filter = selected
        self._search = normalized
        self._tasks = self._filtered_tasks()
        self.endResetModel()

    def filter_counts(self) -> dict[TaskFilter, int]:
        matching_search = [task for task in self._all_tasks if self._matches_search(task)]
        return {
            selected: sum(self._matches_filter(task, selected) for task in matching_search)
            for selected in TaskFilter
        }

    def task_at(self, row: int) -> TaskSummary | None:
        return self._tasks[row] if 0 <= row < len(self._tasks) else None

    def row_for_task_id(self, task_id: str) -> int | None:
        return next(
            (row for row, task in enumerate(self._tasks) if task.id == task_id),
            None,
        )

    @staticmethod
    def _status_text(task: TaskSummary) -> str:
        label = _STATUS_LABELS[task.status]
        if (
            task.status is TaskStatus.QUEUED
            and task.queue_position is not None
            and task.queue_position > 0
        ):
            return f"{label} · 第 {task.queue_position} 位"
        return label

    def _filtered_tasks(self) -> list[TaskSummary]:
        return [
            task
            for task in self._all_tasks
            if self._matches_search(task) and self._matches_filter(task, self._filter)
        ]

    def _matches_search(self, task: TaskSummary) -> bool:
        return not self._search or self._search in task.title.casefold()

    @staticmethod
    def _matches_filter(task: TaskSummary, selected: TaskFilter) -> bool:
        if selected is TaskFilter.ARCHIVED:
            return task.archived
        if task.archived:
            return False
        if selected is TaskFilter.ALL:
            return True
        if selected is TaskFilter.ACTIVE:
            return task.status in {
                TaskStatus.SCANNING,
                TaskStatus.QUEUED,
                TaskStatus.DOWNLOADING,
                TaskStatus.WAITING_RETRY,
            }
        if selected is TaskFilter.PAUSED:
            return task.status is TaskStatus.PAUSED
        if selected is TaskFilter.FAILED:
            return task.status is TaskStatus.PARTIAL_FAILURE
        if selected is TaskFilter.COMPLETED:
            return task.status is TaskStatus.COMPLETED
        return False


class TaskItemTableModel(QAbstractTableModel):
    HEADERS = ("文件", "类型", "状态", "完整性", "进度", "大小", "重试", "错误")

    def __init__(self) -> None:
        super().__init__()
        self._items: list[TaskItemSummary] = []

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return item.id
        if role == Qt.ItemDataRole.DisplayRole:
            values = (
                item.name,
                _MEDIA_LABELS[item.kind],
                _ITEM_STATUS_LABELS[item.status],
                _INTEGRITY_LABELS[item.integrity_status],
                self._progress_text(item),
                self._size_text(item),
                str(item.retry_count),
                item.error_text,
            )
            return values[index.column()]
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 2:
            return _ITEM_STATUS_COLORS.get(item.status)
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 3:
            return _INTEGRITY_COLORS.get(item.integrity_status)
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() > 0:
            return int(Qt.AlignmentFlag.AlignCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            summary = (
                f"{item.name} · {_ITEM_STATUS_LABELS[item.status]}"
                f" · {_INTEGRITY_LABELS[item.integrity_status]}"
            )
            if item.verified_at is not None:
                checked = item.verified_at.astimezone(UTC).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
                summary += f" · {checked}"
            return summary if item.error_text == "—" else f"{summary} · {item.error_text}"
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

    def set_items(self, items: list[TaskItemSummary]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def item_at(self, row: int) -> TaskItemSummary | None:
        return self._items[row] if 0 <= row < len(self._items) else None

    @staticmethod
    def _progress_text(item: TaskItemSummary) -> str:
        if item.status is ItemStatus.COMPLETED:
            return "100%"
        if item.expected_size is None or item.expected_size <= 0:
            return "—"
        progress = round(item.downloaded_bytes * 100 / item.expected_size)
        return f"{max(0, min(100, progress))}%"

    @classmethod
    def _size_text(cls, item: TaskItemSummary) -> str:
        downloaded = cls._format_bytes(item.downloaded_bytes)
        expected = (
            cls._format_bytes(item.expected_size) if item.expected_size is not None else "未知"
        )
        return f"{downloaded} / {expected}"

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(max(0, value))
        units = ("B", "KB", "MB", "GB", "TB")
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{round(amount)} B"
                return f"{amount:.1f} {unit}"
            amount /= 1024
        return "0 B"
