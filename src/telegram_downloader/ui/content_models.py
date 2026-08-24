from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QAbstractTableModel,
    QModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QPixmap

from telegram_downloader.content import (
    ALL_DIALOGS_SCOPE_REF,
    ALL_DIALOGS_TITLE,
    ContentDialog,
    ContentSourceKind,
    SearchResult,
    SearchScope,
    SearchSession,
    SelectionMode,
)
from telegram_downloader.domain import MediaKind

_INVALID_INDEX = QModelIndex()

_MEDIA_LABELS = {
    MediaKind.PHOTO: "图片",
    MediaKind.VIDEO: "视频",
    MediaKind.AUDIO: "音频",
    MediaKind.VOICE: "语音",
    MediaKind.DOCUMENT: "文档",
    MediaKind.ARCHIVE: "压缩包",
}

_MEDIA_COLORS = {
    MediaKind.PHOTO: "#38bdf8",
    MediaKind.VIDEO: "#a78bfa",
    MediaKind.AUDIO: "#34d399",
    MediaKind.VOICE: "#2dd4bf",
    MediaKind.DOCUMENT: "#fbbf24",
    MediaKind.ARCHIVE: "#fb923c",
}

_SOURCE_LABELS = {
    ContentSourceKind.GROUP: "群组",
    ContentSourceKind.CHANNEL: "频道",
    ContentSourceKind.PRIVATE: "私聊",
    ContentSourceKind.BOT: "机器人",
    ContentSourceKind.SAVED: "收藏夹",
    ContentSourceKind.UNKNOWN: "未知来源",
}


@dataclass(frozen=True, slots=True)
class DialogChoice:
    scope: SearchScope
    peer_ref: str
    title: str
    available: bool
    dialog: ContentDialog | None = None


class DialogListModel(QAbstractListModel):
    def __init__(self) -> None:
        super().__init__()
        self._all: tuple[ContentDialog, ...] = ()
        self._visible: tuple[DialogChoice, ...] = ()
        self._filter = ""

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._visible)

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if not index.isValid() or not 0 <= index.row() < len(self._visible):
            return None
        choice = self._visible[index.row()]
        item = choice.dialog
        if role == Qt.ItemDataRole.DisplayRole:
            if item is None:
                return choice.title
            suffixes = []
            if item.archived:
                suffixes.append("已归档")
            if not item.available:
                suffixes.append("不可用")
            suffix = f"  · {' · '.join(suffixes)}" if suffixes else ""
            return item.title + suffix
        if role == Qt.ItemDataRole.UserRole:
            return choice.peer_ref
        if role == Qt.ItemDataRole.ToolTipRole:
            if item is None:
                return "搜索当前账号的全部云端会话"
            username = f"@{item.username}" if item.username else item.peer_ref
            return f"{item.title}\n{username}"
        return None

    def set_dialogs(self, dialogs: list[ContentDialog]) -> None:
        self.beginResetModel()
        self._all = tuple(
            sorted(
                dialogs,
                key=lambda item: (
                    not item.available,
                    item.title.casefold(),
                    item.peer_ref,
                ),
            )
        )
        self._visible = self._filtered()
        self.endResetModel()

    def set_filter(self, value: str) -> None:
        normalized = value.strip().casefold()
        if normalized == self._filter:
            return
        self.beginResetModel()
        self._filter = normalized
        self._visible = self._filtered()
        self.endResetModel()

    def choice_at(self, row: int) -> DialogChoice:
        return self._visible[row]

    def _filtered(self) -> tuple[DialogChoice, ...]:
        all_dialogs = DialogChoice(
            SearchScope.ALL_DIALOGS,
            ALL_DIALOGS_SCOPE_REF,
            ALL_DIALOGS_TITLE,
            True,
        )
        visible = (
            self._all
            if not self._filter
            else tuple(
                item
                for item in self._all
                if self._filter in item.title.casefold()
                or self._filter in item.username.casefold()
            )
        )
        return (all_dialogs,) + tuple(
            DialogChoice(
                SearchScope.SINGLE_DIALOG,
                item.peer_ref,
                item.title,
                item.available,
                item,
            )
            for item in visible
        )


