from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat as stat_module
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from telegram_downloader.domain import IntegrityStatus, ItemStatus
from telegram_downloader.download_paths import DownloadPathPolicy
from telegram_downloader.paths import PortablePaths
from telegram_downloader.settings import DownloadStorageSettings
from telegram_downloader.storage_models import (
    StorageCategory,
    StorageCategorySummary,
    StorageEntry,
    StorageInventory,
    StoragePolicy,
    StorageResultCode,
)
from telegram_downloader.update_helper import RuntimePackageError, read_installed_inventory
from telegram_downloader.update_protection import UpdateProtectionSnapshot

if TYPE_CHECKING:
    from telegram_downloader.repository import TaskRepository


_REPARSE_POINT = 0x400
_ROTATED_LOG = re.compile(r"app\.log\.[1-9]\d*\Z")
_BACKUP_DIRECTORY = re.compile(r".+-[a-f0-9]{8}\Z")
_CORRUPT_ARCHIVE = re.compile(r"\.corrupt(?:\.\d+)?\Z")


class StorageInventoryCancelled(RuntimeError):
    """Raised when a storage scan is cancelled between entries."""


@dataclass(frozen=True, slots=True)
class _FileRecord:
    path: Path
    relative_path: PurePosixPath
    size: int
    mtime_ns: int
    root_id: str = "app"


def storage_entry_id(
    category: StorageCategory,
    relative: PurePosixPath,
    root_id: str = "app",
) -> str:
    payload = f"{root_id}\0{category.value}\0{relative.as_posix()}".encode()
    return hashlib.sha256(payload).hexdigest()


