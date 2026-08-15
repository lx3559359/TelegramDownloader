from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from telegram_downloader.domain import MediaKind
from telegram_downloader.subscription_diagnostics import explain_run
from telegram_downloader.subscriptions import SubscriptionProbeSample, SubscriptionRun

_INVALID_INDEX = QModelIndex()

_MEDIA_LABELS = {
    MediaKind.PHOTO: "图片",
    MediaKind.VIDEO: "视频",
    MediaKind.AUDIO: "音频",
    MediaKind.VOICE: "语音",
    MediaKind.DOCUMENT: "文档",
    MediaKind.ARCHIVE: "压缩包",
}


class SubscriptionRunHistoryModel(QAbstractTableModel):
    HEADERS = ("时间", "结果", "扫描", "关键词", "媒体", "新增", "重复")

    def __init__(self) -> None:
        super().__init__()
        self._runs: tuple[SubscriptionRun, ...] = ()

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._runs)

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
        if not index.isValid() or not 0 <= index.row() < len(self._runs):
            return None
        item = self._runs[index.row()]
        explanation = explain_run(item)
        if role == Qt.ItemDataRole.ToolTipRole:
            return explanation
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        values = (
            item.finished_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            explanation,
            str(item.inspected),
            str(item.keyword_hits),
            str(item.matched),
            str(item.queued),
            str(item.duplicate),
        )
        return values[index.column()]

    def set_runs(self, runs: list[SubscriptionRun]) -> None:
        self.beginResetModel()
        self._runs = tuple(
            sorted(
                runs,
                key=lambda item: (item.finished_at, item.id),
                reverse=True,
            )[:20]
        )
        self.endResetModel()

    def run_at(self, row: int) -> SubscriptionRun:
        return self._runs[row]


class SubscriptionProbeSampleModel(QAbstractTableModel):
    HEADERS = ("日期", "类型", "文件", "大小", "摘要", "状态")

    def __init__(self) -> None:
        super().__init__()
        self._samples: tuple[SubscriptionProbeSample, ...] = ()

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._samples)

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
        if not index.isValid() or not 0 <= index.row() < len(self._samples):
            return None
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        item = self._samples[index.row()]
        values = (
            item.message_date_utc.astimezone().strftime("%Y-%m-%d %H:%M"),
            _MEDIA_LABELS[item.media_kind],
            item.original_name,
            self._format_bytes(item.expected_size),
            item.excerpt,
            "已在队列" if item.already_queued else "可加入队列",
        )
        return values[index.column()]

    def set_samples(
        self,
        samples: list[SubscriptionProbeSample]
        | tuple[SubscriptionProbeSample, ...],
    ) -> None:
        self.beginResetModel()
        self._samples = tuple(samples[:20])
        self.endResetModel()

    def sample_at(self, row: int) -> SubscriptionProbeSample:
        return self._samples[row]

    @staticmethod
    def _format_bytes(value: int | None) -> str:
        if value is None:
            return "未知"
        amount = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if amount < 1024 or unit == "TB":
                return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
            amount /= 1024
        return "未知"
