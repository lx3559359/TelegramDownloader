import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_downloader import catalog as catalog_module
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
from telegram_downloader.subscriptions import (
    SubscriptionRule,
    SubscriptionRun,
    SubscriptionRunStatus,
    SubscriptionState,
)


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


def subscription(
    account_id: str,
    peer_ref: str,
    now: datetime,
    *,
    rule_id: str = "rule-1",
    enabled: bool = True,
    state: SubscriptionState = SubscriptionState.WAITING,
    last_message_id: int | None = 10,
    next_run_at: datetime | None = None,
) -> SubscriptionRule:
    return SubscriptionRule(
        rule_id,
        account_id,
        peer_ref,
        f"群-{account_id}",
        "美女",
        frozenset({MediaKind.PHOTO, MediaKind.VIDEO}),
        30,
        enabled,
        state,
        last_message_id,
        next_run_at if next_run_at is not None else now,
        None,
        None,
        0,
        now,
        now,
    )


def create_v2_catalog_with_run(database: Path, now: datetime) -> None:
    with sqlite3.connect(database) as connection:
        connection.executescript(catalog_module._SCHEMA_V1)
        connection.executescript(catalog_module._SCHEMA_V2_MIGRATION)
        connection.execute(
            "INSERT INTO accounts(account_id, display_name, last_used_at) VALUES(?, ?, ?)",
            ("a1", "旧账号", now.isoformat()),
        )
        connection.execute(
            "INSERT INTO dialogs(account_id, peer_ref, title, username, kind, archived, "
            "available, last_synced_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            ("a1", "-1001", "旧群组", "", "group", 0, 1, now.isoformat()),
        )
        connection.execute(
            "INSERT INTO subscription_rules("
            "id, account_id, peer_ref, dialog_title, keyword, normalized_keyword, "
            "media_kinds, interval_minutes, enabled, state, last_message_id, "
            "next_run_at, last_run_at, last_error, failure_count, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "r1",
                "a1",
                "-1001",
                "旧群组",
                "资料",
                "资料",
                "photo,video",
                30,
                1,
                "waiting",
                10,
                now.isoformat(),
                None,
                None,
                0,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO subscription_runs("
            "id, rule_id, account_id, started_at, finished_at, status, inspected, "
            "matched, queued, duplicate, error) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-old",
                "r1",
                "a1",
                now.isoformat(),
                now.isoformat(),
                "completed",
                5,
                2,
                1,
                1,
                None,
            ),
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
        connection.execute("PRAGMA user_version=4")

    with pytest.raises(CatalogError, match="版本"):
        CatalogRepository(database).initialize()


def test_catalog_schema_v3_keeps_existing_search_tables(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    database = tmp_path / "catalog.sqlite3"
    repo = CatalogRepository(database)
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "账号"), now)
    query = ContentSearchQuery(
        "资料",
        ScanFilters(now, now, frozenset(MediaKind), 10),
    )
    repo.begin_search("search", "a1", "-1001", "群", query, now)

    reopened = CatalogRepository(database)
    reopened.initialize()

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    assert reopened.list_sessions("a1")[0].query.keyword == "资料"


def test_catalog_migrates_existing_v1_database_without_losing_accounts(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    database = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(catalog_module._SCHEMA_V1)
        connection.execute(
            "INSERT INTO accounts(account_id, display_name, last_used_at) VALUES(?, ?, ?)",
            ("a1", "旧账号", now.isoformat()),
        )

    repo = CatalogRepository(database)
    repo.initialize()

    assert repo.schema_version() == 3
    assert repo.most_recent_account() == AccountProfile("a1", "旧账号")
    assert repo.list_subscriptions("a1") == []


def test_subscription_crud_and_due_queries_are_account_scoped(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    for account, peer in (("a1", "-1001"), ("a2", "-1002")):
        repo.upsert_account(AccountProfile(account, account), now)
        repo.replace_dialogs(
            account,
            [dialog(account, peer, f"群-{account}", now)],
            now,
        )
    first = subscription("a1", "-1001", now)
    second = subscription(
        "a2",
        "-1002",
        now,
        rule_id="rule-2",
        next_run_at=now + timedelta(hours=1),
    )
    repo.save_subscription(first)
    repo.save_subscription(second)

    assert repo.list_subscriptions("a1") == [first]
    assert repo.list_due_subscriptions("a1", now) == [first]
    assert repo.list_due_subscriptions("a2", now) == []
    with pytest.raises(KeyError):
        repo.get_subscription("a1", "rule-2")

    changed = replace(first, interval_minutes=60, updated_at=now + timedelta(minutes=1))
    repo.save_subscription(changed)
    assert repo.get_subscription("a1", first.id) == changed
    repo.delete_subscription("a1", first.id)
    assert repo.list_subscriptions("a1") == []
    assert repo.list_subscriptions("a2") == [second]


def test_subscription_cursor_is_monotonic_and_runtime_is_recoverable(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "账号"), now)
    repo.replace_dialogs("a1", [dialog("a1", "-1001", "群-a1", now)], now)
    saved = subscription("a1", "-1001", now)
    repo.save_subscription(saved)

    repo.advance_subscription("a1", saved.id, 15, now)
    with pytest.raises(ValueError, match="倒退"):
        repo.advance_subscription("a1", saved.id, 14, now)

    repo.update_subscription_runtime(
        "a1",
        saved.id,
        state=SubscriptionState.RUNNING,
        next_run_at=None,
        last_run_at=now,
        last_error=None,
        failure_count=0,
        now=now,
    )
    assert repo.recover_interrupted_subscriptions(now + timedelta(minutes=1)) == 1
    recovered = repo.get_subscription("a1", saved.id)
    assert recovered.state is SubscriptionState.WAITING
    assert recovered.last_message_id == 15
    assert recovered.next_run_at == now + timedelta(minutes=1)
    assert recovered.last_error == "上次自动检查未正常结束"


def test_subscription_run_history_is_trimmed_per_rule(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "账号"), now)
    repo.replace_dialogs("a1", [dialog("a1", "-1001", "群-a1", now)], now)
    saved = subscription("a1", "-1001", now)
    repo.save_subscription(saved)

    for number in range(4):
        finished = now + timedelta(seconds=number)
        repo.save_subscription_run(
            SubscriptionRun(
                f"run-{number}",
                saved.id,
                "a1",
                finished,
                finished,
                SubscriptionRunStatus.COMPLETED,
                number,
                number,
                number,
                number,
                0,
            ),
            retain=2,
        )

    latest = repo.latest_subscription_runs("a1")
    assert latest[saved.id].id == "run-3"

    assert [item.id for item in repo.list_subscription_runs("a1", saved.id)] == [
        "run-3",
        "run-2",
    ]


