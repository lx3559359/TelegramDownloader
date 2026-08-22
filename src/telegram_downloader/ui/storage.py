from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSignalBlocker,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from telegram_downloader.storage_maintenance import (
    ManualCleanupConfirmation,
    SafeCleanupConfirmation,
)
from telegram_downloader.storage_models import (
    StorageCategory,
    StorageCategorySummary,
    StorageEntry,
    StorageExecutionResult,
    StorageInventory,
    StorageMaintenanceState,
    StorageResultCode,
)
from telegram_downloader.ui.effects import ElevationLevel, apply_elevation

_INVALID_INDEX = QModelIndex()
_AUTOMATIC_CATEGORIES = frozenset(tuple(StorageCategory)[:5])
_MANUAL_CATEGORIES = frozenset(tuple(StorageCategory)[5:])
_CATEGORY_LABELS = {
    StorageCategory.THUMBNAILS: "缩略图",
    StorageCategory.TEMP: "临时文件",
    StorageCategory.ROTATED_LOGS: "轮转日志",
    StorageCategory.UPDATE_STAGING: "更新暂存",
    StorageCategory.UPDATE_BACKUP: "更新备份",
    StorageCategory.DOWNLOAD_PART: "下载分片",
    StorageCategory.CORRUPT_ARCHIVE: "损坏留档",
}
_POLICY_LABELS = {
    StorageCategory.THUMBNAILS: "超过 1 GiB 后清至 900 MiB",
    StorageCategory.TEMP: "保留 7 天",
    StorageCategory.ROTATED_LOGS: "保留 30 天",
    StorageCategory.UPDATE_STAGING: "保留 7 天",
    StorageCategory.UPDATE_BACKUP: "保留最新 1 份有效备份",
    StorageCategory.DOWNLOAD_PART: "仅手动双确认",
    StorageCategory.CORRUPT_ARCHIVE: "仅手动双确认",
}
_REASON_LABELS = {
    StorageResultCode.UNSAFE_PATH: "路径类型不安全",
    StorageResultCode.PROTECTED_BY_TASK: "任务仍需保护",
    StorageResultCode.PROTECTED_BY_UPDATE: "更新事务正在使用",
    StorageResultCode.STATE_CHANGED: "文件状态已变化",
}


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    amount = float(value)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        amount /= 1024
        if amount < 1024 or unit == "TiB":
            return f"{amount:.2f} {unit}"
    return f"{value} B"


class StorageCategoryModel(QAbstractTableModel):
    HEADERS = ("类别", "当前大小", "可释放", "保留策略", "最近扫描", "状态")

    def __init__(self) -> None:
        super().__init__()
        self._summaries: dict[StorageCategory, StorageCategorySummary] = {}

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(StorageCategory)

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

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(StorageCategory):
            return None
        category = tuple(StorageCategory)[index.row()]
        summary = self._summaries.get(category)
        if role == Qt.ItemDataRole.UserRole:
            return category
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if summary is None:
            values = (
                _CATEGORY_LABELS[category],
                "尚未扫描",
                "尚未扫描",
                _POLICY_LABELS[category],
                "尚未扫描",
                "尚未扫描",
            )
        else:
            scanned = summary.scanned_at.astimezone().strftime("%Y-%m-%d %H:%M")
            values = (
                _CATEGORY_LABELS[category],
                _format_bytes(summary.total_bytes),
                _format_bytes(summary.reclaimable_bytes),
                _POLICY_LABELS[category],
                scanned,
                "可清理" if summary.reclaimable_count else "无需清理",
            )
        return values[index.column()]

    def set_summaries(self, summaries: Sequence[StorageCategorySummary]) -> None:
        self.beginResetModel()
        self._summaries = {summary.category: summary for summary in summaries}
        self.endResetModel()


