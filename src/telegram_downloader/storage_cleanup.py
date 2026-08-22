from __future__ import annotations

import errno
import os
import re
import stat as stat_module
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from telegram_downloader.domain import IntegrityStatus, ItemStatus
from telegram_downloader.download_paths import DownloadPathError, DownloadPathPolicy
from telegram_downloader.paths import PortablePaths
from telegram_downloader.settings import DownloadStorageSettings
from telegram_downloader.storage_models import (
    StorageCategory,
    StorageCleanupPlan,
    StorageEntry,
    StorageExecutionItem,
    StorageExecutionResult,
    StorageInventory,
    StorageResultCode,
    StorageTrigger,
)
from telegram_downloader.update_protection import UpdateProtectionSnapshot

_REPARSE_POINT = 0x400
_ROTATED_LOG = re.compile(r"app\.log\.[1-9]\d*\Z")
_BACKUP_DIRECTORY = re.compile(r".+-[a-f0-9]{8}\Z")
_CORRUPT_ARCHIVE = re.compile(r"\.corrupt(?:\.\d+)?\Z")
_AUTOMATIC_CATEGORIES = frozenset(
    {
        StorageCategory.THUMBNAILS,
        StorageCategory.TEMP,
        StorageCategory.ROTATED_LOGS,
        StorageCategory.UPDATE_STAGING,
        StorageCategory.UPDATE_BACKUP,
    }
)
_MANUAL_CATEGORIES = frozenset({StorageCategory.DOWNLOAD_PART, StorageCategory.CORRUPT_ARCHIVE})
_FAILURE_CODES = frozenset(
    {
        StorageResultCode.FILE_IN_USE,
        StorageResultCode.PERMISSION_DENIED,
        StorageResultCode.LOCAL_ERROR,
    }
)


class _Repository(Protocol):
    def maintenance_media_by_targets(self, targets: Sequence[Path]) -> dict[Path, Any]: ...


class _UpdateProtection(Protocol):
    def snapshot(self) -> UpdateProtectionSnapshot: ...


class _EntryRejected(RuntimeError):
    def __init__(self, code: StorageResultCode) -> None:
        self.code = code


class StorageCleanupPlanner:
    def automatic(
        self,
        inventory: StorageInventory,
        now: datetime,
        *,
        trigger: StorageTrigger = StorageTrigger.AUTOMATIC,
    ) -> StorageCleanupPlan:
        if not isinstance(inventory, StorageInventory):
            raise ValueError("自动清理清单无效")
        if trigger not in {StorageTrigger.AUTOMATIC, StorageTrigger.MANUAL_SAFE}:
            raise ValueError("安全清理触发类型无效")
        entries = tuple(
            sorted(
                (
                    entry
                    for entry in inventory.entries
                    if entry.category in _AUTOMATIC_CATEGORIES and entry.selectable
                ),
                key=self._sort_key,
            )
        )
        return StorageCleanupPlan(uuid4().hex, now, trigger, entries)

    def manual_download(
        self,
        inventory: StorageInventory,
        selected_ids: Sequence[str],
        now: datetime,
    ) -> StorageCleanupPlan:
        if not isinstance(inventory, StorageInventory):
            raise ValueError("分片与留档清单无效")
        if isinstance(selected_ids, (str, bytes)):
            raise ValueError("手动清理选择无效")
        selected = tuple(selected_ids)
        if not selected or len(selected) != len(set(selected)):
            raise ValueError("手动清理选择不能为空或重复")
        by_id = {entry.id: entry for entry in inventory.entries}
        try:
            entries = tuple(by_id[entry_id] for entry_id in selected)
        except (KeyError, TypeError) as exc:
            raise ValueError("手动清理选择已失效") from exc
        if any(
            entry.category not in _MANUAL_CATEGORIES or not entry.selectable for entry in entries
        ):
            raise ValueError("手动清理选择包含受保护项目")
        ordered = tuple(sorted(entries, key=self._sort_key))
        return StorageCleanupPlan(
            uuid4().hex,
            now,
            StorageTrigger.MANUAL_DOWNLOAD,
            ordered,
        )

    @staticmethod
    def _sort_key(entry: StorageEntry) -> tuple[str, str]:
        return entry.category.value, entry.relative_path.as_posix()


