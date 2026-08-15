from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from telegram_downloader.domain import MediaKind
from telegram_downloader.subscriptions import (
    SubscriptionDraft,
    SubscriptionRule,
    SubscriptionState,
)

NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def rule(**changes: object) -> SubscriptionRule:
    value = SubscriptionRule(
        id="rule-1",
        account_id="account-1",
        peer_ref="peer:1",
        dialog_title="测试频道",
        keyword="  美 女  ",
        media_kinds=frozenset({MediaKind.PHOTO, MediaKind.VIDEO}),
        interval_minutes=30,
        enabled=True,
        state=SubscriptionState.WAITING,
        last_message_id=42,
        next_run_at=NOW,
        last_run_at=None,
        last_error=None,
        failure_count=0,
        created_at=NOW,
        updated_at=NOW,
    )
    return replace(value, **changes)


def test_subscription_rule_normalizes_keyword() -> None:
    assert rule().normalized_keyword == "美 女"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"keyword": "   "}, "关键词"),
        ({"media_kinds": frozenset()}, "媒体类型"),
        ({"interval_minutes": 7}, "检查间隔"),
        ({"last_message_id": -1}, "消息游标"),
        ({"failure_count": -1}, "失败次数"),
    ],
)
def test_subscription_rule_rejects_invalid_values(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        rule(**changes)


def test_subscription_draft_trims_keyword_and_validates_fields() -> None:
    draft = SubscriptionDraft(
        "peer:1",
        "  美女  ",
        frozenset({MediaKind.PHOTO}),
        15,
    )

    assert draft.keyword == "美女"
    assert draft.normalized_keyword == "美女"

    with pytest.raises(ValueError, match="群组"):
        SubscriptionDraft("", "美女", frozenset({MediaKind.PHOTO}), 15)
