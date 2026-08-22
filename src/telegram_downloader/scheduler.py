from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from telegram_downloader.domain import (
    ItemStatus,
    MediaItem,
    PauseReason,
    TaskRecord,
    TaskStatus,
)
from telegram_downloader.downloader import (
    DownloadPaused,
    InsufficientSpaceError,
    MediaDownloader,
)
from telegram_downloader.gateway import (
    FloodWaitError,
    MediaReferenceExpired,
    TransientNetworkError,
)
from telegram_downloader.notifications import (
    ApplicationEvent,
    EventKind,
    disk_full_event,
    download_event,
)
from telegram_downloader.resource_control import (
    AdjustableConcurrencyLimiter,
    AsyncBandwidthLimiter,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    base_delay: int = 2

    def __post_init__(self) -> None:
        if self.attempts < 1 or self.base_delay < 0:
            raise ValueError("重试次数必须大于零，延迟不能为负数")


@dataclass(frozen=True, slots=True)
class SchedulerSnapshot:
    active_task_id: str | None
    queued_task_ids: tuple[str, ...]
    concurrency: int
    speed_limit_kib: int

    @property
    def queued_count(self) -> int:
        return len(self.queued_task_ids)


@dataclass(slots=True)
class _QueuedOperation:
    task_id: str
    item_ids: tuple[str, ...] | None
    dispatch_key: tuple[Any, ...]
    sequence: int
    completion: asyncio.Future[None]
    runner: asyncio.Task[None] | None = None


class SchedulerRepository(Protocol):
    def get_tasks(self, task_ids: list[str]) -> list[TaskRecord]: ...

    def get_item(self, item_id: str) -> MediaItem: ...

    def list_items(
        self,
        task_id: str,
        statuses: set[ItemStatus] | None = None,
    ) -> list[MediaItem]: ...

    def update_item_progress(
        self,
        item_id: str,
        downloaded_bytes: int,
        status: ItemStatus,
        error: str | None = None,
        retry_count: int | None = None,
    ) -> None: ...

    def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        error: str | None = None,
        *,
        pause_reason: PauseReason | None = None,
    ) -> None: ...

    def update_task_statuses(
        self,
        task_ids: list[str],
        status: TaskStatus,
        *,
        allowed: set[TaskStatus],
        error: str | None = None,
        pause_reason: PauseReason | None = None,
    ) -> set[str]: ...

    def list_paused_by_reason(self, reason: PauseReason) -> list[TaskRecord]: ...

    def recover_interrupted(self) -> None: ...

    def recompute_task_status(self, task_id: str) -> TaskStatus: ...


class ItemDownloader(Protocol):
    async def download(
        self,
        item: MediaItem,
        should_pause: Callable[[], bool],
    ) -> object: ...