class StorageCleanupExecutor:
    def __init__(
        self,
        paths: PortablePaths,
        repository: _Repository | None,
        update_protection: _UpdateProtection,
        *,
        download_paths: DownloadPathPolicy | None = None,
        utc_clock: Callable[[], datetime] | None = None,
        remove_file: Callable[[Path], None] | None = None,
    ) -> None:
        self.paths = paths
        self.repository = repository
        self.update_protection = update_protection
        self.download_paths = download_paths or DownloadPathPolicy(
            paths,
            DownloadStorageSettings(),
        )
        self.utc_clock = utc_clock or (lambda: datetime.now(UTC))
        self.remove_file = remove_file or (lambda path: path.unlink())

    def execute(
        self,
        plan: StorageCleanupPlan,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> StorageExecutionResult:
        if not isinstance(plan, StorageCleanupPlan):
            raise ValueError("存储清理计划无效")
        started_at = self.utc_clock()
        check_cancelled = cancelled or (lambda: False)
        items: list[StorageExecutionItem] = []
        cancellation_seen = False
        for entry in plan.entries:
            cancellation_seen = cancellation_seen or check_cancelled()
            if cancellation_seen:
                items.append(self._item(entry, StorageResultCode.CANCELLED))
                continue
            items.append(self._execute_entry(plan.trigger, entry))
        completed_at = self.utc_clock()
        return StorageExecutionResult(
            plan_id=plan.id,
            trigger=plan.trigger,
            started_at=started_at,
            completed_at=completed_at,
            result_code=self._aggregate(items),
            items=tuple(items),
        )

    def _execute_entry(
        self,
        trigger: StorageTrigger,
        entry: StorageEntry,
    ) -> StorageExecutionItem:
        try:
            target, category_root = self._validated_target(trigger, entry)
            if entry.category in _MANUAL_CATEGORIES:
                self._validate_manual_target(target)
            elif entry.category in {
                StorageCategory.UPDATE_STAGING,
                StorageCategory.UPDATE_BACKUP,
            }:
                snapshot = self.update_protection.snapshot()
                if snapshot.fail_closed or snapshot.protects(target):
                    raise _EntryRejected(StorageResultCode.PROTECTED_BY_UPDATE)
            try:
                self.remove_file(target)
            except OSError as exc:
                raise _EntryRejected(self._os_error_code(exc)) from exc
            self._remove_empty_parents(target.parent, category_root)
            return self._item(
                entry,
                StorageResultCode.COMPLETED,
                released_bytes=entry.size,
            )
        except _EntryRejected as rejected:
            return self._item(entry, rejected.code)

    def _validated_target(
        self,
        trigger: StorageTrigger,
        entry: StorageEntry,
    ) -> tuple[Path, Path]:
        if not entry.selectable:
            raise _EntryRejected(StorageResultCode.UNSAFE_PATH)
        if trigger is StorageTrigger.MANUAL_DOWNLOAD:
            allowed = _MANUAL_CATEGORIES
        elif trigger in {StorageTrigger.AUTOMATIC, StorageTrigger.MANUAL_SAFE}:
            allowed = _AUTOMATIC_CATEGORIES
        else:
            raise _EntryRejected(StorageResultCode.UNSAFE_PATH)
        if entry.category not in allowed:
            raise _EntryRejected(StorageResultCode.UNSAFE_PATH)

        relative = Path(entry.relative_path.as_posix())
        if entry.category in _MANUAL_CATEGORIES:
            try:
                category_root = self.download_paths.root_for_id(entry.root_id)
                target = category_root / relative
                self.download_paths.guard_in(category_root, target)
            except (DownloadPathError, OSError, ValueError) as exc:
                raise _EntryRejected(StorageResultCode.UNSAFE_PATH) from exc
        else:
            if entry.root_id != "app":
                raise _EntryRejected(StorageResultCode.UNSAFE_PATH)
            target = self.paths.root / relative
            try:
                self.paths.guard(target)
            except ValueError as exc:
                raise _EntryRejected(StorageResultCode.UNSAFE_PATH) from exc
            category_root = self._category_root(entry.category)
        if not target.is_relative_to(category_root) or target == category_root:
            raise _EntryRejected(StorageResultCode.UNSAFE_PATH)
        self._validate_category_shape(entry.category, target, category_root)

        current = category_root
        try:
            root_stat = current.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise _EntryRejected(StorageResultCode.STATE_CHANGED) from exc
        except OSError as exc:
            raise _EntryRejected(self._os_error_code(exc)) from exc
        if self._is_reparse(current, root_stat) or not stat_module.S_ISDIR(
            root_stat.st_mode
        ):
            raise _EntryRejected(StorageResultCode.UNSAFE_PATH)
        parts = target.relative_to(category_root).parts
        for index, part in enumerate(parts):
            current /= part
            try:
                current_stat = current.stat(follow_symlinks=False)
            except FileNotFoundError as exc:
                raise _EntryRejected(StorageResultCode.STATE_CHANGED) from exc
            except OSError as exc:
                raise _EntryRejected(self._os_error_code(exc)) from exc
            if self._is_reparse(current, current_stat):
                raise _EntryRejected(StorageResultCode.UNSAFE_PATH)
            is_final = index == len(parts) - 1
            if not is_final and not stat_module.S_ISDIR(current_stat.st_mode):
                raise _EntryRejected(StorageResultCode.STATE_CHANGED)
            if is_final:
                if not stat_module.S_ISREG(current_stat.st_mode):
                    raise _EntryRejected(StorageResultCode.STATE_CHANGED)
                if current_stat.st_size != entry.size or current_stat.st_mtime_ns != entry.mtime_ns:
                    raise _EntryRejected(StorageResultCode.STATE_CHANGED)
        return target, category_root

    def _validate_category_shape(
        self,
        category: StorageCategory,
        target: Path,
        category_root: Path,
    ) -> None:
        if category is StorageCategory.TEMP and target.is_relative_to(self.paths.diagnostic_temp):
            raise _EntryRejected(StorageResultCode.UNSAFE_PATH)
        if category is StorageCategory.ROTATED_LOGS and (
            target.parent != category_root or _ROTATED_LOG.fullmatch(target.name) is None
        ):
            raise _EntryRejected(StorageResultCode.UNSAFE_PATH)
        if category is StorageCategory.UPDATE_BACKUP:
            top = target.relative_to(category_root).parts[0]
            if _BACKUP_DIRECTORY.fullmatch(top) is None:
                raise _EntryRejected(StorageResultCode.UNSAFE_PATH)
        if category is StorageCategory.DOWNLOAD_PART and not target.name.endswith(".part"):
            raise _EntryRejected(StorageResultCode.UNSAFE_PATH)
        if (
            category is StorageCategory.CORRUPT_ARCHIVE
            and _CORRUPT_ARCHIVE.search(target.name) is None
        ):
            raise _EntryRejected(StorageResultCode.UNSAFE_PATH)

    def _validate_manual_target(self, candidate: Path) -> None:
        target = self._leftover_target(candidate)
        if target is None or self.repository is None:
            raise _EntryRejected(StorageResultCode.PROTECTED_BY_TASK)
        try:
            self.download_paths.guard(target)
            target_stat = target.stat(follow_symlinks=False)
        except (OSError, ValueError) as exc:
            raise _EntryRejected(StorageResultCode.PROTECTED_BY_TASK) from exc
        if self._is_reparse(target, target_stat) or not stat_module.S_ISREG(target_stat.st_mode):
            raise _EntryRejected(StorageResultCode.PROTECTED_BY_TASK)
        normalized = target.resolve()
        try:
            media = self.repository.maintenance_media_by_targets([normalized]).get(normalized)
        except (OSError, ValueError) as exc:
            raise _EntryRejected(StorageResultCode.PROTECTED_BY_TASK) from exc
        if (
            media is None
            or media.item_status is not ItemStatus.COMPLETED
            or media.integrity_status is not IntegrityStatus.VERIFIED
        ):
            raise _EntryRejected(StorageResultCode.PROTECTED_BY_TASK)

    def _category_root(self, category: StorageCategory) -> Path:
        roots = {
            StorageCategory.THUMBNAILS: self.paths.thumbnail_cache,
            StorageCategory.TEMP: self.paths.temp,
            StorageCategory.ROTATED_LOGS: self.paths.log.parent,
            StorageCategory.UPDATE_STAGING: self.paths.update_staging,
            StorageCategory.UPDATE_BACKUP: self.paths.update_backup,
            StorageCategory.DOWNLOAD_PART: self.paths.downloads,
            StorageCategory.CORRUPT_ARCHIVE: self.paths.downloads,
        }
        return roots[category]

    @staticmethod
    def _leftover_target(candidate: Path) -> Path | None:
        name = _CORRUPT_ARCHIVE.sub("", candidate.name)
        if name.endswith(".part"):
            name = name[: -len(".part")]
        if not name:
            return None
        return candidate.with_name(name)

    @staticmethod
    def _is_reparse(path: Path, path_stat: os.stat_result) -> bool:
        return path.is_symlink() or bool(
            getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT
        )

    @staticmethod
    def _os_error_code(error: OSError) -> StorageResultCode:
        if getattr(error, "winerror", None) in {32, 33} or error.errno in {
            errno.EBUSY,
            errno.ETXTBSY,
        }:
            return StorageResultCode.FILE_IN_USE
        if isinstance(error, PermissionError):
            return StorageResultCode.PERMISSION_DENIED
        return StorageResultCode.LOCAL_ERROR

    @staticmethod
    def _item(
        entry: StorageEntry,
        code: StorageResultCode,
        *,
        released_bytes: int = 0,
    ) -> StorageExecutionItem:
        return StorageExecutionItem(
            entry_id=entry.id,
            category=entry.category,
            code=code,
            released_bytes=released_bytes,
        )

    @staticmethod
    def _aggregate(items: Sequence[StorageExecutionItem]) -> StorageResultCode:
        if not items:
            return StorageResultCode.NOTHING_TO_CLEAN
        if any(item.code is StorageResultCode.CANCELLED for item in items):
            return StorageResultCode.CANCELLED
        if any(item.code in _FAILURE_CODES for item in items):
            return StorageResultCode.LOCAL_ERROR
        return StorageResultCode.COMPLETED

    @staticmethod
    def _remove_empty_parents(parent: Path, category_root: Path) -> None:
        current = parent
        while current != category_root and current.is_relative_to(category_root):
            try:
                current_stat = current.stat(follow_symlinks=False)
                if current.is_symlink() or bool(
                    getattr(current_stat, "st_file_attributes", 0) & _REPARSE_POINT
                ):
                    return
                if not stat_module.S_ISDIR(current_stat.st_mode):
                    return
                current.rmdir()
            except OSError:
                return
            current = current.parent
