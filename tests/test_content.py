from datetime import UTC, datetime

import pytest

from telegram_downloader.content import ContentSearchQuery
from telegram_downloader.domain import MediaKind, ScanFilters


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
