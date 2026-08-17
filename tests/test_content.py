import json
from datetime import UTC, datetime

import pytest

from telegram_downloader.content import (
    ALL_DIALOGS_SCOPE_REF,
    ALL_DIALOGS_TITLE,
    ContentSearchQuery,
    ContentSourceKind,
    SearchCursor,
    SearchScope,
)
from telegram_downloader.domain import MediaKind, ScanFilters, SourceKind


def test_content_query_normalizes_keyword_and_has_stable_fingerprint() -> None:
    filters = ScanFilters(
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 14, tzinfo=UTC),
        frozenset({MediaKind.PHOTO, MediaKind.VIDEO}),
        500,
    )
    left = ContentSearchQuery("  安装Ａ  ", filters)
    right = ContentSearchQuery("安装A", filters)

    assert left.keyword == "安装Ａ"
    assert left.normalized_keyword == "安装a"
    assert left.filters_fingerprint == right.filters_fingerprint


@pytest.mark.parametrize("keyword", ["", "   "])
def test_content_query_rejects_empty_keyword(keyword: str) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    with pytest.raises(ValueError, match="关键词"):
        ContentSearchQuery(keyword, ScanFilters(now, now, frozenset(MediaKind), 500))


def test_content_query_rejects_more_than_ten_thousand_results() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    with pytest.raises(ValueError, match="10000"):
        ContentSearchQuery(
            "教程",
            ScanFilters(now, now, frozenset(MediaKind), 10001),
        )


def test_content_query_rejects_invalid_dates_and_empty_media_kinds() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    with pytest.raises(ValueError, match="日期"):
        ContentSearchQuery(
            "教程",
            ScanFilters(
                now,
                datetime(2026, 8, 13, tzinfo=UTC),
                frozenset(MediaKind),
                1,
            ),
        )
    with pytest.raises(ValueError, match="媒体类型"):
        ContentSearchQuery("教程", ScanFilters(now, now, frozenset(), 1))


def test_global_search_contract_has_stable_scope_and_task_values() -> None:
    assert SearchScope.ALL_DIALOGS.value == "all_dialogs"
    assert SearchScope.SINGLE_DIALOG.value == "single_dialog"
    assert ALL_DIALOGS_SCOPE_REF == "__all_dialogs__"
    assert ALL_DIALOGS_TITLE == "全部会话"
    assert ContentSourceKind.SAVED.value == "saved"
    assert SourceKind.ACCOUNT_SEARCH.value == "account_search"


def test_composite_search_cursor_json_round_trip() -> None:
    cursor = SearchCursor(
        offset_id=87,
        offset_rate=13,
        offset_peer_ref="-100123",
    )

    encoded = cursor.to_json()

    assert json.loads(encoded) == {
        "offsetId": 87,
        "offsetPeerRef": "-100123",
        "offsetRate": 13,
        "version": 1,
    }
    assert SearchCursor.from_json(encoded) == cursor
    assert SearchCursor(42).to_json() == (
        '{"offsetId":42,"offsetPeerRef":null,"offsetRate":0,"version":1}'
    )


@pytest.mark.parametrize(
    "payload",
    ["{}", '{"version":2}', '{"version":1,"offsetId":-1}'],
)
def test_composite_search_cursor_rejects_invalid_payload(payload: str) -> None:
    with pytest.raises(ValueError, match="游标"):
        SearchCursor.from_json(payload)
