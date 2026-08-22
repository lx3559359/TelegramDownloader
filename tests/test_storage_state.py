import json
from datetime import UTC, datetime

import pytest

from telegram_downloader.storage_models import (
    StorageCategory,
    StorageCategoryCount,
    StorageMaintenanceState,
    StorageResultCode,
    StorageRunHistory,
    StorageTrigger,
)
from telegram_downloader.storage_state import StorageStateError, StorageStateStore


def make_history(index: int) -> StorageRunHistory:
    count = StorageCategoryCount(
        category=StorageCategory.TEMP,
        deleted_count=index,
        skipped_count=0,
        failed_count=0,
        cancelled_count=0,
        released_bytes=index * 100,
    )
    return StorageRunHistory(
        occurred_at=datetime(2026, 8, 22, 0, index, tzinfo=UTC),
        trigger=StorageTrigger.MANUAL_SAFE,
        categories=(count,),
        deleted_count=index,
        skipped_count=0,
        failed_count=0,
        cancelled_count=0,
        released_bytes=index * 100,
        result_code=StorageResultCode.COMPLETED,
    )


def test_missing_state_returns_empty_schema_one_state(tmp_path) -> None:
    store = StorageStateStore(tmp_path / "storage-state.json")

    assert store.load() == StorageMaintenanceState()


def test_state_history_is_bounded_and_contains_no_paths(tmp_path) -> None:
    store = StorageStateStore(tmp_path / "storage-state.json")
    state = StorageMaintenanceState(history=tuple(make_history(i) for i in range(21)))

    store.save(state)
    loaded = store.load()

    assert len(loaded.history) == 20
    assert loaded.history[0].deleted_count == 1
    payload = store.path.read_text(encoding="utf-8")
    assert "downloads" not in payload
    assert "private.mp4" not in payload
    assert not store.path.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        '{"schemaVersion":2}',
        '{"schemaVersion":1,"schemaVersion":1}',
        json.dumps(
            {
                "schemaVersion": 1,
                "lastScanAt": None,
                "lastAutomaticCheckAt": None,
                "lastCleanupAt": None,
                "nextDueAt": None,
                "summaries": [],
                "history": [],
                "privatePath": "downloads/private.mp4",
            }
        ),
    ],
)
def test_state_rejects_corrupt_unknown_duplicate_and_extra_fields(tmp_path, payload) -> None:
    store = StorageStateStore(tmp_path / "storage-state.json")
    store.path.write_text(payload, encoding="utf-8")

    with pytest.raises(StorageStateError, match="存储维护记录不可用"):
        store.load()


def test_state_rejects_non_utc_time_and_unknown_result_code(tmp_path) -> None:
    store = StorageStateStore(tmp_path / "storage-state.json")
    store.save(StorageMaintenanceState(history=(make_history(1),)))
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["history"][0]["occurredAt"] = "2026-08-22T00:01:00"
    payload["history"][0]["resultCode"] = "invented"
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StorageStateError, match="存储维护记录不可用"):
        store.load()


def test_state_rejects_more_than_twenty_persisted_history_entries(tmp_path) -> None:
    store = StorageStateStore(tmp_path / "storage-state.json")
    store.save(StorageMaintenanceState(history=(make_history(1),)))
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["history"] = payload["history"] * 21
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StorageStateError, match="存储维护记录不可用"):
        store.load()
