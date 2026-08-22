from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from telegram_downloader.maintenance_activity import (
    ActivityKind,
    OperationActivityRegistry,
)
from telegram_downloader.notifications import (
    storage_cleaned_event,
    storage_cleanup_failed_event,
)
from telegram_downloader.paths import PortablePaths
from telegram_downloader.settings import StorageMaintenanceSettings
from telegram_downloader.storage_cleanup import (
    StorageCleanupExecutor,
    StorageCleanupPlanner,
)
from telegram_downloader.storage_inventory import StorageInventoryCancelled, StorageInventoryService
from telegram_downloader.storage_models import (
    StorageCategory,
    StorageCategoryCount,
    StorageCleanupPlan,
    StorageExecutionItem,
    StorageExecutionResult,
    StorageInventory,
    StorageMaintenanceState,
    StorageResultCode,
    StorageRunHistory,
    StorageTrigger,
)
from telegram_downloader.storage_state import StorageStateError, StorageStateStore

logger = logging.getLogger(__name__)
_CONFIRMATION_SECONDS = 300.0
_AUTOMATIC_NOTIFICATION_BYTES = 100 * 1024**2
_FAILED_ITEM_CODES = frozenset(
    {
        StorageResultCode.FILE_IN_USE,
        StorageResultCode.PERMISSION_DENIED,
        StorageResultCode.LOCAL_ERROR,
    }
)


class StorageMaintenanceError(RuntimeError):
    """Fixed user-safe storage maintenance failure."""


@dataclass(frozen=True, slots=True)
class StoragePreviewCategory:
    category: StorageCategory
    item_count: int
    expected_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.category, StorageCategory):
            raise ValueError("预览类别无效")
        for value in (self.item_count, self.expected_bytes):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("预览统计无效")


@dataclass(frozen=True, slots=True)
class SafeCleanupConfirmation:
    id: str
    categories: tuple[StoragePreviewCategory, ...]
    item_count: int
    expected_bytes: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class ManualCleanupConfirmation:
    id: str
    item_count: int
    expected_bytes: int
    expires_at: float


