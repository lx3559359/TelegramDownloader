from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True, slots=True)
class DialogSyncProgress:
    discovered: int


@dataclass(frozen=True, slots=True)
class SearchProgress:
    inspected: int
    matched: int
    phase: str


class SearchProgressReporter:
    def __init__(
        self,
        callback: Callable[[SearchProgress], None],
        *,
        every: int = 10,
        min_interval: float = 0.2,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if every <= 0:
            raise ValueError("进度更新条数必须大于零")
        if min_interval < 0:
            raise ValueError("进度更新间隔不能为负数")
        self._callback = callback
        self._every = every
        self._min_interval = min_interval
        self._clock = clock
        self._inspected = 0
        self._matched = 0
        self._last_emitted_inspected = 0
        self._last_emitted_at = clock()

    def record(self, *, matched: bool) -> None:
        self._inspected += 1
        if matched:
            self._matched += 1
        now = self._clock()
        if (
            self._inspected - self._last_emitted_inspected >= self._every
            and now - self._last_emitted_at >= self._min_interval
        ):
            self._emit("正在扫描", now)

    def finish(self, phase: str) -> None:
        self._emit(phase, self._clock())

    def _emit(self, phase: str, emitted_at: float) -> None:
        self._callback(SearchProgress(self._inspected, self._matched, phase))
        self._last_emitted_inspected = self._inspected
        self._last_emitted_at = emitted_at
