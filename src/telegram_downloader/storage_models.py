from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath

from telegram_downloader.settings import StorageMaintenanceSettings


class StorageCategory(StrEnum):
    THUMBNAILS = "thumbnails"
    TEMP = "temp"
    ROTATED_LOGS = "rotated-logs"
    UPDATE_STAGING = "update-staging"
    UPDATE_BACKUP = "update-backup"
    DOWNLOAD_PART = "download-part"
    CORRUPT_ARCHIVE = "corrupt-archive"


class StorageTrigger(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL_SAFE = "manual-safe"
    MANUAL_DOWNLOAD = "manual-download"


class StorageResultCode(StrEnum):
    COMPLETED = "completed"
    NOTHING_TO_CLEAN = "nothing-to-clean"
    CANCELLED = "cancelled"
    BUSY_DEFERRED = "busy-deferred"
    FILE_IN_USE = "file-in-use"
    PERMISSION_DENIED = "permission-denied"
    STATE_CHANGED = "state-changed"
    UNSAFE_PATH = "unsafe-path"
    PROTECTED_BY_TASK = "protected-by-task"
    PROTECTED_BY_UPDATE = "protected-by-update"
    STATE_SAVE_FAILED = "state-save-failed"
    LOCAL_ERROR = "local-error"


def _require_nonnegative(value: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label}必须是非负整数")


def _require_positive(value: int, label: str) -> None:
    _require_nonnegative(value, label)
    if value == 0:
        raise ValueError(f"{label}必须大于零")


def _require_utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is not UTC:
        raise ValueError(f"{label}必须使用 UTC 时间")


def _require_optional_utc(value: datetime | None, label: str) -> None:
    if value is not None:
        _require_utc(value, label)


def _require_unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label}包含重复值")


@dataclass(frozen=True, slots=True)
class StorageEntry:
    id: str
    relative_path: PurePosixPath
    category: StorageCategory
    size: int
    mtime_ns: int
    selectable: bool
    reason: StorageResultCode | None = None
    task_id: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("存储条目标识不能为空")
        path = self.relative_path
        if (
            not isinstance(path, PurePosixPath)
            or not path.parts
            or path.is_absolute()
            or any(part in {".", ".."} for part in path.parts)
            or "\\" in path.as_posix()
        ):
            raise ValueError("存储条目相对路径不安全")
        if not isinstance(self.category, StorageCategory):
            raise ValueError("存储类别无效")
        _require_nonnegative(self.size, "存储条目大小")
        _require_nonnegative(self.mtime_ns, "存储条目修改时间")
        if not isinstance(self.selectable, bool):
            raise ValueError("存储条目可选状态无效")
        if self.selectable and self.reason is not None:
            raise ValueError("可选条目不能带保护原因")
        if not self.selectable and not isinstance(self.reason, StorageResultCode):
            raise ValueError("受保护条目必须带固定原因")
        for value, label in (
            (self.task_id, "任务标识"),
            (self.display_name, "显示名称"),
        ):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{label}必须是文本")


@dataclass(frozen=True, slots=True)
class StorageCleanupPlan:
    id: str
    generated_at: datetime
    trigger: StorageTrigger
    entries: tuple[StorageEntry, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("清理计划标识不能为空")
        _require_utc(self.generated_at, "计划生成时间")
        if not isinstance(self.trigger, StorageTrigger):
            raise ValueError("清理触发类型无效")
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, StorageEntry) for entry in self.entries
        ):
            raise ValueError("清理计划条目无效")
        _require_unique(tuple(entry.id for entry in self.entries), "清理计划")

    @property
    def expected_bytes(self) -> int:
        return sum(entry.size for entry in self.entries)


@dataclass(frozen=True, slots=True)
class StorageCategorySummary:
    category: StorageCategory
    scanned_at: datetime
    total_count: int
    total_bytes: int
    reclaimable_count: int
    reclaimable_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.category, StorageCategory):
            raise ValueError("汇总类别无效")
        _require_utc(self.scanned_at, "分类扫描时间")
        for value, label in (
            (self.total_count, "分类总数"),
            (self.total_bytes, "分类总字节"),
            (self.reclaimable_count, "可释放数量"),
            (self.reclaimable_bytes, "可释放字节"),
        ):
            _require_nonnegative(value, label)
        if self.reclaimable_count > self.total_count:
            raise ValueError("可释放数量不能超过分类总数")
        if self.reclaimable_bytes > self.total_bytes:
            raise ValueError("可释放字节不能超过分类总字节")