class StorageInventoryService:
    def __init__(
        self,
        paths: PortablePaths,
        repository: TaskRepository | None,
        *,
        policy: StoragePolicy | None = None,
        download_paths: DownloadPathPolicy | None = None,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    ) -> None:
        self.paths = paths
        self.repository = repository
        self.policy = policy or StoragePolicy()
        self.download_paths = download_paths or DownloadPathPolicy(
            paths,
            DownloadStorageSettings(),
        )
        self.disk_usage = disk_usage

    def scan_automatic(
        self,
        now: datetime,
        update_snapshot: UpdateProtectionSnapshot,
        *,
        active_paths: Collection[Path],
        progress: Callable[[int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> StorageInventory:
        if now.tzinfo is not UTC:
            raise ValueError("自动存储扫描时间必须使用 UTC")
        if not isinstance(update_snapshot, UpdateProtectionSnapshot):
            raise ValueError("更新保护快照无效")
        active = frozenset(Path(path).resolve() for path in active_paths)
        counter = [0]
        check_cancelled = cancelled or (lambda: False)

        thumbnail_records, thumbnail_unsafe = self._walk_category(
            self.paths.thumbnail_cache,
            StorageCategory.THUMBNAILS,
            progress,
            check_cancelled,
            counter,
        )
        thumbnail_selected = self._thumbnail_candidates(thumbnail_records)

        temp_records, temp_unsafe = self._walk_category(
            self.paths.temp,
            StorageCategory.TEMP,
            progress,
            check_cancelled,
            counter,
            excluded=(self.paths.diagnostic_temp,),
        )
        temp_cutoff = self._cutoff_ns(now, self.policy.temp_retention_days)
        temp_selected = [
            record
            for record in temp_records
            if record.mtime_ns < temp_cutoff
            and not self._protected(record.path, active)
        ]

        log_records, log_unsafe = self._walk_category(
            self.paths.log.parent,
            StorageCategory.ROTATED_LOGS,
            progress,
            check_cancelled,
            counter,
            recurse=False,
        )
        log_records = [
            record for record in log_records if _ROTATED_LOG.fullmatch(record.path.name)
        ]
        log_unsafe = [
            entry
            for entry in log_unsafe
            if _ROTATED_LOG.fullmatch(entry.relative_path.name)
        ]
        log_cutoff = self._cutoff_ns(now, self.policy.log_retention_days)
        log_selected = [record for record in log_records if record.mtime_ns < log_cutoff]

        staging_records, staging_unsafe = self._walk_category(
            self.paths.update_staging,
            StorageCategory.UPDATE_STAGING,
            progress,
            check_cancelled,
            counter,
        )
        staging_cutoff = self._cutoff_ns(
            now, self.policy.update_staging_retention_days
        )
        staging_selected = [
            record
            for record in staging_records
            if record.mtime_ns < staging_cutoff
            and not update_snapshot.protects(record.path)
            and not self._protected(record.path, active)
        ]

        backup_records, backup_unsafe, backup_selected = self._backup_candidates(
            update_snapshot,
            active,
            progress,
            check_cancelled,
            counter,
        )

        groups = (
            (
                StorageCategory.THUMBNAILS,
                thumbnail_records,
                thumbnail_unsafe,
                thumbnail_selected,
            ),
            (StorageCategory.TEMP, temp_records, temp_unsafe, temp_selected),
            (StorageCategory.ROTATED_LOGS, log_records, log_unsafe, log_selected),
            (
                StorageCategory.UPDATE_STAGING,
                staging_records,
                staging_unsafe,
                staging_selected,
            ),
            (
                StorageCategory.UPDATE_BACKUP,
                backup_records,
                backup_unsafe,
                backup_selected,
            ),
        )
        summaries = tuple(
            self._summary(category, now, records, selected, unsafe)
            for category, records, unsafe, selected in groups
        )
        entries = [entry for _category, _records, unsafe, _selected in groups for entry in unsafe]
        entries.extend(
            self._selectable_entry(category, record)
            for category, _records, _unsafe, selected in groups
            for record in selected
        )
        entries.sort(key=lambda item: (item.category.value, item.relative_path.as_posix()))
        return StorageInventory(
            scanned_at=now,
            disk_free_bytes=int(self.disk_usage(self.paths.root).free),
            entries=tuple(entries),
            summaries=summaries,
        )

    def scan_download_candidates(
        self,
        now: datetime,
        *,
        active_paths: Collection[Path],
        progress: Callable[[int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> StorageInventory:
        if now.tzinfo is not UTC:
            raise ValueError("下载残留扫描时间必须使用 UTC")
        if self.repository is None:
            raise ValueError("下载残留扫描需要任务仓库")
        active = frozenset(Path(path).resolve() for path in active_paths)
        counter = [0]
        check_cancelled = cancelled or (lambda: False)
        candidates, unsafe = self._walk_download_candidates(
            progress,
            check_cancelled,
            counter,
        )
        self._raise_if_cancelled(check_cancelled)
        targets = tuple(
            target
            for _record, _category, target in candidates
            if target is not None
        )
        media_by_target = self.repository.maintenance_media_by_targets(targets)
        self._raise_if_cancelled(check_cancelled)

        entries = list(unsafe)
        selected_by_category: dict[StorageCategory, list[_FileRecord]] = {
            StorageCategory.DOWNLOAD_PART: [],
            StorageCategory.CORRUPT_ARCHIVE: [],
        }
        records_by_category: dict[StorageCategory, list[_FileRecord]] = {
            StorageCategory.DOWNLOAD_PART: [],
            StorageCategory.CORRUPT_ARCHIVE: [],
        }
        for record, category, target in candidates:
            records_by_category[category].append(record)
            media = media_by_target.get(target) if target is not None else None
            selectable = bool(
                media is not None
                and media.item_status is ItemStatus.COMPLETED
                and media.integrity_status is IntegrityStatus.VERIFIED
                and self._is_ordinary_file(target)
                and not self._protected(record.path, active)
                and not self._protected(target, active)
            )
            if selectable:
                selected_by_category[category].append(record)
            entries.append(
                self._download_entry(
                    category,
                    record,
                    selectable=selectable,
                    task_id=media.task_id if media is not None else None,
                    display_name=media.task_title if media is not None else None,
                )
            )

        categories = (
            StorageCategory.DOWNLOAD_PART,
            StorageCategory.CORRUPT_ARCHIVE,
        )
        summaries = tuple(
            self._summary(
                category,
                now,
                records_by_category[category],
                selected_by_category[category],
                tuple(entry for entry in unsafe if entry.category is category),
            )
            for category in categories
        )
        entries.sort(key=lambda item: (item.root_id, item.relative_path.as_posix()))
        return StorageInventory(
            scanned_at=now,
            disk_free_bytes=int(self.disk_usage(self.download_paths.current_root).free),
            entries=tuple(entries),
            summaries=summaries,
        )

    @staticmethod
    def _cutoff_ns(now: datetime, days: int) -> int:
        return int((now - timedelta(days=days)).timestamp() * 1_000_000_000)

    @staticmethod
    def _protected(path: Path, protected: Collection[Path]) -> bool:
        return any(path == root or path.is_relative_to(root) for root in protected)

    def _thumbnail_candidates(self, records: list[_FileRecord]) -> list[_FileRecord]:
        total = sum(record.size for record in records)
        if total <= self.policy.thumbnail_limit_bytes:
            return []
        selected: list[_FileRecord] = []
        remaining = total
        for record in sorted(
            records, key=lambda item: (item.mtime_ns, item.relative_path.as_posix())
        ):
            selected.append(record)
            remaining -= record.size
            if remaining <= self.policy.thumbnail_target_bytes:
                break
        return selected

    def _backup_candidates(
        self,
        update_snapshot: UpdateProtectionSnapshot,
        active: Collection[Path],
        progress: Callable[[int], None] | None,
        cancelled: Callable[[], bool],
        counter: list[int],
    ) -> tuple[list[_FileRecord], list[StorageEntry], list[_FileRecord]]:
        root = self.paths.update_backup
        if not root.exists():
            return [], [], []
        records: list[_FileRecord] = []
        unsafe: list[StorageEntry] = []
        valid: list[tuple[int, str, Path, list[_FileRecord]]] = []
        try:
            with os.scandir(self.paths.guard(root)) as stream:
                top_entries = sorted(stream, key=lambda item: item.name.casefold())
        except (OSError, ValueError):
            return records, unsafe, []
        for item in top_entries:
            self._tick(progress, cancelled, counter)
            path = Path(item.path)
            try:
                item_stat = item.stat(follow_symlinks=False)
            except OSError:
                unsafe.append(self._unsafe_entry(StorageCategory.UPDATE_BACKUP, path, 0, 0))
                continue
            if self._is_reparse(item, item_stat) or not stat_module.S_ISDIR(
                item_stat.st_mode
            ):
                if _BACKUP_DIRECTORY.fullmatch(item.name):
                    unsafe.append(
                        self._unsafe_entry(
                            StorageCategory.UPDATE_BACKUP,
                            path,
                            max(0, int(item_stat.st_size)),
                            max(0, int(item_stat.st_mtime_ns)),
                        )
                    )
                continue
            if not _BACKUP_DIRECTORY.fullmatch(item.name):
                continue
            current_records, current_unsafe = self._walk_category(
                path,
                StorageCategory.UPDATE_BACKUP,
                progress,
                cancelled,
                counter,
            )
            records.extend(current_records)
            unsafe.extend(current_unsafe)
            try:
                inventory = read_installed_inventory(path)
                allowed = {file.path for file in inventory.files} | {
                    "runtime-manifest.json"
                }
                actual = {
                    record.path.relative_to(path).as_posix()
                    for record in current_records
                }
                if current_unsafe or not actual <= allowed:
                    continue
            except (OSError, RuntimePackageError, ValueError):
                continue
            valid.append(
                (
                    int(item_stat.st_mtime_ns),
                    item.name,
                    path,
                    current_records,
                )
            )
        ordered = sorted(valid, key=lambda item: (item[0], item[1]))
        kept = {
            item[2]
            for item in ordered[-self.policy.update_backup_keep_count :]
        }
        selected = [
            record
            for _mtime, _name, directory, directory_records in ordered
            if directory not in kept
            for record in directory_records
            if not update_snapshot.protects(record.path)
            and not self._protected(record.path, active)
        ]
        return records, unsafe, selected

    def _walk_category(
        self,
        root: Path,
        category: StorageCategory,
        progress: Callable[[int], None] | None,
        cancelled: Callable[[], bool],
        counter: list[int],
        *,
        excluded: tuple[Path, ...] = (),
        recurse: bool = True,
    ) -> tuple[list[_FileRecord], list[StorageEntry]]:
        records: list[_FileRecord] = []
        unsafe: list[StorageEntry] = []
        excluded_paths = tuple(path.resolve() for path in excluded)
        if not root.exists():
            return records, unsafe

        def walk(directory: Path) -> None:
            try:
                guarded = self.paths.guard(directory)
                with os.scandir(guarded) as stream:
                    children = sorted(stream, key=lambda item: item.name.casefold())
            except (OSError, ValueError):
                return
            for item in children:
                self._tick(progress, cancelled, counter)
                path = Path(item.path)
                if any(path == excluded for excluded in excluded_paths):
                    continue
                try:
                    item_stat = item.stat(follow_symlinks=False)
                except OSError:
                    unsafe.append(self._unsafe_entry(category, path, 0, 0))
                    continue
                size = max(0, int(item_stat.st_size))
                mtime_ns = max(0, int(item_stat.st_mtime_ns))
                if self._is_reparse(item, item_stat):
                    unsafe.append(self._unsafe_entry(category, path, size, mtime_ns))
                    continue
                if stat_module.S_ISDIR(item_stat.st_mode):
                    if recurse:
                        walk(path)
                    continue
                if not stat_module.S_ISREG(item_stat.st_mode):
                    unsafe.append(self._unsafe_entry(category, path, size, mtime_ns))
                    continue
                records.append(
                    _FileRecord(path, self._relative(path), size, mtime_ns)
                )

        walk(root)
        return records, unsafe

    def _walk_download_candidates(
        self,
        progress: Callable[[int], None] | None,
        cancelled: Callable[[], bool],
        counter: list[int],
    ) -> tuple[
        list[tuple[_FileRecord, StorageCategory, Path | None]],
        list[StorageEntry],
    ]:
        candidates: list[tuple[_FileRecord, StorageCategory, Path | None]] = []
        unsafe: list[StorageEntry] = []
        for root in self.download_paths.roots:
            if not root.exists():
                continue
            root_id = self.download_paths.root_id(root)

            def walk(
                directory: Path,
                *,
                trusted_root: Path = root,
                trusted_root_id: str = root_id,
            ) -> None:
                try:
                    guarded = self.download_paths.guard_in(
                        trusted_root,
                        directory,
                        allow_root=True,
                    )
                    with os.scandir(guarded) as stream:
                        children = sorted(stream, key=lambda item: item.name.casefold())
                except (OSError, ValueError):
                    return
                for item in children:
                    self._tick(progress, cancelled, counter)
                    path = Path(item.path)
                    category = self._download_category(item.name)
                    try:
                        item_stat = item.stat(follow_symlinks=False)
                    except OSError:
                        if category is not None:
                            unsafe.append(
                                self._unsafe_entry(
                                    category,
                                    path,
                                    0,
                                    0,
                                    root=trusted_root,
                                    root_id=trusted_root_id,
                                )
                            )
                        continue
                    size = max(0, int(item_stat.st_size))
                    mtime_ns = max(0, int(item_stat.st_mtime_ns))
                    if self._is_reparse(item, item_stat):
                        if category is not None:
                            unsafe.append(
                                self._unsafe_entry(
                                    category,
                                    path,
                                    size,
                                    mtime_ns,
                                    root=trusted_root,
                                    root_id=trusted_root_id,
                                )
                            )
                        continue
                    if stat_module.S_ISDIR(item_stat.st_mode):
                        walk(path)
                        continue
                    if category is None:
                        continue
                    if not stat_module.S_ISREG(item_stat.st_mode):
                        unsafe.append(
                            self._unsafe_entry(
                                category,
                                path,
                                size,
                                mtime_ns,
                                root=trusted_root,
                                root_id=trusted_root_id,
                            )
                        )
                        continue
                    record = _FileRecord(
                        path,
                        PurePosixPath(path.relative_to(trusted_root).as_posix()),
                        size,
                        mtime_ns,
                        trusted_root_id,
                    )
                    candidates.append((record, category, self._leftover_target(path)))

            walk(root)
        return candidates, unsafe

    @staticmethod
    def _download_category(name: str) -> StorageCategory | None:
        if name.endswith(".part"):
            return StorageCategory.DOWNLOAD_PART
        if _CORRUPT_ARCHIVE.search(name) is not None:
            return StorageCategory.CORRUPT_ARCHIVE
        return None

    @staticmethod
    def _leftover_target(path: Path) -> Path | None:
        name = _CORRUPT_ARCHIVE.sub("", path.name)
        if name.endswith(".part"):
            name = name[: -len(".part")]
        if not name:
            return None
        return path.with_name(name).resolve()

    @staticmethod
    def _is_ordinary_file(path: Path | None) -> bool:
        if path is None:
            return False
        try:
            path_stat = path.stat(follow_symlinks=False)
        except OSError:
            return False
        return stat_module.S_ISREG(path_stat.st_mode) and not bool(
            getattr(path_stat, "st_file_attributes", 0) & _REPARSE_POINT
        )

    @staticmethod
    def _raise_if_cancelled(cancelled: Callable[[], bool]) -> None:
        if cancelled():
            raise StorageInventoryCancelled("存储扫描已取消")

    @staticmethod
    def _is_reparse(item: os.DirEntry[str], item_stat: os.stat_result) -> bool:
        return item.is_symlink() or bool(
            getattr(item_stat, "st_file_attributes", 0) & _REPARSE_POINT
        )

    @staticmethod
    def _tick(
        progress: Callable[[int], None] | None,
        cancelled: Callable[[], bool],
        counter: list[int],
    ) -> None:
        if cancelled():
            raise StorageInventoryCancelled("存储扫描已取消")
        counter[0] += 1
        if progress is not None and counter[0] % 256 == 0:
            progress(counter[0])

    def _relative(self, path: Path) -> PurePosixPath:
        return PurePosixPath(path.relative_to(self.paths.root).as_posix())

    def _unsafe_entry(
        self,
        category: StorageCategory,
        path: Path,
        size: int,
        mtime_ns: int,
        *,
        root: Path | None = None,
        root_id: str = "app",
    ) -> StorageEntry:
        relative = (
            PurePosixPath(path.relative_to(root).as_posix())
            if root is not None
            else self._relative(path)
        )
        return StorageEntry(
            id=storage_entry_id(category, relative, root_id),
            relative_path=relative,
            category=category,
            size=size,
            mtime_ns=mtime_ns,
            selectable=False,
            reason=StorageResultCode.UNSAFE_PATH,
            root_id=root_id,
        )

    @staticmethod
    def _summary(
        category: StorageCategory,
        now: datetime,
        records: list[_FileRecord],
        selected: list[_FileRecord],
        protected: Collection[StorageEntry] = (),
    ) -> StorageCategorySummary:
        return StorageCategorySummary(
            category=category,
            scanned_at=now,
            total_count=len(records) + len(protected),
            total_bytes=sum(record.size for record in records)
            + sum(entry.size for entry in protected),
            reclaimable_count=len(selected),
            reclaimable_bytes=sum(record.size for record in selected),
        )

    @staticmethod
    def _selectable_entry(
        category: StorageCategory,
        record: _FileRecord,
    ) -> StorageEntry:
        return StorageEntry(
            id=storage_entry_id(category, record.relative_path, record.root_id),
            relative_path=record.relative_path,
            category=category,
            size=record.size,
            mtime_ns=record.mtime_ns,
            selectable=True,
            root_id=record.root_id,
        )

    @staticmethod
    def _download_entry(
        category: StorageCategory,
        record: _FileRecord,
        *,
        selectable: bool,
        task_id: str | None,
        display_name: str | None,
    ) -> StorageEntry:
        return StorageEntry(
            id=storage_entry_id(category, record.relative_path, record.root_id),
            relative_path=record.relative_path,
            category=category,
            size=record.size,
            mtime_ns=record.mtime_ns,
            selectable=selectable,
            reason=None if selectable else StorageResultCode.PROTECTED_BY_TASK,
            task_id=task_id,
            display_name=display_name,
            root_id=record.root_id,
        )
