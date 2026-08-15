from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from telegram_downloader.subscription_diagnostics import explain_run
from telegram_downloader.subscriptions import (
    SubscriptionRule,
    SubscriptionRun,
    SubscriptionState,
)

_INVALID_INDEX = QModelIndex()


class SubscriptionTableModel(QAbstractTableModel):
    HEADERS = ("订阅规则", "群组或频道", "状态", "最近结果", "下次检查")
    STATUS_LABELS = {
        SubscriptionState.BASELINING: "正在建立基线",
        SubscriptionState.WAITING: "等待检查",
        SubscriptionState.RUNNING: "检查中",
        SubscriptionState.PAUSED: "已暂停",
        SubscriptionState.WAITING_NETWORK: "等待网络",
        SubscriptionState.AUTH_REQUIRED: "需要重新登录",
        SubscriptionState.FAILED: "检查失败",
    }

    def __init__(self) -> None:
        super().__init__()
        self._rules: tuple[SubscriptionRule, ...] = ()
        self._latest_runs: dict[str, SubscriptionRun] = {}

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rules)

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
        if not index.isValid() or not 0 <= index.row() < len(self._rules):
            return None
        item = self._rules[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return item.id
        if role == Qt.ItemDataRole.ToolTipRole:
            details = [
                f"{item.dialog_title} · {item.keyword}",
                "、".join(sorted(kind.value for kind in item.media_kinds)),
            ]
            if item.last_error:
                details.append(item.last_error)
            return "\n".join(details)
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        values = (
            f"{item.keyword} · 每 {item.interval_minutes} 分钟",
            item.dialog_title,
            self.STATUS_LABELS[item.state],
            self._last_result(item, self._latest_runs.get(item.id)),
            self._next_run(item),
        )
        return values[index.column()]

    def set_rules(
        self,
        rules: list[SubscriptionRule],
        latest_runs: dict[str, SubscriptionRun] | None = None,
    ) -> None:
        self.beginResetModel()
        self._latest_runs = dict(latest_runs or {})
        self._rules = tuple(
            sorted(
                rules,
                key=lambda item: (
                    not item.enabled,
                    item.next_run_at is None,
                    item.next_run_at,
                    item.dialog_title.casefold(),
                    item.keyword.casefold(),
                    item.id,
                ),
            )
        )
        self.endResetModel()

    def rule_at(self, row: int) -> SubscriptionRule:
        return self._rules[row]

    @staticmethod
    def _last_result(
        item: SubscriptionRule,
        latest_run: SubscriptionRun | None,
    ) -> str:
        if item.last_error:
            return item.last_error
        if latest_run is not None:
            return explain_run(latest_run)
        if item.last_run_at is None:
            return "尚未检查"
        return item.last_run_at.astimezone().strftime("完成于 %Y-%m-%d %H:%M")

    @staticmethod
    def _next_run(item: SubscriptionRule) -> str:
        if not item.enabled or item.state is SubscriptionState.PAUSED:
            return "已暂停"
        if item.next_run_at is None:
            return "等待状态恢复"
        return item.next_run_at.astimezone().strftime("%Y-%m-%d %H:%M")