@dataclass(frozen=True, slots=True)
class StoragePolicy:
    temp_retention_days: int = 7
    log_retention_days: int = 30
    thumbnail_limit_bytes: int = 1024**3
    thumbnail_target_bytes: int = 900 * 1024**2
    update_staging_retention_days: int = 7
    update_backup_keep_count: int = 1

    def __post_init__(self) -> None:
        for value, label in (
            (self.temp_retention_days, "临时文件保留天数"),
            (self.log_retention_days, "日志保留天数"),
            (self.thumbnail_limit_bytes, "缩略图上限"),
            (self.thumbnail_target_bytes, "缩略图目标"),
            (self.update_staging_retention_days, "更新暂存保留天数"),
            (self.update_backup_keep_count, "更新备份保留份数"),
        ):
            _require_positive(value, label)
        if self.thumbnail_target_bytes > self.thumbnail_limit_bytes:
            raise ValueError("缩略图目标不能超过上限")

    @classmethod
    def from_settings(cls, settings: StorageMaintenanceSettings) -> StoragePolicy:
        if not isinstance(settings, StorageMaintenanceSettings):
            raise ValueError("存储维护设置无效")
        return cls(
            temp_retention_days=settings.temp_retention_days,
            log_retention_days=settings.log_retention_days,
            thumbnail_limit_bytes=settings.thumbnail_limit_bytes,
            thumbnail_target_bytes=settings.thumbnail_target_bytes,
            update_staging_retention_days=settings.update_staging_retention_days,
            update_backup_keep_count=settings.update_backup_keep_count,
        )


@dataclass(frozen=True, slots=True)
class StorageInventory:
    scanned_at: datetime
    disk_free_bytes: int
    entries: tuple[StorageEntry, ...]
    summaries: tuple[StorageCategorySummary, ...]

    def __post_init__(self) -> None:
        _require_utc(self.scanned_at, "清单扫描时间")
        _require_nonnegative(self.disk_free_bytes, "磁盘剩余字节")
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, StorageEntry) for entry in self.entries
        ):
            raise ValueError("存储清单条目无效")
        if not isinstance(self.summaries, tuple) or any(
            not isinstance(summary, StorageCategorySummary)
            for summary in self.summaries
        ):
            raise ValueError("存储清单汇总无效")
        _require_unique(tuple(entry.id for entry in self.entries), "存储清单")
        _require_unique(
            tuple(summary.category.value for summary in self.summaries),
            "存储清单分类",
        )


@dataclass(frozen=True, slots=True)
class StorageExecutionItem:
    entry_id: str
    category: StorageCategory
    code: StorageResultCode
    released_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.entry_id, str) or not self.entry_id:
            raise ValueError("执行条目标识不能为空")
        if not isinstance(self.category, StorageCategory):
            raise ValueError("执行条目类别无效")
        if not isinstance(self.code, StorageResultCode):
            raise ValueError("执行结果代码无效")
        _require_nonnegative(self.released_bytes, "实际释放字节")
        if self.code is not StorageResultCode.COMPLETED and self.released_bytes:
            raise ValueError("未删除条目不能记录释放字节")


