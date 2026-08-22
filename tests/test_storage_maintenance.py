import asyncio
import logging
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from telegram_downloader.maintenance_activity import (
    ActivityKind,
    OperationActivityRegistry,
)
from telegram_downloader.notifications import EventKind, NotificationRoute
from telegram_downloader.paths import PortablePaths
from telegram_downloader.settings import StorageMaintenanceSettings
from telegram_downloader.storage_cleanup import StorageCleanupPlanner
from telegram_downloader.storage_maintenance import (
    StorageMaintenanceError,
    StorageMaintenanceService,
)
from telegram_downloader.storage_models import (
    StorageCategory,
    StorageCategorySummary,
    StorageEntry,
    StorageExecutionItem,
    StorageExecutionResult,
    StorageInventory,
    StorageMaintenanceState,
    StorageResultCode,
    StorageTrigger,
)
from telegram_downloader.storage_state import StorageStateError
from telegram_downloader.update_protection import UpdateProtectionSnapshot

NOW = datetime(2026, 8, 22, 8, tzinfo=UTC)


class MemoryStateStore:
    def __init__(self) -> None:
        self.state = StorageMaintenanceState()
        self.corrupt = False
        self.fail_save = False
        self.saved: list[StorageMaintenanceState] = []

    def load(self) -> StorageMaintenanceState:
        if self.corrupt:
            raise StorageStateError("private-file-name")
        return self.state

    def save(self, state: StorageMaintenanceState) -> None:
        if self.fail_save:
            raise StorageStateError("private-file-name")
        self.corrupt = False
        self.state = state
        self.saved.append(state)


class SnapshotProvider:
    def snapshot(self) -> UpdateProtectionSnapshot:
        return UpdateProtectionSnapshot(frozenset(), False)


class FakeInventory:
    def __init__(self) -> None:
        auto_entry = StorageEntry(
            "auto-entry",
            PurePosixPath("data/temp/old.tmp"),
            StorageCategory.TEMP,
            10,
            1,
            True,
        )
        manual_entry = StorageEntry(
            "manual-entry",
            PurePosixPath("downloads/media.mp4.part"),
            StorageCategory.DOWNLOAD_PART,
            20,
            1,
            True,
            task_id="task-private",
            display_name="私人任务名",
        )
        self.automatic_result = StorageInventory(
            NOW,
            100,
            (auto_entry,),
            (StorageCategorySummary(StorageCategory.TEMP, NOW, 1, 10, 1, 10),),
        )
        self.download_result = StorageInventory(
            NOW,
            100,
            (manual_entry,),
            (StorageCategorySummary(StorageCategory.DOWNLOAD_PART, NOW, 1, 20, 1, 20),),
        )
        self.automatic_calls = 0
        self.download_calls = 0
        self.started: threading.Event | None = None
        self.release: threading.Event | None = None

    def scan_automatic(self, now, snapshot, *, active_paths, progress, cancelled):
        self.automatic_calls += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            self.release.wait(timeout=5)
        return replace(self.automatic_result, scanned_at=now)

    def scan_download_candidates(self, now, *, active_paths, progress, cancelled):
        self.download_calls += 1
        return replace(self.download_result, scanned_at=now)


class FakeExecutor:
    def __init__(self) -> None:
        self.update_protection = SnapshotProvider()
        self.calls = 0
        self.error: Exception | None = None
        self.item_code = StorageResultCode.COMPLETED

    def execute(self, plan, *, cancelled):
        self.calls += 1
        if self.error is not None:
            raise self.error
        items = tuple(
            StorageExecutionItem(
                entry.id,
                entry.category,
                self.item_code,
                entry.size if self.item_code is StorageResultCode.COMPLETED else 0,
            )
            for entry in plan.entries
        )
        code = StorageResultCode.COMPLETED if items else StorageResultCode.NOTHING_TO_CLEAN
        return StorageExecutionResult(
            plan.id,
            plan.trigger,
            NOW,
            NOW,
            code,
            items,
        )


class MonotonicClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def make_service(
    tmp_path: Path,
    *,
    automatic_enabled: bool = False,
) -> tuple[
    StorageMaintenanceService,
    MemoryStateStore,
    FakeInventory,
    FakeExecutor,
    OperationActivityRegistry,
    list[object],
    MonotonicClock,
]:
    store = MemoryStateStore()
    inventory = FakeInventory()
    executor = FakeExecutor()
    activity = OperationActivityRegistry()
    published: list[object] = []
    monotonic = MonotonicClock()
    settings = replace(StorageMaintenanceSettings(), automatic_enabled=automatic_enabled)
    service = StorageMaintenanceService(
        paths=PortablePaths(tmp_path),
        settings=settings,
        state_store=store,
        inventory=inventory,
        planner=StorageCleanupPlanner(),
        executor=executor,
        activity=activity,
        publish=published.append,
        utc_clock=lambda: NOW,
        monotonic_clock=monotonic,
    )
    return service, store, inventory, executor, activity, published, monotonic