class ManualCleanupModel(QAbstractTableModel):
    HEADERS = (
        "选择",
        "相对文件名",
        "关联任务",
        "类别",
        "大小",
        "修改时间",
        "状态",
        "保护原因",
    )

    def __init__(self) -> None:
        super().__init__()
        self._entries: tuple[StorageEntry, ...] = ()
        self._selected: set[str] = set()

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._entries)

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

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        entry = self._entries[index.row()]
        if index.column() == 0 and entry.selectable:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._entries):
            return None
        entry = self._entries[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return entry.id
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            if not entry.selectable:
                return None
            return Qt.CheckState.Checked if entry.id in self._selected else Qt.CheckState.Unchecked
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        modified = datetime.fromtimestamp(entry.mtime_ns / 1_000_000_000).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        values = (
            "",
            entry.relative_path.as_posix(),
            entry.display_name or "来源无法确认",
            _CATEGORY_LABELS[entry.category],
            _format_bytes(entry.size),
            modified,
            "可删除" if entry.selectable else "受保护",
            "" if entry.reason is None else _REASON_LABELS.get(entry.reason, "受保护"),
        )
        return values[index.column()]

    def setData(
        self,
        index: QModelIndex,
        value,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if not index.isValid() or index.column() != 0 or role != Qt.ItemDataRole.CheckStateRole:
            return False
        entry = self._entries[index.row()]
        if not entry.selectable:
            return False
        if value == Qt.CheckState.Checked:
            self._selected.add(entry.id)
        else:
            self._selected.discard(entry.id)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        return True

    def set_entries(self, entries: Sequence[StorageEntry]) -> None:
        self.beginResetModel()
        self._entries = tuple(entries)
        self._selected.clear()
        self.endResetModel()

    def selected_ids(self) -> tuple[str, ...]:
        return tuple(entry.id for entry in self._entries if entry.id in self._selected)


class ManualCleanupDialog(QDialog):
    prepare_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("管理下载分片与损坏留档")
        self.resize(920, 520)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "只允许选择仓库确认已完成、正式文件仍存在且完整性已验证的残留；受保护项目不能勾选。"
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("muted")
        layout.addWidget(explanation)
        self.model = ManualCleanupModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4, 5, 6, 7):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        self.close_button = QPushButton("关闭")
        self.prepare_button = QPushButton("准备清理所选项目")
        self.prepare_button.setObjectName("primaryButton")
        actions.addWidget(self.close_button)
        actions.addWidget(self.prepare_button)
        layout.addLayout(actions)
        self.close_button.clicked.connect(self.close)
        self.prepare_button.clicked.connect(self._prepare)

    def set_entries(self, entries: Sequence[StorageEntry]) -> None:
        self.model.set_entries(entries)

    def _prepare(self) -> None:
        selected = self.model.selected_ids()
        if not selected:
            QMessageBox.information(self, "尚未选择", "请先选择至少一个可删除项目。")
            return
        answer = QMessageBox.question(
            self,
            "第一次确认",
            f"准备清理 {len(selected)} 个下载残留。删除是永久操作，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.prepare_requested.emit(selected)


class StoragePage(QWidget):
    activated = Signal()
    scan_requested = Signal()
    cancel_requested = Signal()
    automatic_changed = Signal(bool)
    safe_prepare_requested = Signal()
    safe_execute_requested = Signal(str)
    download_scan_requested = Signal()
    manual_prepare_requested = Signal(object)
    manual_execute_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._summaries: dict[StorageCategory, StorageCategorySummary] = {}
        self._disk_free_bytes: int | None = None
        self._busy = False
        self._open_manual_after_scan = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(12)

        title = QLabel("存储空间")
        title.setObjectName("pageTitle")
        subtitle = QLabel("查看项目内空间占用，并以白名单规则安全清理可再生成或已确认的残留")
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        overview_layout = QHBoxLayout()
        overview_specs = (
            ("磁盘可用", "disk_free_value"),
            ("已管理空间", "managed_value"),
            ("安全可释放", "safe_reclaim_value"),
            ("手动可释放", "manual_reclaim_value"),
        )
        self.overview_value_labels: list[QLabel] = []
        for heading, attribute in overview_specs:
            card = QFrame()
            card.setObjectName("elevatedCard")
            apply_elevation(card, ElevationLevel.SECONDARY)
            card_layout = QVBoxLayout(card)
            label = QLabel(heading)
            label.setObjectName("muted")
            value = QLabel("尚未扫描")
            value.setObjectName("sectionTitle")
            card_layout.addWidget(label)
            card_layout.addWidget(value)
            overview_layout.addWidget(card, 1)
            setattr(self, attribute, value)
            self.overview_value_labels.append(value)
        layout.addLayout(overview_layout)

        controls = QHBoxLayout()
        self.automatic_checkbox = QCheckBox("启用空闲自动清理")
        self.scan_button = QPushButton("重新扫描")
        self.cancel_button = QPushButton("取消")
        self.safe_cleanup_button = QPushButton("立即清理安全项目")
        self.safe_cleanup_button.setObjectName("primaryButton")
        self.download_button = QPushButton("管理分片与留档")
        controls.addWidget(self.automatic_checkbox)
        controls.addStretch()
        controls.addWidget(self.scan_button)
        controls.addWidget(self.cancel_button)
        controls.addWidget(self.safe_cleanup_button)
        controls.addWidget(self.download_button)
        layout.addLayout(controls)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        self.category_model = StorageCategoryModel()
        self.category_table = QTableView()
        self.category_table.setModel(self.category_model)
        self.category_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.category_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.category_table.verticalHeader().hide()
        category_header = self.category_table.horizontalHeader()
        category_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        category_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        category_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        category_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        category_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        category_header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.category_table, 1)

        self.last_summary_label = QLabel("尚无清理记录")
        self.last_summary_label.setObjectName("muted")
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        self.error_label = QLabel()
        self.error_label.setObjectName("errorText")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.last_summary_label)
        layout.addWidget(self.result_label)
        layout.addWidget(self.error_label)

        self.manual_dialog = ManualCleanupDialog(self)
        self.manual_dialog.prepare_requested.connect(self.manual_prepare_requested.emit)
        self.scan_button.clicked.connect(self.scan_requested.emit)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        self.safe_cleanup_button.clicked.connect(self.safe_prepare_requested.emit)
        self.download_button.clicked.connect(self._manage_downloads)
        self.automatic_checkbox.toggled.connect(self._automatic_toggled)
        self.set_busy(False)

    def set_state(self, state: StorageMaintenanceState) -> None:
        if not isinstance(state, StorageMaintenanceState):
            return
        if not state.history:
            self.last_summary_label.setText("尚无清理记录")
            return
        last = state.history[-1]
        occurred = last.occurred_at.astimezone().strftime("%Y-%m-%d %H:%M")
        self.last_summary_label.setText(
            f"上次清理 {occurred} · 删除 {last.deleted_count} · "
            f"跳过 {last.skipped_count} · 失败 {last.failed_count} · "
            f"释放 {_format_bytes(last.released_bytes)}"
        )

    def set_inventory(self, inventory: StorageInventory) -> None:
        if not isinstance(inventory, StorageInventory):
            return
        self._disk_free_bytes = inventory.disk_free_bytes
        self._summaries.update({summary.category: summary for summary in inventory.summaries})
        self.category_model.set_summaries(tuple(self._summaries.values()))
        manual_entries = tuple(
            entry for entry in inventory.entries if entry.category in _MANUAL_CATEGORIES
        )
        if any(summary.category in _MANUAL_CATEGORIES for summary in inventory.summaries):
            self.manual_dialog.set_entries(manual_entries)
            if self._open_manual_after_scan:
                self._open_manual_after_scan = False
                self.manual_dialog.show()
                self.manual_dialog.raise_()
        self._update_overviews()

    def set_progress(self, scanned: int | None) -> None:
        if scanned is None:
            self.progress_bar.hide()
            return
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat(f"已扫描 {scanned} 个文件")
        self.progress_bar.show()

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        for widget in (
            self.scan_button,
            self.safe_cleanup_button,
            self.download_button,
            self.automatic_checkbox,
        ):
            widget.setEnabled(not self._busy)
        self.cancel_button.setEnabled(self._busy)
        self.manual_dialog.prepare_button.setEnabled(not self._busy)

    def set_automatic_enabled(self, enabled: bool) -> None:
        with QSignalBlocker(self.automatic_checkbox):
            self.automatic_checkbox.setChecked(enabled)

    def present_safe_confirmation(self, confirmation: SafeCleanupConfirmation) -> None:
        categories = (
            "、".join(
                f"{_CATEGORY_LABELS[item.category]} {item.item_count} 项"
                for item in confirmation.categories
            )
            or "无可清理项目"
        )
        answer = QMessageBox.question(
            self,
            "确认安全清理",
            f"本次预计清理 {confirmation.item_count} 项（{categories}），"
            f"预计释放 {_format_bytes(confirmation.expected_bytes)}。是否永久删除？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.safe_execute_requested.emit(confirmation.id)

    def present_manual_confirmation(self, confirmation: ManualCleanupConfirmation) -> None:
        answer = QMessageBox.question(
            self.manual_dialog,
            "第二次确认",
            f"最终确认永久删除 {confirmation.item_count} 项，"
            f"预计释放 {_format_bytes(confirmation.expected_bytes)}？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.manual_execute_requested.emit(confirmation.id)

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def show_result(self, result: StorageExecutionResult) -> None:
        if result.result_code is StorageResultCode.STATE_SAVE_FAILED:
            prefix = "清理完成，记录保存失败"
        else:
            prefix = "清理已结束"
        self.result_label.setText(
            f"{prefix}：删除 {result.deleted_count}，跳过 {result.skipped_count}，"
            f"失败 {result.failed_count}，取消 {result.cancelled_count}，"
            f"实际释放 {_format_bytes(result.released_bytes)}"
        )

    def _automatic_toggled(self, enabled: bool) -> None:
        if not enabled:
            self.automatic_changed.emit(False)
            return
        expected = self.safe_reclaim_value.text()
        answer = QMessageBox.question(
            self,
            "启用空闲自动清理",
            "固定规则：临时/更新暂存保留 7 天，轮转日志保留 30 天，"
            "更新备份保留最新 1 份，缩略图超过 1 GiB 后清至 900 MiB；"
            f"正式下载、分片和损坏留档不自动删除。当前预计安全范围：{expected}。是否启用？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.set_automatic_enabled(False)
            return
        self.automatic_changed.emit(True)

    def _manage_downloads(self) -> None:
        scanned = any(category in self._summaries for category in _MANUAL_CATEGORIES)
        if not scanned:
            self._open_manual_after_scan = True
            self.download_scan_requested.emit()
            return
        self.manual_dialog.show()
        self.manual_dialog.raise_()

    def _update_overviews(self) -> None:
        self.disk_free_value.setText(
            "尚未扫描" if self._disk_free_bytes is None else _format_bytes(self._disk_free_bytes)
        )
        self.managed_value.setText(
            "尚未扫描"
            if not self._summaries
            else _format_bytes(sum(summary.total_bytes for summary in self._summaries.values()))
        )
        self.safe_reclaim_value.setText(self._group_reclaim(_AUTOMATIC_CATEGORIES))
        self.manual_reclaim_value.setText(self._group_reclaim(_MANUAL_CATEGORIES))

    def _group_reclaim(self, categories: frozenset[StorageCategory]) -> str:
        values = [
            summary.reclaimable_bytes
            for category, summary in self._summaries.items()
            if category in categories
        ]
        return "尚未扫描" if not values else _format_bytes(sum(values))