@dataclass(frozen=True, slots=True)
class StorageExecutionResult:
    plan_id: str
    trigger: StorageTrigger
    started_at: datetime
    completed_at: datetime
    result_code: StorageResultCode
    items: tuple[StorageExecutionItem, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.plan_id, str) or not self.plan_id:
            raise ValueError("执行计划标识不能为空")
        if not isinstance(self.trigger, StorageTrigger):
            raise ValueError("执行触发类型无效")
        _require_utc(self.started_at, "执行开始时间")
        _require_utc(self.completed_at, "执行完成时间")
        if self.completed_at < self.started_at:
            raise ValueError("执行完成时间不能早于开始时间")
        if not isinstance(self.result_code, StorageResultCode):
            raise ValueError("执行聚合结果无效")
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, StorageExecutionItem) for item in self.items
        ):
            raise ValueError("执行条目结果无效")
        _require_unique(tuple(item.entry_id for item in self.items), "执行结果")

    @property
    def released_bytes(self) -> int:
        return sum(item.released_bytes for item in self.items)

    @property
    def deleted_count(self) -> int:
        return sum(item.code is StorageResultCode.COMPLETED for item in self.items)

    @property
    def failed_count(self) -> int:
        failures = {
            StorageResultCode.FILE_IN_USE,
            StorageResultCode.PERMISSION_DENIED,
            StorageResultCode.LOCAL_ERROR,
        }
        return sum(item.code in failures for item in self.items)

    @property
    def cancelled_count(self) -> int:
        return sum(item.code is StorageResultCode.CANCELLED for item in self.items)

    @property
    def skipped_count(self) -> int:
        return len(self.items) - self.deleted_count - self.failed_count - self.cancelled_count


@dataclass(frozen=True, slots=True)
class StorageCategoryCount:
    category: StorageCategory
    deleted_count: int
    skipped_count: int
    failed_count: int
    cancelled_count: int
    released_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.category, StorageCategory):
            raise ValueError("历史类别无效")
        for value, label in (
            (self.deleted_count, "删除数量"),
            (self.skipped_count, "跳过数量"),
            (self.failed_count, "失败数量"),
            (self.cancelled_count, "取消数量"),
            (self.released_bytes, "释放字节"),
        ):
            _require_nonnegative(value, label)


@dataclass(frozen=True, slots=True)
class StorageRunHistory:
    occurred_at: datetime
    trigger: StorageTrigger
    categories: tuple[StorageCategoryCount, ...]
    deleted_count: int
    skipped_count: int
    failed_count: int
    cancelled_count: int
    released_bytes: int
    result_code: StorageResultCode

    def __post_init__(self) -> None:
        _require_utc(self.occurred_at, "历史发生时间")
        if not isinstance(self.trigger, StorageTrigger):
            raise ValueError("历史触发类型无效")
        if not isinstance(self.result_code, StorageResultCode):
            raise ValueError("历史结果代码无效")
        if not isinstance(self.categories, tuple) or any(
            not isinstance(category, StorageCategoryCount) for category in self.categories
        ):
            raise ValueError("历史分类汇总无效")
        _require_unique(
            tuple(category.category.value for category in self.categories),
            "历史分类",
        )
        totals = (
            self.deleted_count,
            self.skipped_count,
            self.failed_count,
            self.cancelled_count,
            self.released_bytes,
        )
        for value, label in zip(
            totals,
            ("删除数量", "跳过数量", "失败数量", "取消数量", "释放字节"),
            strict=True,
        ):
            _require_nonnegative(value, label)
        category_totals = tuple(
            sum(getattr(category, field) for category in self.categories)
            for field in (
                "deleted_count",
                "skipped_count",
                "failed_count",
                "cancelled_count",
                "released_bytes",
            )
        )
        if totals != category_totals:
            raise ValueError("历史分类汇总与总计不一致")


@dataclass(frozen=True, slots=True)
class StorageMaintenanceState:
    schema_version: int = 1
    last_scan_at: datetime | None = None
    last_automatic_check_at: datetime | None = None
    last_cleanup_at: datetime | None = None
    next_due_at: datetime | None = None
    summaries: tuple[StorageCategorySummary, ...] = ()
    history: tuple[StorageRunHistory, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != 1
        ):
            raise ValueError("存储维护状态 schema 无效")
        for value, label in (
            (self.last_scan_at, "上次扫描时间"),
            (self.last_automatic_check_at, "上次自动检查时间"),
            (self.last_cleanup_at, "上次清理时间"),
            (self.next_due_at, "下次到期时间"),
        ):
            _require_optional_utc(value, label)
        if not isinstance(self.summaries, tuple) or any(
            not isinstance(summary, StorageCategorySummary)
            for summary in self.summaries
        ):
            raise ValueError("状态分类汇总无效")
        if not isinstance(self.history, tuple) or any(
            not isinstance(item, StorageRunHistory) for item in self.history
        ):
            raise ValueError("状态历史无效")
        _require_unique(
            tuple(summary.category.value for summary in self.summaries),
            "状态分类",
        )
