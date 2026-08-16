from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from telegram_downloader.diagnostics import (
    DiagnosticProgress,
    DiagnosticReport,
    DiagnosticResult,
    DiagnosticStatus,
)

_INVALID_INDEX = QModelIndex()

STATUS_LABELS = {
    DiagnosticStatus.PENDING: "等待中",
    DiagnosticStatus.RUNNING: "检查中",
    DiagnosticStatus.PASSED: "正常",
    DiagnosticStatus.WARNING: "需关注",
    DiagnosticStatus.FAILED: "异常",
    DiagnosticStatus.SKIPPED: "已跳过",
    DiagnosticStatus.CANCELLED: "已取消",
}


class DiagnosticResultModel(QAbstractTableModel):
    HEADERS = ("检查项", "状态", "耗时", "说明")

    def __init__(self) -> None:
        super().__init__()
        self._results: tuple[DiagnosticResult, ...] = ()

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
        item = self._results[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return item.id
        if role == Qt.ItemDataRole.ToolTipRole:
            return item.summary
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        values = (
            item.title,
            STATUS_LABELS[item.status],
            f"{item.duration_ms} ms",
            item.summary,
        )
        return values[index.column()]

    def set_results(self, results: Sequence[DiagnosticResult]) -> None:
        self.beginResetModel()
        self._results = tuple(results)
        self.endResetModel()

    def result_at(self, row: int) -> DiagnosticResult:
        return self._results[row]


class DiagnosticsPage(QWidget):
    run_requested = Signal()
    cancel_requested = Signal()
    export_requested = Signal()
    open_directory_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.report: DiagnosticReport | None = None
        self._running = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(13)

        title = QLabel("健康诊断")
        title.setObjectName("pageTitle")
        subtitle = QLabel("检查运行环境、账号连接、数据库与双源签名更新状态")
        subtitle.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.status_banner = QLabel("尚未运行自检")
        self.status_banner.setObjectName("contentHint")
        self.status_banner.setWordWrap(True)
        layout.addWidget(self.status_banner)

        progress_card = QFrame()
        progress_card.setObjectName("card")
        progress_layout = QVBoxLayout(progress_card)
        progress_layout.setContentsMargins(14, 12, 14, 13)
        progress_layout.setSpacing(8)
        progress_heading = QHBoxLayout()
        self.report_context_label = QLabel("等待本次自检")
        self.report_context_label.setObjectName("sectionTitle")
        self.progress_label = QLabel("点击“开始自检”后逐项检查")
        self.progress_label.setObjectName("muted")
        progress_heading.addWidget(self.report_context_label)
        progress_heading.addStretch()
        progress_heading.addWidget(self.progress_label)
        progress_layout.addLayout(progress_heading)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0 / 0")
        progress_layout.addWidget(self.progress_bar)
        layout.addWidget(progress_card)

        self.model = DiagnosticResultModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(40)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        privacy = QLabel(
            "诊断不会修改任务或账号数据。导出包只包含聚合状态与固定说明，不包含日志、凭据、群组、消息或文件路径。"
        )
        privacy.setObjectName("muted")
        privacy.setWordWrap(True)
        layout.addWidget(privacy)

        self.error_label = QLabel()
        self.error_label.setObjectName("errorText")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        actions = QHBoxLayout()
        self.start_button = QPushButton("开始自检")
        self.start_button.setObjectName("primaryButton")
        self.cancel_button = QPushButton("取消")
        self.export_button = QPushButton("导出诊断包")
        self.open_button = QPushButton("打开诊断目录")
        actions.addWidget(self.start_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        actions.addWidget(self.export_button)
        actions.addWidget(self.open_button)
        layout.addLayout(actions)

        self.start_button.clicked.connect(self.run_requested.emit)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        self.export_button.clicked.connect(self.export_requested.emit)
        self.open_button.clicked.connect(self.open_directory_requested.emit)
        self.set_running(False)

    def set_report(self, report: DiagnosticReport | None, *, historical: bool) -> None:
        self.report = report
        self.model.set_results(report.results if report is not None else ())
        if report is None:
            self.report_context_label.setText("等待本次自检")
            self.status_banner.setText("尚未运行自检")
        else:
            self.report_context_label.setText("上次自检结果" if historical else "本次自检结果")
            self.status_banner.setText(
                f"检查完成：{STATUS_LABELS[report.status]} · 共 {len(report.results)} 项"
            )
            self.progress_bar.setRange(0, len(report.results))
            self.progress_bar.setValue(len(report.results))
            self.progress_bar.setFormat(f"{len(report.results)} / {len(report.results)}")
            self.progress_label.setText("全部检查已结束")
        self._update_buttons()

    def set_progress(self, progress: DiagnosticProgress | None) -> None:
        if progress is None:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("0 / 0")
            self.progress_label.setText("点击“开始自检”后逐项检查")
            return
        self.progress_bar.setRange(0, progress.total)
        self.progress_bar.setValue(progress.completed)
        self.progress_bar.setFormat(f"{progress.completed} / {progress.total}")
        self.progress_label.setText(
            f"正在检查：{progress.current_title}"
            if progress.current_title is not None
            else f"检查完成：{STATUS_LABELS[progress.status]}"
        )

    def set_running(self, running: bool) -> None:
        self._running = running
        if running:
            self.report_context_label.setText("本次自检进行中")
            self.status_banner.setText("正在执行健康检查，请稍候…")
            self.show_error("")
        self._update_buttons()

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def _update_buttons(self) -> None:
        self.start_button.setEnabled(not self._running)
        self.cancel_button.setEnabled(self._running)
        self.export_button.setEnabled(not self._running and self.report is not None)
        self.open_button.setEnabled(not self._running)
