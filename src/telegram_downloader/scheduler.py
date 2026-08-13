from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from telegram_downloader.domain import ItemStatus, MediaItem, TaskStatus
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


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    base_delay: int = 2

    def __post_init__(self) -> None:
        if self.attempts < 1 or self.base_delay < 0:
            raise ValueError("重试次数必须大于零，延迟不能为负数")


class SchedulerRepository(Protocol):
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
    ) -> None: ...

    def recover_interrupted(self) -> None: ...


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
    ) -> None:
        self.repository = repository
        self.downloader = downloader
        self.concurrency = min(5, max(1, concurrency))
        self.retry = retry or RetryPolicy()
        self.sleep = sleep or asyncio.sleep
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self._pause_flags: dict[str, asyncio.Event] = {}
        self._active: dict[str, asyncio.Task[None]] = {}
        self._shutting_down = False

    def recover(self) -> None:
        self.repository.recover_interrupted()

    def pause_task(self, task_id: str) -> None:
        self._pause_flag(task_id).set()
        self.repository.update_task_status(task_id, TaskStatus.PAUSED)

    async def resume_task(self, task_id: str) -> None:
        if self._shutting_down:
            return
        self._pause_flag(task_id).clear()
        self.repository.update_task_status(task_id, TaskStatus.QUEUED)
        await self.run_task(task_id)

    async def run_task(self, task_id: str) -> None:
        if self._shutting_down:
            return
        existing = self._active.get(task_id)
        if existing is not None:
            await asyncio.shield(existing)
            return

        task = asyncio.create_task(self._execute_task(task_id))
        self._active[task_id] = task
        try:
            await asyncio.shield(task)
        finally:
            if self._active.get(task_id) is task and task.done():
                self._active.pop(task_id, None)

    async def shutdown(self) -> None:
        self._shutting_down = True
        for task_id in tuple(self._active):
            self._pause_flag(task_id).set()
        active = tuple(self._active.values())
        if not active:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*active, return_exceptions=True),
                timeout=5,
            )
        except TimeoutError:
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)

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
            self.repository.update_task_status(task_id, TaskStatus.PAUSED)
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
        else:
            self.repository.update_task_status(task_id, TaskStatus.COMPLETED)

    async def _guarded_item(
        self,
        task_id: str,
        item: MediaItem,
        pause_flag: asyncio.Event,
    ) -> ItemStatus:
        async with self._semaphore:
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
            except (DownloadPaused, InsufficientSpaceError) as error:
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
        current = next(
            (item for item in self.repository.list_items(task_id) if item.id == item_id),
            None,
        )
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
