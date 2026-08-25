from __future__ import annotations

from bisect import bisect_left
from collections.abc import Collection, Mapping, Sequence
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
        self._all_by_id: dict[str, TaskSummary] = {}
        self._row_by_id: dict[str, int] = {}
        self._normalized_titles: dict[str, str] = {}
        self._order_keys: dict[str, tuple[float, str]] = {}
        self._filter_counts = {selected: 0 for selected in TaskFilter}
        self._filter = TaskFilter.ALL
        self._search = ""
        self._initialized = False

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
        order_keys = {
            task.id: (float(row), task.id) for row, task in enumerate(tasks)
        }
        self.apply_snapshot(tasks, order_keys)

    def apply_snapshot(
        self,
        tasks: Sequence[TaskSummary],
        order_keys: Mapping[str, tuple[float, str]],
    ) -> None:
        target = tuple(tasks)
        self._validate_unique(target)
        self._require_order_keys(target, order_keys)
        if not self._initialized:
            self.beginResetModel()
            self._all_by_id = {task.id: task for task in target}
            self._order_keys = {
                task.id: order_keys[task.id] for task in target
            }
            self._normalized_titles = {
                task.id: task.title.casefold() for task in target
            }
            self._rebuild_all_tasks()
            self._tasks, self._filter_counts = self._compute_filter_state()
            self._rebuild_row_index()
            self._initialized = True
            self.endResetModel()
            return
        removed_ids = set(self._all_by_id) - {task.id for task in target}
        self.apply_tasks(target, order_keys, removed_ids)

    def apply_tasks(
        self,
        tasks: Sequence[TaskSummary],
        order_keys: Mapping[str, tuple[float, str]],
        removed_ids: Collection[str] = (),
    ) -> None:
        replacements = tuple(tasks)
        self._validate_unique(replacements)
        self._require_order_keys(replacements, order_keys)
        replacement_ids = {task.id for task in replacements}
        removed = set(removed_ids) - replacement_ids
        old_order_keys = dict(self._order_keys)
        existing_ids = set(self._all_by_id)
        for task_id in removed:
            self._all_by_id.pop(task_id, None)
            self._order_keys.pop(task_id, None)
            self._normalized_titles.pop(task_id, None)
        for task in replacements:
            self._all_by_id[task.id] = task
            self._order_keys[task.id] = order_keys[task.id]
            self._normalized_titles[task.id] = task.title.casefold()
        self._rebuild_all_tasks()
        target, counts = self._compute_filter_state()
        moved_ids = {
            task.id
            for task in replacements
            if task.id in existing_ids
            and old_order_keys.get(task.id) != self._order_keys[task.id]
        }
        self._apply_visible_incremental(target, moved_ids=moved_ids)
        self._filter_counts = counts

    def set_filter(self, selected: TaskFilter, search: str = "") -> None:
        normalized = search.strip().casefold()
        if selected is self._filter and normalized == self._search:
            return
        self._filter = selected
        self._search = normalized
        target, counts = self._compute_filter_state()
        self._apply_layout_tasks(target)
        self._filter_counts = counts

    def filter_counts(self) -> dict[TaskFilter, int]:
        _target, counts = self._compute_filter_state()
        self._filter_counts = counts
        return dict(self._filter_counts)

    def task_by_id(self, task_id: str) -> TaskSummary | None:
        return self._all_by_id.get(task_id)

    def all_tasks(self) -> tuple[TaskSummary, ...]:
        return tuple(self._all_tasks)

    def task_at(self, row: int) -> TaskSummary | None:
        return self._tasks[row] if 0 <= row < len(self._tasks) else None

    def row_for_task_id(self, task_id: str) -> int | None:
        return self._row_by_id.get(task_id)

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

    def _compute_filter_state(
        self,
    ) -> tuple[list[TaskSummary], dict[TaskFilter, int]]:
        counts = {selected: 0 for selected in TaskFilter}
        visible: list[TaskSummary] = []
        for task in self._all_tasks:
            if not self._matches_search(task):
                continue
            if task.archived:
                counts[TaskFilter.ARCHIVED] += 1
            else:
                counts[TaskFilter.ALL] += 1
                if task.status in {
                    TaskStatus.SCANNING,
                    TaskStatus.QUEUED,
                    TaskStatus.DOWNLOADING,
                    TaskStatus.WAITING_RETRY,
                }:
                    counts[TaskFilter.ACTIVE] += 1
                elif task.status is TaskStatus.PAUSED:
                    counts[TaskFilter.PAUSED] += 1
                elif task.status is TaskStatus.PARTIAL_FAILURE:
                    counts[TaskFilter.FAILED] += 1
                elif task.status is TaskStatus.COMPLETED:
                    counts[TaskFilter.COMPLETED] += 1
            if self._matches_filter(task, self._filter):
                visible.append(task)
        return visible, counts

    def _matches_search(self, task: TaskSummary) -> bool:
        return not self._search or self._search in self._normalized_titles[task.id]

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

    @staticmethod
    def _validate_unique(tasks: Sequence[TaskSummary]) -> None:
        task_ids = [task.id for task in tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("任务视图包含重复 ID")

    @staticmethod
    def _require_order_keys(
        tasks: Sequence[TaskSummary],
        order_keys: Mapping[str, tuple[float, str]],
    ) -> None:
        for task in tasks:
            if task.id not in order_keys:
                raise KeyError(task.id)

    def _rebuild_all_tasks(self) -> None:
        self._all_tasks = sorted(
            self._all_by_id.values(),
            key=lambda task: self._order_keys[task.id],
        )

    def _rebuild_row_index(self) -> None:
        self._row_by_id = {task.id: row for row, task in enumerate(self._tasks)}

    def _apply_visible_incremental(
        self,
        target: Sequence[TaskSummary],
        *,
        moved_ids: set[str],
    ) -> None:
        target_by_id = {task.id: task for task in target}
        target_ids = set(target_by_id)
        for row in range(len(self._tasks) - 1, -1, -1):
            if self._tasks[row].id in target_ids:
                continue
            self.beginRemoveRows(_INVALID_INDEX, row, row)
            self._tasks.pop(row)
            self.endRemoveRows()

        current_ids = {task.id for task in self._tasks}
        desired_existing = [task.id for task in target if task.id in current_ids]
        current_order = [task.id for task in self._tasks]
        if current_order != desired_existing:
            visible_moves = moved_ids & current_ids
            if len(visible_moves) == 1:
                task_id = next(iter(visible_moves))
                source = current_order.index(task_id)
                destination = desired_existing.index(task_id)
                self._move_visible_row(source, destination)
            else:
                by_id = {task.id: task for task in self._tasks}
                self._apply_layout_tasks([by_id[task_id] for task_id in desired_existing])

        current_ids = {task.id for task in self._tasks}
        for task in target:
            if task.id in current_ids:
                continue
            keys = [self._order_keys[value.id] for value in self._tasks]
            row = bisect_left(keys, self._order_keys[task.id])
            self.beginInsertRows(_INVALID_INDEX, row, row)
            self._tasks.insert(row, task)
            self.endInsertRows()
            current_ids.add(task.id)

        changed_rows: list[int] = []
        for row, task in enumerate(target):
            if self._tasks[row] == task:
                continue
            self._tasks[row] = task
            changed_rows.append(row)
        self._rebuild_row_index()
        self._emit_changed_rows(changed_rows)

    def _move_visible_row(self, source: int, destination: int) -> None:
        if source == destination:
            return
        destination_child = destination + 1 if source < destination else destination
        self.beginMoveRows(
            _INVALID_INDEX,
            source,
            source,
            _INVALID_INDEX,
            destination_child,
        )
        task = self._tasks.pop(source)
        self._tasks.insert(destination, task)
        self.endMoveRows()

    def _apply_layout_tasks(self, target: Sequence[TaskSummary]) -> None:
        if [task.id for task in self._tasks] == [task.id for task in target]:
            self._tasks = list(target)
            self._rebuild_row_index()
            return
        persistent = self.persistentIndexList()
        anchors = [
            (self._tasks[index.row()].id, index.column())
            if index.isValid() and 0 <= index.row() < len(self._tasks)
            else None
            for index in persistent
        ]
        self.layoutAboutToBeChanged.emit()
        self._tasks = list(target)
        self._rebuild_row_index()
        mapped = [
            (
                self.index(self._row_by_id[anchor[0]], anchor[1])
                if anchor is not None and anchor[0] in self._row_by_id
                else _INVALID_INDEX
            )
            for anchor in anchors
        ]
        self.changePersistentIndexList(persistent, mapped)
        self.layoutChanged.emit()

    def _emit_changed_rows(self, rows: Sequence[int]) -> None:
        if not rows:
            return
        start = previous = rows[0]
        for row in rows[1:]:
            if row == previous + 1:
                previous = row
                continue
            self.dataChanged.emit(
                self.index(start, 0),
                self.index(previous, self.columnCount() - 1),
            )
            start = previous = row
        self.dataChanged.emit(
            self.index(start, 0),
            self.index(previous, self.columnCount() - 1),
        )


class TaskItemTableModel(QAbstractTableModel):
    HEADERS = ("文件", "类型", "状态", "完整性", "进度", "大小", "重试", "错误")

    def __init__(self) -> None:
        super().__init__()
        self._items: list[TaskItemSummary] = []
        self._task_id: str | None = None
        self._total_count = 0
        self._row_by_id: dict[str, int] = {}

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

    @property
    def loaded_count(self) -> int:
        return len(self._items)

    @property
    def total_count(self) -> int:
        return self._total_count

    @property
    def has_more(self) -> bool:
        return self.loaded_count < self.total_count

    def begin_task(self, task_id: str, *, total_count: int) -> None:
        if total_count < 0:
            raise ValueError("媒体总数不能为负数")
        self.beginResetModel()
        self._task_id = task_id
        self._total_count = total_count
        self._items = []
        self._row_by_id = {}
        self.endResetModel()

    def append_page(
        self,
        task_id: str,
        items: Sequence[TaskItemSummary],
    ) -> None:
        self._require_task(task_id)
        page = tuple(items)
        page_ids = [item.id for item in page]
        if len(page_ids) != len(set(page_ids)):
            raise ValueError("媒体页包含重复 ID")
        if any(item_id in self._row_by_id for item_id in page_ids):
            raise ValueError("媒体页与已加载 ID 重复")
        if self.loaded_count + len(page) > self.total_count:
            raise ValueError("媒体页超过任务总数")
        if not page:
            return
        first = self.loaded_count
        last = first + len(page) - 1
        self.beginInsertRows(_INVALID_INDEX, first, last)
        self._items.extend(page)
        self._row_by_id.update(
            (item.id, row) for row, item in enumerate(page, start=first)
        )
        self.endInsertRows()

    def apply_items(
        self,
        task_id: str,
        items: Sequence[TaskItemSummary],
    ) -> None:
        self._require_task(task_id)
        replacements = tuple(items)
        item_ids = [item.id for item in replacements]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("媒体补丁包含重复 ID")
        changed_rows: list[int] = []
        for item in replacements:
            row = self._row_by_id.get(item.id)
            if row is None or self._items[row] == item:
                continue
            self._items[row] = item
            changed_rows.append(row)
        self._emit_changed_rows(sorted(changed_rows))

    def set_items(self, items: list[TaskItemSummary]) -> None:
        selected_task = self._task_id or ""
        self.begin_task(selected_task, total_count=len(items))
        self.append_page(selected_task, items)

    def item_at(self, row: int) -> TaskItemSummary | None:
        return self._items[row] if 0 <= row < len(self._items) else None

    def item_by_id(self, item_id: str) -> TaskItemSummary | None:
        row = self._row_by_id.get(item_id)
        return self._items[row] if row is not None else None

    def loaded_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self._items)

    def visible_item_ids(self, first_row: int, last_row: int) -> tuple[str, ...]:
        first = max(0, first_row)
        last = min(last_row, self.loaded_count - 1)
        if first > last:
            return ()
        return tuple(item.id for item in self._items[first : last + 1])

    def _require_task(self, task_id: str) -> None:
        if task_id != self._task_id:
            raise ValueError("媒体页不属于当前任务")

    def _emit_changed_rows(self, rows: Sequence[int]) -> None:
        if not rows:
            return
        start = previous = rows[0]
        for row in rows[1:]:
            if row == previous + 1:
                previous = row
                continue
            self.dataChanged.emit(
                self.index(start, 0),
                self.index(previous, self.columnCount() - 1),
            )
            start = previous = row
        self.dataChanged.emit(
            self.index(start, 0),
            self.index(previous, self.columnCount() - 1),
        )

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
