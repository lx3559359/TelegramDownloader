from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

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