class DownloadScheduler:
    def __init__(
        self,
        repository: SchedulerRepository,
        downloader: MediaDownloader | ItemDownloader,
        concurrency: int = 3,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        shutdown_grace_seconds: float = 30.0,
        bandwidth: AsyncBandwidthLimiter | None = None,
        publish: Callable[[ApplicationEvent], None] | None = None,
    ) -> None:
        if shutdown_grace_seconds <= 0:
            raise ValueError("关闭等待时间必须大于零")
        self.repository = repository
        self.downloader = downloader
        self.retry = retry or RetryPolicy()
        self.sleep = sleep or asyncio.sleep
        self.shutdown_grace_seconds = float(shutdown_grace_seconds)
        self._permits = AdjustableConcurrencyLimiter(min(5, max(1, concurrency)))
        downloader_bandwidth = getattr(downloader, "bandwidth", None)
        self._bandwidth = bandwidth or downloader_bandwidth or AsyncBandwidthLimiter()
        self.publish = publish or (lambda _event: None)
        self._pause_flags: dict[str, asyncio.Event] = {}
        self._pause_reasons: dict[str, PauseReason] = {}
        self._disk_full_tasks: set[str] = set()
        self._disk_notified: set[str] = set()
        self._terminal_notified: set[str] = set()
        self._pending: list[_QueuedOperation] = []
        self._operations: dict[str, _QueuedOperation] = {}
        self._active_operation: _QueuedOperation | None = None
        self._sequence = 0
        self._shutting_down = False
        self._admission_open = True

    @property
    def concurrency(self) -> int:
        return self._permits.limit

    @property
    def active_task_id(self) -> str | None:
        operation = self._active_operation
        return operation.task_id if operation is not None else None

    def set_admission_open(self, opened: bool) -> None:
        self._admission_open = bool(opened)
        if self._admission_open:
            self._admit_next()

    async def set_schedule_open(self, opened: bool) -> set[str]:
        self.set_admission_open(opened)
        if not opened:
            active = self.active_task_id
            if active is not None:
                self._pause_reasons.setdefault(active, PauseReason.SCHEDULE)
                self._pause_flag(active).set()
            return set()
        tasks = self.repository.list_paused_by_reason(PauseReason.SCHEDULE)
        return await self.resume_tasks([task.id for task in tasks])

    def recover(self) -> None:
        self.repository.recover_interrupted()

    def pause_task(self, task_id: str) -> None:
        self.pause_tasks([task_id])

    def pause_tasks(self, task_ids: list[str]) -> set[str]:
        ordered = tuple(dict.fromkeys(task_ids))
        for task_id in ordered:
            self._pause_flag(task_id).set()
            operation = self._operations.get(task_id)
            if operation is self._active_operation:
                self._pause_reasons[task_id] = PauseReason.USER
            if operation is None or operation is self._active_operation:
                continue
            if operation in self._pending:
                self._pending.remove(operation)
            self._operations.pop(task_id, None)
            if not operation.completion.done():
                operation.completion.set_result(None)
        return self.repository.update_task_statuses(
            list(ordered),
            TaskStatus.PAUSED,
            allowed={
                TaskStatus.QUEUED,
                TaskStatus.DOWNLOADING,
                TaskStatus.WAITING_RETRY,
            },
            pause_reason=PauseReason.USER,
        )

    async def resume_task(self, task_id: str) -> None:
        await self.resume_tasks([task_id])

    async def resume_tasks(self, task_ids: list[str]) -> set[str]:
        if self._shutting_down:
            return set()
        ordered = tuple(dict.fromkeys(task_ids))
        accepted = self.repository.update_task_statuses(
            list(ordered),
            TaskStatus.QUEUED,
            allowed={
                TaskStatus.PAUSED,
                TaskStatus.PARTIAL_FAILURE,
                TaskStatus.WAITING_RETRY,
            },
        )
        scheduled = [task_id for task_id in ordered if task_id in accepted]
        for task_id in scheduled:
            self._pause_reasons.pop(task_id, None)
            self._pause_flag(task_id).clear()
        if scheduled:
            await asyncio.gather(*(self.run_task(task_id) for task_id in scheduled))
        return accepted

    async def run_task(self, task_id: str) -> None:
        if self._shutting_down:
            return
        existing = self._operations.get(task_id)
        if existing is not None:
            await asyncio.shield(existing.completion)
            return
        await self._queue_operation(task_id, None)

    async def run_items(self, task_id: str, item_ids: list[str]) -> None:
        ordered_ids = tuple(dict.fromkeys(item_ids))
        if not ordered_ids:
            raise ValueError("请至少选择一个媒体项")
        if self._shutting_down:
            return
        existing = self._operations.get(task_id)
        if existing is not None:
            await asyncio.shield(existing.completion)
            if self._shutting_down:
                return
            remaining_ids: list[str] = []
            for item_id in ordered_ids:
                item = self.repository.get_item(item_id)
                if item.task_id != task_id:
                    raise ValueError("所选媒体项不属于当前任务")
                if item.status is ItemStatus.QUEUED:
                    remaining_ids.append(item_id)
                elif item.status is not ItemStatus.COMPLETED:
                    raise ValueError("所选媒体项尚未处于等待下载状态")
            if not remaining_ids:
                self.repository.recompute_task_status(task_id)
                return
            ordered_ids = tuple(remaining_ids)
        await self._queue_operation(task_id, ordered_ids)

    def snapshot(self) -> SchedulerSnapshot:
        return SchedulerSnapshot(
            self.active_task_id,
            tuple(operation.task_id for operation in self._sorted_pending()),
            self._permits.limit,
            self._bandwidth.speed_limit_kib,
        )

    def queue_positions(self) -> dict[str, int]:
        return {
            operation.task_id: position
            for position, operation in enumerate(self._sorted_pending(), start=1)
        }

    def is_active(self, task_id: str) -> bool:
        return self.active_task_id == task_id

    def prioritize_task(self, task_id: str) -> bool:
        operation = self._operations.get(task_id)
        if (
            operation is None
            or operation is self._active_operation
            or operation not in self._pending
        ):
            return False
        operation.dispatch_key = self._dispatch_key(task_id, operation.sequence)
        self._pending.sort(key=self._operation_sort_key)
        return True

    def configure_resources(self, concurrency: int, speed_limit_kib: int) -> None:
        self._permits.set_limit(concurrency)
        self._bandwidth.set_speed_limit_kib(speed_limit_kib)

    async def shutdown(self) -> None:
        self._shutting_down = True
        pending = tuple(self._pending)
        self._pending.clear()
        for operation in pending:
            self._operations.pop(operation.task_id, None)
            if not operation.completion.done():
                operation.completion.set_result(None)

        active = self._active_operation
        if active is None or active.runner is None:
            return
        self._pause_flag(active.task_id).set()
        try:
            await asyncio.wait_for(
                asyncio.gather(active.runner, return_exceptions=True),
                timeout=self.shutdown_grace_seconds,
            )
        except TimeoutError:
            active.runner.cancel()
            await asyncio.gather(active.runner, return_exceptions=True)
        await asyncio.sleep(0)

    async def _queue_operation(
        self,
        task_id: str,
        item_ids: tuple[str, ...] | None,
    ) -> None:
        loop = asyncio.get_running_loop()
        self._sequence += 1
        operation = _QueuedOperation(
            task_id,
            item_ids,
            self._dispatch_key(task_id, self._sequence),
            self._sequence,
            loop.create_future(),
        )
        self._operations[task_id] = operation
        self._pending.append(operation)
        self._pending.sort(key=self._operation_sort_key)
        self._admit_next()
        await asyncio.shield(operation.completion)

    def _admit_next(self) -> None:
        if (
            self._shutting_down
            or not self._admission_open
            or self._active_operation is not None
            or not self._pending
        ):
            return
        self._pending.sort(key=self._operation_sort_key)
        operation = self._pending.pop(0)
        self._active_operation = operation
        operation.runner = asyncio.create_task(self._perform(operation))
        operation.runner.add_done_callback(
            lambda runner, selected=operation: self._finish_operation(selected, runner)
        )

    async def _perform(self, operation: _QueuedOperation) -> None:
        clear_priority = getattr(self.repository, "clear_task_priority", None)
        if clear_priority is not None:
            clear_priority(operation.task_id)
        try:
            if operation.item_ids is None:
                await self._execute_task(operation.task_id)
            else:
                await self._execute_items(operation.task_id, operation.item_ids)
        finally:
            self._pause_reasons.pop(operation.task_id, None)

    def _finish_operation(
        self,
        operation: _QueuedOperation,
        runner: asyncio.Task[None],
    ) -> None:
        if self._active_operation is operation:
            self._active_operation = None
        if self._operations.get(operation.task_id) is operation:
            self._operations.pop(operation.task_id, None)

        error: BaseException | None = None
        try:
            runner.result()
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            error = exc

        if not operation.completion.done():
            if error is None:
                operation.completion.set_result(None)
            else:
                operation.completion.set_exception(error)
        self._admit_next()

    def _sorted_pending(self) -> list[_QueuedOperation]:
        return sorted(self._pending, key=self._operation_sort_key)

    @staticmethod
    def _operation_sort_key(operation: _QueuedOperation) -> tuple[Any, ...]:
        return (*operation.dispatch_key, operation.sequence)

    def _dispatch_key(self, task_id: str, sequence: int) -> tuple[Any, ...]:
        dispatch_key = getattr(self.repository, "task_dispatch_key", None)
        if dispatch_key is None:
            return (0, sequence, task_id)
        return tuple(dispatch_key(task_id))

    async def _execute_task(self, task_id: str) -> None:
        pause_flag = self._pause_flag(task_id)
        self.repository.update_task_status(task_id, TaskStatus.DOWNLOADING)
        states = {
            ItemStatus.QUEUED,
            ItemStatus.PAUSED,
            ItemStatus.WAITING_RETRY,
            ItemStatus.FAILED,
        }
        items = self.repository.list_items(task_id, states)
        results = await asyncio.gather(
            *(self._guarded_item(task_id, item, pause_flag) for item in items)
        )
        if any(status is ItemStatus.PAUSED for status in results):
            reason = self._pause_reasons.pop(task_id, PauseReason.USER)
            self.repository.update_task_status(
                task_id,
                TaskStatus.PAUSED,
                pause_reason=reason,
            )
            self._publish_disk_full(task_id)
        elif any(status is ItemStatus.FAILED for status in results):
            failed = self.repository.list_items(task_id, {ItemStatus.FAILED})
            reason = next(
                (item.last_error for item in failed if item.last_error),
                "部分文件下载失败",
            )
            self.repository.update_task_status(
                task_id,
                TaskStatus.PARTIAL_FAILURE,
                reason,
            )
            self._publish_terminal(EventKind.DOWNLOAD_FAILED, task_id)
        else:
            self.repository.update_task_status(task_id, TaskStatus.COMPLETED)
            self._publish_terminal(EventKind.DOWNLOAD_COMPLETED, task_id)

    async def _execute_items(
        self,
        task_id: str,
        item_ids: tuple[str, ...],
    ) -> None:
        items = [self.repository.get_item(item_id) for item_id in item_ids]
        if any(item.task_id != task_id for item in items):
            raise ValueError("所选媒体项不属于当前任务")
        if any(item.status is not ItemStatus.QUEUED for item in items):
            raise ValueError("所选媒体项尚未处于等待下载状态")

        pause_flag = self._pause_flag(task_id)
        pause_flag.clear()
        self.repository.update_task_status(task_id, TaskStatus.DOWNLOADING)
        results = await asyncio.gather(
            *(self._guarded_item(task_id, item, pause_flag) for item in items)
        )
        if any(status is ItemStatus.PAUSED for status in results):
            reason = self._pause_reasons.pop(task_id, PauseReason.USER)
            self.repository.update_task_status(
                task_id,
                TaskStatus.PAUSED,
                pause_reason=reason,
            )
            self._publish_disk_full(task_id)
        else:
            terminal = self.repository.recompute_task_status(task_id)
            if terminal is TaskStatus.COMPLETED:
                self._publish_terminal(EventKind.DOWNLOAD_COMPLETED, task_id)
            elif terminal is TaskStatus.PARTIAL_FAILURE:
                self._publish_terminal(EventKind.DOWNLOAD_FAILED, task_id)

    async def _guarded_item(
        self,
        task_id: str,
        item: MediaItem,
        pause_flag: asyncio.Event,
    ) -> ItemStatus:
        async with self._permits:
            return await self._run_item(task_id, item, pause_flag)

    async def _run_item(
        self,
        task_id: str,
        item: MediaItem,
        pause_flag: asyncio.Event,
    ) -> ItemStatus:
        transient_attempts = 0
        initial_retries = getattr(item, "retry_count", 0)
        while True:
            if pause_flag.is_set() or self._shutting_down:
                self._set_item_state(task_id, item.id, ItemStatus.PAUSED)
                return ItemStatus.PAUSED
            try:
                await self.downloader.download(
                    item,
                    should_pause=lambda: pause_flag.is_set() or self._shutting_down,
                )
                self._set_item_state(task_id, item.id, ItemStatus.COMPLETED)
                return ItemStatus.COMPLETED
            except FloodWaitError as error:
                self._set_item_state(
                    task_id,
                    item.id,
                    ItemStatus.WAITING_RETRY,
                    error=str(error),
                )
                self.repository.update_task_status(
                    task_id,
                    TaskStatus.WAITING_RETRY,
                    str(error),
                )
                await self.sleep(error.seconds)
                if not pause_flag.is_set() and not self._shutting_down:
                    self.repository.update_task_status(task_id, TaskStatus.DOWNLOADING)
            except (TransientNetworkError, MediaReferenceExpired) as error:
                transient_attempts += 1
                retry_count = initial_retries + transient_attempts
                if transient_attempts >= self.retry.attempts:
                    self._set_item_state(
                        task_id,
                        item.id,
                        ItemStatus.FAILED,
                        retry_count=retry_count,
                        error=str(error),
                    )
                    return ItemStatus.FAILED
                self._set_item_state(
                    task_id,
                    item.id,
                    ItemStatus.WAITING_RETRY,
                    retry_count=retry_count,
                    error=str(error),
                )
                self.repository.update_task_status(
                    task_id,
                    TaskStatus.WAITING_RETRY,
                    str(error),
                )
                delay = self.retry.base_delay * 2 ** (transient_attempts - 1)
                await self.sleep(delay)
                if not pause_flag.is_set() and not self._shutting_down:
                    self.repository.update_task_status(task_id, TaskStatus.DOWNLOADING)
            except InsufficientSpaceError as error:
                self._pause_reasons[task_id] = PauseReason.USER
                self._disk_full_tasks.add(task_id)
                self._set_item_state(
                    task_id,
                    item.id,
                    ItemStatus.PAUSED,
                    error=str(error),
                )
                return ItemStatus.PAUSED
            except DownloadPaused as error:
                self._set_item_state(
                    task_id,
                    item.id,
                    ItemStatus.PAUSED,
                    error=str(error),
                )
                return ItemStatus.PAUSED
            except asyncio.CancelledError:
                self._set_item_state(task_id, item.id, ItemStatus.PAUSED)
                raise
            except Exception as error:
                self._set_item_state(
                    task_id,
                    item.id,
                    ItemStatus.FAILED,
                    error=type(error).__name__,
                )
                return ItemStatus.FAILED

    def _set_item_state(
        self,
        task_id: str,
        item_id: str,
        status: ItemStatus,
        *,
        error: str | None = None,
        retry_count: int | None = None,
    ) -> None:
        current = self.repository.get_item(item_id)
        downloaded = getattr(current, "downloaded_bytes", 0)
        self.repository.update_item_progress(
            item_id,
            downloaded,
            status,
            error=error,
            retry_count=retry_count,
        )

    def _pause_flag(self, task_id: str) -> asyncio.Event:
        return self._pause_flags.setdefault(task_id, asyncio.Event())

    def _publish_terminal(self, kind: EventKind, task_id: str) -> None:
        if task_id in self._terminal_notified:
            return
        self._terminal_notified.add(task_id)
        self._publish(download_event(kind, task_id))

    def _publish_disk_full(self, task_id: str) -> None:
        if task_id not in self._disk_full_tasks or task_id in self._disk_notified:
            return
        self._disk_notified.add(task_id)
        self._disk_full_tasks.discard(task_id)
        self._publish(disk_full_event(task_id))

    def _publish(self, event: ApplicationEvent) -> None:
        try:
            self.publish(event)
        except Exception:
            _LOGGER.error("notification event callback failed")
