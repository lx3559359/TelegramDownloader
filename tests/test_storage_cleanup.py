import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from telegram_downloader.domain import IntegrityStatus, ItemStatus
from telegram_downloader.download_paths import DownloadPathPolicy
from telegram_downloader.paths import PortablePaths
from telegram_downloader.settings import DownloadStorageSettings
from telegram_downloader.storage_cleanup import (
    StorageCleanupExecutor,
    StorageCleanupPlanner,
)
from telegram_downloader.storage_models import (
    StorageCategory,
    StorageCategorySummary,
    StorageCleanupPlan,
    StorageEntry,
    StorageInventory,
    StorageResultCode,
    StorageTrigger,
)
from telegram_downloader.update_protection import UpdateProtectionSnapshot

NOW = datetime(2026, 8, 22, 8, tzinfo=UTC)


def custom_policy(tmp_path: Path) -> tuple[PortablePaths, DownloadPathPolicy]:
    paths = PortablePaths(tmp_path / "app")
    paths.ensure_layout()
    old_root = tmp_path / "old-media"
    current_root = tmp_path / "current-media"
    old_root.mkdir()
    current_root.mkdir()
    policy = DownloadPathPolicy(paths, DownloadStorageSettings())
    old_settings = policy.prepare(DownloadStorageSettings(str(old_root)))
    policy.apply(old_settings)
    current_settings = policy.prepare(
        DownloadStorageSettings(str(current_root), old_settings.trusted_roots)
    )
    policy.apply(current_settings)
    return paths, policy


def test_cleanup_rejects_entry_with_unknown_download_root_id(tmp_path) -> None:
    paths, policy = custom_policy(tmp_path)
    target = policy.current_root / "file.bin.part"
    target.write_bytes(b"part")
    target_stat = target.stat(follow_symlinks=False)
    entry = StorageEntry(
        id="forged-root",
        relative_path=PurePosixPath("file.bin.part"),
        category=StorageCategory.DOWNLOAD_PART,
        size=target_stat.st_size,
        mtime_ns=target_stat.st_mtime_ns,
        selectable=True,
        root_id="download-0000000000000000",
    )
    plan = StorageCleanupPlan(
        "manual-forged-root",
        NOW,
        StorageTrigger.MANUAL_DOWNLOAD,
        (entry,),
    )
    cleanup = StorageCleanupExecutor(
        paths,
        repository=None,
        update_protection=SnapshotProvider(),
        download_paths=policy,
        utc_clock=lambda: NOW,
    )

    result = cleanup.execute(plan)

    assert result.items[0].code is StorageResultCode.UNSAFE_PATH
    assert target.exists()


def test_cleanup_deletes_verified_leftover_from_external_root(tmp_path) -> None:
    paths, policy = custom_policy(tmp_path)
    media = policy.current_root / "file.bin"
    leftover = policy.current_root / "file.bin.part"
    media.write_bytes(b"done")
    leftover.write_bytes(b"part")
    file_stat = leftover.stat(follow_symlinks=False)
    entry = StorageEntry(
        id="external-leftover",
        relative_path=PurePosixPath("file.bin.part"),
        category=StorageCategory.DOWNLOAD_PART,
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
        selectable=True,
        root_id=policy.root_id(policy.current_root),
    )
    plan = StorageCleanupPlan(
        "manual-external",
        NOW,
        StorageTrigger.MANUAL_DOWNLOAD,
        (entry,),
    )
    cleanup = StorageCleanupExecutor(
        paths,
        repository=FakeRepository(media),
        update_protection=SnapshotProvider(),
        download_paths=policy,
        utc_clock=lambda: NOW,
    )

    result = cleanup.execute(plan)

    assert result.items[0].code is StorageResultCode.COMPLETED
    assert not leftover.exists()
    assert media.read_bytes() == b"done"