def test_catalog_migrates_v2_subscription_runs_to_v3(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    database = tmp_path / "catalog.sqlite3"
    create_v2_catalog_with_run(database, now)

    repository = CatalogRepository(database)
    repository.initialize()

    assert repository.schema_version() == 3
    run = repository.list_subscription_runs("a1", "r1")[0]
    assert run.keyword_hits == 0
    assert (run.inspected, run.matched, run.queued, run.duplicate) == (5, 2, 1, 1)


def test_subscription_run_round_trip_includes_keyword_hits(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repository = CatalogRepository(tmp_path / "catalog.sqlite3")
    repository.initialize()
    repository.upsert_account(AccountProfile("a1", "账号"), now)
    repository.replace_dialogs("a1", [dialog("a1", "-1001", "群-a1", now)], now)
    saved = subscription("a1", "-1001", now)
    repository.save_subscription(saved)
    run = SubscriptionRun(
        "run-keywords",
        saved.id,
        "a1",
        now,
        now,
        SubscriptionRunStatus.COMPLETED,
        5,
        3,
        2,
        1,
        1,
    )

    repository.save_subscription_run(run)

    assert repository.list_subscription_runs("a1", saved.id) == [run]


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


def test_catalog_service_helpers_are_account_scoped(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    query = ContentSearchQuery(
        "资料",
        ScanFilters(now, now, frozenset(MediaKind), 10),
    )
    sessions = {}
    for account, peer in (("a1", "-1001"), ("a2", "-1002")):
        repo.upsert_account(AccountProfile(account, account), now)
        repo.replace_dialogs(
            account,
            [dialog(account, peer, f"群-{account}", now)],
            now,
        )
        sessions[account] = repo.begin_search(
            f"search-{account}",
            account,
            peer,
            f"群-{account}",
            query,
            now,
        )
    first = replace(
        result(sessions["a1"].id, "a1", now),
        thumbnail_key="a1:shared",
    )
    second = replace(
        result(
            sessions["a2"].id,
            "a2",
            now,
            result_id="result-a2",
            message_id=8,
        ),
        peer_ref="-1002",
        thumbnail_key="a2:shared",
    )
    repo.save_search_page("a1", sessions["a1"].id, 1, [first])
    repo.save_search_page("a2", sessions["a2"].id, 1, [second])

    assert repo.get_dialog("a1", "-1001").title == "群-a1"
    with pytest.raises(KeyError):
        repo.get_dialog("a1", "-1002")
    assert repo.get_result("a1", first.id) == first
    with pytest.raises(KeyError):
        repo.get_result("a1", second.id)

    repo.set_selected("a1", sessions["a1"].id, first.id, True)
    repo.mark_unavailable("a1", (first.id, second.id))
    unavailable = repo.get_result("a1", first.id)
    assert unavailable.available is False
    assert unavailable.selected is True
    with pytest.raises(ValueError, match="该媒体当前不可选择"):
        repo.set_selected("a1", sessions["a1"].id, first.id, True)

    repo.mark_queued("a1", (first.id, second.id))
    queued = repo.get_result("a1", first.id)
    assert queued.queued is True
    assert queued.selected is False
    assert repo.get_result("a2", second.id).queued is False


def test_thumbnail_reference_queries_follow_history_deletion(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "账号"), now)
    first_query = ContentSearchQuery(
        "资料一",
        ScanFilters(now, now, frozenset(MediaKind), 10),
    )
    second_query = ContentSearchQuery(
        "资料二",
        ScanFilters(now, now, frozenset(MediaKind), 10),
    )
    first = repo.begin_search("s1", "a1", "-1001", "群", first_query, now)
    second = repo.begin_search("s2", "a1", "-1001", "群", second_query, now)
    shared_first = replace(
        result(first.id, "a1", now),
        thumbnail_key="a1:shared",
    )
    shared_second = replace(
        result(second.id, "a1", now, result_id="r2"),
        thumbnail_key="a1:shared",
    )
    repo.save_search_page("a1", first.id, first.generation, [shared_first])
    repo.save_search_page("a1", second.id, second.generation, [shared_second])

    assert repo.list_thumbnail_keys("a1") == {"a1:shared"}
    assert repo.list_thumbnail_keys("a1", first.id) == {"a1:shared"}
    assert repo.referenced_thumbnail_keys("a1", {"a1:shared", "missing"}) == {
        "a1:shared"
    }

    repo.delete_session("a1", first.id)
    assert repo.referenced_thumbnail_keys("a1", {"a1:shared"}) == {"a1:shared"}
    repo.delete_session("a1", second.id)
    assert repo.referenced_thumbnail_keys("a1", {"a1:shared"}) == set()