@pytest.mark.asyncio
async def test_scan_merges_summaries_and_releases_activity_token(tmp_path: Path) -> None:
    service, store, inventory, _executor, activity, _published, _clock = make_service(tmp_path)

    result = await service.scan_automatic()

    assert result.entries[0].id == "auto-entry"
    assert inventory.automatic_calls == 1
    assert store.state.last_scan_at == NOW
    assert store.state.summaries == result.summaries
    assert activity.is_idle is True


@pytest.mark.asyncio
async def test_duplicate_scan_is_rejected_without_parallel_traversal(tmp_path: Path) -> None:
    service, _store, inventory, _executor, _activity, _published, _clock = make_service(tmp_path)
    inventory.started = threading.Event()
    inventory.release = threading.Event()
    first = asyncio.create_task(service.scan_automatic())
    while not inventory.started.is_set():
        await asyncio.sleep(0)

    with pytest.raises(StorageMaintenanceError, match="正在进行"):
        await service.scan_automatic()
    inventory.release.set()
    await first

    assert inventory.automatic_calls == 1


@pytest.mark.asyncio
async def test_safe_confirmation_is_one_time_and_contains_only_aggregates(
    tmp_path: Path,
) -> None:
    service, _store, _inventory, executor, _activity, _published, _clock = make_service(tmp_path)

    confirmation = await service.prepare_safe()
    result = await service.execute_safe(confirmation.id)

    assert confirmation.item_count == 1
    assert confirmation.expected_bytes == 10
    assert confirmation.categories[0].category is StorageCategory.TEMP
    assert not hasattr(confirmation, "entries")
    assert result.deleted_count == 1
    assert executor.calls == 1
    with pytest.raises(StorageMaintenanceError, match="确认"):
        await service.execute_safe(confirmation.id)


@pytest.mark.asyncio
async def test_busy_execute_does_not_consume_confirmation(tmp_path: Path) -> None:
    service, _store, _inventory, executor, activity, _published, _clock = make_service(tmp_path)
    confirmation = await service.prepare_safe()

    with activity.track(ActivityKind.DOWNLOAD):
        busy = await service.execute_safe(confirmation.id)
    completed = await service.execute_safe(confirmation.id)

    assert busy.result_code is StorageResultCode.BUSY_DEFERRED
    assert completed.result_code is StorageResultCode.COMPLETED
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_manual_confirmation_expires_and_cannot_execute(tmp_path: Path) -> None:
    service, _store, _inventory, executor, _activity, _published, clock = make_service(tmp_path)
    inventory = await service.scan_downloads()
    confirmation = service.prepare_manual([inventory.entries[0].id])
    clock.value = confirmation.expires_at + 1

    with pytest.raises(StorageMaintenanceError, match="过期"):
        await service.execute_manual(confirmation.id)
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_rescan_invalidates_old_manual_confirmation(tmp_path: Path) -> None:
    service, _store, _inventory, executor, _activity, _published, _clock = make_service(tmp_path)
    inventory = await service.scan_downloads()
    confirmation = service.prepare_manual([inventory.entries[0].id])

    await service.scan_downloads()

    with pytest.raises(StorageMaintenanceError, match="确认"):
        await service.execute_manual(confirmation.id)
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_state_save_failure_after_delete_is_not_reported_as_rollback(
    tmp_path: Path,
) -> None:
    service, store, _inventory, executor, _activity, _published, _clock = make_service(tmp_path)
    confirmation = await service.prepare_safe()
    store.fail_save = True

    result = await service.execute_safe(confirmation.id)

    assert executor.calls == 1
    assert result.deleted_count == 1
    assert result.result_code is StorageResultCode.STATE_SAVE_FAILED


@pytest.mark.asyncio
async def test_corrupt_state_blocks_automatic_cleanup_without_scan(tmp_path: Path) -> None:
    service, store, inventory, executor, _activity, published, _clock = make_service(
        tmp_path, automatic_enabled=True
    )
    store.corrupt = True

    result = await service.clean_safe(StorageTrigger.AUTOMATIC)

    assert result.result_code is StorageResultCode.LOCAL_ERROR
    assert inventory.automatic_calls == 0
    assert executor.calls == 0
    assert published == []