class SearchHistoryTableModel(QAbstractTableModel):
    HEADERS = ("搜索范围", "关键词", "筛选", "状态", "结果数", "更新时间")
    _STATUS_LABELS = {
        "running": "搜索中",
        "completed": "已完成",
        "incomplete": "不完整",
    }

    def __init__(self) -> None:
        super().__init__()
        self._sessions: tuple[SearchSession, ...] = ()

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._sessions)

    def columnCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self.HEADERS)
        ):
            return self.HEADERS[section]
        return None

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if not index.isValid() or not 0 <= index.row() < len(self._sessions):
            return None
        session = self._sessions[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return session.id
        if role == Qt.ItemDataRole.ToolTipRole:
            return session.last_error
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        values = (
            (
                ALL_DIALOGS_TITLE
                if session.scope is SearchScope.ALL_DIALOGS
                else session.dialog_title
            ),
            session.query.keyword,
            self._filter_summary(session),
            self._STATUS_LABELS[session.status.value],
            session.result_count,
            session.updated_at.strftime("%Y-%m-%d %H:%M"),
        )
        return values[index.column()]

    def set_sessions(self, sessions: list[SearchSession]) -> None:
        self.beginResetModel()
        self._sessions = tuple(sessions)
        self.endResetModel()

    def session_at(self, row: int) -> SearchSession:
        return self._sessions[row]

    @staticmethod
    def _filter_summary(session: SearchSession) -> str:
        filters = session.query.filters
        kinds = "、".join(
            _MEDIA_LABELS[kind]
            for kind in sorted(filters.media_kinds, key=lambda value: value.value)
        )
        return (
            f"{filters.date_from_utc:%Y-%m-%d} 至 "
            f"{filters.date_to_utc:%Y-%m-%d} · {kinds} · "
            f"上限 {filters.item_limit}"
        )


class SearchResultTableModel(QAbstractTableModel):
    _FRAGMENT_RESET_THRESHOLD = 64
    HEADERS = ("选择", "预览", "日期", "来源", "摘要", "类型", "大小", "状态")
    selection_changed = Signal(str, bool)

    def __init__(self) -> None:
        super().__init__()
        self._results: list[SearchResult] = []
        self._row_by_id: dict[str, int] = {}
        self._thumbnails: dict[str, Path] = {}
        self._fallback_icons: dict[MediaKind, QIcon] = {}

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._results)

    def columnCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self.HEADERS)
        ):
            return self.HEADERS[section]
        return None

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if not index.isValid() or not 0 <= index.row() < len(self._results):
            return None
        result = self._results[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return result.id
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            return (
                Qt.CheckState.Checked
                if result.selected
                else Qt.CheckState.Unchecked
            )
        if role == Qt.ItemDataRole.DecorationRole and index.column() == 1:
            path = self._thumbnails.get(result.id)
            if path is not None and path.is_file():
                icon = QIcon(str(path))
                if not icon.isNull():
                    return icon
            return self._fallback_icon(result.media_kind)
        if role == Qt.ItemDataRole.ToolTipRole:
            if index.column() == 3:
                source_title = result.source_title or result.peer_ref
                return (
                    f"{_SOURCE_LABELS[result.source_kind]}：{source_title}"
                    f"\n会话标识：{result.peer_ref}"
                )
            if index.column() == 4:
                return result.excerpt
            return result.original_name
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        values = (
            "",
            "",
            result.message_date_utc.strftime("%Y-%m-%d %H:%M"),
            result.source_title or result.peer_ref,
            result.excerpt,
            _MEDIA_LABELS[result.media_kind],
            self._format_bytes(result.expected_size),
            self._status_text(result),
        )
        return values[index.column()]

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid() or not 0 <= index.row() < len(self._results):
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        result = self._results[index.row()]
        if index.column() == 0 and result.available and not result.queued:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(
        self,
        index: QModelIndex,
        value,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if (
            role != Qt.ItemDataRole.CheckStateRole
            or index.column() != 0
            or not self.flags(index) & Qt.ItemFlag.ItemIsUserCheckable
        ):
            return False
        try:
            requested_state = Qt.CheckState(value)
        except (TypeError, ValueError):
            return False
        requested = requested_state == Qt.CheckState.Checked
        result = self._results[index.row()]
        if result.selected == requested:
            return True
        self._results[index.row()] = replace(result, selected=requested)
        self.dataChanged.emit(
            index,
            index,
            [Qt.ItemDataRole.CheckStateRole],
        )
        self.selection_changed.emit(result.id, requested)
        return True

    def set_results(self, results: list[SearchResult]) -> None:
        target = list(results)
        self._validate_target(target)
        self._reset_results(target)

    def row_for_result_id(self, result_id: str) -> int | None:
        return self._row_by_id.get(result_id)

    def results(self) -> tuple[SearchResult, ...]:
        return tuple(self._results)

    def _reindex(self) -> None:
        self._row_by_id = {
            result.id: row for row, result in enumerate(self._results)
        }

    @staticmethod
    def _ranges(rows: list[int]) -> list[tuple[int, int]]:
        if not rows:
            return []
        ranges: list[tuple[int, int]] = []
        first = previous = rows[0]
        for row in rows[1:]:
            if row == previous + 1:
                previous = row
                continue
            ranges.append((first, previous))
            first = previous = row
        ranges.append((first, previous))
        return ranges

    @staticmethod
    def _validate_target(target: list[SearchResult]) -> None:
        ids = [result.id for result in target]
        if len(ids) != len(set(ids)):
            raise ValueError("搜索结果 ID 重复")

    def _prune_thumbnails(self, target_ids: set[str]) -> None:
        self._thumbnails = {
            result_id: path
            for result_id, path in self._thumbnails.items()
            if result_id in target_ids
        }

    def _reset_results(self, target: list[SearchResult]) -> None:
        self.beginResetModel()
        self._results = list(target)
        self._reindex()
        self._prune_thumbnails(set(self._row_by_id))
        self.endResetModel()

    def apply_results(self, results: list[SearchResult]) -> None:
        target = list(results)
        self._validate_target(target)
        target_ids = [item.id for item in target]
        target_id_set = set(target_ids)
        current_ids = [item.id for item in self._results]

        if current_ids == target_ids:
            changed_rows = [
                row
                for row, (before, after) in enumerate(
                    zip(self._results, target, strict=True)
                )
                if before != after
            ]
            self._results = target
            self._reindex()
            self._prune_thumbnails(target_id_set)
            for first, last in self._ranges(changed_rows):
                self.dataChanged.emit(
                    self.index(first, 0),
                    self.index(last, self.columnCount() - 1),
                )
            return

        removed_rows = [
            row
            for row, result in enumerate(self._results)
            if result.id not in target_id_set
        ]
        removed_ranges = self._ranges(removed_rows)
        if len(removed_ranges) > self._FRAGMENT_RESET_THRESHOLD:
            self._reset_results(target)
            return
        for first, last in reversed(removed_ranges):
            self.beginRemoveRows(_INVALID_INDEX, first, last)
            del self._results[first : last + 1]
            self._reindex()
            self.endRemoveRows()

        surviving_ids = set(self._row_by_id)
        additions = [item for item in target if item.id not in surviving_ids]
        if additions:
            first = len(self._results)
            last = first + len(additions) - 1
            self.beginInsertRows(_INVALID_INDEX, first, last)
            self._results.extend(additions)
            self._reindex()
            self.endInsertRows()

        existing_order = [item.id for item in self._results]
        if existing_order != target_ids:
            persistent = self.persistentIndexList()
            persistent_ids = [
                self._results[index.row()].id if index.isValid() else ""
                for index in persistent
            ]
            self.layoutAboutToBeChanged.emit()
            self._results = target
            self._reindex()
            remapped = [
                self.index(self._row_by_id[result_id], index.column())
                if result_id in self._row_by_id
                else QModelIndex()
                for index, result_id in zip(
                    persistent,
                    persistent_ids,
                    strict=True,
                )
            ]
            self.changePersistentIndexList(persistent, remapped)
            self.layoutChanged.emit()
        else:
            before_by_id = {item.id: item for item in self._results}
            changed_rows = [
                row
                for row, item in enumerate(target)
                if before_by_id[item.id] != item
            ]
            self._results = target
            self._reindex()
            for first, last in self._ranges(changed_rows):
                self.dataChanged.emit(
                    self.index(first, 0),
                    self.index(last, self.columnCount() - 1),
                )
        self._prune_thumbnails(target_id_set)

    def set_thumbnail(self, result_id: str, path: Path) -> None:
        row = self._row_by_id.get(result_id)
        if row is None:
            return
        self._thumbnails[result_id] = path
        index = self.index(row, 1)
        self.dataChanged.emit(
            index,
            index,
            [Qt.ItemDataRole.DecorationRole],
        )

    def thumbnail_path(self, result_id: str) -> Path | None:
        return self._thumbnails.get(result_id)

    def result_at(self, row: int) -> SearchResult:
        return self._results[row]

    def selected_results(self) -> tuple[SearchResult, ...]:
        return tuple(item for item in self._results if item.selected)

    def apply_selection_mode(self, mode: SelectionMode) -> int:
        if mode not in (SelectionMode.SELECT_ALL, SelectionMode.INVERT):
            raise ValueError("批量选择模式无效")
        changed_rows: list[int] = []
        for row, item in enumerate(self._results):
            if not item.available or item.queued:
                selected = False
            else:
                selected = (
                    True
                    if mode is SelectionMode.SELECT_ALL
                    else not item.selected
                )
            if item.selected == selected:
                continue
            self._results[row] = replace(item, selected=selected)
            changed_rows.append(row)
        for first, last in self._ranges(changed_rows):
            self.dataChanged.emit(
                self.index(first, 0),
                self.index(last, 0),
                [Qt.ItemDataRole.CheckStateRole],
            )
        return len(changed_rows)

    def _fallback_icon(self, kind: MediaKind) -> QIcon:
        icon = self._fallback_icons.get(kind)
        if icon is None:
            pixmap = QPixmap(36, 28)
            pixmap.fill(QColor(_MEDIA_COLORS[kind]))
            icon = QIcon(pixmap)
            self._fallback_icons[kind] = icon
        return icon

    @staticmethod
    def _format_bytes(value: int | None) -> str:
        if value is None:
            return "未知"
        amount = float(value)
        units = ("B", "KB", "MB", "GB", "TB")
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                return (
                    f"{amount:.0f} {unit}"
                    if unit == "B"
                    else f"{amount:.1f} {unit}"
                )
            amount /= 1024
        return f"{value} B"

    @staticmethod
    def _status_text(result: SearchResult) -> str:
        if not result.available:
            return "不可用"
        if result.queued:
            return "已入队"
        return "可选择"
