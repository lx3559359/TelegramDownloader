from __future__ import annotations

import pytest

from telegram_downloader.subscription_matching import (
    SubscriptionCriteria,
    SubscriptionMatchMode,
)


@pytest.mark.parametrize(
    ("criteria", "text", "expected"),
    [
        (SubscriptionCriteria(("AI", "模型")), "新的 ai 工具", True),
        (SubscriptionCriteria(("AI", "模型")), "只有新闻", False),
        (
            SubscriptionCriteria(("AI", "大 模型"), mode=SubscriptionMatchMode.ALL),
            "AI   大\n模型 发布",
            True,
        ),
        (
            SubscriptionCriteria(("AI",), ("广告",)),
            "AI 广告",
            False,
        ),
    ],
)
def test_structured_subscription_matching(
    criteria: SubscriptionCriteria,
    text: str,
    expected: bool,
) -> None:
    assert criteria.matches(text) is expected


def test_criteria_deduplicates_terms_and_rejects_conflicts() -> None:
    value = SubscriptionCriteria((" AI ", "ai", "模型"), (" 广告 ", "广告"))

    assert value.include_keywords == ("AI", "模型")
    assert value.exclude_keywords == ("广告",)

    with pytest.raises(ValueError, match="不能同时"):
        SubscriptionCriteria(("AI",), (" ai ",))


def test_fingerprint_is_order_independent_but_semantics_sensitive() -> None:
    first = SubscriptionCriteria(("AI", "模型"), ("广告",))
    reordered = SubscriptionCriteria(("模型", "AI"), ("广告",))
    all_terms = SubscriptionCriteria(
        ("AI", "模型"),
        ("广告",),
        SubscriptionMatchMode.ALL,
    )

    assert first.fingerprint == reordered.fingerprint
    assert first.fingerprint != all_terms.fingerprint


def test_matching_uses_unicode_casefold_and_collapsed_whitespace() -> None:
    criteria = SubscriptionCriteria(("STRASSE  模型",))

    assert criteria.matches("Straße\n模型") is True


@pytest.mark.parametrize(
    "criteria",
    [
        lambda: SubscriptionCriteria(tuple(f"词-{number}" for number in range(21))),
        lambda: SubscriptionCriteria(("词" * 101,)),
        lambda: SubscriptionCriteria(
            tuple(f"{number:02d}" + "词" * 98 for number in range(20)),
            ("排" * 100,),
        ),
    ],
)
def test_criteria_rejects_quantity_and_length_limits(criteria) -> None:
    with pytest.raises(ValueError):
        criteria()
