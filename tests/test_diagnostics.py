from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest

from telegram_downloader.diagnostics import (
    DiagnosticProgress,
    DiagnosticReport,
    DiagnosticResult,
    DiagnosticsService,
    DiagnosticStatus,
    reduce_status,
)

NOW = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)


def result(
    result_id: str,
    status: DiagnosticStatus = DiagnosticStatus.PASSED,
    *,
    duration_ms: int = 1,
    metrics: dict[str, bool | int | float | str] | None = None,
) -> DiagnosticResult:
    return DiagnosticResult(
        result_id,
        result_id,
        status,
        "check-ok",
        "检查完成",
        duration_ms,
        metrics or {},
    )


def test_diagnostic_status_values_are_stable() -> None:
    assert [item.value for item in DiagnosticStatus] == [
        "pending",
        "running",
        "passed",
        "warning",
        "failed",
        "skipped",
        "cancelled",
    ]


def test_report_status_prioritizes_cancel_failure_and_warning() -> None:
    passed = result("paths", DiagnosticStatus.PASSED)
    skipped = result("telegram", DiagnosticStatus.SKIPPED)
    warning = result("disk", DiagnosticStatus.WARNING)
    failed = result("tasks-db", DiagnosticStatus.FAILED)

    assert reduce_status((passed,)) is DiagnosticStatus.PASSED
    assert reduce_status((passed, skipped)) is DiagnosticStatus.PASSED
    assert reduce_status((skipped,)) is DiagnosticStatus.WARNING
    assert reduce_status((passed, warning)) is DiagnosticStatus.WARNING
    assert reduce_status((warning, failed)) is DiagnosticStatus.FAILED
    assert reduce_status((failed,), cancelled=True) is DiagnosticStatus.CANCELLED


def test_report_builds_schema_one_and_freezes_scalar_metrics() -> None:
    source = {
        "available": True,
        "count": 2,
        "latencyMs": 12.5,
        "version": "0.10.0",
    }
    check = result("updates", metrics=source)
    report = DiagnosticReport.build(
        "0.10.0",
        NOW,
        NOW + timedelta(seconds=1),
        (check,),
    )
    source["count"] = 999

    assert report.schema_version == 1
    assert report.app_version == "0.10.0"
    assert report.status is DiagnosticStatus.PASSED
    assert report.results[0].metrics["count"] == 2
    with pytest.raises(TypeError):
        report.results[0].metrics["count"] = 3  # type: ignore[index]


@pytest.mark.parametrize("value", ["", "UPPER", "two_words", "9starts-wrong"])
def test_result_rejects_unstable_ids(value: str) -> None:
    with pytest.raises(ValueError, match="标识"):
        result(value)


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_result_rejects_invalid_duration(value: object) -> None:
    with pytest.raises(ValueError, match="耗时"):
        result("paths", duration_ms=value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "metrics",
    [
        {"bad-key": 1},
        {"nested": []},
        {"nested": {}},
        {"nothing": None},
        {"nan": float("nan")},
    ],
)
def test_result_rejects_non_scalar_or_unstable_metrics(metrics: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="指标"):
        result("paths", metrics=metrics)  # type: ignore[arg-type]


def test_report_rejects_duplicate_ids_and_incomplete_results() -> None:
    with pytest.raises(ValueError, match="重复"):
        DiagnosticReport.build(
            "0.10.0",
            NOW,
            NOW,
            (result("disk"), result("disk")),
        )
    with pytest.raises(ValueError, match="未完成"):
        DiagnosticReport.build(
            "0.10.0",
            NOW,
            NOW,
            (result("disk", DiagnosticStatus.RUNNING),),
        )


def test_report_requires_ordered_utc_timestamps_and_results() -> None:
    non_utc = NOW.astimezone(timezone(timedelta(hours=8)))
    with pytest.raises(ValueError, match="UTC"):
        DiagnosticReport.build("0.10.0", non_utc, non_utc, (result("disk"),))
    with pytest.raises(ValueError, match="时间"):
        DiagnosticReport.build(
            "0.10.0",
            NOW,
            NOW - timedelta(seconds=1),
            (result("disk"),),
        )
    with pytest.raises(ValueError, match="检查结果"):
        DiagnosticReport.build("0.10.0", NOW, NOW, ())


class Probe:
    def __init__(
        self,
        probe_id: str,
        response: DiagnosticResult | None = None,
        *,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.id = probe_id
        self.title = probe_id
        self.response = response or result(probe_id)
        self.started = started
        self.release = release
        self.calls = 0

    async def run(self, cancel_event: asyncio.Event) -> DiagnosticResult:
        self.calls += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        return self.response


@pytest.mark.asyncio
async def test_diagnostics_repeated_runs_share_one_active_task_and_progress() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    probe = Probe("telegram", started=started, release=release)
    progress: list[DiagnosticProgress] = []
    service = DiagnosticsService(
        (probe,),
        app_version="0.10.0",
        utc_now=lambda: NOW,
    )

    first = asyncio.create_task(service.run(progress.append))
    await started.wait()
    second = asyncio.create_task(service.run())
    await asyncio.sleep(0)
    release.set()
    first_report, second_report = await asyncio.gather(first, second)

    assert first_report is second_report
    assert probe.calls == 1
    assert [(item.completed, item.current_id, item.status) for item in progress] == [
        (0, "telegram", DiagnosticStatus.RUNNING),
        (1, "telegram", DiagnosticStatus.PASSED),
        (1, None, DiagnosticStatus.PASSED),
    ]


@pytest.mark.asyncio
async def test_diagnostics_cancel_keeps_completed_and_marks_remaining() -> None:
    started = asyncio.Event()
    blocker = Probe("telegram", started=started, release=asyncio.Event())
    never_started = Probe("updates")
    service = DiagnosticsService(
        (Probe("paths"), blocker, never_started),
        app_version="0.10.0",
        utc_now=lambda: NOW,
    )

    running = asyncio.create_task(service.run())
    await started.wait()
    await service.cancel()
    report = await running
    await service.cancel()

    assert [item.status for item in report.results] == [
        DiagnosticStatus.PASSED,
        DiagnosticStatus.CANCELLED,
        DiagnosticStatus.CANCELLED,
    ]
    assert report.status is DiagnosticStatus.CANCELLED
    assert never_started.calls == 0


@pytest.mark.asyncio
async def test_diagnostics_maps_unexpected_probe_error_to_safe_failure() -> None:
    class BrokenProbe:
        id = "telegram"
        title = "Telegram"

        async def run(self, cancel_event: asyncio.Event) -> DiagnosticResult:
            raise RuntimeError(r"D:\\private\\session secret")

    report = await DiagnosticsService(
        (BrokenProbe(),),
        app_version="0.10.0",
        utc_now=lambda: NOW,
    ).run()

    assert report.results[0].code == "probe-failed"
    assert "private" not in report.results[0].summary