@pytest.mark.asyncio
async def test_automatic_success_updates_due_and_aggregate_history(tmp_path: Path) -> None:
    service, store, inventory, executor, _activity, published, _clock = make_service(
        tmp_path, automatic_enabled=True
    )

    result = await service.clean_safe(StorageTrigger.AUTOMATIC)

    assert result.result_code is StorageResultCode.COMPLETED
    assert inventory.automatic_calls == 1
    assert executor.calls == 1
    assert store.state.last_automatic_check_at == NOW
    assert store.state.last_cleanup_at == NOW
    assert store.state.next_due_at == NOW.replace(day=23)
    assert store.state.history[-1].deleted_count == 1
    assert store.state.history[-1].released_bytes == 10
    assert "old.tmp" not in repr(store.state.history[-1])
    assert published == []


@pytest.mark.asyncio
async def test_automatic_busy_records_attempt_without_advancing_due(tmp_path: Path) -> None:
    service, store, inventory, executor, activity, _published, _clock = make_service(
        tmp_path, automatic_enabled=True
    )

    with activity.track(ActivityKind.DOWNLOAD):
        result = await service.clean_safe(StorageTrigger.AUTOMATIC)

    assert result.result_code is StorageResultCode.BUSY_DEFERRED
    assert inventory.automatic_calls == 0
    assert executor.calls == 0
    assert store.state.last_automatic_check_at == NOW
    assert store.state.last_cleanup_at is None
    assert store.state.next_due_at is None
    assert store.state.history[-1].result_code is StorageResultCode.BUSY_DEFERRED


@pytest.mark.asyncio
async def test_user_scan_replaces_corrupt_state_with_schema_one(tmp_path: Path) -> None:
    service, store, inventory, _executor, _activity, _published, _clock = make_service(tmp_path)
    store.corrupt = True

    await service.scan_automatic()

    assert inventory.automatic_calls == 1
    assert store.corrupt is False
    assert store.state.schema_version == 1
    assert store.state.last_scan_at == NOW


@pytest.mark.asyncio
async def test_executor_exception_is_redacted_from_logs_and_published_result(
    tmp_path: Path,
    caplog,
) -> None:
    service, _store, _inventory, executor, _activity, published, _clock = make_service(tmp_path)
    confirmation = await service.prepare_safe()
    secret = "D:/private/secret-file-name.tmp api_hash=top-secret"
    executor.error = RuntimeError(secret)

    with caplog.at_level(logging.WARNING):
        result = await service.execute_safe(confirmation.id)

    assert result.result_code is StorageResultCode.LOCAL_ERROR
    assert secret not in caplog.text
    assert "secret-file-name" not in caplog.text
    assert secret not in repr(result)
    assert published == []


@pytest.mark.asyncio
async def test_large_automatic_cleanup_publishes_aggregate_event(tmp_path: Path) -> None:
    service, _store, inventory, _executor, _activity, published, _clock = make_service(
        tmp_path,
        automatic_enabled=True,
    )
    size = 100 * 1024**2
    entry = replace(inventory.automatic_result.entries[0], size=size)
    summary = replace(
        inventory.automatic_result.summaries[0],
        total_bytes=size,
        reclaimable_bytes=size,
    )
    inventory.automatic_result = replace(
        inventory.automatic_result,
        entries=(entry,),
        summaries=(summary,),
    )

    result = await service.clean_safe(StorageTrigger.AUTOMATIC)

    assert result.released_bytes == size
    assert len(published) == 1
    assert published[0].kind is EventKind.STORAGE_CLEANED
    assert published[0].count == 1
    assert published[0].byte_count == size
    assert published[0].route is NotificationRoute.MAINTENANCE
    assert published[0].private_context == ""


@pytest.mark.asyncio
async def test_automatic_item_failure_publishes_fixed_failure_event(tmp_path: Path) -> None:
    service, _store, _inventory, executor, _activity, published, _clock = make_service(
        tmp_path,
        automatic_enabled=True,
    )
    executor.item_code = StorageResultCode.PERMISSION_DENIED

    result = await service.clean_safe(StorageTrigger.AUTOMATIC)

    assert result.failed_count == 1
    assert len(published) == 1
    assert published[0].kind is EventKind.STORAGE_CLEANUP_FAILED
    assert published[0].count == 1
    assert published[0].byte_count == 0
    assert published[0].private_context == ""
