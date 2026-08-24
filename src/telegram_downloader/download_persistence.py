from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Sequence
from contextlib import suppress
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

    def update_item_progresses(
        self,
        updates: Sequence[ItemProgressUpdate],
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


async def _run_shielded[T](
    runner: BlockingRunner,
    operation: Callable[[], T],
) -> T:
    task = asyncio.ensure_future(runner(operation))
    try:
        return cast(T, await asyncio.shield(task))
    except asyncio.CancelledError:
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        if isinstance(result, BaseException):
            raise result from None
        raise


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
        return await _run_shielded(self._runner, operation)


class DownloadPersistenceCoordinator:
    def __init__(
        self,
        repository: ProgressRepository,
        *,
        flush_interval: float = 0.5,
        runner: BlockingRunner | None = None,
    ) -> None:
        if flush_interval <= 0:
            raise ValueError("进度持久化间隔必须大于零")
        self.repository = repository
        self.flush_interval = float(flush_interval)
        self._runner = runner or _to_thread
        self._operation_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._pending: dict[str, ItemProgressUpdate] = {}
        self._deadline: float | None = None
        self._worker: asyncio.Task[None] | None = None
        self._fault: BaseException | None = None
        self._fault_handler: Callable[[BaseException], None] | None = None
        self._closing = False
        self._closed = False

    def set_fault_handler(
        self,
        handler: Callable[[BaseException], None] | None,
    ) -> None:
        self._fault_handler = handler
        if handler is not None and self._fault is not None:
            self._notify_fault(handler, self._fault)

    async def record_progress(self, update: ItemProgressUpdate) -> None:
        self._raise_if_unavailable()
        loop = asyncio.get_running_loop()
        self._ensure_worker()
        if not self._pending:
            self._deadline = loop.time() + self.flush_interval
        self._pending[update.item_id] = update
        self._wake.set()

    async def execute(
        self,
        operation: Callable[[], T],
        *,
        flush_item_ids: Collection[str] = (),
        flush_all: bool = False,
    ) -> T:
        self._raise_if_unavailable()
        selected_ids = frozenset(flush_item_ids)
        async with self._operation_lock:
            self._raise_if_unavailable()
            if flush_all:
                await self._flush_locked()
            elif selected_ids:
                await self._flush_locked(selected_ids)
            return await self._run_repository(operation)

    async def drain(self) -> None:
        self._raise_if_unavailable()
        async with self._operation_lock:
            self._raise_if_unavailable()
            while self._pending:
                await self._flush_locked()
            self._raise_if_unavailable()

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                self._raise_fault()
                return
            self._closing = True
            try:
                try:
                    async with self._operation_lock:
                        while self._pending:
                            await self._flush_locked()
                finally:
                    self._wake.set()
                    if self._worker is not None:
                        await asyncio.gather(self._worker, return_exceptions=True)
            finally:
                self._closed = True
                self._closing = False
            self._raise_fault()

    def _ensure_worker(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._worker_loop())

    async def _worker_loop(self) -> None:
        while True:
            if self._fault is not None:
                return
            if self._closing and not self._pending:
                return
            if not self._pending:
                self._deadline = None
                self._wake.clear()
                if self._closing:
                    return
                await self._wake.wait()
                continue

            loop = asyncio.get_running_loop()
            if self._deadline is None:
                self._deadline = loop.time() + self.flush_interval
            remaining = self._deadline - loop.time()
            if remaining > 0:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=remaining)
                except TimeoutError:
                    pass
                else:
                    continue

            try:
                async with self._operation_lock:
                    self._raise_fault()
                    await self._flush_locked()
            except asyncio.CancelledError:
                raise
            except BaseException:
                return

    async def _flush_locked(
        self,
        item_ids: Collection[str] | None = None,
    ) -> None:
        if item_ids is None:
            selected = tuple(self._pending)
        else:
            selected_set = frozenset(item_ids)
            selected = tuple(
                item_id for item_id in self._pending if item_id in selected_set
            )
        if not selected:
            return
        updates = tuple(self._pending.pop(item_id) for item_id in selected)
        if not self._pending:
            self._deadline = None
        await self._run_repository(
            lambda: self.repository.update_item_progresses(updates)
        )

    async def _run_repository(self, operation: Callable[[], T]) -> T:
        try:
            return await _run_shielded(self._runner, operation)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self._set_fault(error)
            raise

    def _set_fault(self, error: BaseException) -> None:
        if self._fault is not None:
            return
        self._fault = error
        self._wake.set()
        handler = self._fault_handler
        if handler is not None:
            self._notify_fault(handler, error)

    @staticmethod
    def _notify_fault(
        handler: Callable[[BaseException], None],
        error: BaseException,
    ) -> None:
        with suppress(Exception):
            handler(error)

    def _raise_if_unavailable(self) -> None:
        self._raise_fault()
        if self._closing or self._closed:
            raise RuntimeError("下载持久化已关闭")

    def _raise_fault(self) -> None:
        if self._fault is not None:
            raise self._fault