class StorageMaintenanceService:
    def __init__(
        self,
        *,
        paths: PortablePaths,
        settings: StorageMaintenanceSettings,
        state_store: StorageStateStore,
        inventory: StorageInventoryService,
        planner: StorageCleanupPlanner,
        executor: StorageCleanupExecutor,
        activity: OperationActivityRegistry,
        publish: Callable[[Any], None],
        utc_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.state_store = state_store
        self.inventory = inventory
        self.planner = planner
        self.executor = executor
        self.activity = activity
        self.publish = publish
        self.utc_clock = utc_clock or (lambda: datetime.now(UTC))
        self.monotonic_clock = monotonic_clock or time.monotonic
        self._operation_lock = asyncio.Lock()
        self._worker_task: asyncio.Task[Any] | None = None
        self._cancel_event: threading.Event | None = None
        self._state: StorageMaintenanceState | None = None
        self._state_invalid = False
        self._automatic_inventory: StorageInventory | None = None
        self._download_inventory: StorageInventory | None = None
        self._safe_confirmations: dict[str, tuple[SafeCleanupConfirmation, StorageCleanupPlan]] = {}
        self._manual_confirmations: dict[
            str, tuple[ManualCleanupConfirmation, StorageCleanupPlan]
        ] = {}
        self._shutdown = False

    def load_state(self) -> StorageMaintenanceState:
        try:
            return self._trusted_state()
        except StorageStateError as exc:
            raise StorageMaintenanceError("存储维护记录不可用") from exc

    async def scan_automatic(
        self,
        progress: Callable[[int], None] | None = None,
    ) -> StorageInventory:
        self._ensure_available()
        if self._operation_lock.locked():
            raise StorageMaintenanceError("存储维护操作正在进行")
        async with self._operation_lock:
            token = self.activity.try_track_maintenance(ActivityKind.STORAGE_SCAN)
            if token is None:
                raise StorageMaintenanceError("当前有业务操作，暂时无法扫描存储")
            with token:
                try:
                    result = await self._scan_automatic_worker(progress)
                    self._save_scan(result)
                    self._automatic_inventory = result
                    self._safe_confirmations.clear()
                    return result
                except StorageMaintenanceError:
                    raise
                except StorageInventoryCancelled as exc:
                    raise StorageMaintenanceError("存储扫描已取消") from exc
                except Exception as exc:
                    logger.warning("storage automatic scan failed")
                    raise StorageMaintenanceError("存储扫描失败") from exc

    async def scan_downloads(
        self,
        progress: Callable[[int], None] | None = None,
    ) -> StorageInventory:
        self._ensure_available()
        if self._operation_lock.locked():
            raise StorageMaintenanceError("存储维护操作正在进行")
        async with self._operation_lock:
            token = self.activity.try_track_maintenance(ActivityKind.STORAGE_SCAN)
            if token is None:
                raise StorageMaintenanceError("当前有业务操作，暂时无法扫描存储")
            with token:
                try:
                    result = await self._scan_downloads_worker(progress)
                    self._save_scan(result)
                    self._download_inventory = result
                    self._manual_confirmations.clear()
                    return result
                except StorageMaintenanceError:
                    raise
                except StorageInventoryCancelled as exc:
                    raise StorageMaintenanceError("分片与留档扫描已取消") from exc
                except Exception as exc:
                    logger.warning("storage download scan failed")
                    raise StorageMaintenanceError("分片与留档扫描失败") from exc

    async def prepare_safe(self) -> SafeCleanupConfirmation:
        self._ensure_available()
        if self._operation_lock.locked():
            raise StorageMaintenanceError("存储维护操作正在进行")
        async with self._operation_lock:
            token = self.activity.try_track_maintenance(ActivityKind.STORAGE_SCAN)
            if token is None:
                raise StorageMaintenanceError("当前有业务操作，暂时无法扫描存储")
            with token:
                try:
                    inventory = await self._scan_automatic_worker(None)
                    self._save_scan(inventory)
                    self._automatic_inventory = inventory
                    self._safe_confirmations.clear()
                    plan = self.planner.automatic(
                        inventory,
                        self.utc_clock(),
                        trigger=StorageTrigger.MANUAL_SAFE,
                    )
                except StorageMaintenanceError:
                    raise
                except Exception as exc:
                    logger.warning("storage safe preview failed")
                    raise StorageMaintenanceError("安全清理预览失败") from exc
            confirmation = self._safe_confirmation(plan)
            self._safe_confirmations = {confirmation.id: (confirmation, plan)}
            return confirmation

    async def execute_safe(self, confirmation_id: str) -> StorageExecutionResult:
        return await self._execute_confirmation(
            confirmation_id,
            self._safe_confirmations,
            "安全清理确认无效",
        )

    async def clean_safe(self, trigger: StorageTrigger) -> StorageExecutionResult:
        self._ensure_available()
        if trigger is not StorageTrigger.AUTOMATIC:
            raise StorageMaintenanceError("自动清理触发类型无效")
        if not self.settings.automatic_enabled:
            raise StorageMaintenanceError("自动清理尚未启用")
        try:
            self._trusted_state()
        except StorageStateError:
            result = self._execution_result(
                uuid4().hex,
                trigger,
                StorageResultCode.LOCAL_ERROR,
            )
            return self._publish_without_state(result)
        if self._operation_lock.locked():
            return self._publish_result(
                self._execution_result(uuid4().hex, trigger, StorageResultCode.BUSY_DEFERRED)
            )
        async with self._operation_lock:
            token = self.activity.try_track_maintenance(ActivityKind.STORAGE_CLEANUP)
            if token is None:
                result = self._execution_result(
                    uuid4().hex,
                    trigger,
                    StorageResultCode.BUSY_DEFERRED,
                )
                return self._publish_result(result)
            with token:
                try:
                    inventory = await self._scan_automatic_worker(None)
                    self._save_scan(inventory)
                    self._automatic_inventory = inventory
                    self._safe_confirmations.clear()
                    plan = self.planner.automatic(
                        inventory,
                        self.utc_clock(),
                        trigger=trigger,
                    )
                    result = await self._execute_plan_worker(plan)
                except Exception:
                    logger.warning("storage automatic cleanup failed")
                    result = self._execution_result(
                        uuid4().hex,
                        trigger,
                        StorageResultCode.LOCAL_ERROR,
                    )
            return self._publish_result(result)

    def prepare_manual(
        self,
        selected_ids: Sequence[str],
    ) -> ManualCleanupConfirmation:
        self._ensure_available()
        if self._operation_lock.locked():
            raise StorageMaintenanceError("存储维护操作正在进行")
        if self._download_inventory is None:
            raise StorageMaintenanceError("请先重新扫描分片与留档")
        now = self.utc_clock()
        if now - self._download_inventory.scanned_at > timedelta(minutes=5):
            raise StorageMaintenanceError("分片与留档清单已过期，请重新扫描")
        try:
            plan = self.planner.manual_download(
                self._download_inventory,
                selected_ids,
                now,
            )
        except ValueError as exc:
            raise StorageMaintenanceError("手动清理选择无效，请重新扫描") from exc
        confirmation = ManualCleanupConfirmation(
            id=uuid4().hex,
            item_count=len(plan.entries),
            expected_bytes=plan.expected_bytes,
            expires_at=self.monotonic_clock() + _CONFIRMATION_SECONDS,
        )
        self._manual_confirmations = {confirmation.id: (confirmation, plan)}
        return confirmation

    async def execute_manual(self, confirmation_id: str) -> StorageExecutionResult:
        return await self._execute_confirmation(
            confirmation_id,
            self._manual_confirmations,
            "手动清理确认无效",
        )

    def cancel(self) -> None:
        event = self._cancel_event
        if event is not None:
            event.set()

    async def shutdown(self) -> None:
        self._shutdown = True
        self.cancel()
        worker = self._worker_task
        if worker is not None:
            await asyncio.gather(asyncio.shield(worker), return_exceptions=True)
        async with self._operation_lock:
            pass

    async def _execute_confirmation(
        self,
        confirmation_id: str,
        confirmations: dict[str, tuple[Any, StorageCleanupPlan]],
        invalid_message: str,
    ) -> StorageExecutionResult:
        self._ensure_available()
        if self._operation_lock.locked():
            raise StorageMaintenanceError("存储维护操作正在进行")
        async with self._operation_lock:
            pair = confirmations.get(confirmation_id)
            if pair is None:
                raise StorageMaintenanceError(invalid_message)
            confirmation, plan = pair
            if self.monotonic_clock() >= confirmation.expires_at:
                confirmations.pop(confirmation_id, None)
                raise StorageMaintenanceError("清理确认已过期，请重新扫描")
            token = self.activity.try_track_maintenance(ActivityKind.STORAGE_CLEANUP)
            if token is None:
                result = self._execution_result(
                    plan.id,
                    plan.trigger,
                    StorageResultCode.BUSY_DEFERRED,
                )
                return self._publish_result(result)
            confirmations.pop(confirmation_id, None)
            with token:
                result = await self._execute_plan_worker(plan)
            return self._publish_result(result)

    async def _scan_automatic_worker(
        self,
        progress: Callable[[int], None] | None,
    ) -> StorageInventory:
        try:
            snapshot = self.executor.update_protection.snapshot()
        except Exception as exc:
            raise StorageMaintenanceError("更新保护状态不可用") from exc
        now = self.utc_clock()
        return await self._run_worker(
            lambda event: self.inventory.scan_automatic(
                now,
                snapshot,
                active_paths=frozenset(),
                progress=progress,
                cancelled=event.is_set,
            )
        )

    async def _scan_downloads_worker(
        self,
        progress: Callable[[int], None] | None,
    ) -> StorageInventory:
        now = self.utc_clock()
        return await self._run_worker(
            lambda event: self.inventory.scan_download_candidates(
                now,
                active_paths=frozenset(),
                progress=progress,
                cancelled=event.is_set,
            )
        )

    async def _execute_plan_worker(
        self,
        plan: StorageCleanupPlan,
    ) -> StorageExecutionResult:
        try:
            return await self._run_worker(
                lambda event: self.executor.execute(plan, cancelled=event.is_set)
            )
        except Exception:
            logger.warning("storage cleanup executor failed")
            items = tuple(
                StorageExecutionItem(
                    entry.id,
                    entry.category,
                    StorageResultCode.LOCAL_ERROR,
                    0,
                )
                for entry in plan.entries
            )
            now = self.utc_clock()
            return StorageExecutionResult(
                plan.id,
                plan.trigger,
                now,
                now,
                StorageResultCode.LOCAL_ERROR,
                items,
            )

    async def _run_worker(self, call: Callable[[threading.Event], Any]) -> Any:
        event = threading.Event()
        worker = asyncio.create_task(asyncio.to_thread(call, event))
        self._cancel_event = event
        self._worker_task = worker
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            event.set()
            await asyncio.gather(asyncio.shield(worker), return_exceptions=True)
            raise
        finally:
            if self._worker_task is worker:
                self._worker_task = None
                self._cancel_event = None

    def _trusted_state(self) -> StorageMaintenanceState:
        if self._state_invalid:
            raise StorageStateError("存储维护记录不可用")
        if self._state is None:
            try:
                self._state = self.state_store.load()
            except StorageStateError:
                self._state_invalid = True
                raise
        return self._state

    def _save_scan(self, inventory: StorageInventory) -> None:
        try:
            state = self._trusted_state()
        except StorageStateError:
            state = StorageMaintenanceState()
        summaries = {summary.category: summary for summary in state.summaries}
        summaries.update({summary.category: summary for summary in inventory.summaries})
        updated = replace(
            state,
            last_scan_at=inventory.scanned_at,
            summaries=tuple(
                summaries[category] for category in StorageCategory if category in summaries
            ),
        )
        try:
            self.state_store.save(updated)
        except StorageStateError as exc:
            logger.warning("storage scan state save failed")
            raise StorageMaintenanceError("存储维护记录保存失败") from exc
        self._state = updated
        self._state_invalid = False

    def _record_result(
        self,
        result: StorageExecutionResult,
    ) -> StorageExecutionResult:
        try:
            state = self._trusted_state()
        except StorageStateError:
            state = StorageMaintenanceState()
        history = self._history(result)
        changes: dict[str, Any] = {
            "history": (*state.history, history)[-20:],
        }
        if result.trigger is StorageTrigger.AUTOMATIC:
            changes["last_automatic_check_at"] = result.completed_at
        if result.result_code in {
            StorageResultCode.COMPLETED,
            StorageResultCode.NOTHING_TO_CLEAN,
        }:
            changes["last_cleanup_at"] = result.completed_at
            changes["next_due_at"] = result.completed_at + timedelta(
                seconds=self.settings.check_interval_seconds
            )
        updated = replace(state, **changes)
        self._state = updated
        self._state_invalid = False
        try:
            self.state_store.save(updated)
        except StorageStateError:
            logger.warning("storage cleanup state save failed")
            return replace(result, result_code=StorageResultCode.STATE_SAVE_FAILED)
        return result

    @staticmethod
    def _history(result: StorageExecutionResult) -> StorageRunHistory:
        counts: dict[StorageCategory, Counter[str]] = {}
        for item in result.items:
            count = counts.setdefault(item.category, Counter())
            if item.code is StorageResultCode.COMPLETED:
                count["deleted"] += 1
                count["bytes"] += item.released_bytes
            elif item.code is StorageResultCode.CANCELLED:
                count["cancelled"] += 1
            elif item.code in _FAILED_ITEM_CODES:
                count["failed"] += 1
            else:
                count["skipped"] += 1
        categories = tuple(
            StorageCategoryCount(
                category=category,
                deleted_count=count["deleted"],
                skipped_count=count["skipped"],
                failed_count=count["failed"],
                cancelled_count=count["cancelled"],
                released_bytes=count["bytes"],
            )
            for category, count in sorted(counts.items(), key=lambda item: item[0].value)
        )
        return StorageRunHistory(
            occurred_at=result.completed_at,
            trigger=result.trigger,
            categories=categories,
            deleted_count=result.deleted_count,
            skipped_count=result.skipped_count,
            failed_count=result.failed_count,
            cancelled_count=result.cancelled_count,
            released_bytes=result.released_bytes,
            result_code=result.result_code,
        )

    def _safe_confirmation(
        self,
        plan: StorageCleanupPlan,
    ) -> SafeCleanupConfirmation:
        grouped: dict[StorageCategory, list[int]] = {}
        for entry in plan.entries:
            totals = grouped.setdefault(entry.category, [0, 0])
            totals[0] += 1
            totals[1] += entry.size
        categories = tuple(
            StoragePreviewCategory(category, totals[0], totals[1])
            for category, totals in sorted(grouped.items(), key=lambda item: item[0].value)
        )
        return SafeCleanupConfirmation(
            id=uuid4().hex,
            categories=categories,
            item_count=len(plan.entries),
            expected_bytes=plan.expected_bytes,
            expires_at=self.monotonic_clock() + _CONFIRMATION_SECONDS,
        )

    def _execution_result(
        self,
        plan_id: str,
        trigger: StorageTrigger,
        code: StorageResultCode,
    ) -> StorageExecutionResult:
        now = self.utc_clock()
        return StorageExecutionResult(plan_id, trigger, now, now, code, ())

    def _publish_result(
        self,
        result: StorageExecutionResult,
    ) -> StorageExecutionResult:
        recorded = self._record_result(result)
        return self._publish_without_state(recorded)

    def _publish_without_state(
        self,
        result: StorageExecutionResult,
    ) -> StorageExecutionResult:
        event = None
        if result.trigger is StorageTrigger.AUTOMATIC:
            if result.failed_count > 0:
                event = storage_cleanup_failed_event(result.plan_id, result.failed_count)
            elif result.released_bytes >= _AUTOMATIC_NOTIFICATION_BYTES:
                event = storage_cleaned_event(
                    result.plan_id,
                    result.deleted_count,
                    result.released_bytes,
                )
        if event is not None:
            try:
                self.publish(event)
            except Exception:
                logger.warning("storage cleanup notification publish failed")
        return result

    def _ensure_available(self) -> None:
        if self._shutdown:
            raise StorageMaintenanceError("存储维护服务已经关闭")
