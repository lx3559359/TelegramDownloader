import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_downloader.catalog import CatalogError, CatalogRepository, StaleSearchError
from telegram_downloader.content import (
    AccountProfile,
    ContentDialog,
    ContentSearchQuery,
    DialogKind,
    SearchCursor,
    SearchResult,
    SearchStatus,
)
from telegram_downloader.domain import MediaKind, ScanFilters


def dialog(account: str, peer: str, title: str, now: datetime) -> ContentDialog:
    return ContentDialog(
        account,
        peer,
        title,
        "",
        DialogKind.GROUP,
        False,
        True,
        now,
    )


def result(
    search_id: str,
    account_id: str,
    now: datetime,
    *,
    result_id: str = "result-1",
    message_id: int = 7,
) -> SearchResult:
    return SearchResult(
        result_id,
        search_id,
        account_id,
        "-1001",
        message_id,
        None,
        f"media-{message_id}",
        MediaKind.VIDEO,
        f"{message_id}.mp4",
        12,
        now,
        "安装教程",
        f"thumb-{message_id}",
    )


def test_dialog_sync_is_account_scoped_and_marks_missing_unavailable(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "账号一"), now)
    repo.upsert_account(AccountProfile("a2", "账号二"), now)
    repo.replace_dialogs("a1", [dialog("a1", "-1001", "群一", now)], now)
    repo.replace_dialogs("a2", [dialog("a2", "-1002", "群二", now)], now)

    repo.replace_dialogs("a1", [], now)

    assert repo.list_dialogs("a1") == []
    stale = repo.list_dialogs("a1", include_unavailable=True)
    assert stale == [replace(dialog("a1", "-1001", "群一", now), available=False)]
    assert [item.title for item in repo.list_dialogs("a2")] == ["群二"]


def test_dialog_sync_updates_title_kind_and_archive_state(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "账号"), now)
    original = dialog("a1", "-1001", "旧标题", now)
    repo.replace_dialogs("a1", [original], now)
    changed = replace(
        original,
        title="新标题",
        username="new_name",
        kind=DialogKind.CHANNEL,
        archived=True,
    )

    repo.replace_dialogs("a1", [changed], now)

    assert repo.list_dialogs("a1") == [changed]


def test_most_recent_account_supports_offline_history(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "旧账号"), now)
    repo.upsert_account(AccountProfile("a2", "最近账号"), now.replace(second=1))

    assert repo.most_recent_account() == AccountProfile("a2", "最近账号")


def test_initialize_rejects_unknown_newer_schema(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=2")

    with pytest.raises(CatalogError, match="版本"):
        CatalogRepository(database).initialize()


def test_search_refresh_preserves_selection_and_removes_stale_after_success(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "账号"), now)
    repo.replace_dialogs("a1", [dialog("a1", "-1001", "群", now)], now)
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now - timedelta(days=7), now, frozenset(MediaKind), 500),
    )
    first = repo.begin_search("search-1", "a1", "-1001", "群", query, now)
    repo.save_search_page(
        "a1",
        first.id,
        first.generation,
        [
            result(first.id, "a1", now),
            result(first.id, "a1", now, result_id="stale", message_id=8),
        ],
    )
    repo.set_selected("a1", first.id, "result-1", True)
    repo.finish_search("a1", first.id, first.generation, SearchCursor(7), True, now)

    second = repo.begin_search("ignored", "a1", "-1001", "群", query, now)
    assert second.id == first.id
    assert second.generation == 2
    repo.save_search_page(
        "a1",
        second.id,
        second.generation,
        [result(second.id, "a1", now)],
    )
    repo.finish_search("a1", second.id, second.generation, None, True, now)

    saved = repo.list_results("a1", second.id)
    assert len(saved) == 1
    assert saved[0].selected is True
    assert repo.list_sessions("a1")[0].status is SearchStatus.COMPLETED


def test_incomplete_search_keeps_current_generation_and_clear_is_account_scoped(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    for account in ("a1", "a2"):
        repo.upsert_account(AccountProfile(account, account), now)
        repo.replace_dialogs(account, [dialog(account, f"-{account}", account, now)], now)
        query = ContentSearchQuery(
            "资料",
            ScanFilters(now, now, frozenset(MediaKind), 500),
        )
        session = repo.begin_search(
            f"s-{account}",
            account,
            f"-{account}",
            account,
            query,
            now,
        )
        repo.finish_search(
            account,
            session.id,
            session.generation,
            SearchCursor(9),
            False,
            now,
            status=SearchStatus.INCOMPLETE,
            error="网络中断",
        )

    repo.clear_history("a1")

    assert repo.list_sessions("a1") == []
    assert len(repo.list_sessions("a2")) == 1


def test_stale_generation_cannot_overwrite_refreshed_search(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "账号"), now)
    query = ContentSearchQuery(
        "资料",
        ScanFilters(now, now, frozenset(MediaKind), 10),
    )
    first = repo.begin_search("search", "a1", "-1001", "群", query, now)
    repo.begin_search("ignored", "a1", "-1001", "群", query, now)

    with pytest.raises(StaleSearchError):
        repo.save_search_page(
            "a1",
            first.id,
            first.generation,
            [result(first.id, "a1", now)],
        )


def test_recover_interrupted_searches_preserves_results(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "账号"), now)
    query = ContentSearchQuery(
        "资料",
        ScanFilters(now, now, frozenset(MediaKind), 10),
    )
    session = repo.begin_search("search", "a1", "-1001", "群", query, now)
    repo.save_search_page(
        "a1",
        session.id,
        session.generation,
        [result(session.id, "a1", now)],
    )
    repo.set_selected("a1", session.id, "result-1", True)

    assert repo.recover_interrupted_searches("a1", now) == 1

    recovered = repo.get_session("a1", session.id)
    assert recovered.status is SearchStatus.INCOMPLETE
    assert recovered.last_error == "上次搜索未正常结束"
    assert repo.list_results("a1", session.id)[0].selected is True
