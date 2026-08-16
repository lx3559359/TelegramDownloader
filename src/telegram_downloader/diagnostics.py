from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from telegram_downloader import __version__

_STABLE_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_METRIC_KEY = re.compile(r"^[a-z][A-Za-z0-9]*$")


class DiagnosticStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


type MetricValue = bool | int | float | str


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    id: str
    title: str
    status: DiagnosticStatus
    code: str
    summary: str
    duration_ms: int
    metrics: Mapping[str, MetricValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or _STABLE_ID.fullmatch(self.id) is None:
            raise ValueError("诊断结果标识无效")
        if not isinstance(self.code, str) or _STABLE_ID.fullmatch(self.code) is None:
            raise ValueError("诊断结果代码标识无效")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("诊断结果标题无效")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("诊断结果说明无效")
        if (
            not isinstance(self.duration_ms, int)
            or isinstance(self.duration_ms, bool)
            or self.duration_ms < 0
        ):
            raise ValueError("诊断耗时必须是非负整数")
        if not isinstance(self.status, DiagnosticStatus):
            raise ValueError("诊断状态无效")
        if not isinstance(self.metrics, Mapping):
            raise ValueError("诊断指标必须是映射")
        frozen: dict[str, MetricValue] = {}
        for key, value in self.metrics.items():
            if not isinstance(key, str) or _METRIC_KEY.fullmatch(key) is None:
                raise ValueError("诊断指标名称无效")
            if isinstance(value, float):
                valid_value = math.isfinite(value)
            else:
                valid_value = isinstance(value, (bool, int, str))
            if not valid_value:
                raise ValueError("诊断指标只允许安全标量")
            frozen[key] = value
        object.__setattr__(self, "metrics", MappingProxyType(frozen))


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    schema_version: int
    app_version: str
    started_at: datetime
    finished_at: datetime
    status: DiagnosticStatus
    results: tuple[DiagnosticResult, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("诊断报告版本无效")
        if not isinstance(self.app_version, str) or not self.app_version.strip():
            raise ValueError("应用版本无效")
        if not _is_utc(self.started_at) or not _is_utc(self.finished_at):
            raise ValueError("诊断报告时间必须使用 UTC")
        if self.finished_at < self.started_at:
            raise ValueError("诊断报告结束时间不能早于开始时间")
        if not self.results:
            raise ValueError("诊断报告必须包含检查结果")
        identifiers = [item.id for item in self.results]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("诊断报告包含重复检查标识")
        if any(
            item.status in {DiagnosticStatus.PENDING, DiagnosticStatus.RUNNING}
            for item in self.results
        ):
            raise ValueError("诊断报告包含未完成检查结果")
        if self.status in {DiagnosticStatus.PENDING, DiagnosticStatus.RUNNING}:
            raise ValueError("诊断报告总状态未完成")

    @classmethod
    def build(
        cls,
        app_version: str,
        started_at: datetime,
        finished_at: datetime,
        results: Sequence[DiagnosticResult],
        *,
        cancelled: bool = False,
    ) -> DiagnosticReport:
        frozen = tuple(results)
        return cls(
            1,
            app_version,
            started_at,
            finished_at,
            reduce_status(frozen, cancelled=cancelled),
            frozen,
        )


class DiagnosticProbe(Protocol):
    id: str
    title: str

    async def run(self, cancel_event: asyncio.Event) -> DiagnosticResult: ...


@dataclass(frozen=True, slots=True)
class DiagnosticProgress:
    completed: int
    total: int
    current_id: str | None
    current_title: str | None
    status: DiagnosticStatus

    def __post_init__(self) -> None:
        if self.total <= 0 or not 0 <= self.completed <= self.total:
            raise ValueError("诊断进度无效")
        if (self.current_id is None) != (self.current_title is None):
            raise ValueError("诊断进度当前项不完整")


class DiagnosticsService:
    def __init__(
        self,
        probes: Sequence[DiagnosticProbe],
        *,
        app_version: str = __version__,
        utc_now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.probes = tuple(probes)
        if not self.probes:
            raise ValueError("诊断服务必须包含检查项")
        identifiers = [probe.id for probe in self.probes]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("诊断服务包含重复检查标识")
        if any(_STABLE_ID.fullmatch(value) is None for value in identifiers):
            raise ValueError("诊断服务检查标识无效")
        self.app_version = app_version
        self.utc_now = utc_now or (lambda: datetime.now(UTC))
        self.monotonic = monotonic
        self._lock = asyncio.Lock()
        self._active_task: asyncio.Task[DiagnosticReport] | None = None
        self._active_child: asyncio.Task[DiagnosticResult] | None = None
        self._cancel_event: asyncio.Event | None = None

    async def run(
        self,
        on_progress: Callable[[DiagnosticProgress], None] | None = None,
    ) -> DiagnosticReport:
        async with self._lock:
            task = self._active_task
            if task is None or task.done():
                cancel_event = asyncio.Event()
                task = asyncio.create_task(self._run(cancel_event, on_progress))
                self._active_task = task
                self._cancel_event = cancel_event
        try:
            return await asyncio.shield(task)
        finally:
            async with self._lock:
                if self._active_task is task and task.done():
                    self._active_task = None
                    self._active_child = None
                    self._cancel_event = None

    async def cancel(self) -> None:
        async with self._lock:
            task = self._active_task
            cancel_event = self._cancel_event
            child = self._active_child
            if task is None or task.done() or cancel_event is None:
                return
            cancel_event.set()
            if child is not None and not child.done():
                child.cancel()
        await asyncio.shield(task)

    async def _run(
        self,
        cancel_event: asyncio.Event,
        on_progress: Callable[[DiagnosticProgress], None] | None,
    ) -> DiagnosticReport:
        started_at = self.utc_now()
        total = len(self.probes)
        results: list[DiagnosticResult] = []
        for index, probe in enumerate(self.probes):
            if cancel_event.is_set():
                item = _cancelled_result(probe)
            else:
                _notify_progress(
                    on_progress,
                    DiagnosticProgress(
                        index,
                        total,
                        probe.id,
                        probe.title,
                        DiagnosticStatus.RUNNING,
                    ),
                )
                item = await self._execute_probe(probe, cancel_event)
            results.append(item)
            _notify_progress(
                on_progress,
                DiagnosticProgress(
                    index + 1,
                    total,
                    probe.id,
                    probe.title,
                    item.status,
                ),
            )
        report = DiagnosticReport.build(
            self.app_version,
            started_at,
            self.utc_now(),
            results,
            cancelled=cancel_event.is_set(),
        )
        _notify_progress(
            on_progress,
            DiagnosticProgress(total, total, None, None, report.status),
        )
        return report

    async def _execute_probe(
        self,
        probe: DiagnosticProbe,
        cancel_event: asyncio.Event,
    ) -> DiagnosticResult:
        started = self.monotonic()
        child = asyncio.create_task(probe.run(cancel_event))
        self._active_child = child
        try:
            item = await child
            if item.id != probe.id or item.title != probe.title:
                raise ValueError("诊断探针返回标识不一致")
        except asyncio.CancelledError:
            if not cancel_event.is_set():
                raise
            item = _cancelled_result(probe)
        except Exception:
            item = DiagnosticResult(
                probe.id,
                probe.title,
                DiagnosticStatus.FAILED,
                "probe-failed",
                "检查执行失败",
                0,
            )
        finally:
            if self._active_child is child:
                self._active_child = None
        duration_ms = max(0, int(round((self.monotonic() - started) * 1000)))
        return replace(item, duration_ms=duration_ms)


def reduce_status(
    results: Sequence[DiagnosticResult],
    *,
    cancelled: bool = False,
) -> DiagnosticStatus:
    if cancelled:
        return DiagnosticStatus.CANCELLED
    statuses = {item.status for item in results}
    if DiagnosticStatus.FAILED in statuses:
        return DiagnosticStatus.FAILED
    if DiagnosticStatus.WARNING in statuses:
        return DiagnosticStatus.WARNING
    if DiagnosticStatus.PASSED in statuses:
        return DiagnosticStatus.PASSED
    return DiagnosticStatus.WARNING


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _cancelled_result(probe: DiagnosticProbe) -> DiagnosticResult:
    return DiagnosticResult(
        probe.id,
        probe.title,
        DiagnosticStatus.CANCELLED,
        "check-cancelled",
        "检查已取消",
        0,
    )


def _notify_progress(
    callback: Callable[[DiagnosticProgress], None] | None,
    progress: DiagnosticProgress,
) -> None:
    if callback is None:
        return
    try:
        callback(progress)
    except Exception:
        return