class SnapshotProvider:
    def __init__(self, snapshot: UpdateProtectionSnapshot | None = None) -> None:
        self.value = snapshot or UpdateProtectionSnapshot(frozenset(), False)

    def snapshot(self) -> UpdateProtectionSnapshot:
        return self.value


class FakeRepository:
    def __init__(self, target: Path) -> None:
        self.target = target.resolve()
        self.item_status = ItemStatus.COMPLETED
        self.integrity_status = IntegrityStatus.VERIFIED

    def maintenance_media_by_targets(self, targets):
        if self.target not in {Path(target).resolve() for target in targets}:
            return {}
        return {
            self.target: SimpleNamespace(
                task_id="task-1",
                task_title="资料群",
                target_path=self.target,
                item_status=self.item_status,
                integrity_status=self.integrity_status,
            )
        }


def make_entry(
    paths: PortablePaths,
    target: Path,
    category: StorageCategory,
    *,
    entry_id: str | None = None,
    selectable: bool = True,
) -> StorageEntry:
    file_stat = target.stat(follow_symlinks=False)
    manual = category in {
        StorageCategory.DOWNLOAD_PART,
        StorageCategory.CORRUPT_ARCHIVE,
    }
    if manual:
        root = paths.downloads.resolve()
        root_id = DownloadPathPolicy(
            paths,
            DownloadStorageSettings(),
        ).root_id(root)
    else:
        root = paths.root
        root_id = "app"
    return StorageEntry(
        id=entry_id or target.name,
        relative_path=PurePosixPath(target.relative_to(root).as_posix()),
        category=category,
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
        selectable=selectable,
        reason=None if selectable else StorageResultCode.PROTECTED_BY_TASK,
        root_id=root_id,
    )


def make_inventory(entries: tuple[StorageEntry, ...]) -> StorageInventory:
    categories = tuple(dict.fromkeys(entry.category for entry in entries))
    summaries = tuple(StorageCategorySummary(category, NOW, 1, 1, 1, 1) for category in categories)
    return StorageInventory(NOW, 1, entries, summaries)


def prepared_safe_file(
    tmp_path: Path,
    *,
    name: str = "old.tmp",
    content: bytes = b"old",
    remove_file=None,
) -> tuple[StorageEntry, StorageCleanupPlan, StorageCleanupExecutor]:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.temp / "nested" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    entry = make_entry(paths, target, StorageCategory.TEMP)
    plan = StorageCleanupPlan(
        "plan-safe",
        NOW,
        StorageTrigger.AUTOMATIC,
        (entry,),
    )
    executor = StorageCleanupExecutor(
        paths,
        repository=None,
        update_protection=SnapshotProvider(),
        utc_clock=lambda: NOW,
        remove_file=remove_file,
    )
    return entry, plan, executor


def test_planner_isolates_categories_and_sorts_entries() -> None:
    automatic_b = StorageEntry(
        "b",
        PurePosixPath("data/temp/b.tmp"),
        StorageCategory.TEMP,
        1,
        1,
        True,
    )
    automatic_a = replace(
        automatic_b,
        id="a",
        relative_path=PurePosixPath("data/cache/thumbnails/a.thumb"),
        category=StorageCategory.THUMBNAILS,
    )
    manual = replace(
        automatic_b,
        id="part",
        relative_path=PurePosixPath("downloads/file.bin.part"),
        category=StorageCategory.DOWNLOAD_PART,
    )
    protected = replace(
        automatic_b,
        id="protected",
        selectable=False,
        reason=StorageResultCode.UNSAFE_PATH,
    )
    inventory = make_inventory((automatic_b, manual, protected, automatic_a))
    planner = StorageCleanupPlanner()

    automatic = planner.automatic(inventory, NOW)
    selected = planner.manual_download(inventory, [manual.id], NOW)

    assert [entry.id for entry in automatic.entries] == ["b", "a"]
    assert automatic.entries is not inventory.entries
    assert [entry.id for entry in selected.entries] == [manual.id]
    assert selected.trigger is StorageTrigger.MANUAL_DOWNLOAD


