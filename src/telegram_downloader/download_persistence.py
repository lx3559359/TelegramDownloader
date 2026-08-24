from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection
from typing import Any, Protocol, TypeVar, cast

from telegram_downloader.domain import ItemStatus
from telegram_downloader.repository import ItemProgressUpdate

T = TypeVar("T")
BlockingRunner = Callable[[Callable[[], Any]], Awaitable[Any]]


class ProgressRepository(Protocol):
    def update_item_progress(
        self,
        item_id: str,
        downloaded_bytes: int,
        status: ItemStatus,
        error: str | None = None,
        retry_count: int | None = None,
    ) -> None: ...


class DownloadPersistence(Protocol):
    async def record_progress(self, update: ItemProgressUpdate) -> None: ...

    async def execute(
        self,
        operation: Callable[[], T],
        *,
        flush_item_ids: Collection[str] = (),
        flush_all: bool = False,
    ) -> T: ...

    async def drain(self) -> None: ...

    async def close(self) -> None: ...


async def _to_thread[T](operation: Callable[[], T]) -> T:
    return await asyncio.to_thread(operation)


class ThreadedDownloadPersistence:
    def __init__(
        self,
        repository: ProgressRepository,
        *,
        runner: BlockingRunner | None = None,
    ) -> None:
        self.repository = repository
        self._runner = runner or _to_thread
        self._operation_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False

    async def record_progress(self, update: ItemProgressUpdate) -> None:
        await self.execute(
            lambda: self.repository.update_item_progress(
                update.item_id,
                update.downloaded_bytes,
                update.status,
                update.error,
                update.retry_count,
            )
        )

    async def execute(
        self,
        operation: Callable[[], T],
        *,
        flush_item_ids: Collection[str] = (),
        flush_all: bool = False,
    ) -> T:
        del flush_item_ids, flush_all
        if self._closed:
            raise RuntimeError("下载持久化已关闭")
        async with self._operation_lock:
            return await self._run_blocking(operation)

    async def drain(self) -> None:
        async with self._operation_lock:
            pass

    async def close(self) -> None:
        async with self._close_lock:
            self._closed = True
            await self.drain()

    async def _run_blocking(self, operation: Callable[[], T]) -> T:
        task = asyncio.ensure_future(self._runner(operation))
        try:
            return cast(T, await asyncio.shield(task))
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise
