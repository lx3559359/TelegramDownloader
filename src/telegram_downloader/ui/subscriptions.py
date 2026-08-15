from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from telegram_downloader.content import ContentDialog
from telegram_downloader.domain import MediaKind
from telegram_downloader.subscriptions import (
    SUPPORTED_INTERVAL_MINUTES,
    SubscriptionDraft,
    SubscriptionProgress,
    SubscriptionRule,
    SubscriptionRun,
)
from telegram_downloader.ui.subscription_models import SubscriptionTableModel

_MEDIA_LABELS = {
    MediaKind.PHOTO: "图片",
    MediaKind.VIDEO: "视频",
    MediaKind.AUDIO: "音频",
    MediaKind.VOICE: "语音",
    MediaKind.DOCUMENT: "文档",
    MediaKind.ARCHIVE: "压缩包",
}


class SubscriptionEditorDialog(QDialog):
    def __init__(
        self,
        dialogs: list[ContentDialog],
        rule: SubscriptionRule | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑自动订阅" if rule is not None else "新建自动订阅")
        self.setMinimumWidth(520)
        self._draft: SubscriptionDraft | None = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.dialog_combo = QComboBox()
        for item in sorted(
            (value for value in dialogs if value.available),
            key=lambda value: (value.title.casefold(), value.peer_ref),
        ):
            self.dialog_combo.addItem(item.title, item.peer_ref)
        form.addRow("群组或频道", self.dialog_combo)

        self.keyword_input = QLineEdit()
        self.keyword_input.setPlaceholderText("输入需要持续关注的关键词")
        self.keyword_input.setClearButtonEnabled(True)
        form.addRow("关键词", self.keyword_input)

        media_panel = QWidget()
        media_layout = QHBoxLayout(media_panel)
        media_layout.setContentsMargins(0, 0, 0, 0)
        self.media_checks: dict[MediaKind, QCheckBox] = {}
        for kind in MediaKind:
            check = QCheckBox(_MEDIA_LABELS[kind])
            check.setChecked(True)
            self.media_checks[kind] = check
            media_layout.addWidget(check)
        media_layout.addStretch()
        form.addRow("媒体类型", media_panel)

        self.interval_combo = QComboBox()
        for value in sorted(SUPPORTED_INTERVAL_MINUTES):
            self.interval_combo.addItem(f"每 {value} 分钟", value)
        self.interval_combo.setCurrentIndex(
            self.interval_combo.findData(rule.interval_minutes if rule else 30)
        )
        form.addRow("检查间隔", self.interval_combo)
        layout.addLayout(form)

        baseline = QLabel("首次保存只建立当前位置，从之后出现的新消息开始检查。")
        baseline.setObjectName("muted")
        baseline.setWordWrap(True)
        layout.addWidget(baseline)
        self.error_label = QLabel("")
        self.error_label.setObjectName("errorBanner")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self._validate_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        if rule is not None:
            peer_index = self.dialog_combo.findData(rule.peer_ref)
            if peer_index >= 0:
                self.dialog_combo.setCurrentIndex(peer_index)
            self.keyword_input.setText(rule.keyword)
            for kind, check in self.media_checks.items():
                check.setChecked(kind in rule.media_kinds)

    def draft(self) -> SubscriptionDraft:
        if self._draft is None:
            self._draft = self._form_draft()
        return self._draft

    def _form_draft(self) -> SubscriptionDraft:
        peer_ref = self.dialog_combo.currentData()
        return SubscriptionDraft(
            str(peer_ref or ""),
            self.keyword_input.text(),
            frozenset(
                kind for kind, check in self.media_checks.items() if check.isChecked()
            ),
            int(self.interval_combo.currentData()),
        )

    def _validate_accept(self) -> None:
        try:
            self._draft = self._form_draft()
        except ValueError as error:
            self.error_label.setText(str(error))
            self.error_label.show()
            return
        self.error_label.hide()
        self.accept()


class SubscriptionPage(QWidget):
    create_requested = Signal(object)
    update_requested = Signal(str, object)
    run_requested = Signal(str)
    enabled_requested = Signal(str, bool)
    delete_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.rule_model = SubscriptionTableModel()
        self._logged_in = False
        self._dialogs: list[ContentDialog] = []
        self._busy_rule_id: str | None = None
        self._busy = False
        self._editors: set[SubscriptionEditorDialog] = set()
        self._build_ui()
        self._connect_signals()
        self._refresh_actions()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(13)

        title = QLabel("自动订阅")
        title.setObjectName("pageTitle")
        subtitle = QLabel("定时增量检查群组新消息，匹配媒体自动进入下载队列")
        subtitle.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.connection_label = QLabel("请先登录 Telegram 后创建订阅")
        self.connection_label.setObjectName("muted")
        layout.addWidget(self.connection_label)

        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 14, 14, 14)
        card_layout.setSpacing(10)
        header = QHBoxLayout()
        section = QLabel("订阅规则")
        section.setObjectName("sectionTitle")
        header.addWidget(section)
        header.addStretch()
        self.new_button = QPushButton("新建订阅")
        self.new_button.setObjectName("primaryButton")
        header.addWidget(self.new_button)
        card_layout.addLayout(header)

        self.rule_table = QTableView()
        self.rule_table.setModel(self.rule_model)
        self.rule_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.rule_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.rule_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.rule_table.setAlternatingRowColors(True)
        self.rule_table.setShowGrid(False)
        self.rule_table.verticalHeader().hide()
        self.rule_table.verticalHeader().setDefaultSectionSize(44)
        header_view = self.rule_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (2, 3, 4):
            header_view.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        card_layout.addWidget(self.rule_table, 1)

        self.progress_label = QLabel("")
        self.progress_label.setObjectName("muted")
        self.progress_label.hide()
        card_layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        card_layout.addWidget(self.progress_bar)
        self.busy_label = QLabel("")
        self.busy_label.setObjectName("muted")
        self.busy_label.hide()
        card_layout.addWidget(self.busy_label)

        actions = QHBoxLayout()
        self.edit_button = QPushButton("编辑")
        self.run_button = QPushButton("立即检查")
        self.toggle_button = QPushButton("暂停")
        self.delete_button = QPushButton("删除")
        actions.addWidget(self.edit_button)
        actions.addWidget(self.run_button)
        actions.addWidget(self.toggle_button)
        actions.addWidget(self.delete_button)
        actions.addStretch()
        card_layout.addLayout(actions)
        layout.addWidget(card, 1)

        self.error_label = QLabel("")
        self.error_label.setObjectName("errorBanner")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

    def _connect_signals(self) -> None:
        self.new_button.clicked.connect(self._open_create)
        self.edit_button.clicked.connect(self._open_edit)
        self.run_button.clicked.connect(self._emit_run)
        self.toggle_button.clicked.connect(self._emit_toggle)
        self.delete_button.clicked.connect(self._confirm_delete)
        self.rule_table.doubleClicked.connect(lambda _index: self._open_edit())
        self.rule_table.selectionModel().selectionChanged.connect(
            self._refresh_actions
        )

    def set_logged_in(self, logged_in: bool) -> None:
        self._logged_in = logged_in
        self.connection_label.setText(
            "连接正常，订阅仅在程序运行期间自动检查"
            if logged_in
            else "请先登录 Telegram；已有规则仍可离线查看"
        )
        self._refresh_actions()

    def set_dialogs(self, dialogs: list[ContentDialog]) -> None:
        self._dialogs = list(dialogs)
        self._refresh_actions()

    def set_rules(
        self,
        rules: list[SubscriptionRule],
        latest_runs: dict[str, SubscriptionRun] | None = None,
    ) -> None:
        selected = self._selected_rule_id()
        self.rule_model.set_rules(rules, latest_runs)
        if selected is not None:
            for row in range(self.rule_model.rowCount()):
                if self.rule_model.rule_at(row).id == selected:
                    self.rule_table.selectRow(row)
                    break
        self._refresh_actions()

    def set_rule_busy(
        self,
        rule_id: str | None,
        busy: bool,
        text: str = "",
    ) -> None:
        self._busy_rule_id = rule_id if busy else None
        self._busy = busy
        self.busy_label.setText(text or ("正在处理订阅…" if busy else ""))
        self.busy_label.setVisible(busy)
        if not busy and not self.progress_label.text():
            self.progress_bar.hide()
        self._refresh_actions()

    def set_progress(self, progress: SubscriptionProgress | None) -> None:
        if progress is None:
            if self._busy_rule_id is not None:
                self._busy_rule_id = None
                self._busy = False
            self.progress_label.clear()
            self.progress_label.hide()
            self.progress_bar.hide()
            self._refresh_actions()
            return
        self._busy_rule_id = progress.rule_id
        self._busy = True
        self.progress_label.setText(
            f"已扫描 {progress.inspected} 条 · 匹配 {progress.matched} 项 · "
            f"新增 {progress.queued} 项 · 重复 {progress.duplicate} 项 · "
            f"{progress.phase}"
        )
        self.progress_label.show()
        self.progress_bar.show()
        self._refresh_actions()

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def _open_create(self) -> None:
        editor = SubscriptionEditorDialog(self._dialogs, parent=self)
        self._retain_editor(editor)
        editor.accepted.connect(lambda retained=editor: self._create_from(retained))
        editor.open()

    def _open_edit(self) -> None:
        current = self._selected_rule()
        if current is None:
            return
        editor = SubscriptionEditorDialog(self._dialogs, current, self)
        self._retain_editor(editor)
        editor.accepted.connect(
            lambda retained=editor, rule_id=current.id: self._update_from(
                rule_id,
                retained,
            )
        )
        editor.open()

    def _retain_editor(self, editor: SubscriptionEditorDialog) -> None:
        self._editors.add(editor)
        editor.finished.connect(
            lambda _result, retained=editor: self._editors.discard(retained)
        )

    def _create_from(self, editor: SubscriptionEditorDialog) -> None:
        self.set_rule_busy(None, True, "正在建立订阅基线…")
        self.create_requested.emit(editor.draft())

    def _update_from(self, rule_id: str, editor: SubscriptionEditorDialog) -> None:
        self.set_rule_busy(rule_id, True, "正在更新订阅…")
        self.update_requested.emit(rule_id, editor.draft())

    def _emit_run(self) -> None:
        current = self._selected_rule()
        if current is None:
            return
        self.set_rule_busy(current.id, True, "正在准备立即检查…")
        self.run_requested.emit(current.id)

    def _emit_toggle(self) -> None:
        current = self._selected_rule()
        if current is None:
            return
        requested = not current.enabled
        self.set_rule_busy(
            current.id,
            True,
            "正在继续订阅…" if requested else "正在暂停订阅…",
        )
        self.enabled_requested.emit(current.id, requested)

    def _confirm_delete(self) -> None:
        current = self._selected_rule()
        if current is None:
            return
        answer = QMessageBox.question(
            self,
            "删除自动订阅",
            "只删除订阅规则和运行记录；已有下载任务和文件会保留。\n\n继续删除？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.set_rule_busy(current.id, True, "正在删除订阅…")
        self.delete_requested.emit(current.id)

    def _selected_rule(self) -> SubscriptionRule | None:
        rows = self.rule_table.selectionModel().selectedRows()
        return self.rule_model.rule_at(rows[0].row()) if rows else None

    def _selected_rule_id(self) -> str | None:
        current = self._selected_rule()
        return current.id if current is not None else None

    def _refresh_actions(self, *_args) -> None:
        current = self._selected_rule()
        online_ready = self._logged_in and not self._busy
        has_dialog = any(item.available for item in self._dialogs)
        self.new_button.setEnabled(online_ready and has_dialog)
        self.edit_button.setEnabled(online_ready and current is not None)
        self.run_button.setEnabled(
            online_ready and current is not None and current.enabled
        )
        self.toggle_button.setEnabled(online_ready and current is not None)
        self.delete_button.setEnabled(online_ready and current is not None)
        self.toggle_button.setText(
            "暂停" if current is None or current.enabled else "继续"
        )
