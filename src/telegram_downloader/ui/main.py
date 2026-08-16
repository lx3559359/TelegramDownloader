from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QDate, QItemSelectionModel, QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from telegram_downloader import __version__
from telegram_downloader.domain import IntegrityStatus, ItemStatus, MediaKind, TaskStatus
from telegram_downloader.file_integrity import IntegrityProgress
from telegram_downloader.ui.content_browser import ContentBrowserPage
from telegram_downloader.ui.models import (
    TaskFilter,
    TaskItemSummary,
    TaskItemTableModel,
    TaskSummary,
    TaskTableModel,
)
from telegram_downloader.ui.subscriptions import SubscriptionPage
from telegram_downloader.ui.theme import DARK_STYLESHEET, ensure_cjk_font

_MEDIA_LABELS = {
    MediaKind.PHOTO: "图片",
    MediaKind.VIDEO: "视频",
    MediaKind.AUDIO: "音频",
    MediaKind.VOICE: "语音",
    MediaKind.DOCUMENT: "文档",
    MediaKind.ARCHIVE: "压缩包",
}

_TASK_FILTER_LABELS = {
    TaskFilter.ALL: "全部",
    TaskFilter.ACTIVE: "进行中",
    TaskFilter.PAUSED: "已暂停",
    TaskFilter.FAILED: "部分失败",
    TaskFilter.COMPLETED: "已完成",
    TaskFilter.ARCHIVED: "已归档",
}

_INTEGRITY_FAILURES = frozenset(
    {
        IntegrityStatus.MISSING,
        IntegrityStatus.SIZE_MISMATCH,
        IntegrityStatus.HASH_MISMATCH,
        IntegrityStatus.READ_ERROR,
    }
)