@pytest.mark.parametrize(
    "selected_ids",
    ([], ["part", "part"], ["missing"], ["automatic"]),
)
def test_manual_planner_rejects_entire_invalid_selection(selected_ids) -> None:
    manual = StorageEntry(
        "part",
        PurePosixPath("downloads/file.bin.part"),
        StorageCategory.DOWNLOAD_PART,
        1,
        1,
        True,
    )
    automatic = replace(
        manual,
        id="automatic",
        relative_path=PurePosixPath("data/temp/file.tmp"),
        category=StorageCategory.TEMP,
    )

    with pytest.raises(ValueError):
        StorageCleanupPlanner().manual_download(
            make_inventory((manual, automatic)), selected_ids, NOW
        )


def test_executor_skips_file_changed_after_plan(tmp_path: Path) -> None:
    entry, plan, executor = prepared_safe_file(tmp_path, content=b"old")
    target = tmp_path / Path(entry.relative_path.as_posix())
    target.write_bytes(b"changed")

    result = executor.execute(plan)

    assert target.read_bytes() == b"changed"
    assert result.items[0].code is StorageResultCode.STATE_CHANGED


def test_executor_skips_mtime_changed_after_plan(tmp_path: Path) -> None:
    entry, plan, executor = prepared_safe_file(tmp_path, content=b"same")
    target = tmp_path / Path(entry.relative_path.as_posix())
    os.utime(target, ns=(entry.mtime_ns + 1_000_000, entry.mtime_ns + 1_000_000))

    result = executor.execute(plan)

    assert target.exists()
    assert result.items[0].code is StorageResultCode.STATE_CHANGED


def test_executor_rejects_file_replaced_by_link(tmp_path: Path) -> None:
    entry, plan, executor = prepared_safe_file(tmp_path)
    target = tmp_path / Path(entry.relative_path.as_posix())
    outside = tmp_path.parent / f"{tmp_path.name}-outside.bin"
    outside.write_bytes(b"private")
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError:
        outside.unlink(missing_ok=True)
        pytest.skip("Windows symlink privilege is unavailable")
    try:
        result = executor.execute(plan)
        assert result.items[0].code is StorageResultCode.UNSAFE_PATH
        assert outside.read_bytes() == b"private"
    finally:
        outside.unlink(missing_ok=True)


