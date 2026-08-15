from __future__ import annotations

from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, QModelIndex, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from telegram_downloader.content import (
    ContentDialog,
    SearchResult,
    SearchSession,
    SearchStatus,
)
from telegram_downloader.content_progress import SearchProgress
from telegram_downloader.domain import MediaKind
from telegram_downloader.links import is_telegram_link_candidate
from telegram_downloader.ui.content_models import (
    DialogListModel,
    SearchHistoryTableModel,
    SearchResultTableModel,
)
from telegram_downloader.ui.media_preview import MediaPreviewDialog

_MEDIA_LABELS = {
    MediaKind.PHOTO: "图片",
    MediaKind.VIDEO: "视频",
    MediaKind.AUDIO: "音频",
    MediaKind.VOICE: "语音",
    MediaKind.DOCUMENT: "文档",
    MediaKind.ARCHIVE: "压缩包",
}


class ContentBrowserPage(QWidget):
    refresh_requested = Signal()
    connection_retry_requested = Signal()
    dialog_selected = Signal(str)
    link_requested = Signal(str)
    search_requested = Signal(str, str, object, object, object, int)
    cancel_search_requested = Signal()
    load_more_requested = Signal(str)
    history_open_requested = Signal(str)
    history_delete_requested = Signal(str)
    history_clear_requested = Signal()
    selection_changed = Signal(str, str, bool)
    queue_requested = Signal(str)
    thumbnail_requested = Signal(str)
    preview_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("contentBrowserPage")
        self.dialog_model = DialogListModel()
        self.history_model = SearchHistoryTableModel()
        self.result_model = SearchResultTableModel()
        self.active_search_id: str | None = None
        self.active_session: SearchSession | None = None
        self.results: list[SearchResult] = []
        self._logged_in = False
        self._sync_busy = False
        self._search_busy = False
        self._connection_action_busy = False
        self._connection_retryable = False
        self._queue_busy = False
        self._thumbnail_requested_ids: set[str] = set()
        self._preview_dialogs: set[MediaPreviewDialog] = set()
        self._build_ui()
        self._connect_signals()
        self._refresh_actions()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(12)

        title = QLabel("账号内容")
        title.setObjectName("pageTitle")
        subtitle = QLabel("浏览已加入的群组和频道，搜索后选择性下载媒体")
        subtitle.setObjectName("muted")
        root.addWidget(title)
        root.addWidget(subtitle)

        self.empty_hint = QLabel("登录 Telegram 后可同步群组并搜索；本地历史仍可查看")
        self.empty_hint.setObjectName("contentHint")
        self.empty_hint.setWordWrap(True)
        root.addWidget(self.empty_hint)
        self.connection_retry_button = QPushButton("重新连接")
        self.connection_retry_button.hide()
        root.addWidget(
            self.connection_retry_button,
            0,
            Qt.AlignmentFlag.AlignLeft,
        )

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("contentSplitter")
        splitter.addWidget(self._build_dialog_panel())
        splitter.addWidget(self._build_search_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 760])
        root.addWidget(splitter, 1)

        self.error_label = QLabel("")
        self.error_label.setObjectName("errorText")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        root.addWidget(self.error_label)

    def _build_dialog_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("contentPanel")
        panel.setMinimumWidth(230)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        heading = QHBoxLayout()
        label = QLabel("群组与频道")
        label.setObjectName("sectionTitle")
        self.refresh_button = QPushButton("刷新")
        heading.addWidget(label)
        heading.addStretch()
        heading.addWidget(self.refresh_button)
        layout.addLayout(heading)

        self.dialog_filter = QLineEdit()
        self.dialog_filter.setPlaceholderText("筛选名称或用户名")
        self.dialog_filter.setClearButtonEnabled(True)
        layout.addWidget(self.dialog_filter)
        self.sync_state_label = QLabel("尚未同步")
        self.sync_state_label.setObjectName("muted")
        layout.addWidget(self.sync_state_label)
        self.sync_progress = QProgressBar()
        self.sync_progress.setRange(0, 0)
        self.sync_progress.setTextVisible(False)
        self.sync_progress.hide()
        layout.addWidget(self.sync_progress)
        self.dialog_list = QListView()
        self.dialog_list.setModel(self.dialog_model)
        self.dialog_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.dialog_list, 1)
        return panel

    def _build_search_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("contentPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        form_header = QHBoxLayout()
        self.current_dialog_label = QLabel("请选择群组或频道")
        self.current_dialog_label.setObjectName("sectionTitle")
        form_header.addWidget(self.current_dialog_label)
        form_header.addStretch()
        layout.addLayout(form_header)

        query_row = QHBoxLayout()
        self.keyword_input = QLineEdit()
        self.keyword_input.setPlaceholderText("输入 Telegram 服务端搜索关键词")
        self.keyword_input.setClearButtonEnabled(True)
        self.search_button = QPushButton("搜索")
        self.search_button.setObjectName("primaryButton")
        self.cancel_button = QPushButton("取消")
        query_row.addWidget(self.keyword_input, 1)
        query_row.addWidget(self.search_button)
        query_row.addWidget(self.cancel_button)
        layout.addLayout(query_row)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("开始日期"))
        self.date_from = QDateEdit(QDate.currentDate().addDays(-7))
        self.date_from.setCalendarPopup(True)
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        filter_row.addWidget(self.date_from)
        filter_row.addWidget(QLabel("结束日期（含）"))
        self.date_to = QDateEdit(QDate.currentDate())
        self.date_to.setCalendarPopup(True)
        self.date_to.setDisplayFormat("yyyy-MM-dd")
        filter_row.addWidget(self.date_to)
        filter_row.addWidget(QLabel("数量上限"))
        self.limit_input = QSpinBox()
        self.limit_input.setRange(1, 10_000)
        self.limit_input.setValue(500)
        filter_row.addWidget(self.limit_input)
        filter_row.addStretch()
        layout.addLayout(filter_row)

        media_row = QHBoxLayout()
        media_row.addWidget(QLabel("媒体类型"))
        self.media_checks: dict[MediaKind, QCheckBox] = {}
        for kind in MediaKind:
            check = QCheckBox(_MEDIA_LABELS[kind])
            check.setChecked(True)
            self.media_checks[kind] = check
            media_row.addWidget(check)
        media_row.addStretch()
        layout.addLayout(media_row)

        self.search_state_label = QLabel("正在准备搜索…")
        self.search_state_label.setObjectName("muted")
        self.search_state_label.hide()
        layout.addWidget(self.search_state_label)
        self.search_progress = QProgressBar()
        self.search_progress.setRange(0, 0)
        self.search_progress.setTextVisible(False)
        self.search_progress.hide()
        layout.addWidget(self.search_progress)

        self.tabs = QTabWidget()
        self.results_tab = QWidget()
        results_layout = QVBoxLayout(self.results_tab)
        results_layout.setContentsMargins(0, 8, 0, 0)
        self.result_table = QTableView()
        self.result_table.setModel(self.result_model)
        self._configure_table(self.result_table)
        self.result_table.setIconSize(QSize(112, 84))
        self.result_table.verticalHeader().setDefaultSectionSize(96)
        result_header = self.result_table.horizontalHeader()
        result_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        result_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.result_table.setColumnWidth(1, 124)
        for column in (0, 2, 4, 5, 6):
            result_header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        results_layout.addWidget(self.result_table, 1)
        self.load_more_button = QPushButton("加载更多")
        results_layout.addWidget(
            self.load_more_button,
            0,
            Qt.AlignmentFlag.AlignHCenter,
        )

        self.history_tab = QWidget()
        history_layout = QVBoxLayout(self.history_tab)
        history_layout.setContentsMargins(0, 8, 0, 0)
        self.history_table = QTableView()
        self.history_table.setModel(self.history_model)
        self._configure_table(self.history_table)
        self.history_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.history_table.horizontalHeader().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.Stretch,
        )
        history_layout.addWidget(self.history_table, 1)
        history_actions = QHBoxLayout()
        self.history_delete_button = QPushButton("删除记录")
        self.history_clear_button = QPushButton("清空历史")
        history_actions.addStretch()
        history_actions.addWidget(self.history_delete_button)
        history_actions.addWidget(self.history_clear_button)
        history_layout.addLayout(history_actions)

        self.tabs.addTab(self.results_tab, "搜索结果")
        self.tabs.addTab(self.history_tab, "搜索记录")
        layout.addWidget(self.tabs, 1)

        selection_bar = QHBoxLayout()
        self.selection_summary = QLabel("已选 0 项")
        self.selection_summary.setObjectName("selectionSummary")
        self.select_all_button = QPushButton("全选")
        self.invert_button = QPushButton("反选")
        self.queue_button = QPushButton("加入下载队列")
        self.queue_button.setObjectName("primaryButton")
        selection_bar.addWidget(self.selection_summary)
        selection_bar.addStretch()
        selection_bar.addWidget(self.select_all_button)
        selection_bar.addWidget(self.invert_button)
        selection_bar.addWidget(self.queue_button)
        layout.addLayout(selection_bar)
        return panel

    @staticmethod
    def _configure_table(table: QTableView) -> None:
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)
        table.verticalHeader().hide()

    def _connect_signals(self) -> None:
        self.dialog_filter.textChanged.connect(self.dialog_model.set_filter)
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.connection_retry_button.clicked.connect(
            self.connection_retry_requested.emit
        )
        self.dialog_list.selectionModel().currentChanged.connect(
            self._dialog_changed
        )
        self.search_button.clicked.connect(self._emit_search)
        self.keyword_input.returnPressed.connect(self._emit_search)
        self.cancel_button.clicked.connect(self.cancel_search_requested.emit)
        self.load_more_button.clicked.connect(self._emit_load_more)
        self.result_model.selection_changed.connect(self._selection_changed)
        self.select_all_button.clicked.connect(self._select_all)
        self.invert_button.clicked.connect(self._invert_selection)
        self.queue_button.clicked.connect(self._emit_queue)
        self.history_table.doubleClicked.connect(self._open_history)
        self.history_delete_button.clicked.connect(self._delete_history)
        self.history_clear_button.clicked.connect(
            self.history_clear_requested.emit
        )
        self.history_table.selectionModel().selectionChanged.connect(
            self._refresh_actions
        )
        self.result_table.verticalScrollBar().valueChanged.connect(
            lambda _value: self.request_visible_thumbnails()
        )
        self.result_table.doubleClicked.connect(self._request_preview)

    def set_logged_in(self, logged_in: bool) -> None:
        self._logged_in = logged_in
        self.empty_hint.setText(
            "请选择群组或频道开始搜索"
            if logged_in
            else "请先登录 Telegram；已保存的搜索历史仍可查看"
        )
        if logged_in:
            self.connection_retry_button.hide()
        self._refresh_actions()

    def set_connection_state(self, text: str, *, retryable: bool = False) -> None:
        self.empty_hint.setText(text)
        self._connection_retryable = retryable
        self.connection_retry_button.setVisible(
            retryable or self._connection_action_busy
        )

    def set_connection_action_busy(self, busy: bool) -> None:
        self._connection_action_busy = busy
        self.connection_retry_button.setText("重连中…" if busy else "重新连接")
        self.connection_retry_button.setVisible(busy or self._connection_retryable)
        self._refresh_actions()

    def set_dialogs(self, dialogs: list[ContentDialog]) -> None:
        selected_peer = self._current_peer_ref()
        self.dialog_model.set_dialogs(dialogs)
        if selected_peer is not None:
            for row in range(self.dialog_model.rowCount()):
                if self.dialog_model.dialog_at(row).peer_ref == selected_peer:
                    self.dialog_list.setCurrentIndex(
                        self.dialog_model.index(row, 0)
                    )
                    break
        self._refresh_actions()

    def set_sync_state(
        self,
        text: str,
        *,
        busy: bool = False,
        count: int = 0,
    ) -> None:
        self._sync_busy = busy
        self.sync_state_label.setText(text)
        self.refresh_button.setText("刷新中…" if busy else "刷新")
        self.sync_progress.setVisible(busy)
        self._refresh_actions()

    def set_sessions(self, sessions: list[SearchSession]) -> None:
        self.history_model.set_sessions(sessions)
        self._refresh_actions()

    def set_active_search(self, session: SearchSession | None) -> None:
        self.active_session = session
        self.active_search_id = session.id if session is not None else None
        self._set_form_from_session(session)
        self._refresh_actions()

    def set_results(self, results: list[SearchResult]) -> None:
        self.results = list(results)
        self.result_model.set_results(results)
        self._thumbnail_requested_ids.intersection_update(
            item.id for item in results
        )
        self._update_selection_summary()
        self._refresh_actions()
        QTimer.singleShot(0, self.request_visible_thumbnails)

    def set_search_busy(self, busy: bool) -> None:
        self._search_busy = busy
        self.search_state_label.setVisible(busy)
        self.search_progress.setVisible(busy)
        self._refresh_actions()

    def set_queue_busy(self, busy: bool) -> None:
        self._queue_busy = busy
        selected = sum(
            item.selected and item.available and not item.queued for item in self.results
        )
        self.queue_button.setText(
            f"正在准备已选 {selected} 项…" if busy else "加入下载队列"
        )
        self._refresh_actions()

    def set_search_progress(self, progress: SearchProgress | None) -> None:
        if progress is None:
            if not self._search_busy:
                self.search_state_label.hide()
                self.search_progress.hide()
            return
        self.search_state_label.setText(
            f"已扫描 {progress.inspected} 条 · 找到 {progress.matched} 项 · "
            f"{progress.phase}"
        )
        if self._search_busy:
            self.search_state_label.show()
            self.search_progress.show()

    def set_thumbnail(self, result_id: str, path: Path) -> None:
        self.result_model.set_thumbnail(result_id, path)

    def show_preview(self, result: SearchResult, path: Path | None) -> None:
        dialog = MediaPreviewDialog(result, path, self)
        self._preview_dialogs.add(dialog)
        dialog.finished.connect(
            lambda _result, retained=dialog: self._preview_dialogs.discard(retained)
        )
        dialog.open()

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def request_visible_thumbnails(self) -> None:
        viewport = self.result_table.viewport().rect()
        for row in range(self.result_model.rowCount()):
            index = self.result_model.index(row, 1)
            if not self.result_table.visualRect(index).intersects(viewport):
                continue
            result_id = str(
                self.result_model.data(index, Qt.ItemDataRole.UserRole)
            )
            if result_id in self._thumbnail_requested_ids:
                continue
            self._thumbnail_requested_ids.add(result_id)
            self.thumbnail_requested.emit(result_id)

    def _request_preview(self, index: QModelIndex) -> None:
        if not index.isValid() or index.column() != 1:
            return
        result_id = self.result_model.data(index, Qt.ItemDataRole.UserRole)
        if result_id:
            self.preview_requested.emit(str(result_id))

    def _dialog_changed(
        self,
        current: QModelIndex,
        _previous: QModelIndex,
    ) -> None:
        if current.isValid():
            dialog = self.dialog_model.dialog_at(current.row())
            self.current_dialog_label.setText(dialog.title)
            self.dialog_selected.emit(dialog.peer_ref)
        else:
            self.current_dialog_label.setText("请选择群组或频道")
        self._refresh_actions()

    def _emit_search(self) -> None:
        dialog = self._current_dialog()
        keyword = self.keyword_input.text().strip()
        if is_telegram_link_candidate(keyword):
            self.show_error("")
            self.link_requested.emit(keyword)
            return
        start = self.date_from.date().toPython()
        end = self.date_to.date().toPython()
        kinds = frozenset(
            kind for kind, check in self.media_checks.items() if check.isChecked()
        )
        if dialog is None or not dialog.available:
            self.show_error("请选择一个当前可用的群组或频道")
            return
        if not keyword:
            self.show_error("搜索关键词不能为空")
            return
        if not isinstance(start, date) or not isinstance(end, date) or start > end:
            self.show_error("开始日期不能晚于结束日期")
            return
        if not kinds:
            self.show_error("请至少选择一种媒体类型")
            return
        self.show_error("")
        self.search_requested.emit(
            dialog.peer_ref,
            keyword,
            start,
            end,
            kinds,
            self.limit_input.value(),
        )

    def _selection_changed(self, result_id: str, selected: bool) -> None:
        self.results = [
            self.result_model.result_at(row)
            for row in range(self.result_model.rowCount())
        ]
        self._update_selection_summary()
        self._refresh_actions()
        if self.active_search_id:
            self.selection_changed.emit(self.active_search_id, result_id, selected)

    def _select_all(self) -> None:
        for row in range(self.result_model.rowCount()):
            index = self.result_model.index(row, 0)
            if self.result_model.flags(index) & Qt.ItemFlag.ItemIsUserCheckable:
                self.result_model.setData(
                    index,
                    Qt.CheckState.Checked,
                    Qt.ItemDataRole.CheckStateRole,
                )

    def _invert_selection(self) -> None:
        for row in range(self.result_model.rowCount()):
            index = self.result_model.index(row, 0)
            if not (
                self.result_model.flags(index)
                & Qt.ItemFlag.ItemIsUserCheckable
            ):
                continue
            current = self.result_model.data(
                index,
                Qt.ItemDataRole.CheckStateRole,
            )
            requested = (
                Qt.CheckState.Unchecked
                if current == Qt.CheckState.Checked
                else Qt.CheckState.Checked
            )
            self.result_model.setData(
                index,
                requested,
                Qt.ItemDataRole.CheckStateRole,
            )

    def _emit_queue(self) -> None:
        if self.active_search_id is not None:
            self.set_queue_busy(True)
            self.queue_requested.emit(self.active_search_id)

    def _emit_load_more(self) -> None:
        if self.active_search_id is not None:
            self.load_more_requested.emit(self.active_search_id)

    def _open_history(self, index: QModelIndex) -> None:
        value = self.history_model.data(index, Qt.ItemDataRole.UserRole)
        if value:
            self.history_open_requested.emit(str(value))

    def _delete_history(self) -> None:
        search_id = self._selected_history_id()
        if search_id is not None:
            self.history_delete_requested.emit(search_id)

    def _selected_history_id(self) -> str | None:
        rows = self.history_table.selectionModel().selectedRows()
        if not rows:
            return None
        value = self.history_model.data(rows[0], Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    def _current_dialog(self) -> ContentDialog | None:
        index = self.dialog_list.currentIndex()
        if not index.isValid():
            return None
        return self.dialog_model.dialog_at(index.row())

    def _current_peer_ref(self) -> str | None:
        dialog = self._current_dialog()
        return dialog.peer_ref if dialog is not None else None

    def _update_selection_summary(self) -> None:
        selected = self.result_model.selected_results()
        known = sum(item.expected_size or 0 for item in selected)
        unknown = sum(item.expected_size is None for item in selected)
        text = f"已选 {len(selected)} 项"
        if selected:
            text += f" · 已知 {self._format_bytes(known)}"
            if unknown:
                text += f" · {unknown} 项大小未知"
        self.selection_summary.setText(text)

    def _set_form_from_session(self, session: SearchSession | None) -> None:
        if session is None:
            self.keyword_input.clear()
            self.date_from.setDate(QDate.currentDate().addDays(-7))
            self.date_to.setDate(QDate.currentDate())
            self.limit_input.setValue(500)
            selected = frozenset(MediaKind)
        else:
            filters = session.query.filters
            start = filters.date_from_utc.astimezone().date()
            end = filters.date_to_utc.astimezone().date()
            self.keyword_input.setText(session.query.keyword)
            self.date_from.setDate(QDate(start.year, start.month, start.day))
            self.date_to.setDate(QDate(end.year, end.month, end.day))
            self.limit_input.setValue(filters.item_limit)
            selected = filters.media_kinds
        for kind, check in self.media_checks.items():
            check.setChecked(kind in selected)

    def _refresh_actions(self, *_args) -> None:
        dialog = self._current_dialog()
        dialog_available = dialog is not None and dialog.available
        online_ready = self._logged_in and not self._search_busy
        form_ready = not self._search_busy
        selectable = any(
            item.selected and item.available and not item.queued
            for item in self.results
        )
        self.refresh_button.setEnabled(not self._sync_busy)
        self.connection_retry_button.setEnabled(not self._connection_action_busy)
        self.search_button.setEnabled(form_ready)
        self.keyword_input.setEnabled(form_ready)
        self.cancel_button.setVisible(self._search_busy)
        self.select_all_button.setEnabled(
            online_ready and dialog_available and bool(self.results)
        )
        self.invert_button.setEnabled(
            online_ready and dialog_available and bool(self.results)
        )
        self.queue_button.setEnabled(
            online_ready
            and dialog_available
            and self.active_search_id is not None
            and selectable
            and not self._queue_busy
        )
        can_load = (
            self.active_session is not None
            and self.active_session.status is SearchStatus.RUNNING
            and not self.active_session.exhausted
            and not self._search_busy
            and self._logged_in
        )
        self.load_more_button.setVisible(can_load)
        self.history_table.setEnabled(True)
        self.history_delete_button.setEnabled(
            self._selected_history_id() is not None
        )
        self.history_clear_button.setEnabled(
            self.history_model.rowCount() > 0
        )

    @staticmethod
    def _format_bytes(value: int) -> str:
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
