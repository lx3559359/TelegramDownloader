from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from telegram_downloader.settings import StorageMaintenanceSettings
from telegram_downloader.storage_models import (
    StorageCategory,
    StorageCategoryCount,
    StorageCategorySummary,
    StorageCleanupPlan,
    StorageEntry,
    StorageExecutionItem,
    StorageExecutionResult,
    StorageInventory,
    StorageMaintenanceState,
    StoragePolicy,
    StorageResultCode,
    StorageRunHistory,
    StorageTrigger,
)

NOW = datetime(2026, 8, 22, 8, tzinfo=UTC)


def make_entry(identifier: str = "entry-1") -> StorageEntry:
    return StorageEntry(
        id=identifier,
        relative_path=PurePosixPath("data/temp/old.bin"),
        category=StorageCategory.TEMP,
        size=10,
        mtime_ns=1,
        selectable=True,
    )


def test_storage_categories_and_policy_are_fixed() -> None:
    assert tuple(StorageCategory) == (
        StorageCategory.THUMBNAILS,
        StorageCategory.TEMP,
        StorageCategory.ROTATED_LOGS,
        StorageCategory.UPDATE_STAGING,
        StorageCategory.UPDATE_BACKUP,
        StorageCategory.DOWNLOAD_PART,
        StorageCategory.CORRUPT_ARCHIVE,
    )
    assert StoragePolicy.from_settings(StorageMaintenanceSettings()) == StoragePolicy()
    assert StoragePolicy(thumbnail_limit_bytes=100, thumbnail_target_bytes=90)


@pytest.mark.parametrize(
    "relative",
    [
        PurePosixPath(),
        PurePosixPath("/absolute.bin"),
        PurePosixPath("../escape.bin"),
        PurePosixPath("data\\escape.bin"),
    ],
)
def test_storage_entry_rejects_unsafe_relative_paths(relative) -> None:
    with pytest.raises(ValueError):
        StorageEntry(
            id="entry",
            relative_path=relative,
            category=StorageCategory.TEMP,
            size=1,
            mtime_ns=1,
            selectable=True,
        )


def test_storage_entry_requires_consistent_protection_reason() -> None:
    with pytest.raises(ValueError):
        StorageEntry(
            id="entry",
            relative_path=PurePosixPath("data/temp/item"),
            category=StorageCategory.TEMP,
            size=1,
            mtime_ns=1,
            selectable=False,
        )
    with pytest.raises(ValueError):
        StorageEntry(
            id="entry",
            relative_path=PurePosixPath("data/temp/item"),
            category=StorageCategory.TEMP,
            size=1,
            mtime_ns=1,
            selectable=True,
            reason=StorageResultCode.UNSAFE_PATH,
        )


@pytest.mark.parametrize(
    "root_id",
    ("", "downloads", "download-xyz", "download-00000000000000000"),
)
def test_storage_entry_rejects_invalid_root_ids(root_id: str) -> None:
    with pytest.raises(ValueError, match="根目录标识"):
        StorageEntry(
            id="entry",
            relative_path=PurePosixPath("file.bin.part"),
            category=StorageCategory.DOWNLOAD_PART,
            size=1,
            mtime_ns=1,
            selectable=True,
            root_id=root_id,
        )


def test_storage_entry_accepts_app_and_download_root_ids() -> None:
    assert make_entry().root_id == "app"
    entry = StorageEntry(
        id="entry",
        relative_path=PurePosixPath("file.bin.part"),
        category=StorageCategory.DOWNLOAD_PART,
        size=1,
        mtime_ns=1,
        selectable=True,
        root_id="download-0123456789abcdef",
    )
    assert entry.root_id == "download-0123456789abcdef"


def test_plan_and_inventory_reject_duplicate_entries_and_are_immutable() -> None:
    entry = make_entry()
    with pytest.raises(ValueError, match="重复"):
        StorageCleanupPlan(
            id="plan",
            generated_at=NOW,
            trigger=StorageTrigger.MANUAL_SAFE,
            entries=(entry, entry),
        )
    with pytest.raises(ValueError, match="重复"):
        StorageInventory(
            scanned_at=NOW,
            disk_free_bytes=100,
            entries=(entry, entry),
            summaries=(),
        )
    plan = StorageCleanupPlan("plan", NOW, StorageTrigger.MANUAL_SAFE, (entry,))
    assert plan.expected_bytes == 10
    with pytest.raises(FrozenInstanceError):
        plan.id = "changed"


def test_execution_result_counts_partial_outcomes() -> None:
    result = StorageExecutionResult(
        plan_id="plan",
        trigger=StorageTrigger.MANUAL_SAFE,
        started_at=NOW,
        completed_at=NOW,
        result_code=StorageResultCode.LOCAL_ERROR,
        items=(
            StorageExecutionItem(
                "one", StorageCategory.TEMP, StorageResultCode.COMPLETED, 10
            ),
            StorageExecutionItem(
                "two", StorageCategory.TEMP, StorageResultCode.STATE_CHANGED, 0
            ),
            StorageExecutionItem(
                "three", StorageCategory.TEMP, StorageResultCode.PERMISSION_DENIED, 0
            ),
            StorageExecutionItem(
                "four", StorageCategory.TEMP, StorageResultCode.CANCELLED, 0
            ),
        ),
    )
    assert result.released_bytes == 10
    assert result.deleted_count == 1
    assert result.skipped_count == 1
    assert result.failed_count == 1
    assert result.cancelled_count == 1


def test_models_reject_negative_counts_non_utc_and_duplicate_categories() -> None:
    with pytest.raises(ValueError):
        StorageCategorySummary(StorageCategory.TEMP, NOW, -1, 0, 0, 0)
    with pytest.raises(ValueError):
        StorageCleanupPlan(
            "plan",
            datetime(2026, 8, 22, 8),
            StorageTrigger.AUTOMATIC,
            (),
        )
    summary = StorageCategorySummary(StorageCategory.TEMP, NOW, 1, 1, 0, 0)
    with pytest.raises(ValueError, match="重复"):
        StorageMaintenanceState(summaries=(summary, summary))
    with pytest.raises(ValueError):
        StorageMaintenanceState(schema_version=1.0)


def test_history_counts_must_match_category_aggregates() -> None:
    category = StorageCategoryCount(StorageCategory.TEMP, 1, 0, 0, 0, 10)
    history = StorageRunHistory(
        occurred_at=NOW,
        trigger=StorageTrigger.MANUAL_SAFE,
        categories=(category,),
        deleted_count=1,
        skipped_count=0,
        failed_count=0,
        cancelled_count=0,
        released_bytes=10,
        result_code=StorageResultCode.COMPLETED,
    )
    assert history.categories == (category,)
    with pytest.raises(ValueError, match="汇总"):
        StorageRunHistory(
            occurred_at=NOW,
            trigger=StorageTrigger.MANUAL_SAFE,
            categories=(category,),
            deleted_count=2,
            skipped_count=0,
            failed_count=0,
            cancelled_count=0,
            released_bytes=10,
            result_code=StorageResultCode.COMPLETED,
        )