def test_executor_rechecks_update_protection(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.update_staging / "old.zip"
    target.write_bytes(b"package")
    entry = make_entry(paths, target, StorageCategory.UPDATE_STAGING)
    plan = StorageCleanupPlan("update-plan", NOW, StorageTrigger.AUTOMATIC, (entry,))
    provider = SnapshotProvider()
    provider.value = UpdateProtectionSnapshot(frozenset({target.resolve()}), False)
    executor = StorageCleanupExecutor(paths, None, provider, utc_clock=lambda: NOW)

    result = executor.execute(plan)

    assert target.exists()
    assert result.items[0].code is StorageResultCode.PROTECTED_BY_UPDATE


def test_executor_rechecks_manual_task_state(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    formal = paths.downloads / "video.mp4"
    formal.write_bytes(b"formal")
    candidate = paths.downloads / "video.mp4.part"
    candidate.write_bytes(b"leftover")
    entry = make_entry(paths, candidate, StorageCategory.DOWNLOAD_PART)
    plan = StorageCleanupPlan("manual-plan", NOW, StorageTrigger.MANUAL_DOWNLOAD, (entry,))
    repository = FakeRepository(formal)
    repository.item_status = ItemStatus.WAITING_RETRY
    executor = StorageCleanupExecutor(paths, repository, SnapshotProvider(), utc_clock=lambda: NOW)

    result = executor.execute(plan)

    assert candidate.exists()
    assert result.items[0].code is StorageResultCode.PROTECTED_BY_TASK


def test_executor_maps_permission_and_sharing_failures(tmp_path: Path) -> None:
    def permission_denied(_path: Path) -> None:
        raise PermissionError("denied")

    _entry, plan, executor = prepared_safe_file(
        tmp_path / "permission", remove_file=permission_denied
    )
    permission_result = executor.execute(plan)

    sharing_error = OSError("sharing violation")
    sharing_error.winerror = 32

    def file_in_use(_path: Path) -> None:
        raise sharing_error

    _entry, plan, executor = prepared_safe_file(tmp_path / "sharing", remove_file=file_in_use)
    sharing_result = executor.execute(plan)

    assert permission_result.items[0].code is StorageResultCode.PERMISSION_DENIED
    assert permission_result.result_code is StorageResultCode.LOCAL_ERROR
    assert sharing_result.items[0].code is StorageResultCode.FILE_IN_USE


def test_executor_cancellation_preserves_partial_success(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    entries = []
    for name in ("a.tmp", "b.tmp", "c.tmp"):
        target = paths.temp / name
        target.write_bytes(name.encode("utf-8"))
        entries.append(make_entry(paths, target, StorageCategory.TEMP))
    plan = StorageCleanupPlan("cancel-plan", NOW, StorageTrigger.AUTOMATIC, tuple(entries))
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    executor = StorageCleanupExecutor(paths, None, SnapshotProvider(), utc_clock=lambda: NOW)
    result = executor.execute(plan, cancelled=cancelled)

    assert result.deleted_count == 1
    assert result.cancelled_count == 2
    assert result.result_code is StorageResultCode.CANCELLED


def test_executor_reports_partial_failure_without_undoing_success(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    first = paths.temp / "a.tmp"
    second = paths.temp / "b.tmp"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    entries = (
        make_entry(paths, first, StorageCategory.TEMP),
        make_entry(paths, second, StorageCategory.TEMP),
    )

    def remove(path: Path) -> None:
        if path.name == "b.tmp":
            raise PermissionError("denied")
        path.unlink()

    executor = StorageCleanupExecutor(
        paths,
        None,
        SnapshotProvider(),
        utc_clock=lambda: NOW,
        remove_file=remove,
    )
    result = executor.execute(StorageCleanupPlan("partial", NOW, StorageTrigger.AUTOMATIC, entries))

    assert first.exists() is False
    assert second.exists()
    assert result.deleted_count == 1
    assert result.failed_count == 1
    assert result.result_code is StorageResultCode.LOCAL_ERROR


def test_executor_never_removes_parent_with_unknown_file(tmp_path: Path) -> None:
    entry, plan, executor = prepared_safe_file(tmp_path, content=b"old")
    parent = (tmp_path / Path(entry.relative_path.as_posix())).parent
    (parent / "unknown.keep").write_bytes(b"keep")

    result = executor.execute(plan)

    assert result.deleted_count == 1
    assert (parent / "unknown.keep").is_file()
    assert parent.is_dir()


def test_executor_rejects_category_root_mismatch(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "private.bin"
    target.write_bytes(b"private")
    forged = make_entry(paths, target, StorageCategory.TEMP)
    plan = StorageCleanupPlan("forged", NOW, StorageTrigger.AUTOMATIC, (forged,))
    executor = StorageCleanupExecutor(paths, None, SnapshotProvider(), utc_clock=lambda: NOW)

    result = executor.execute(plan)

    assert target.read_bytes() == b"private"
    assert result.items[0].code is StorageResultCode.UNSAFE_PATH


def test_empty_plan_returns_nothing_to_clean(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    executor = StorageCleanupExecutor(paths, None, SnapshotProvider(), utc_clock=lambda: NOW)
    plan = StorageCleanupPlan("empty", NOW, StorageTrigger.AUTOMATIC, ())

    result = executor.execute(plan)

    assert result.items == ()
    assert result.result_code is StorageResultCode.NOTHING_TO_CLEAN
    assert result.completed_at == NOW + timedelta(0)
