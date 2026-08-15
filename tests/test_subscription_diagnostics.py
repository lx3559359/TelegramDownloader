from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from telegram_downloader.subscription_diagnostics import explain_probe, explain_run
from telegram_downloader.subscriptions import (
    SubscriptionProbeReport,
    SubscriptionRun,
    SubscriptionRunStatus,
)

NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def subscription_run(**changes: object) -> SubscriptionRun:
    value = SubscriptionRun(
        id="run-1",
        rule_id="rule-1",
        account_id="account-1",
        started_at=NOW,
        finished_at=NOW,
        status=SubscriptionRunStatus.COMPLETED,
        inspected=4,
        keyword_hits=2,
        matched=2,
        queued=1,
        duplicate=1,
    )
    return replace(value, **changes)


def probe_report(**changes: object) -> SubscriptionProbeReport:
    value = SubscriptionProbeReport(
        rule_id="rule-1",
        inspected=4,
        keyword_hits=2,
        matched=2,
        duplicate=1,
        samples=(),
        finished_at=NOW,
    )
    return replace(value, **changes)


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (
            subscription_run(
                status=SubscriptionRunStatus.FAILED,
                error="secret-token TimeoutError private-channel",
            ),
            "检查失败",
        ),
        (
            subscription_run(
                inspected=0,
                keyword_hits=0,
                matched=0,
                queued=0,
                duplicate=0,
            ),
            "没有新消息",
        ),
        (subscription_run(keyword_hits=0, matched=0, queued=0, duplicate=0), "未命中关键词"),
        (subscription_run(matched=0, queued=0, duplicate=0), "没有所选媒体类型"),
        (subscription_run(queued=0, duplicate=2), "均已在队列"),
        (subscription_run(), "新增 1 项"),
    ],
)
def test_explain_run_covers_actionable_outcomes(
    run: SubscriptionRun,
    expected: str,
) -> None:
    explanation = explain_run(run)

    assert expected in explanation
    assert "secret-token" not in explanation
    assert "private-channel" not in explanation


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (probe_report(inspected=0, keyword_hits=0, matched=0, duplicate=0), "没有最近消息"),
        (probe_report(keyword_hits=0, matched=0, duplicate=0), "未出现关键词"),
        (probe_report(matched=0, duplicate=0), "没有所选媒体类型"),
        (probe_report(duplicate=2), "均已在队列"),
        (probe_report(), "匹配 2 项"),
    ],
)
def test_explain_probe_covers_read_only_outcomes(
    report: SubscriptionProbeReport,
    expected: str,
) -> None:
    assert expected in explain_probe(report)
