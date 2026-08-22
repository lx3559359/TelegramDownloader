from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class ActivityKind(StrEnum):
    DOWNLOAD = "download"
    SCAN = "scan"
    SEARCH = "search"
    SUBSCRIPTION = "subscription"
    INTEGRITY = "integrity"
    DIAGNOSTICS = "diagnostics"
    UPDATE = "update"
    STORAGE_SCAN = "storage-scan"
    STORAGE_CLEANUP = "storage-cleanup"


class MaintenanceBusyError(RuntimeError):
    """Raised when a business action reaches an active maintenance window."""


_MAINTENANCE_KINDS = frozenset(
    {ActivityKind.STORAGE_SCAN, ActivityKind.STORAGE_CLEANUP}
)


@dataclass(slots=True)
class ActivityToken:
    registry: OperationActivityRegistry
    kind: ActivityKind
    released: bool = False

    def __enter__(self) -> ActivityToken:
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.release()

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.registry._release(self.kind)


class OperationActivityRegistry:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._counts: Counter[ActivityKind] = Counter()
        self._generation = 0
        self._changed = asyncio.Event()
        self._closed = False

    @property
    def active_count(self) -> int:
        return sum(self._counts.values())

    @property
    def is_idle(self) -> bool:
        return self.active_count == 0

    def active(self, kind: ActivityKind) -> int:
        return self._counts[kind]

    def track(self, kind: ActivityKind) -> ActivityToken:
        if self._closed:
            raise RuntimeError("活动登记器已经关闭")
        if kind in _MAINTENANCE_KINDS:
            raise ValueError("维护活动必须取得独占令牌")
        if any(self.active(kind) for kind in _MAINTENANCE_KINDS):
            raise MaintenanceBusyError("存储维护正在收尾，请稍后重试")
        return self._begin(kind)

    def try_track_maintenance(self, kind: ActivityKind) -> ActivityToken | None:
        if self._closed:
            return None
        if kind not in _MAINTENANCE_KINDS:
            raise ValueError("不是维护活动类型")
        if self.active_count:
            return None
        return self._begin(kind)

    def _begin(self, kind: ActivityKind) -> ActivityToken:
        self._counts[kind] += 1
        self._notify()
        return ActivityToken(self, kind)

    def _release(self, kind: ActivityKind) -> None:
        if self._counts[kind] <= 0:
            raise RuntimeError("活动令牌重复释放")
        self._counts[kind] -= 1
        if self._counts[kind] == 0:
            del self._counts[kind]
        self._notify()

    def _notify(self) -> None:
        event = self._changed
        self._changed = asyncio.Event()
        self._generation += 1
        event.set()

    def close(self) -> None:
        if self.active_count:
            raise RuntimeError("仍有未释放的活动令牌")
        self._closed = True
        self._notify()

    async def wait_for_change(self, generation: int, timeout: float) -> int:
        if generation != self._generation or self._closed:
            return self._generation
        event = self._changed
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return self._generation
        return self._generation

    async def wait_for_continuous_idle(self, seconds: float) -> bool:
        if seconds <= 0:
            raise ValueError("连续空闲时间必须大于零")
        idle_since: float | None = None
        while not self._closed:
            if self.is_idle:
                if idle_since is None:
                    idle_since = self.clock()
                remaining = seconds - (self.clock() - idle_since)
                if remaining <= 0:
                    return True
            else:
                idle_since = None
                remaining = 3600.0
            generation = self._generation
            observed_generation = await self.wait_for_change(generation, remaining)
            if observed_generation != generation:
                idle_since = None
        return False