class MainWindow(QMainWindow):
    scan_requested = Signal(str)
    content_activated = Signal()
    subscriptions_activated = Signal()
    pause_requested = Signal(str)
    resume_requested = Signal(str)
    retry_failed_requested = Signal(str)
    open_directory_requested = Signal(str)
    task_selection_changed = Signal(object)
    pause_tasks_requested = Signal(object)
    resume_tasks_requested = Signal(object)
    retry_tasks_requested = Signal(object)
    archive_tasks_requested = Signal(object)
    restore_tasks_requested = Signal(object)
    open_media_requested = Signal(str)
    verify_media_requested = Signal(object)
    repair_media_requested = Signal(object)
    verify_tasks_requested = Signal(object)
    integrity_cancel_requested = Signal()
    settings_requested = Signal()
    login_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._restoring_task_selection = False
        self._detail_task_id: str | None = None
        self._integrity_busy = False
        self.setWindowTitle("Telegram 下载器")
        self.setMinimumSize(1180, 720)
        self.resize(1280, 780)
        ensure_cjk_font()
        self.setStyleSheet(DARK_STYLESHEET)

        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_navigation())
        self.task_page = self._build_workspace()
        self.content_page = ContentBrowserPage()
        self.subscriptions_page = SubscriptionPage()
        self.page_stack = QStackedWidget()
        self.page_stack.addWidget(self.task_page)
        self.page_stack.addWidget(self.content_page)
        self.page_stack.addWidget(self.subscriptions_page)
        root_layout.addWidget(self.page_stack, 1)
        self.statistics_panel = self._build_statistics()
        root_layout.addWidget(self.statistics_panel)
        self.setCentralWidget(root)
        self.statusBar().showMessage("准备就绪")

        self.scan_button.clicked.connect(self._emit_scan)
        self.pause_button.clicked.connect(
            lambda: self._emit_task_batch(
                self.pause_tasks_requested.emit,
                self.pause_requested.emit,
            )
        )
        self.resume_button.clicked.connect(
            lambda: self._emit_task_batch(
                self.resume_tasks_requested.emit,
                self.resume_requested.emit,
            )
        )
        self.retry_button.clicked.connect(
            lambda: self._emit_task_batch(
                self.retry_tasks_requested.emit,
                self.retry_failed_requested.emit,
            )
        )
        self.open_button.clicked.connect(
            lambda: self._emit_for_selected(self.open_directory_requested.emit)
        )
        self.archive_button.clicked.connect(self._confirm_archive)
        self.restore_button.clicked.connect(self._confirm_restore)
        self.open_file_button.clicked.connect(self._emit_selected_media)
        self.verify_media_button.clicked.connect(self._emit_verify_media)
        self.repair_media_button.clicked.connect(self._confirm_repair_media)
        self.verify_tasks_button.clicked.connect(self._emit_verify_tasks)
        self.integrity_cancel_button.clicked.connect(self._emit_integrity_cancel)
        self.task_search.textChanged.connect(self._apply_task_filter)
        self.task_filter.currentIndexChanged.connect(self._apply_task_filter)
        self.task_table.selectionModel().selectionChanged.connect(self._task_selection_changed)
        self.task_item_table.selectionModel().selectionChanged.connect(
            self._update_media_action_state
        )
        self.task_item_table.doubleClicked.connect(self._emit_open_media)
        self.tasks_nav_button.clicked.connect(lambda: self.show_page("tasks"))
        self.content_nav_button.clicked.connect(lambda: self.show_page("content"))
        self.subscriptions_nav_button.clicked.connect(lambda: self.show_page("subscriptions"))
        self._update_action_state()
        self._update_task_filter_labels()

    def _build_navigation(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("navRail")
        rail.setFixedWidth(184)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(16, 22, 16, 18)
        layout.setSpacing(9)

        brand = QHBoxLayout()
        mark = QLabel("T")
        mark.setObjectName("brandMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        names = QVBoxLayout()
        names.setSpacing(1)
        name = QLabel("Telegram")
        name.setObjectName("brandName")
        caption = QLabel("下载工作台")
        caption.setObjectName("brandCaption")
        names.addWidget(name)
        names.addWidget(caption)
        brand.addWidget(mark)
        brand.addSpacing(8)
        brand.addLayout(names)
        brand.addStretch()
        layout.addLayout(brand)
        layout.addSpacing(25)

        self.tasks_nav_button = self._nav_button("任务中心", active=True)
        self.content_nav_button = self._nav_button("账号内容")
        self.subscriptions_nav_button = self._nav_button("自动订阅")
        self.login_nav_button = self._nav_button("账号登录")
        self.settings_nav_button = self._nav_button("设置")
        self.login_nav_button.clicked.connect(self.login_requested.emit)
        self.settings_nav_button.clicked.connect(self.settings_requested.emit)
        layout.addWidget(self.tasks_nav_button)
        layout.addWidget(self.content_nav_button)
        layout.addWidget(self.subscriptions_nav_button)
        layout.addWidget(self.login_nav_button)
        layout.addWidget(self.settings_nav_button)
        layout.addStretch()

        privacy = QLabel("本地存储\n数据不离开应用目录")
        privacy.setObjectName("muted")
        privacy.setWordWrap(True)
        layout.addWidget(privacy)
        self.version_label = QLabel(f"v{__version__} · stable")
        self.version_label.setObjectName("muted")
        layout.addWidget(self.version_label)
        return rail

    def _build_workspace(self) -> QWidget:
        workspace = QWidget()
        workspace.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(15)

        header = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel("下载任务")
        title.setObjectName("pageTitle")
        subtitle = QLabel("扫描 Telegram 来源，确认后加入可恢复队列")
        subtitle.setObjectName("muted")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        self.account_badge = QLabel("未登录")
        self.account_badge.setObjectName("accountBadge")
        self.account_badge.setProperty("connected", False)
        header.addWidget(self.account_badge, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        layout.addWidget(self._build_source_card())

        queue_header = QHBoxLayout()
        queue_header.addWidget(self._section_label("任务队列"))
        queue_header.addStretch()
        hint = QLabel("支持暂停、断网与程序重启后续传")
        hint.setObjectName("muted")
        queue_header.addWidget(hint)
        layout.addLayout(queue_header)

        task_filters = QHBoxLayout()
        task_filters.setSpacing(9)
        self.task_search = QLineEdit()
        self.task_search.setPlaceholderText("筛选任务名称")
        self.task_search.setClearButtonEnabled(True)
        self.task_filter = QComboBox()
        self.task_filter.setMinimumWidth(132)
        for selected in TaskFilter:
            self.task_filter.addItem(_TASK_FILTER_LABELS[selected], selected.value)
        task_filters.addWidget(self.task_search, 1)
        task_filters.addWidget(self.task_filter)
        layout.addLayout(task_filters)

        self.task_model = TaskTableModel()
        self.task_table = QTableView()
        self.task_table.setModel(self.task_model)
        self.task_table.setAlternatingRowColors(True)
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.task_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.task_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.task_table.setShowGrid(False)
        self.task_table.verticalHeader().hide()
        self.task_table.verticalHeader().setDefaultSectionSize(42)
        header_view = self.task_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, self.task_model.columnCount()):
            header_view.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

        task_panel = QWidget()
        task_panel_layout = QVBoxLayout(task_panel)
        task_panel_layout.setContentsMargins(0, 0, 0, 0)
        task_panel_layout.setSpacing(5)
        self.task_empty_hint = QLabel("尚无下载任务")
        self.task_empty_hint.setObjectName("muted")
        self.task_empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        task_panel_layout.addWidget(self.task_table, 1)
        task_panel_layout.addWidget(self.task_empty_hint)

        detail_panel = QFrame()
        detail_panel.setObjectName("card")
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(12, 9, 12, 10)
        detail_layout.setSpacing(7)
        detail_header = QHBoxLayout()
        self.task_detail_title = QLabel("任务详情")
        self.task_detail_title.setObjectName("sectionTitle")
        self.task_detail_hint = QLabel("请选择一个任务查看媒体明细")
        self.task_detail_hint.setObjectName("muted")
        detail_header.addWidget(self.task_detail_title)
        detail_header.addSpacing(8)
        detail_header.addWidget(self.task_detail_hint)
        detail_header.addStretch()
        self.verify_media_button = QPushButton("校验所选")
        self.repair_media_button = QPushButton("重新下载所选")
        self.open_file_button = QPushButton("打开文件")
        detail_header.addWidget(self.verify_media_button)
        detail_header.addWidget(self.repair_media_button)
        detail_header.addWidget(self.open_file_button)
        detail_layout.addLayout(detail_header)

        self.integrity_progress_panel = QWidget()
        integrity_progress_layout = QHBoxLayout(self.integrity_progress_panel)
        integrity_progress_layout.setContentsMargins(0, 0, 0, 0)
        integrity_progress_layout.setSpacing(8)
        self.integrity_progress_label = QLabel("正在准备校验…")
        self.integrity_progress_label.setObjectName("muted")
        self.integrity_progress = QProgressBar()
        self.integrity_progress.setTextVisible(True)
        self.integrity_progress.setMinimumWidth(180)
        self.integrity_cancel_button = QPushButton("取消校验")
        integrity_progress_layout.addWidget(self.integrity_progress_label)
        integrity_progress_layout.addWidget(self.integrity_progress, 1)
        integrity_progress_layout.addWidget(self.integrity_cancel_button)
        self.integrity_progress_panel.hide()
        detail_layout.addWidget(self.integrity_progress_panel)

        self.task_item_model = TaskItemTableModel()
        self.task_item_table = QTableView()
        self.task_item_table.setModel(self.task_item_model)
        self.task_item_table.setAlternatingRowColors(True)
        self.task_item_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.task_item_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.task_item_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.task_item_table.setShowGrid(False)
        self.task_item_table.verticalHeader().hide()
        self.task_item_table.verticalHeader().setDefaultSectionSize(36)
        item_header = self.task_item_table.horizontalHeader()
        item_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, self.task_item_model.columnCount()):
            item_header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        detail_layout.addWidget(self.task_item_table, 1)

        self.task_splitter = QSplitter(Qt.Orientation.Vertical)
        self.task_splitter.setChildrenCollapsible(False)
        self.task_splitter.addWidget(task_panel)
        self.task_splitter.addWidget(detail_panel)
        self.task_splitter.setStretchFactor(0, 3)
        self.task_splitter.setStretchFactor(1, 2)
        self.task_splitter.setSizes([300, 190])
        layout.addWidget(self.task_splitter, 1)

        actions = QHBoxLayout()
        self.pause_button = QPushButton("暂停")
        self.resume_button = QPushButton("继续")
        self.retry_button = QPushButton("重试失败项")
        self.verify_tasks_button = QPushButton("校验文件")
        self.archive_button = QPushButton("归档所选")
        self.restore_button = QPushButton("恢复所选")
        self.open_button = QPushButton("打开目录")
        for button in (
            self.pause_button,
            self.resume_button,
            self.retry_button,
            self.verify_tasks_button,
            self.archive_button,
            self.restore_button,
            self.open_button,
        ):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        return workspace

    def _build_source_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 15)
        layout.setSpacing(11)
        layout.addWidget(self._section_label("新建下载任务"))

        source_row = QHBoxLayout()
        self.link_input = QLineEdit()
        self.link_input.setPlaceholderText("粘贴消息、频道或群组的 t.me 链接")
        self.link_input.setClearButtonEnabled(True)
        self.link_input.returnPressed.connect(self._emit_scan)
        self.scan_button = QPushButton("扫描预览")
        self.scan_button.setObjectName("primaryButton")
        self.scan_button.setMinimumWidth(112)
        source_row.addWidget(self.link_input, 1)
        source_row.addWidget(self.scan_button)
        layout.addLayout(source_row)

        filters = QHBoxLayout()
        filters.setSpacing(9)
        filters.addWidget(self._field_caption("开始日期"))
        self.date_from = QDateEdit(QDate.currentDate().addDays(-7))
        self.date_from.setCalendarPopup(True)
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        filters.addWidget(self.date_from)
        filters.addWidget(self._field_caption("结束日期（含）"))
        self.date_to = QDateEdit(QDate.currentDate())
        self.date_to.setCalendarPopup(True)
        self.date_to.setDisplayFormat("yyyy-MM-dd")
        filters.addWidget(self.date_to)
        filters.addSpacing(6)
        filters.addWidget(self._field_caption("数量上限"))
        self.limit_input = QSpinBox()
        self.limit_input.setRange(1, 100000)
        self.limit_input.setValue(500)
        self.limit_input.setMinimumWidth(90)
        filters.addWidget(self.limit_input)
        filters.addStretch()
        layout.addLayout(filters)

        media_row = QHBoxLayout()
        media_row.setSpacing(13)
        media_row.addWidget(self._field_caption("媒体类型"))
        self.media_checks: dict[MediaKind, QCheckBox] = {}
        for kind in MediaKind:
            check = QCheckBox(_MEDIA_LABELS[kind])
            check.setChecked(True)
            self.media_checks[kind] = check
            media_row.addWidget(check)
        media_row.addStretch()
        layout.addLayout(media_row)
        return card

    def _build_statistics(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("statsRail")
        rail.setFixedWidth(230)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(17, 22, 17, 18)
        layout.setSpacing(12)
        layout.addWidget(self._section_label("实时概览"))

        speed_card, self.speed_value = self._stat_card("总速度", "0 B/s", accent=True)
        completed_card, self.completed_value = self._stat_card("已完成", "0")
        remaining_card, self.remaining_value = self._stat_card("队列剩余", "0")
        layout.addWidget(speed_card)
        layout.addWidget(completed_card)
        layout.addWidget(remaining_card)

        current = QFrame()
        current.setObjectName("card")
        current_layout = QVBoxLayout(current)
        current_layout.setContentsMargins(13, 13, 13, 14)
        current_layout.setSpacing(9)
        current_layout.addWidget(self._section_label("当前任务"))
        self.current_task_label = QLabel("暂无活动任务")
        self.current_task_label.setObjectName("muted")
        self.current_task_label.setWordWrap(True)
        current_layout.addWidget(self.current_task_label)
        self.current_progress = QProgressBar()
        self.current_progress.setRange(0, 100)
        self.current_progress.setValue(0)
        current_layout.addWidget(self.current_progress)
        self.current_detail = QLabel("等待任务进入队列")
        self.current_detail.setObjectName("muted")
        self.current_detail.setWordWrap(True)
        current_layout.addWidget(self.current_detail)
        layout.addWidget(current)
        layout.addStretch()

        update_hint = QLabel("启动时检查签名更新\nGitHub / 魔搭双源")
        update_hint.setObjectName("muted")
        update_hint.setWordWrap(True)
        layout.addWidget(update_hint)
        return rail

    @staticmethod
    def _nav_button(text: str, *, active: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("navButton")
        button.setProperty("active", active)
        return button

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        return label

    @staticmethod
    def _field_caption(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("fieldCaption")
        return label

    @staticmethod
    def _stat_card(title: str, value: str, *, accent: bool = False) -> tuple[QFrame, QLabel]:
        card = QFrame()
        card.setObjectName("statCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(13, 12, 13, 13)
        layout.setSpacing(5)
        caption = QLabel(title)
        caption.setObjectName("muted")
        number = QLabel(value)
        number.setObjectName("statAccent" if accent else "statValue")
        layout.addWidget(caption)
        layout.addWidget(number)
        return card, number

    def _emit_scan(self) -> None:
        link = self.link_input.text().strip()
        if link:
            self.scan_requested.emit(link)

    def set_scan_busy(self, busy: bool) -> None:
        self.link_input.setEnabled(not busy)
        self.scan_button.setEnabled(not busy)
        self.scan_button.setText("扫描中…" if busy else "扫描预览")

    def _emit_for_selected(self, callback: Callable[[str], None]) -> None:
        task_id = self.selected_task_id()
        if task_id is not None:
            callback(task_id)

    def _emit_task_batch(
        self,
        batch_callback: Callable[[list[str]], None],
        legacy_callback: Callable[[str], None],
    ) -> None:
        task_ids = self.selected_task_ids()
        if not task_ids:
            return
        batch_callback(task_ids)
        if len(task_ids) == 1:
            legacy_callback(task_ids[0])

    def _update_action_state(self, *_args) -> None:
        tasks = self._selected_task_summaries()
        self.pause_button.setEnabled(
            any(
                not task.archived
                and task.status
                in {
                    TaskStatus.QUEUED,
                    TaskStatus.DOWNLOADING,
                    TaskStatus.WAITING_RETRY,
                }
                for task in tasks
            )
        )
        self.resume_button.setEnabled(
            any(not task.archived and task.status is TaskStatus.PAUSED for task in tasks)
        )
        self.retry_button.setEnabled(
            any(not task.archived and task.status is TaskStatus.PARTIAL_FAILURE for task in tasks)
        )
        self.verify_tasks_button.setEnabled(
            not self._integrity_busy
            and any(
                not task.archived
                and task.status in {TaskStatus.COMPLETED, TaskStatus.PARTIAL_FAILURE}
                for task in tasks
            )
        )
        self.archive_button.setEnabled(
            bool(tasks)
            and all(not task.archived and task.status is TaskStatus.COMPLETED for task in tasks)
        )
        self.restore_button.setEnabled(bool(tasks) and all(task.archived for task in tasks))
        self.open_button.setEnabled(len(tasks) == 1)
        self._update_media_action_state()

    def _selected_task_summary(self) -> TaskSummary | None:
        tasks = self._selected_task_summaries()
        return tasks[0] if tasks else None

    def _selected_task_summaries(self) -> list[TaskSummary]:
        tasks: list[TaskSummary] = []
        for row in self._selected_task_rows():
            task = self.task_model.task_at(row)
            if task is not None:
                tasks.append(task)
        return tasks

    def _selected_task_rows(self) -> list[int]:
        return sorted({index.row() for index in self.task_table.selectionModel().selectedRows()})

    def selected_task_ids(self) -> list[str]:
        return [task.id for task in self._selected_task_summaries()]

    def selected_task_id(self) -> str | None:
        task_ids = self.selected_task_ids()
        return task_ids[0] if task_ids else None

    def selected_media_kinds(self) -> frozenset[MediaKind]:
        return frozenset(kind for kind, check in self.media_checks.items() if check.isChecked())

    def set_task_summaries(self, tasks: list[TaskSummary]) -> None:
        selected_task_ids = self.selected_task_ids()
        self._restoring_task_selection = True
        try:
            self.task_model.set_tasks(tasks)
            self._restore_task_selection(selected_task_ids)
        finally:
            self._restoring_task_selection = False
        self._update_task_filter_labels()
        self._task_selection_changed()

        active_tasks = [task for task in tasks if not task.archived]
        total_speed = sum(
            task.speed_bps for task in active_tasks if task.status is TaskStatus.DOWNLOADING
        )
        completed = sum(task.completed_items for task in active_tasks)
        remaining = sum(max(0, task.total_items - task.completed_items) for task in active_tasks)
        self.speed_value.setText(self._format_rate(total_speed))
        self.completed_value.setText(str(completed))
        self.remaining_value.setText(str(remaining))

        active = next(
            (
                task
                for task in active_tasks
                if task.status in {TaskStatus.DOWNLOADING, TaskStatus.WAITING_RETRY}
            ),
            None,
        )
        if active is None:
            self.current_task_label.setText("暂无活动任务")
            self.current_progress.setValue(0)
            self.current_detail.setText("等待任务进入队列")
            return

        if active.total_bytes is not None and active.total_bytes > 0:
            progress = round(active.downloaded_bytes * 100 / active.total_bytes)
        elif active.total_items > 0:
            progress = round(active.completed_items * 100 / active.total_items)
        else:
            progress = 0
        self.current_task_label.setText(active.title)
        self.current_progress.setValue(max(0, min(100, progress)))
        detail = f"{active.progress_text} · {active.speed_text}"
        if active.remaining_text != "—":
            detail += f" · 剩余 {active.remaining_text}"
        self.current_detail.setText(detail)

    def set_task_items(
        self,
        task_id: str,
        items: list[TaskItemSummary],
    ) -> None:
        tasks = self._selected_task_summaries()
        if len(tasks) != 1 or tasks[0].id != task_id:
            return
        selected_media_ids = (
            self.selected_media_ids() if self._detail_task_id == task_id else []
        )
        self._detail_task_id = task_id
        self.task_detail_title.setText(tasks[0].title)
        self.task_detail_hint.setText(f"共 {len(items)} 个媒体文件")
        self.task_item_model.set_items(items)
        self._restore_media_selection(selected_media_ids)
        self._update_media_action_state()

    def _task_selection_changed(self, *_args) -> None:
        if self._restoring_task_selection:
            return
        tasks = self._selected_task_summaries()
        if len(tasks) == 1:
            task = tasks[0]
            if self._detail_task_id != task.id:
                self._detail_task_id = task.id
                self.task_item_model.set_items([])
            self.task_detail_title.setText(task.title)
            self.task_detail_hint.setText("正在加载媒体明细…")
        elif tasks:
            self._clear_task_details(
                "批量选择",
                f"已选 {len(tasks)} 个任务，可使用下方批量操作",
            )
        else:
            self._clear_task_details("任务详情", "请选择一个任务查看媒体明细")
        self.task_selection_changed.emit([task.id for task in tasks])
        self._update_action_state()

    def _clear_task_details(self, title: str, hint: str) -> None:
        self._detail_task_id = None
        self.task_detail_title.setText(title)
        self.task_detail_hint.setText(hint)
        self.task_item_model.set_items([])
        self._update_media_action_state()

    def _restore_task_selection(self, task_ids: list[str]) -> None:
        selection = self.task_table.selectionModel()
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        for task_id in task_ids:
            row = self.task_model.row_for_task_id(task_id)
            if row is not None:
                selection.select(self.task_model.index(row, 0), flags)

    def _restore_media_selection(self, item_ids: list[str]) -> None:
        wanted = set(item_ids)
        if not wanted:
            return
        selection = self.task_item_table.selectionModel()
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        for row in range(self.task_item_model.rowCount()):
            item = self.task_item_model.item_at(row)
            if item is not None and item.id in wanted:
                selection.select(self.task_item_model.index(row, 0), flags)

    def _apply_task_filter(self, *_args) -> None:
        selected_task_ids = self.selected_task_ids()
        selected = TaskFilter(str(self.task_filter.currentData()))
        self._restoring_task_selection = True
        try:
            self.task_model.set_filter(selected, self.task_search.text())
            self._restore_task_selection(selected_task_ids)
        finally:
            self._restoring_task_selection = False
        self._update_task_filter_labels()
        self._task_selection_changed()

    def _update_task_filter_labels(self) -> None:
        counts = self.task_model.filter_counts()
        blocker = QSignalBlocker(self.task_filter)
        try:
            for index, selected in enumerate(TaskFilter):
                self.task_filter.setItemText(
                    index,
                    f"{_TASK_FILTER_LABELS[selected]} ({counts[selected]})",
                )
        finally:
            del blocker
        self.task_empty_hint.setVisible(self.task_model.rowCount() == 0)
        self.task_empty_hint.setText(
            "尚无下载任务"
            if not self.task_search.text().strip()
            and TaskFilter(str(self.task_filter.currentData())) is TaskFilter.ALL
            else "没有符合当前筛选的任务"
        )

    def _confirm_archive(self) -> None:
        task_ids = self.selected_task_ids()
        if not task_ids:
            return
        answer = QMessageBox.question(
            self,
            "归档完成任务",
            f"归档所选 {len(task_ids)} 个任务？下载文件会保留，可随时恢复。",
        )
        if answer is QMessageBox.StandardButton.Yes:
            self.archive_tasks_requested.emit(task_ids)

    def _confirm_restore(self) -> None:
        task_ids = self.selected_task_ids()
        if not task_ids:
            return
        answer = QMessageBox.question(
            self,
            "恢复归档任务",
            f"恢复所选 {len(task_ids)} 个任务到任务队列？",
        )
        if answer is QMessageBox.StandardButton.Yes:
            self.restore_tasks_requested.emit(task_ids)

    def _selected_media_summaries(self) -> list[TaskItemSummary]:
        items: list[TaskItemSummary] = []
        rows = sorted(
            {index.row() for index in self.task_item_table.selectionModel().selectedRows()}
        )
        for row in rows:
            item = self.task_item_model.item_at(row)
            if item is not None:
                items.append(item)
        return items

    def _selected_media_summary(self) -> TaskItemSummary | None:
        items = self._selected_media_summaries()
        return items[0] if len(items) == 1 else None

    def selected_media_ids(self) -> list[str]:
        return [item.id for item in self._selected_media_summaries()]

    @staticmethod
    def _can_open_media(item: TaskItemSummary) -> bool:
        return (
            item.status is ItemStatus.COMPLETED
            and item.integrity_status not in _INTEGRITY_FAILURES
        )

    @staticmethod
    def _can_verify_media(item: TaskItemSummary) -> bool:
        return (
            item.status is ItemStatus.COMPLETED
            or item.integrity_status in _INTEGRITY_FAILURES
        )

    def _update_media_action_state(self, *_args) -> None:
        items = self._selected_media_summaries()
        single = items[0] if len(items) == 1 else None
        self.open_file_button.setEnabled(
            single is not None and self._can_open_media(single)
        )
        self.verify_media_button.setEnabled(
            not self._integrity_busy and any(self._can_verify_media(item) for item in items)
        )
        self.repair_media_button.setEnabled(
            not self._integrity_busy
            and any(item.integrity_status in _INTEGRITY_FAILURES for item in items)
        )

    def _emit_selected_media(self) -> None:
        item = self._selected_media_summary()
        if item is not None and self._can_open_media(item):
            self.open_media_requested.emit(item.id)

    def _emit_open_media(self, index) -> None:
        item = self.task_item_model.item_at(index.row())
        if item is not None and self._can_open_media(item):
            self.open_media_requested.emit(item.id)

    def _emit_verify_media(self) -> None:
        item_ids = [
            item.id
            for item in self._selected_media_summaries()
            if self._can_verify_media(item)
        ]
        if item_ids and not self._integrity_busy:
            self.verify_media_requested.emit(item_ids)

    def _confirm_repair_media(self) -> None:
        item_ids = [
            item.id
            for item in self._selected_media_summaries()
            if item.integrity_status in _INTEGRITY_FAILURES
        ]
        if not item_ids or self._integrity_busy:
            return
        answer = QMessageBox.question(
            self,
            "重新下载异常文件",
            f"重新下载所选 {len(item_ids)} 个异常文件？"
            "现有文件和分片会先保留为 .corrupt* 留档。",
        )
        if answer is QMessageBox.StandardButton.Yes:
            self.repair_media_requested.emit(item_ids)

    def _emit_verify_tasks(self) -> None:
        task_ids = [
            task.id
            for task in self._selected_task_summaries()
            if not task.archived
            and task.status in {TaskStatus.COMPLETED, TaskStatus.PARTIAL_FAILURE}
        ]
        if task_ids and not self._integrity_busy:
            self.verify_tasks_requested.emit(task_ids)

    def _emit_integrity_cancel(self) -> None:
        if not self._integrity_busy:
            return
        self.integrity_progress_label.setText("正在取消校验…")
        self.integrity_cancel_button.setEnabled(False)
        self.integrity_cancel_requested.emit()

    def set_integrity_busy(self, busy: bool) -> None:
        self._integrity_busy = bool(busy)
        if busy:
            self.integrity_progress_label.setText("正在准备校验…")
            self.integrity_progress.setRange(0, 0)
            self.integrity_progress_panel.show()
            self.integrity_cancel_button.setEnabled(True)
        else:
            self.integrity_progress_panel.hide()
            self.integrity_progress.setRange(0, 1)
            self.integrity_progress.setValue(0)
            self.integrity_cancel_button.setEnabled(False)
        self._update_action_state()
        self._update_media_action_state()

    def set_integrity_progress(self, progress: IntegrityProgress | None) -> None:
        if progress is None:
            if not self._integrity_busy:
                self.integrity_progress_panel.hide()
            return
        self.integrity_progress_panel.show()
        self.integrity_progress_label.setText(f"正在校验 {progress.file_name}")
        self.integrity_progress.setRange(0, max(1, progress.total))
        self.integrity_progress.setValue(progress.completed)
        self.integrity_progress.setFormat(
            f"{progress.completed} / {progress.total}"
        )

    @staticmethod
    def _format_rate(value: float) -> str:
        if value <= 0:
            return "0 B/s"
        amount = value
        for unit in ("B/s", "KB/s", "MB/s", "GB/s", "TB/s"):
            if amount < 1024 or unit == "TB/s":
                return f"{amount:.0f} {unit}" if unit == "B/s" else f"{amount:.1f} {unit}"
            amount /= 1024
        return "0 B/s"

    def set_account(self, display_name: str | None) -> None:
        self.account_badge.setText(display_name or "未登录")
        self.account_badge.setProperty("connected", bool(display_name))
        self.account_badge.style().unpolish(self.account_badge)
        self.account_badge.style().polish(self.account_badge)
        self.content_page.set_logged_in(bool(display_name))
        self.subscriptions_page.set_logged_in(bool(display_name))

    def show_page(self, name: str) -> None:
        content = name == "content"
        subscriptions = name == "subscriptions"
        page = (
            self.content_page
            if content
            else self.subscriptions_page
            if subscriptions
            else self.task_page
        )
        self.page_stack.setCurrentWidget(page)
        self.statistics_panel.setVisible(not (content or subscriptions))
        active = (
            self.content_nav_button
            if content
            else self.subscriptions_nav_button
            if subscriptions
            else self.tasks_nav_button
        )
        self._set_nav_active(active)
        if content:
            self.content_activated.emit()
        elif subscriptions:
            self.subscriptions_activated.emit()

    def open_link_preview(self, link: str) -> None:
        self.link_input.setText(link)
        self.show_page("tasks")
        self.scan_requested.emit(link)

    def _set_nav_active(self, active_button: QPushButton) -> None:
        for button in (
            self.tasks_nav_button,
            self.content_nav_button,
            self.subscriptions_nav_button,
        ):
            button.setProperty("active", button is active_button)
            button.style().unpolish(button)
            button.style().polish(button)
