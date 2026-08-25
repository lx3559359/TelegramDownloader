from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor
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
from telegram_downloader.ui.diagnostic_details import present_diagnostic_details
from telegram_downloader.ui.effects import ElevationLevel, apply_elevation

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
_STATUS_COLORS = {
    DiagnosticStatus.PENDING: "#94a3b8",
    DiagnosticStatus.RUNNING: "#67e8f9",
    DiagnosticStatus.PASSED: "#6ee7b7",
    DiagnosticStatus.WARNING: "#fbbf24",
    DiagnosticStatus.FAILED: "#fda4af",
    DiagnosticStatus.SKIPPED: "#a5b4fc",
    DiagnosticStatus.CANCELLED: "#cbd5e1",
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
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 1:
            return QColor(_STATUS_COLORS[item.status])
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
        self._cancelling = False
        self._exporting = False
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
        self.status_banner.setObjectName("diagnosticStatus")
        self.status_banner.setWordWrap(True)
        self.status_banner.setProperty("status", DiagnosticStatus.PENDING.value)
        layout.addWidget(self.status_banner)

        self.progress_card = QFrame()
        self.progress_card.setObjectName("elevatedCard")
        apply_elevation(self.progress_card, ElevationLevel.MAJOR)
        progress_layout = QVBoxLayout(self.progress_card)
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
        layout.addWidget(self.progress_card)

        self.results_card = QFrame()
        self.results_card.setObjectName("elevatedCard")
        apply_elevation(self.results_card, ElevationLevel.MAJOR)
        results_layout = QVBoxLayout(self.results_card)
        results_layout.setContentsMargins(14, 12, 14, 14)
        results_layout.setSpacing(10)

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
        results_layout.addWidget(self.table, 1)

        self.details_card = QFrame(self.results_card)
        self.details_card.setObjectName("elevatedSubCard")
        details_layout = QHBoxLayout(self.details_card)
        details_layout.setContentsMargins(10, 8, 10, 8)
        details_layout.setSpacing(18)

        remediation_layout = QVBoxLayout()
        remediation_heading = QLabel("处理建议")
        remediation_heading.setObjectName("sectionTitle")
        self.details_remediation_label = QLabel()
        self.details_remediation_label.setObjectName("muted")
        self.details_remediation_label.setWordWrap(True)
        self.details_remediation_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        remediation_layout.addWidget(remediation_heading)
        remediation_layout.addWidget(self.details_remediation_label)
        remediation_layout.addStretch()

        metrics_layout = QVBoxLayout()
        metrics_heading = QLabel("安全指标")
        metrics_heading.setObjectName("sectionTitle")
        self.details_metrics_label = QLabel()
        self.details_metrics_label.setObjectName("muted")
        self.details_metrics_label.setWordWrap(True)
        self.details_metrics_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        metrics_layout.addWidget(metrics_heading)
        metrics_layout.addWidget(self.details_metrics_label)
        metrics_layout.addStretch()

        details_layout.addLayout(remediation_layout, 1)
        details_layout.addLayout(metrics_layout, 1)
        results_layout.addWidget(self.details_card)

        privacy = QLabel(
            "诊断不会修改任务或账号数据。导出包只包含聚合状态与固定说明，不包含日志、凭据、群组、消息或文件路径。"
        )
        privacy.setObjectName("muted")
        privacy.setWordWrap(True)
        results_layout.addWidget(privacy)

        self.error_label = QLabel()
        self.error_label.setObjectName("errorText")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        results_layout.addWidget(self.error_label)

        self.actions_card = QFrame(self.results_card)
        self.actions_card.setObjectName("elevatedSubCard")
        apply_elevation(self.actions_card, ElevationLevel.SECONDARY)
        actions = QHBoxLayout(self.actions_card)
        actions.setContentsMargins(10, 8, 10, 8)
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
        results_layout.addWidget(self.actions_card)
        layout.addWidget(self.results_card, 1)

        self.start_button.clicked.connect(self.run_requested.emit)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        self.export_button.clicked.connect(self.export_requested.emit)
        self.open_button.clicked.connect(self.open_directory_requested.emit)
        self.table.selectionModel().currentRowChanged.connect(
            self._show_details_for_index
        )
        self.set_running(False)

    def set_report(self, report: DiagnosticReport | None, *, historical: bool) -> None:
        self.report = report
        self.model.set_results(report.results if report is not None else ())
        if report is None:
            self.table.clearSelection()
            self.table.setCurrentIndex(QModelIndex())
            self._clear_details()
            self.report_context_label.setText("等待本次自检")
            self._set_status_banner("尚未运行自检", DiagnosticStatus.PENDING)
        else:
            first = self.model.index(0, 0)
            self.table.setCurrentIndex(first)
            self.table.selectRow(0)
            self._show_details_for_index(first)
            completed = report.finished_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            self.report_context_label.setText(
                f"历史结果 · 上次自检 {completed}"
                if historical
                else f"本次自检结果 · {completed}"
            )
            self._set_status_banner(
                f"检查完成：{STATUS_LABELS[report.status]} · 共 {len(report.results)} 项",
                report.status,
            )
            self.progress_bar.setRange(0, len(report.results))
            self.progress_bar.setValue(len(report.results))
            self.progress_bar.setFormat(
                f"{len(report.results)} / {len(report.results)} · 100%"
            )
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
        percent = round(progress.completed * 100 / progress.total)
        self.progress_bar.setFormat(
            f"{progress.completed} / {progress.total} · {percent}%"
        )
        if self._cancelling:
            message = "正在取消，当前本地检查完成后停止"
            self.progress_label.setText(message)
            self._set_status_banner(message, DiagnosticStatus.RUNNING)
            return
        self.progress_label.setText(
            f"正在检查：{progress.current_title}"
            if progress.current_title is not None
            else f"检查完成：{STATUS_LABELS[progress.status]}"
        )
        self._set_status_banner(
            "正在执行健康检查，请稍候…"
            if progress.current_title is not None
            else f"检查完成：{STATUS_LABELS[progress.status]}",
            DiagnosticStatus.RUNNING
            if progress.current_title is not None
            else progress.status,
        )

    def set_running(self, running: bool) -> None:
        self._running = running
        self._cancelling = False
        if running:
            self.report_context_label.setText("本次自检进行中")
            self._set_status_banner(
                "正在执行健康检查，请稍候…",
                DiagnosticStatus.RUNNING,
            )
            self.show_error("")
        self._update_buttons()

    def _show_details_for_index(self, current: QModelIndex, _previous=None) -> None:
        if not current.isValid() or not 0 <= current.row() < self.model.rowCount():
            self._clear_details()
            return
        details = present_diagnostic_details(self.model.result_at(current.row()))
        self.details_remediation_label.setText(details.remediation)
        self.details_metrics_label.setText(details.metrics_text)

    def _clear_details(self) -> None:
        self.details_remediation_label.clear()
        self.details_metrics_label.clear()

    def set_cancelling(self, cancelling: bool) -> None:
        self._cancelling = bool(cancelling and self._running)
        if self._cancelling:
            message = "正在取消，当前本地检查完成后停止"
            self.progress_label.setText(message)
            self._set_status_banner(message, DiagnosticStatus.RUNNING)
        self._update_buttons()

    def set_export_busy(self, busy: bool) -> None:
        self._exporting = busy
        self.export_button.setText("正在导出…" if busy else "导出诊断包")
        self._update_buttons()

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def _update_buttons(self) -> None:
        self.start_button.setEnabled(not self._running)
        self.cancel_button.setEnabled(self._running and not self._cancelling)
        self.export_button.setEnabled(
            not self._running and not self._exporting and self.report is not None
        )
        self.open_button.setEnabled(not self._running)

    def _set_status_banner(self, text: str, status: DiagnosticStatus) -> None:
        self.status_banner.setText(text)
        self.status_banner.setProperty("status", status.value)
        self.status_banner.style().unpolish(self.status_banner)
        self.status_banner.style().polish(self.status_banner)
