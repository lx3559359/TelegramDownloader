from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping
from contextlib import suppress
from dataclasses import dataclass


@dataclass(slots=True)
class _PatchWaiter:
    generation: int
    target_revisions: Mapping[str, int]
    future: asyncio.Future[None]


@dataclass(slots=True)
class _FullWaiter:
    generation: int
    target_revision: int
    future: asyncio.Future[None]


class TaskRefreshCoordinator[FullT, PatchT]:
    def __init__(
        self,
        *,
        load_full: Callable[[], Awaitable[FullT]],
        load_ids: Callable[[tuple[str, ...]], Awaitable[PatchT]],
        apply_full: Callable[[FullT], None],
        apply_patch: Callable[[PatchT], None],
        progress_interval: float = 0.5,
        reconcile_interval: float = 5.0,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        if progress_interval <= 0:
            raise ValueError("进度刷新间隔必须大于零")
        if reconcile_interval <= 0:
            raise ValueError("完整校准间隔必须大于零")
        self._load_full = load_full
        self._load_ids = load_ids
        self._apply_full = apply_full
        self._apply_patch = apply_patch
        self._progress_interval = progress_interval
        self._reconcile_interval = reconcile_interval
        self._on_error = on_error
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._active = False
        self._closed = False
        self._generation = 0
        self._revision = 0
        self._dirty_revisions: dict[str, int] = {}
        self._immediate_ids: set[str] = set()
        self._applied_revisions: dict[str, int] = {}
        self._progress_deadline: float | None = None
        self._full_revision = 0
        self._pending_full_revision = 0
        self._reconcile_deadline: float | None = None
        self._patch_waiters: list[_PatchWaiter] = []
        self._full_waiters: list[_FullWaiter] = []

    async def activate(self) -> None:
        self._require_open()
        if self._active:
            return
        self._active = True
        self._ensure_worker()
        await self.reconcile_now()

    def deactivate(self) -> None:
        if self._closed or not self._active:
            return
        self._active = False
        self._advance_generation("刷新代次已替换")

    def mark_progress(self, task_ids: Collection[str]) -> None:
        self._require_open()
        ordered = tuple(dict.fromkeys(task_ids))
        if not ordered:
            return
        self._ensure_worker()
        self._mark_dirty(ordered, immediate=False)

    async def refresh_now(self, task_ids: Collection[str]) -> None:
        self._require_open()
        ordered = tuple(dict.fromkeys(task_ids))
        if not ordered:
            return
        loop = asyncio.get_running_loop()
        self._ensure_worker()
        target_revisions = self._mark_dirty(ordered, immediate=True)
        future = loop.create_future()
        self._patch_waiters.append(
            _PatchWaiter(self._generation, target_revisions, future)
        )
        self._resolve_patch_waiters()
        await future

    async def reconcile_now(self) -> None:
        self._require_open()
        loop = asyncio.get_running_loop()
        self._ensure_worker()
        revision = self._queue_full()
        future = loop.create_future()
        self._full_waiters.append(_FullWaiter(self._generation, revision, future))
        self._wake.set()
        await future

    def replace_generation(self) -> None:
        self._require_open()
        self._advance_generation("刷新代次已替换")
        if self._active:
            self._queue_full()

    async def close(self) -> None:
        if self._closed:
            worker = self._worker
            if worker is not None and worker is not asyncio.current_task():
                await worker
            return
        self._closed = True
        self._active = False
        self._generation += 1
        self._clear_pending()
        self._fail_all_waiters(RuntimeError("任务刷新协调器已关闭"))
        self._wake.set()
        worker = self._worker
        if worker is not None and worker is not asyncio.current_task():
            await worker

    def _mark_dirty(
        self,
        task_ids: tuple[str, ...],
        *,
        immediate: bool,
    ) -> dict[str, int]:
        self._revision += 1
        revision = self._revision
        for task_id in task_ids:
            self._dirty_revisions[task_id] = revision
        if immediate:
            self._immediate_ids.update(task_ids)
        elif self._progress_deadline is None:
            self._progress_deadline = (
                asyncio.get_running_loop().time() + self._progress_interval
            )
        self._wake.set()
        return {task_id: revision for task_id in task_ids}

    def _queue_full(self) -> int:
        self._full_revision += 1
        self._pending_full_revision = self._full_revision
        self._wake.set()
        return self._full_revision

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._worker = asyncio.create_task(
            self._run(),
            name="task-refresh-coordinator",
        )
        self._worker.add_done_callback(self._worker_finished)

    async def _run(self) -> None:
        while not self._closed:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self._immediate_ids and self._dirty_revisions:
                batch = self._take_dirty_batch()
                await self._run_patch(batch, self._generation)
                continue
            if self._pending_full_revision:
                revision = self._pending_full_revision
                self._pending_full_revision = 0
                await self._run_full(revision, self._generation)
                continue
            if (
                self._progress_deadline is not None
                and now >= self._progress_deadline
                and self._dirty_revisions
            ):
                batch = self._take_dirty_batch()
                await self._run_patch(batch, self._generation)
                continue
            if self._active and self._reconcile_deadline is None:
                revision = self._queue_full()
                self._pending_full_revision = 0
                await self._run_full(revision, self._generation)
                continue
            if (
                self._active
                and self._reconcile_deadline is not None
                and now >= self._reconcile_deadline
            ):
                revision = self._queue_full()
                self._pending_full_revision = 0
                await self._run_full(revision, self._generation)
                continue
            deadlines = [
                deadline
                for deadline in (self._progress_deadline, self._reconcile_deadline)
                if deadline is not None
            ]
            timeout = max(0.0, min(deadlines) - now) if deadlines else None
            self._wake.clear()
            try:
                if timeout is None:
                    await self._wake.wait()
                else:
                    await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                pass

    def _take_dirty_batch(self) -> dict[str, int]:
        batch = dict(self._dirty_revisions)
        self._dirty_revisions.clear()
        self._immediate_ids.difference_update(batch)
        self._progress_deadline = None
        return batch

    async def _run_patch(self, batch: Mapping[str, int], generation: int) -> None:
        task_ids = tuple(sorted(batch))
        try:
            patch = await self._load_ids(task_ids)
            if self._is_stale(generation):
                return
            self._apply_patch(patch)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if self._is_stale(generation):
                return
            self._restore_failed_batch(batch)
            self._fail_covered_patch_waiters(batch, generation, error)
            self._report_error(error)
            return
        if self._is_stale(generation):
            return
        for task_id, revision in batch.items():
            self._applied_revisions[task_id] = max(
                revision,
                self._applied_revisions.get(task_id, 0),
            )
        self._resolve_patch_waiters()

    async def _run_full(self, revision: int, generation: int) -> None:
        try:
            full = await self._load_full()
            if self._is_stale(generation):
                return
            self._apply_full(full)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if self._is_stale(generation):
                return
            self._fail_covered_full_waiters(revision, generation, error)
            self._report_error(error)
        else:
            if not self._is_stale(generation):
                self._resolve_full_waiters(revision, generation)
        finally:
            if self._active and not self._is_stale(generation):
                self._reconcile_deadline = (
                    asyncio.get_running_loop().time() + self._reconcile_interval
                )

    def _restore_failed_batch(self, batch: Mapping[str, int]) -> None:
        for task_id, revision in batch.items():
            self._dirty_revisions[task_id] = max(
                revision,
                self._dirty_revisions.get(task_id, 0),
            )
        if self._dirty_revisions and self._progress_deadline is None:
            self._progress_deadline = (
                asyncio.get_running_loop().time() + self._progress_interval
            )
        self._wake.set()

    def _resolve_patch_waiters(self) -> None:
        remaining: list[_PatchWaiter] = []
        for waiter in self._patch_waiters:
            if waiter.future.done():
                continue
            if waiter.generation != self._generation:
                waiter.future.set_exception(RuntimeError("刷新代次已替换"))
                continue
            covered = all(
                self._applied_revisions.get(task_id, 0) >= revision
                for task_id, revision in waiter.target_revisions.items()
            )
            if covered:
                waiter.future.set_result(None)
            else:
                remaining.append(waiter)
        self._patch_waiters = remaining

    def _fail_covered_patch_waiters(
        self,
        batch: Mapping[str, int],
        generation: int,
        error: BaseException,
    ) -> None:
        remaining: list[_PatchWaiter] = []
        for waiter in self._patch_waiters:
            covered = waiter.generation == generation and all(
                batch.get(task_id, 0) >= revision
                for task_id, revision in waiter.target_revisions.items()
            )
            if covered and not waiter.future.done():
                waiter.future.set_exception(error)
            elif not waiter.future.done():
                remaining.append(waiter)
        self._patch_waiters = remaining

    def _resolve_full_waiters(self, revision: int, generation: int) -> None:
        remaining: list[_FullWaiter] = []
        for waiter in self._full_waiters:
            if waiter.future.done():
                continue
            if waiter.generation != self._generation:
                waiter.future.set_exception(RuntimeError("刷新代次已替换"))
            elif waiter.generation == generation and waiter.target_revision <= revision:
                waiter.future.set_result(None)
            else:
                remaining.append(waiter)
        self._full_waiters = remaining

    def _fail_covered_full_waiters(
        self,
        revision: int,
        generation: int,
        error: BaseException,
    ) -> None:
        remaining: list[_FullWaiter] = []
        for waiter in self._full_waiters:
            covered = (
                waiter.generation == generation and waiter.target_revision <= revision
            )
            if covered and not waiter.future.done():
                waiter.future.set_exception(error)
            elif not waiter.future.done():
                remaining.append(waiter)
        self._full_waiters = remaining

    def _advance_generation(self, message: str) -> None:
        self._generation += 1
        self._clear_pending()
        self._fail_all_waiters(RuntimeError(message))
        self._wake.set()

    def _clear_pending(self) -> None:
        self._dirty_revisions.clear()
        self._immediate_ids.clear()
        self._applied_revisions.clear()
        self._progress_deadline = None
        self._pending_full_revision = 0
        self._reconcile_deadline = None

    def _fail_all_waiters(self, error: BaseException) -> None:
        for waiter in (*self._patch_waiters, *self._full_waiters):
            if not waiter.future.done():
                waiter.future.set_exception(error)
        self._patch_waiters.clear()
        self._full_waiters.clear()

    def _is_stale(self, generation: int) -> bool:
        return self._closed or generation != self._generation

    def _report_error(self, error: BaseException) -> None:
        if self._on_error is None:
            return
        with suppress(BaseException):
            self._on_error(error)

    def _worker_finished(self, task: asyncio.Task[None]) -> None:
        if self._worker is task:
            self._worker = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and not self._closed:
            self._report_error(error)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("任务刷新协调器已关闭")
