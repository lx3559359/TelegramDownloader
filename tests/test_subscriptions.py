from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from telegram_downloader.domain import MediaKind
from telegram_downloader.subscription_matching import SubscriptionCriteria
from telegram_downloader.subscriptions import (
    SubscriptionDraft,
    SubscriptionProbeProgress,
    SubscriptionProbeReport,
    SubscriptionProbeSample,
    SubscriptionProgress,
    SubscriptionRule,
    SubscriptionRun,
    SubscriptionRunStatus,
    SubscriptionState,
)

NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def rule(**changes: object) -> SubscriptionRule:
    value = SubscriptionRule(
        id="rule-1",
        account_id="account-1",
        peer_ref="peer:1",
        dialog_title="测试频道",
        criteria=SubscriptionCriteria(("  美 女  ",)),
        media_kinds=frozenset({MediaKind.PHOTO, MediaKind.VIDEO}),
        interval_minutes=30,
        history_days=0,
        enabled=True,
        state=SubscriptionState.WAITING,
        last_message_id=42,
        backfill_from_utc=None,
        backfill_through_id=None,
        next_run_at=NOW,
        last_run_at=None,
        last_error=None,
        failure_count=0,
        created_at=NOW,
        updated_at=NOW,
    )
    return replace(value, **changes)


def test_subscription_rule_exposes_structured_summary_and_fingerprint() -> None:
    saved = rule()

    assert saved.keyword == "任意：美 女"
    assert saved.normalized_keyword == saved.criteria.fingerprint


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"criteria": "invalid"}, "订阅规则"),
        ({"media_kinds": frozenset()}, "媒体类型"),
        ({"interval_minutes": 7}, "检查间隔"),
        ({"history_days": 2}, "历史补抓"),
        ({"last_message_id": -1}, "消息游标"),
        ({"backfill_through_id": -1}, "补抓"),
        ({"failure_count": -1}, "失败次数"),
    ],
)
def test_subscription_rule_rejects_invalid_values(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        rule(**changes)


def test_subscription_draft_trims_peer_and_exposes_structured_summary() -> None:
    draft = SubscriptionDraft(
        " peer:1 ",
        SubscriptionCriteria(("AI", "模型"), ("广告",)),
        frozenset({MediaKind.PHOTO}),
        15,
        7,
    )

    assert draft.peer_ref == "peer:1"
    assert draft.keyword == "任意：AI、模型；排除：广告"
    assert draft.matcher_fingerprint == draft.criteria.fingerprint

    with pytest.raises(ValueError, match="群组"):
        SubscriptionDraft(
            "",
            SubscriptionCriteria(("美女",)),
            frozenset({MediaKind.PHOTO}),
            15,
        )


def test_backfill_snapshot_requires_a_persisted_start_time() -> None:
    with pytest.raises(ValueError, match="补抓起点"):
        rule(backfill_through_id=99, backfill_from_utc=None)


def test_subscription_counters_reject_impossible_keyword_totals() -> None:
    with pytest.raises(ValueError, match="关键词命中"):
        SubscriptionProbeProgress("rule-1", 2, 3, 0, "正在筛选")

    with pytest.raises(ValueError, match="关键词命中"):
        SubscriptionProgress("rule-1", 2, 3, 0, 0, 0, "正在筛选")

    with pytest.raises(ValueError, match="关键词命中"):
        SubscriptionRun(
            "run-1",
            "rule-1",
            "account-1",
            NOW,
            NOW,
            SubscriptionRunStatus.COMPLETED,
            2,
            3,
            0,
            0,
            0,
        )


def test_probe_report_rejects_invalid_counts_and_limits_samples() -> None:
    with pytest.raises(ValueError, match="关键词命中"):
        SubscriptionProbeReport("rule-1", 2, 3, 0, 0, (), NOW)

    sample = SubscriptionProbeSample(
        1,
        NOW,
        MediaKind.PHOTO,
        "photo.jpg",
        10,
        False,
        "摘要",
    )
    with pytest.raises(ValueError, match="20"):
        SubscriptionProbeReport("rule-1", 21, 21, 21, 0, (sample,) * 21, NOW)


@pytest.mark.parametrize(
    ("message_id", "expected_size", "message"),
    [
        (0, 10, "消息"),
        (1, -1, "大小"),
    ],
)
def test_probe_sample_rejects_invalid_media_metadata(
    message_id: int,
    expected_size: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        SubscriptionProbeSample(
            message_id,
            NOW,
            MediaKind.PHOTO,
            "photo.jpg",
            expected_size,
            False,
            "摘要",
        )
