from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from typing import Protocol

from telegram_downloader.gateway import TransientNetworkError


class Connectable(Protocol):
    async def connect(self) -> None: ...


AttemptCallback = Callable[[tuple[int, int]], None]
Sleeper = Callable[[float], Awaitable[None]]


class ConnectionRecovery:
    def __init__(
        self,
        *,
        delays: Sequence[float] = (0.0, 1.0, 3.0),
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        if not delays or delays[0] != 0 or any(value < 0 for value in delays):
            raise ValueError("重连延迟必须以零开始且不能为负数")
        self.delays = tuple(float(value) for value in delays)
        self.sleeper = sleeper
        self._active: asyncio.Task[None] | None = None

    async def ensure_connected(
        self,
        gateway: Connectable,
        on_attempt: AttemptCallback | None = None,
    ) -> None:
        task = self._active
        if task is None or task.done():
            task = asyncio.create_task(self._run(gateway, on_attempt))
            self._active = task
        try:
            await asyncio.shield(task)
        finally:
            if self._active is task and task.done():
                self._active = None

    async def cancel(self) -> None:
        task = self._active
        self._active = None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(
        self,
        gateway: Connectable,
        on_attempt: AttemptCallback | None,
    ) -> None:
        total = len(self.delays)
        for index, delay in enumerate(self.delays, start=1):
            if on_attempt is not None:
                on_attempt((index, total))
            if delay:
                await self.sleeper(delay)
            try:
                await gateway.connect()
                return
            except TransientNetworkError:
                if index == total:
                    raise
