from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


def validate_speed_limit_kib(value: int) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= 1_048_576
    ):
        raise ValueError("总下载限速必须是 0 到 1048576 KiB/s 之间的整数")


class AsyncBandwidthLimiter:
    def __init__(
        self,
        speed_limit_kib: int = 0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._sleeper = sleeper
        self._lock = asyncio.Lock()
        self._speed_limit_kib = 0
        self._next_available = clock()
        self._configuration_generation = 0
        self._sleeping_tasks: set[asyncio.Task[Any]] = set()
        self._configuration_cancels: dict[asyncio.Task[Any], int] = {}
        self.set_speed_limit_kib(speed_limit_kib)

    @property
    def speed_limit_kib(self) -> int:
        return self._speed_limit_kib

    def set_speed_limit_kib(self, value: int) -> None:
        validate_speed_limit_kib(value)
        self._speed_limit_kib = value
        self._reset_reservations()

    def _reset_reservations(
        self,
        *,
        exclude: asyncio.Task[Any] | None = None,
    ) -> None:
        self._next_available = self._clock()
        self._configuration_generation += 1
        for task in tuple(self._sleeping_tasks):
            if task is exclude:
                continue
            if task.cancel():
                self._configuration_cancels[task] = (
                    self._configuration_cancels.get(task, 0) + 1
                )

    async def acquire(self, byte_count: int) -> None:
        if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
            raise ValueError("下载字节数必须是非负整数")
        while True:
            if byte_count == 0 or self._speed_limit_kib == 0:
                return
            async with self._lock:
                speed_limit_kib = self._speed_limit_kib
                if speed_limit_kib == 0:
                    return
                generation = self._configuration_generation
                now = self._clock()
                finish = (
                    max(now, self._next_available)
                    + byte_count / (speed_limit_kib * 1024)
                )
                delay = max(0.0, finish - now)
                self._next_available = finish
            if not delay:
                return
            current_task = asyncio.current_task()
            if current_task is None:
                await self._sleeper(delay)
                return
            self._sleeping_tasks.add(current_task)
            try:
                await self._sleeper(delay)
                return
            except asyncio.CancelledError:
                configuration_cancels = self._configuration_cancels.pop(
                    current_task,
                    0,
                )
                for _ in range(configuration_cancels):
                    current_task.uncancel()
                if (
                    generation != self._configuration_generation
                    and current_task.cancelling() == 0
                ):
                    continue
                self._reset_reservations(exclude=current_task)
                raise
            finally:
                self._sleeping_tasks.discard(current_task)
                self._configuration_cancels.pop(current_task, None)


@dataclass(slots=True)
class _ConcurrencyWaiter:
    key: object | None
    future: asyncio.Future[None]


class AdjustableConcurrencyLimiter:
    def __init__(self, limit: int) -> None:
        self._validate_limit(limit)
        self._limit = limit
        self._active = 0
        self._waiters: deque[_ConcurrencyWaiter] = deque()
        self._last_granted_key: object | None = None

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return sum(not waiter.future.done() for waiter in self._waiters)

    async def acquire(self, key: object | None = None) -> None:
        if self._active < self._limit and not self._waiters:
            self._active += 1
            self._last_granted_key = key
            return

        waiter = _ConcurrencyWaiter(
            key,
            asyncio.get_running_loop().create_future(),
        )
        self._waiters.append(waiter)
        self._wake_waiters()
        try:
            await asyncio.shield(waiter.future)
        except BaseException:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            elif waiter.future.done() and not waiter.future.cancelled():
                self._active -= 1
            self._wake_waiters()
            raise

    def release(self) -> None:
        if self._active <= 0:
            raise RuntimeError("并发许可释放次数超过获取次数")
        self._active -= 1
        self._wake_waiters()

    def set_limit(self, value: int) -> None:
        self._validate_limit(value)
        self._limit = value
        self._wake_waiters()

    async def __aenter__(self) -> AdjustableConcurrencyLimiter:
        await self.acquire()
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.release()

    def _wake_waiters(self) -> None:
        while self._active < self._limit and self._waiters:
            waiter = self._pop_next_waiter()
            if waiter.future.done():
                continue
            self._active += 1
            self._last_granted_key = waiter.key
            waiter.future.set_result(None)

    def _pop_next_waiter(self) -> _ConcurrencyWaiter:
        if self._last_granted_key is not None:
            for index, waiter in enumerate(self._waiters):
                if waiter.key != self._last_granted_key:
                    del self._waiters[index]
                    return waiter
        return self._waiters.popleft()

    @staticmethod
    def _validate_limit(value: int) -> None:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= 5
        ):
            raise ValueError("并发数必须在 1 到 5 之间")
