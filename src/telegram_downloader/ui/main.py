from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from telegram_downloader import __version__
from telegram_downloader.domain import MediaKind, TaskStatus
from telegram_downloader.ui.models import TaskSummary, TaskTableModel
from telegram_downloader.ui.theme import DARK_STYLESHEET, ensure_cjk_font

_MEDIA_LABELS = {
    MediaKind.PHOTO: "图片",
    MediaKind.VIDEO: "视频",
    MediaKind.AUDIO: "音频",
    MediaKind.VOICE: "语音",
    MediaKind.DOCUMENT: "文档",
    MediaKind.ARCHIVE: "压缩包",
}


class MainWindow(QMainWindow):
    scan_requested = Signal(str)
    pause_requested = Signal(str)
    resume_requested = Signal(str)
    retry_failed_requested = Signal(str)
    open_directory_requested = Signal(str)
    settings_requested = Signal()
    login_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
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
        root_layout.addWidget(self._build_workspace(), 1)
        root_layout.addWidget(self._build_statistics())
        self.setCentralWidget(root)
        self.statusBar().showMessage("准备就绪")

        self.scan_button.clicked.connect(self._emit_scan)
        self.pause_button.clicked.connect(
            lambda: self._emit_for_selected(self.pause_requested.emit)
        )
        self.resume_button.clicked.connect(
            lambda: self._emit_for_selected(self.resume_requested.emit)
        )
        self.retry_button.clicked.connect(
            lambda: self._emit_for_selected(self.retry_failed_requested.emit)
        )
        self.open_button.clicked.connect(
            lambda: self._emit_for_selected(self.open_directory_requested.emit)
        )
        self.task_table.selectionModel().selectionChanged.connect(
            self._update_action_state
        )
        self._update_action_state()

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

        tasks = self._nav_button("任务中心", active=True)
        login = self._nav_button("账号登录")
        settings = self._nav_button("设置")
        login.clicked.connect(self.login_requested.emit)
        settings.clicked.connect(self.settings_requested.emit)
        layout.addWidget(tasks)
        layout.addWidget(login)
        layout.addWidget(settings)
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

        self.task_model = TaskTableModel()
        self.task_table = QTableView()
        self.task_table.setModel(self.task_model)
        self.task_table.setAlternatingRowColors(True)
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.task_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.task_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.task_table.setShowGrid(False)
        self.task_table.verticalHeader().hide()
        self.task_table.verticalHeader().setDefaultSectionSize(42)
        header_view = self.task_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, self.task_model.columnCount()):
            header_view.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.task_table, 1)

        actions = QHBoxLayout()
        self.pause_button = QPushButton("暂停")
        self.resume_button = QPushButton("继续")
        self.retry_button = QPushButton("重试失败项")
        self.open_button = QPushButton("打开目录")
        for button in (
            self.pause_button,
            self.resume_button,
            self.retry_button,
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

    def _update_action_state(self, *_args) -> None:
        enabled = self.selected_task_id() is not None
        for button in (
            self.pause_button,
            self.resume_button,
            self.retry_button,
            self.open_button,
        ):
            button.setEnabled(enabled)

    def selected_task_id(self) -> str | None:
        rows = self.task_table.selectionModel().selectedRows()
        if not rows:
            return None
        value = self.task_model.data(rows[0], Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    def selected_media_kinds(self) -> frozenset[MediaKind]:
        return frozenset(kind for kind, check in self.media_checks.items() if check.isChecked())

    def set_task_summaries(self, tasks: list[TaskSummary]) -> None:
        selected_task_id = self.selected_task_id()
        self.task_model.set_tasks(tasks)
        if selected_task_id is not None:
            selected_row = next(
                (row for row, task in enumerate(tasks) if task.id == selected_task_id),
                None,
            )
            if selected_row is not None:
                self.task_table.selectRow(selected_row)
        self._update_action_state()

        total_speed = sum(
            task.speed_bps for task in tasks if task.status is TaskStatus.DOWNLOADING
        )
        completed = sum(task.completed_items for task in tasks)
        remaining = sum(
            max(0, task.total_items - task.completed_items) for task in tasks
        )
        self.speed_value.setText(self._format_rate(total_speed))
        self.completed_value.setText(str(completed))
        self.remaining_value.setText(str(remaining))

        active = next(
            (
                task
                for task in tasks
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

    @staticmethod
    def _format_rate(value: float) -> str:
        if value <= 0:
            return "0 B/s"
        amount = value
        for unit in ("B/s", "KB/s", "MB/s", "GB/s", "TB/s"):
            if amount < 1024 or unit == "TB/s":
                return (
                    f"{amount:.0f} {unit}"
                    if unit == "B/s"
                    else f"{amount:.1f} {unit}"
                )
            amount /= 1024
        return "0 B/s"

    def set_account(self, display_name: str | None) -> None:
        self.account_badge.setText(display_name or "未登录")
        self.account_badge.setProperty("connected", bool(display_name))
        self.account_badge.style().unpolish(self.account_badge)
        self.account_badge.style().polish(self.account_badge)
