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
    HEADERS = ("选择", "预览", "日期", "来源", "摘要", "类型", "大小", "状态")
    selection_changed = Signal(str, bool)

    def __init__(self) -> None:
        super().__init__()
        self._results: tuple[SearchResult, ...] = ()
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
        mutable = list(self._results)
        mutable[index.row()] = replace(result, selected=requested)
        self._results = tuple(mutable)
        self.dataChanged.emit(
            index,
            index,
            [Qt.ItemDataRole.CheckStateRole],
        )
        self.selection_changed.emit(result.id, requested)
        return True

    def set_results(self, results: list[SearchResult]) -> None:
        self.beginResetModel()
        self._results = tuple(results)
        ids = {item.id for item in results}
        self._thumbnails = {
            result_id: path
            for result_id, path in self._thumbnails.items()
            if result_id in ids
        }
        self.endResetModel()

    def apply_results(self, results: list[SearchResult]) -> None:
        target = list(results)
        target_ids = {item.id for item in target}
        current = list(self._results)
        row = len(current) - 1
        while row >= 0:
            if current[row].id in target_ids:
                row -= 1
                continue
            last = row
            while row >= 0 and current[row].id not in target_ids:
                row -= 1
            first = row + 1
            self.beginRemoveRows(_INVALID_INDEX, first, last)
            del current[first : last + 1]
            self._results = tuple(current)
            self.endRemoveRows()

        for row, wanted in enumerate(target):
            existing = next(
                (
                    index
                    for index, item in enumerate(current)
                    if item.id == wanted.id
                ),
                None,
            )
            if existing is None:
                self.beginInsertRows(_INVALID_INDEX, row, row)
                current.insert(row, wanted)
                self._results = tuple(current)
                self.endInsertRows()
            elif existing != row:
                self.beginRemoveRows(_INVALID_INDEX, existing, existing)
                moved = current.pop(existing)
                self._results = tuple(current)
                self.endRemoveRows()
                self.beginInsertRows(_INVALID_INDEX, row, row)
                current.insert(row, moved)
                self._results = tuple(current)
                self.endInsertRows()
            if current[row] != wanted:
                current[row] = wanted
                self._results = tuple(current)
                self.dataChanged.emit(
                    self.index(row, 0),
                    self.index(row, self.columnCount() - 1),
                )

        self._results = tuple(current)
        self._thumbnails = {
            result_id: path
            for result_id, path in self._thumbnails.items()
            if result_id in target_ids
        }

    def set_thumbnail(self, result_id: str, path: Path) -> None:
        for row, result in enumerate(self._results):
            if result.id != result_id:
                continue
            self._thumbnails[result_id] = path
            index = self.index(row, 1)
            self.dataChanged.emit(
                index,
                index,
                [Qt.ItemDataRole.DecorationRole],
            )
            return

    def thumbnail_path(self, result_id: str) -> Path | None:
        return self._thumbnails.get(result_id)

    def result_at(self, row: int) -> SearchResult:
        return self._results[row]

    def selected_results(self) -> tuple[SearchResult, ...]:
        return tuple(item for item in self._results if item.selected)

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
