from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock

from telegram_downloader.content import (
    AccountProfile,
    ContentDialog,
    ContentSearchQuery,
    ContentSourceKind,
    DialogKind,
    SearchCursor,
    SearchResult,
    SearchScope,
    SearchSelectionIntent,
    SearchSession,
    SearchSnapshot,
    SearchStatus,
    SelectionCommit,
    SelectionMode,
)
from telegram_downloader.domain import MediaKind, ScanFilters
from telegram_downloader.subscription_matching import (
    SubscriptionCriteria,
    SubscriptionMatchMode,
)
from telegram_downloader.subscriptions import (
    SubscriptionRule,
    SubscriptionRun,
    SubscriptionRunStatus,
    SubscriptionState,
)

CATALOG_SCHEMA_VERSION = 5


class CatalogError(RuntimeError):
    pass


class StaleSearchError(CatalogError):
    pass


@dataclass(frozen=True, slots=True)
class SearchPageCommit:
    session: SearchSession
    results: tuple[SearchResult, ...]
    result_count: int


_SCHEMA_V1 = """
CREATE TABLE accounts (
    account_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    last_used_at TEXT NOT NULL
);
CREATE TABLE dialogs (
    account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    peer_ref TEXT NOT NULL,
    title TEXT NOT NULL,
    username TEXT NOT NULL,
    kind TEXT NOT NULL,
    archived INTEGER NOT NULL CHECK(archived IN (0, 1)),
    available INTEGER NOT NULL CHECK(available IN (0, 1)),
    last_synced_at TEXT NOT NULL,
    PRIMARY KEY(account_id, peer_ref)
);
CREATE INDEX idx_dialogs_account_available_title
    ON dialogs(account_id, available, title COLLATE NOCASE);
CREATE TABLE search_sessions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    peer_ref TEXT NOT NULL,
    dialog_title TEXT NOT NULL,
    keyword TEXT NOT NULL,
    normalized_keyword TEXT NOT NULL,
    date_from_utc TEXT NOT NULL,
    date_to_utc TEXT NOT NULL,
    media_kinds TEXT NOT NULL,
    item_limit INTEGER NOT NULL CHECK(item_limit BETWEEN 1 AND 10000),
    filters_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation > 0),
    next_offset_id INTEGER,
    exhausted INTEGER NOT NULL CHECK(exhausted IN (0, 1)),
    result_count INTEGER NOT NULL DEFAULT 0 CHECK(result_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT,
    UNIQUE(account_id, peer_ref, normalized_keyword, filters_fingerprint)
);
CREATE TABLE search_results (
    id TEXT PRIMARY KEY,
    search_id TEXT NOT NULL REFERENCES search_sessions(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    peer_ref TEXT NOT NULL,
    message_id INTEGER NOT NULL CHECK(message_id > 0),
    grouped_id INTEGER,
    media_id TEXT NOT NULL,
    media_kind TEXT NOT NULL,
    original_name TEXT NOT NULL,
    expected_size INTEGER CHECK(expected_size IS NULL OR expected_size >= 0),
    message_date_utc TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    thumbnail_key TEXT NOT NULL,
    selected INTEGER NOT NULL CHECK(selected IN (0, 1)),
    available INTEGER NOT NULL CHECK(available IN (0, 1)),
    queued INTEGER NOT NULL CHECK(queued IN (0, 1)),
    generation INTEGER NOT NULL CHECK(generation > 0),
    UNIQUE(search_id, peer_ref, message_id, media_id)
);
CREATE INDEX idx_results_search_generation_date
    ON search_results(search_id, generation, message_date_utc DESC, message_id DESC);
PRAGMA user_version=1;
"""

_SCHEMA_V2_MIGRATION = """
CREATE TABLE subscription_rules (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    peer_ref TEXT NOT NULL,
    dialog_title TEXT NOT NULL,
    keyword TEXT NOT NULL,
    normalized_keyword TEXT NOT NULL,
    media_kinds TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL CHECK(interval_minutes IN (5, 15, 30, 60, 180)),
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    state TEXT NOT NULL,
    last_message_id INTEGER CHECK(last_message_id IS NULL OR last_message_id >= 0),
    next_run_at TEXT,
    last_run_at TEXT,
    last_error TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK(failure_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE CASCADE,
    FOREIGN KEY(account_id, peer_ref) REFERENCES dialogs(account_id, peer_ref),
    UNIQUE(account_id, peer_ref, normalized_keyword)
);
CREATE INDEX idx_subscription_rules_due
    ON subscription_rules(account_id, enabled, next_run_at);
CREATE TABLE subscription_runs (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL REFERENCES subscription_rules(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL,
    inspected INTEGER NOT NULL CHECK(inspected >= 0),
    matched INTEGER NOT NULL CHECK(matched >= 0),
    queued INTEGER NOT NULL CHECK(queued >= 0),
    duplicate INTEGER NOT NULL CHECK(duplicate >= 0),
    error TEXT
);
CREATE INDEX idx_subscription_runs_rule_started
    ON subscription_runs(rule_id, started_at DESC);
PRAGMA user_version=2;
"""

_SCHEMA_V3_MIGRATION = """
ALTER TABLE subscription_runs
    ADD COLUMN keyword_hits INTEGER NOT NULL DEFAULT 0 CHECK(keyword_hits >= 0);
PRAGMA user_version=3;
"""

_SCHEMA_V4_MIGRATION = """
ALTER TABLE search_sessions
    ADD COLUMN scope TEXT NOT NULL DEFAULT 'single_dialog';
ALTER TABLE search_sessions
    ADD COLUMN cursor_json TEXT;
ALTER TABLE search_results
    ADD COLUMN source_title TEXT NOT NULL DEFAULT '';
ALTER TABLE search_results
    ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'unknown';
UPDATE search_results
SET source_title = COALESCE(
    (SELECT dialog_title FROM search_sessions
     WHERE search_sessions.id = search_results.search_id),
    peer_ref
);
UPDATE search_results
SET source_kind = COALESCE(
    (SELECT kind FROM dialogs
     WHERE dialogs.account_id = search_results.account_id
       AND dialogs.peer_ref = search_results.peer_ref),
    'unknown'
);
PRAGMA user_version=4;
"""

_SCHEMA_V5_COLUMN_MIGRATIONS = (
    "ALTER TABLE subscription_rules ADD COLUMN include_keywords_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE subscription_rules ADD COLUMN exclude_keywords_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE subscription_rules ADD COLUMN match_mode TEXT NOT NULL DEFAULT 'any'",
    "ALTER TABLE subscription_rules ADD COLUMN matcher_fingerprint TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE subscription_rules "
    "ADD COLUMN history_days INTEGER NOT NULL DEFAULT 0 "
    "CHECK(history_days IN (0, 1, 3, 7, 30))",
    "ALTER TABLE subscription_rules ADD COLUMN backfill_from_utc TEXT",
    "ALTER TABLE subscription_rules ADD COLUMN backfill_through_id INTEGER "
    "CHECK(backfill_through_id IS NULL OR backfill_through_id >= 0)",
)


class CatalogRepository:
    def __init__(self, database: Path) -> None:
        self.database = database.resolve()
        self._connection_lock = RLock()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self._connection_lock:
            connection = sqlite3.connect(self.database, timeout=5)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute("PRAGMA busy_timeout=5000")
                with connection:
                    yield connection
            finally:
                connection.close()

    def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                connection.executescript(_SCHEMA_V1)
                version = 1
            if version == 1:
                connection.executescript(_SCHEMA_V2_MIGRATION)
                version = 2
            if version == 2:
                connection.executescript(_SCHEMA_V3_MIGRATION)
                version = 3
            if version == 3:
                connection.executescript(_SCHEMA_V4_MIGRATION)
                version = 4
            if version == 4:
                self._migrate_v4_to_v5(connection)
                version = 5
            if version != CATALOG_SCHEMA_VERSION:
                raise CatalogError(f"不支持的内容目录版本：{version}")

    @staticmethod
    def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
        connection.execute("SAVEPOINT catalog_schema_v5")
        try:
            for statement in _SCHEMA_V5_COLUMN_MIGRATIONS:
                connection.execute(statement)
            rows = connection.execute(
                "SELECT id, keyword FROM subscription_rules ORDER BY id"
            ).fetchall()
            for row in rows:
                criteria = SubscriptionCriteria((str(row["keyword"]),))
                connection.execute(
                    "UPDATE subscription_rules SET keyword=?, normalized_keyword=?, "
                    "include_keywords_json=?, exclude_keywords_json=?, match_mode=?, "
                    "matcher_fingerprint=?, history_days=0 WHERE id=?",
                    (
                        criteria.summary,
                        criteria.fingerprint,
                        CatalogRepository._keywords_json(criteria.include_keywords),
                        CatalogRepository._keywords_json(criteria.exclude_keywords),
                        criteria.mode.value,
                        criteria.fingerprint,
                        str(row["id"]),
                    ),
                )
            connection.execute("PRAGMA user_version=5")
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT catalog_schema_v5")
            connection.execute("RELEASE SAVEPOINT catalog_schema_v5")
            raise
        connection.execute("RELEASE SAVEPOINT catalog_schema_v5")

    def schema_version(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def upsert_account(self, profile: AccountProfile, used_at: datetime) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO accounts(account_id, display_name, last_used_at) "
                "VALUES(?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET "
                "display_name=excluded.display_name, last_used_at=excluded.last_used_at",
                (profile.account_id, profile.display_name, used_at.isoformat()),
            )

    def most_recent_account(self) -> AccountProfile | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT account_id, display_name FROM accounts "
                "ORDER BY last_used_at DESC, account_id LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return AccountProfile(str(row["account_id"]), str(row["display_name"]))

    def replace_dialogs(
        self,
        account_id: str,
        dialogs: list[ContentDialog],
        synced_at: datetime,
    ) -> None:
        if any(item.account_id != account_id for item in dialogs):
            raise ValueError("会话不属于当前账号")
        with self._connection() as connection:
            connection.execute(
                "UPDATE dialogs SET available=0, last_synced_at=? WHERE account_id=?",
                (synced_at.isoformat(), account_id),
            )
            for item in dialogs:
                connection.execute(
                    "INSERT INTO dialogs(account_id, peer_ref, title, username, kind, "
                    "archived, available, last_synced_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, 1, ?) "
                    "ON CONFLICT(account_id, peer_ref) DO UPDATE SET "
                    "title=excluded.title, username=excluded.username, "
                    "kind=excluded.kind, archived=excluded.archived, available=1, "
                    "last_synced_at=excluded.last_synced_at",
                    (
                        account_id,
                        item.peer_ref,
                        item.title,
                        item.username,
                        item.kind.value,
                        int(item.archived),
                        synced_at.isoformat(),
                    ),
                )

    def list_dialogs(
        self,
        account_id: str,
        *,
        include_unavailable: bool = False,
    ) -> list[ContentDialog]:
        where = "account_id=?" if include_unavailable else "account_id=? AND available=1"
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT account_id, peer_ref, title, username, kind, archived, "
                f"available, last_synced_at FROM dialogs WHERE {where} "
                "ORDER BY title COLLATE NOCASE, peer_ref",
                (account_id,),
            ).fetchall()
        return [self._dialog_from_row(row) for row in rows]

    def get_dialog(self, account_id: str, peer_ref: str) -> ContentDialog:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT account_id, peer_ref, title, username, kind, archived, "
                "available, last_synced_at FROM dialogs "
                "WHERE account_id=? AND peer_ref=?",
                (account_id, peer_ref),
            ).fetchone()
        if row is None:
            raise KeyError(peer_ref)
        return self._dialog_from_row(row)

    def begin_search(
        self,
        search_id: str,
        account_id: str,
        peer_ref: str,
        dialog_title: str,
        query: ContentSearchQuery,
        now: datetime,
        *,
        scope: SearchScope = SearchScope.SINGLE_DIALOG,
    ) -> SearchSession:
        kinds = ",".join(sorted(kind.value for kind in query.filters.media_kinds))
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO search_sessions(id, account_id, peer_ref, dialog_title, "
                "keyword, normalized_keyword, date_from_utc, date_to_utc, "
                "media_kinds, item_limit, filters_fingerprint, status, generation, "
                "next_offset_id, exhausted, result_count, created_at, updated_at, "
                "last_error, scope, cursor_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, 1, NULL, 0, 0, ?, ?, NULL, ?, NULL) "
                "ON CONFLICT(account_id, peer_ref, "
                "normalized_keyword, filters_fingerprint) DO UPDATE SET "
                "dialog_title=excluded.dialog_title, keyword=excluded.keyword, "
                "status=excluded.status, generation=search_sessions.generation+1, "
                "next_offset_id=NULL, cursor_json=NULL, exhausted=0, result_count=0, "
                "scope=excluded.scope, "
                "updated_at=excluded.updated_at, last_error=NULL",
                (
                    search_id,
                    account_id,
                    peer_ref,
                    dialog_title,
                    query.keyword,
                    query.normalized_keyword,
                    query.filters.date_from_utc.isoformat(),
                    query.filters.date_to_utc.isoformat(),
                    kinds,
                    query.filters.item_limit,
                    query.filters_fingerprint,
                    SearchStatus.RUNNING.value,
                    now.isoformat(),
                    now.isoformat(),
                    scope.value,
                ),
            )
            row = connection.execute(
                "SELECT * FROM search_sessions WHERE account_id=? AND peer_ref=? "
                "AND normalized_keyword=? AND filters_fingerprint=?",
                (
                    account_id,
                    peer_ref,
                    query.normalized_keyword,
                    query.filters_fingerprint,
                ),
            ).fetchone()
        if row is None:
            raise CatalogError("无法创建搜索记录")
        return self._session_from_row(row)

    def get_session(self, account_id: str, search_id: str) -> SearchSession:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM search_sessions WHERE account_id=? AND id=?",
                (account_id, search_id),
            ).fetchone()
        if row is None:
            raise KeyError(search_id)
        return self._session_from_row(row)

    def list_sessions(self, account_id: str) -> list[SearchSession]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM search_sessions WHERE account_id=? ORDER BY updated_at DESC, id",
                (account_id,),
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def load_search_snapshot(
        self,
        account_id: str,
        search_id: str,
    ) -> SearchSnapshot:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM search_sessions WHERE account_id=? AND id=?",
                (account_id, search_id),
            ).fetchone()
            if row is None:
                raise KeyError(search_id)
            rows = self._select_result_rows(connection, account_id, search_id)
        return SearchSnapshot(
            self._session_from_row(row),
            tuple(self._result_from_row(item) for item in rows),
        )

    def save_search_page(
        self,
        account_id: str,
        search_id: str,
        generation: int,
        results: list[SearchResult],
    ) -> None:
        with self._connection() as connection:
            session = connection.execute(
                "SELECT * FROM search_sessions WHERE account_id=? AND id=?",
                (account_id, search_id),
            ).fetchone()
            self._insert_search_results(
                connection,
                account_id,
                search_id,
                generation,
                results,
                session=session,
            )

    def commit_search_page(
        self,
        account_id: str,
        search_id: str,
        generation: int,
        results: list[SearchResult],
        *,
        cursor: SearchCursor | None,
        complete: bool,
        finished_at: datetime,
        status: SearchStatus,
        error: str | None,
    ) -> SearchPageCommit:
        with self._connection() as connection:
            session = connection.execute(
                "SELECT * FROM search_sessions WHERE account_id=? AND id=? AND generation=?",
                (account_id, search_id, generation),
            ).fetchone()
            if session is None:
                raise StaleSearchError("搜索结果已被更新的搜索代次取代")
            self._insert_search_results(
                connection,
                account_id,
                search_id,
                generation,
                results,
                session=session,
            )
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM search_results "
                    "WHERE account_id=? AND search_id=? AND generation=?",
                    (account_id, search_id, generation),
                ).fetchone()[0]
            )
            self._update_search_state(
                connection,
                account_id,
                search_id,
                generation,
                cursor,
                complete,
                finished_at,
                status,
                error,
                count,
            )
            session_row = connection.execute(
                "SELECT * FROM search_sessions WHERE account_id=? AND id=?",
                (account_id, search_id),
            ).fetchone()
            result_rows = self._select_result_rows(
                connection,
                account_id,
                search_id,
            )
        if session_row is None:
            raise CatalogError("无法读取搜索提交状态")
        return SearchPageCommit(
            self._session_from_row(session_row),
            tuple(self._result_from_row(row) for row in result_rows),
            count,
        )

    def _insert_search_results(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        search_id: str,
        generation: int,
        results: list[SearchResult],
        *,
        session: sqlite3.Row | None = None,
    ) -> None:
        if any(item.search_id != search_id or item.account_id != account_id for item in results):
            raise ValueError("搜索结果不属于当前搜索")
        if session is None:
            session = connection.execute(
                "SELECT * FROM search_sessions WHERE account_id=? AND id=?",
                (account_id, search_id),
            ).fetchone()
        if session is None or int(session["generation"]) != generation:
            raise StaleSearchError("搜索结果已被更新的搜索代次取代")
        if SearchScope(str(session["scope"])) is SearchScope.SINGLE_DIALOG and any(
            item.peer_ref != str(session["peer_ref"]) for item in results
        ):
            raise ValueError("搜索结果不属于当前会话")
        for item in results:
            connection.execute(
                "INSERT INTO search_results(id, search_id, account_id, peer_ref, "
                "message_id, grouped_id, media_id, media_kind, original_name, "
                "expected_size, message_date_utc, excerpt, thumbnail_key, "
                "selected, available, queued, source_title, source_kind, "
                "generation) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?) "
                "ON CONFLICT(search_id, peer_ref, message_id, media_id) "
                "DO UPDATE SET grouped_id=excluded.grouped_id, "
                "media_kind=excluded.media_kind, "
                "original_name=excluded.original_name, "
                "expected_size=excluded.expected_size, "
                "message_date_utc=excluded.message_date_utc, "
                "excerpt=excluded.excerpt, thumbnail_key=excluded.thumbnail_key, "
                "source_title=excluded.source_title, "
                "source_kind=excluded.source_kind, "
                "available=excluded.available, "
                "queued=MAX(search_results.queued, excluded.queued), "
                "selected=CASE WHEN excluded.queued=1 THEN 0 "
                "ELSE search_results.selected END, "
                "generation=excluded.generation",
                (
                    item.id,
                    item.search_id,
                    item.account_id,
                    item.peer_ref,
                    item.message_id,
                    item.grouped_id,
                    item.media_id,
                    item.media_kind.value,
                    item.original_name,
                    item.expected_size,
                    item.message_date_utc.isoformat(),
                    item.excerpt,
                    item.thumbnail_key,
                    int(item.selected),
                    int(item.available),
                    int(item.queued),
                    item.source_title,
                    item.source_kind.value,
                    generation,
                ),
            )

    def finish_search(
        self,
        account_id: str,
        search_id: str,
        generation: int,
        cursor: SearchCursor | None,
        exhausted: bool,
        now: datetime,
        *,
        status: SearchStatus = SearchStatus.COMPLETED,
        error: str | None = None,
    ) -> None:
        with self._connection() as connection:
            session = connection.execute(
                "SELECT 1 FROM search_sessions WHERE account_id=? AND id=? AND generation=?",
                (account_id, search_id, generation),
            ).fetchone()
            if session is None:
                raise StaleSearchError("搜索结果已被更新的搜索代次取代")
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM search_results "
                    "WHERE account_id=? AND search_id=? AND generation=?",
                    (account_id, search_id, generation),
                ).fetchone()[0]
            )
            self._update_search_state(
                connection,
                account_id,
                search_id,
                generation,
                cursor,
                exhausted,
                now,
                status,
                error,
                count,
            )

    @staticmethod
    def _update_search_state(
        connection: sqlite3.Connection,
        account_id: str,
        search_id: str,
        generation: int,
        cursor: SearchCursor | None,
        exhausted: bool,
        now: datetime,
        status: SearchStatus,
        error: str | None,
        result_count: int,
    ) -> None:
        connection.execute(
            "UPDATE search_sessions SET status=?, next_offset_id=?, cursor_json=?, "
            "exhausted=?, result_count=?, updated_at=?, last_error=? "
            "WHERE account_id=? AND id=? AND generation=?",
            (
                status.value,
                cursor.offset_id if cursor else None,
                cursor.to_json() if cursor else None,
                int(exhausted),
                result_count,
                now.isoformat(),
                error,
                account_id,
                search_id,
                generation,
            ),
        )
        if status is SearchStatus.COMPLETED:
            connection.execute(
                "DELETE FROM search_results WHERE account_id=? AND search_id=? AND generation<>?",
                (account_id, search_id, generation),
            )

    def list_results(
        self,
        account_id: str,
        search_id: str,
        *,
        selected_only: bool = False,
    ) -> list[SearchResult]:
        with self._connection() as connection:
            rows = self._select_result_rows(
                connection,
                account_id,
                search_id,
                selected_only=selected_only,
            )
        return [self._result_from_row(row) for row in rows]

    @staticmethod
    def _select_result_rows(
        connection: sqlite3.Connection,
        account_id: str,
        search_id: str,
        *,
        selected_only: bool = False,
    ) -> list[sqlite3.Row]:
        selected = " AND result.selected=1" if selected_only else ""
        return connection.execute(
            "SELECT result.* FROM search_results AS result "
            "JOIN search_sessions AS session ON session.id=result.search_id "
            "WHERE session.account_id=? AND session.id=? "
            "AND result.generation=session.generation"
            f"{selected} ORDER BY result.message_date_utc DESC, "
            "result.message_id DESC, result.id",
            (account_id, search_id),
        ).fetchall()

    def get_result(self, account_id: str, result_id: str) -> SearchResult:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT result.* FROM search_results AS result "
                "JOIN search_sessions AS session ON session.id=result.search_id "
                "WHERE session.account_id=? AND result.id=? "
                "AND result.generation=session.generation",
                (account_id, result_id),
            ).fetchone()
        if row is None:
            raise KeyError(result_id)
        return self._result_from_row(row)

    def set_selected(
        self,
        account_id: str,
        search_id: str,
        result_id: str,
        selected: bool,
    ) -> None:
        generation = self.get_session(account_id, search_id).generation
        self.apply_selection(
            account_id,
            SearchSelectionIntent(
                search_id,
                generation,
                1,
                SelectionMode.PATCH,
                ((result_id, selected),),
            ),
        )

    def apply_selection(
        self,
        account_id: str,
        intent: SearchSelectionIntent,
    ) -> SelectionCommit:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT generation FROM search_sessions WHERE account_id=? AND id=?",
                (account_id, intent.search_id),
            ).fetchone()
            if row is None or int(row["generation"]) != intent.generation:
                raise StaleSearchError("选择操作已被更新的搜索代次取代")
            changed = 0
            if intent.mode is SelectionMode.PATCH:
                for result_id, selected in intent.final_changes:
                    cursor = connection.execute(
                        "UPDATE search_results SET selected=? WHERE account_id=? "
                        "AND search_id=? AND id=? AND generation=? "
                        "AND (?=0 OR (available=1 AND queued=0))",
                        (
                            int(selected),
                            account_id,
                            intent.search_id,
                            result_id,
                            intent.generation,
                            int(selected),
                        ),
                    )
                    if cursor.rowcount != 1:
                        exists = connection.execute(
                            "SELECT 1 FROM search_results WHERE account_id=? "
                            "AND search_id=? AND id=? AND generation=?",
                            (
                                account_id,
                                intent.search_id,
                                result_id,
                                intent.generation,
                            ),
                        ).fetchone()
                        if exists is None:
                            raise KeyError(result_id)
                        raise ValueError("该媒体当前不可选择")
                    changed += 1
            elif intent.mode is SelectionMode.SELECT_ALL:
                cursor = connection.execute(
                    "UPDATE search_results SET selected=CASE "
                    "WHEN available=1 AND queued=0 THEN 1 ELSE 0 END "
                    "WHERE account_id=? AND search_id=? AND generation=?",
                    (account_id, intent.search_id, intent.generation),
                )
                changed = max(0, cursor.rowcount)
            else:
                cursor = connection.execute(
                    "UPDATE search_results SET selected=CASE "
                    "WHEN available=1 AND queued=0 THEN "
                    "CASE selected WHEN 1 THEN 0 ELSE 1 END ELSE 0 END "
                    "WHERE account_id=? AND search_id=? AND generation=?",
                    (account_id, intent.search_id, intent.generation),
                )
                changed = max(0, cursor.rowcount)
        return SelectionCommit(
            intent.search_id,
            intent.generation,
            intent.revision,
            changed,
        )

    def mark_queued(self, account_id: str, result_ids: tuple[str, ...]) -> None:
        if not result_ids:
            return
        placeholders = ",".join("?" for _ in result_ids)
        with self._connection() as connection:
            connection.execute(
                "UPDATE search_results SET queued=1, selected=0 WHERE account_id=? "
                f"AND id IN ({placeholders})",
                (account_id, *result_ids),
            )

    def mark_unavailable(
        self,
        account_id: str,
        result_ids: tuple[str, ...],
    ) -> None:
        if not result_ids:
            return
        placeholders = ",".join("?" for _ in result_ids)
        with self._connection() as connection:
            connection.execute(
                "UPDATE search_results SET available=0 WHERE account_id=? "
                f"AND id IN ({placeholders})",
                (account_id, *result_ids),
            )

    def list_thumbnail_keys(
        self,
        account_id: str,
        search_id: str | None = None,
    ) -> set[str]:
        parameters: list[object] = [account_id]
        where = "session.account_id=?"
        if search_id is not None:
            where += " AND session.id=?"
            parameters.append(search_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT result.thumbnail_key "
                "FROM search_results AS result "
                "JOIN search_sessions AS session ON session.id=result.search_id "
                f"WHERE {where}",
                parameters,
            ).fetchall()
        return {str(row["thumbnail_key"]) for row in rows}

    def referenced_thumbnail_keys(
        self,
        account_id: str,
        keys: set[str],
    ) -> set[str]:
        if not keys:
            return set()
        found: set[str] = set()
        ordered = sorted(keys)
        with self._connection() as connection:
            for start in range(0, len(ordered), 500):
                chunk = ordered[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    "SELECT DISTINCT result.thumbnail_key "
                    "FROM search_results AS result "
                    "JOIN search_sessions AS session ON session.id=result.search_id "
                    "WHERE session.account_id=? "
                    f"AND result.thumbnail_key IN ({placeholders})",
                    (account_id, *chunk),
                ).fetchall()
                found.update(str(row["thumbnail_key"]) for row in rows)
        return found

    def delete_session(self, account_id: str, search_id: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM search_sessions WHERE account_id=? AND id=?",
                (account_id, search_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(search_id)

    def clear_history(self, account_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM search_sessions WHERE account_id=?",
                (account_id,),
            )

    def recover_interrupted_searches(
        self,
        account_id: str,
        now: datetime,
    ) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE search_sessions SET status=?, updated_at=?, last_error=? "
                "WHERE account_id=? AND status=?",
                (
                    SearchStatus.INCOMPLETE.value,
                    now.isoformat(),
                    "上次搜索未正常结束",
                    account_id,
                    SearchStatus.RUNNING.value,
                ),
            )
            return cursor.rowcount

    def save_subscription(self, rule: SubscriptionRule) -> None:
        media_kinds = ",".join(sorted(kind.value for kind in rule.media_kinds))
        include_keywords = self._keywords_json(rule.criteria.include_keywords)
        exclude_keywords = self._keywords_json(rule.criteria.exclude_keywords)
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT account_id FROM subscription_rules WHERE id=?",
                (rule.id,),
            ).fetchone()
            if existing is not None and str(existing["account_id"]) != rule.account_id:
                raise CatalogError("订阅规则不属于当前账号")
            try:
                connection.execute(
                    "INSERT INTO subscription_rules("
                    "id, account_id, peer_ref, dialog_title, keyword, "
                    "normalized_keyword, media_kinds, interval_minutes, enabled, "
                    "state, last_message_id, next_run_at, last_run_at, last_error, "
                    "failure_count, created_at, updated_at, include_keywords_json, "
                    "exclude_keywords_json, match_mode, matcher_fingerprint, "
                    "history_days, backfill_from_utc, backfill_through_id) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "peer_ref=excluded.peer_ref, dialog_title=excluded.dialog_title, "
                    "keyword=excluded.keyword, "
                    "normalized_keyword=excluded.normalized_keyword, "
                    "media_kinds=excluded.media_kinds, "
                    "interval_minutes=excluded.interval_minutes, "
                    "enabled=excluded.enabled, state=excluded.state, "
                    "last_message_id=excluded.last_message_id, "
                    "next_run_at=excluded.next_run_at, "
                    "last_run_at=excluded.last_run_at, "
                    "last_error=excluded.last_error, "
                    "failure_count=excluded.failure_count, "
                    "include_keywords_json=excluded.include_keywords_json, "
                    "exclude_keywords_json=excluded.exclude_keywords_json, "
                    "match_mode=excluded.match_mode, "
                    "matcher_fingerprint=excluded.matcher_fingerprint, "
                    "history_days=excluded.history_days, "
                    "backfill_from_utc=excluded.backfill_from_utc, "
                    "backfill_through_id=excluded.backfill_through_id, "
                    "updated_at=excluded.updated_at",
                    (
                        rule.id,
                        rule.account_id,
                        rule.peer_ref,
                        rule.dialog_title,
                        rule.keyword,
                        rule.normalized_keyword,
                        media_kinds,
                        rule.interval_minutes,
                        int(rule.enabled),
                        rule.state.value,
                        rule.last_message_id,
                        self._datetime_value(rule.next_run_at),
                        self._datetime_value(rule.last_run_at),
                        rule.last_error,
                        rule.failure_count,
                        rule.created_at.isoformat(),
                        rule.updated_at.isoformat(),
                        include_keywords,
                        exclude_keywords,
                        rule.criteria.mode.value,
                        rule.criteria.fingerprint,
                        rule.history_days,
                        self._datetime_value(rule.backfill_from_utc),
                        rule.backfill_through_id,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise CatalogError("相同群组和规则的订阅已经存在") from error

    def get_subscription(self, account_id: str, rule_id: str) -> SubscriptionRule:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_rules WHERE account_id=? AND id=?",
                (account_id, rule_id),
            ).fetchone()
        if row is None:
            raise KeyError(rule_id)
        return self._subscription_from_row(row)

    def list_subscriptions(self, account_id: str) -> list[SubscriptionRule]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM subscription_rules WHERE account_id=? "
                "ORDER BY enabled DESC, next_run_at, dialog_title COLLATE NOCASE, id",
                (account_id,),
            ).fetchall()
        return [self._subscription_from_row(row) for row in rows]

    def list_due_subscriptions(
        self,
        account_id: str,
        now: datetime,
    ) -> list[SubscriptionRule]:
        states = (
            SubscriptionState.WAITING.value,
            SubscriptionState.WAITING_NETWORK.value,
            SubscriptionState.FAILED.value,
        )
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM subscription_rules WHERE account_id=? AND enabled=1 "
                "AND next_run_at IS NOT NULL AND next_run_at<=? "
                "AND state IN (?, ?, ?) ORDER BY next_run_at, created_at, id",
                (account_id, now.isoformat(), *states),
            ).fetchall()
        return [self._subscription_from_row(row) for row in rows]

    def delete_subscription(self, account_id: str, rule_id: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM subscription_rules WHERE account_id=? AND id=?",
                (account_id, rule_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(rule_id)

    def advance_subscription(
        self,
        account_id: str,
        rule_id: str,
        message_id: int,
        now: datetime,
        *,
        complete_backfill: bool = False,
    ) -> None:
        if message_id < 0:
            raise ValueError("消息游标不能为负数")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT last_message_id, backfill_through_id FROM subscription_rules "
                "WHERE account_id=? AND id=?",
                (account_id, rule_id),
            ).fetchone()
            if row is None:
                raise KeyError(rule_id)
            previous = row["last_message_id"]
            if previous is not None and message_id < int(previous):
                raise ValueError("订阅消息游标不能倒退")
            if complete_backfill:
                through_id = row["backfill_through_id"]
                if through_id is None or message_id != int(through_id):
                    raise ValueError("订阅补抓尚未到达固定截止消息")
                connection.execute(
                    "UPDATE subscription_rules SET last_message_id=?, "
                    "backfill_from_utc=NULL, backfill_through_id=NULL, updated_at=? "
                    "WHERE account_id=? AND id=?",
                    (message_id, now.isoformat(), account_id, rule_id),
                )
            else:
                connection.execute(
                    "UPDATE subscription_rules SET last_message_id=?, updated_at=? "
                    "WHERE account_id=? AND id=?",
                    (message_id, now.isoformat(), account_id, rule_id),
                )

    def update_subscription_runtime(
        self,
        account_id: str,
        rule_id: str,
        *,
        state: SubscriptionState,
        next_run_at: datetime | None,
        last_run_at: datetime | None,
        last_error: str | None,
        failure_count: int,
        now: datetime,
    ) -> None:
        if failure_count < 0:
            raise ValueError("失败次数不能为负数")
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE subscription_rules SET state=?, next_run_at=?, "
                "last_run_at=?, last_error=?, failure_count=?, updated_at=? "
                "WHERE account_id=? AND id=?",
                (
                    state.value,
                    self._datetime_value(next_run_at),
                    self._datetime_value(last_run_at),
                    last_error,
                    failure_count,
                    now.isoformat(),
                    account_id,
                    rule_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(rule_id)

    def recover_interrupted_subscriptions(self, now: datetime) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE subscription_rules SET state=?, next_run_at=?, "
                "last_error=?, updated_at=? WHERE enabled=1 AND state IN (?, ?)",
                (
                    SubscriptionState.WAITING.value,
                    now.isoformat(),
                    "上次自动检查未正常结束",
                    now.isoformat(),
                    SubscriptionState.RUNNING.value,
                    SubscriptionState.BASELINING.value,
                ),
            )
            return cursor.rowcount

    def resume_connection_blocked_subscriptions(
        self,
        account_id: str,
        now: datetime,
    ) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE subscription_rules SET state=?, next_run_at=?, updated_at=? "
                "WHERE account_id=? AND enabled=1 AND state IN (?, ?)",
                (
                    SubscriptionState.WAITING.value,
                    now.isoformat(),
                    now.isoformat(),
                    account_id,
                    SubscriptionState.WAITING_NETWORK.value,
                    SubscriptionState.AUTH_REQUIRED.value,
                ),
            )
            return cursor.rowcount

    def save_subscription_run(
        self,
        run: SubscriptionRun,
        *,
        retain: int = 100,
    ) -> None:
        if retain <= 0:
            raise ValueError("订阅运行记录保留数必须大于零")
        with self._connection() as connection:
            rule = connection.execute(
                "SELECT account_id FROM subscription_rules WHERE id=?",
                (run.rule_id,),
            ).fetchone()
            if rule is None or str(rule["account_id"]) != run.account_id:
                raise CatalogError("订阅运行记录不属于当前账号")
            connection.execute(
                "INSERT INTO subscription_runs("
                "id, rule_id, account_id, started_at, finished_at, status, "
                "inspected, keyword_hits, matched, queued, duplicate, error) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.id,
                    run.rule_id,
                    run.account_id,
                    run.started_at.isoformat(),
                    run.finished_at.isoformat(),
                    run.status.value,
                    run.inspected,
                    run.keyword_hits,
                    run.matched,
                    run.queued,
                    run.duplicate,
                    run.error,
                ),
            )
            connection.execute(
                "DELETE FROM subscription_runs WHERE rule_id=? AND id IN ("
                "SELECT id FROM subscription_runs WHERE rule_id=? "
                "ORDER BY finished_at DESC, id DESC LIMIT -1 OFFSET ?)",
                (run.rule_id, run.rule_id, retain),
            )

    def list_subscription_runs(
        self,
        account_id: str,
        rule_id: str,
    ) -> list[SubscriptionRun]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM subscription_runs WHERE account_id=? AND rule_id=? "
                "ORDER BY finished_at DESC, id DESC",
                (account_id, rule_id),
            ).fetchall()
        return [self._subscription_run_from_row(row) for row in rows]

    def latest_subscription_runs(
        self,
        account_id: str,
    ) -> dict[str, SubscriptionRun]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM subscription_runs WHERE account_id=? "
                "ORDER BY finished_at DESC, id DESC",
                (account_id,),
            ).fetchall()
        latest: dict[str, SubscriptionRun] = {}
        for row in rows:
            run = self._subscription_run_from_row(row)
            latest.setdefault(run.rule_id, run)
        return latest

    @staticmethod
    def _dialog_from_row(row: sqlite3.Row) -> ContentDialog:
        return ContentDialog(
            account_id=str(row["account_id"]),
            peer_ref=str(row["peer_ref"]),
            title=str(row["title"]),
            username=str(row["username"]),
            kind=DialogKind(str(row["kind"])),
            archived=bool(row["archived"]),
            available=bool(row["available"]),
            last_synced_at=datetime.fromisoformat(str(row["last_synced_at"])),
        )

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> SearchSession:
        filters = ScanFilters(
            datetime.fromisoformat(str(row["date_from_utc"])),
            datetime.fromisoformat(str(row["date_to_utc"])),
            frozenset(MediaKind(value) for value in str(row["media_kinds"]).split(",") if value),
            int(row["item_limit"]),
        )
        cursor_json = row["cursor_json"]
        cursor_value = row["next_offset_id"]
        cursor = (
            SearchCursor.from_json(str(cursor_json))
            if cursor_json is not None
            else SearchCursor(int(cursor_value))
            if cursor_value is not None
            else None
        )
        return SearchSession(
            id=str(row["id"]),
            account_id=str(row["account_id"]),
            peer_ref=str(row["peer_ref"]),
            dialog_title=str(row["dialog_title"]),
            query=ContentSearchQuery(str(row["keyword"]), filters),
            status=SearchStatus(str(row["status"])),
            generation=int(row["generation"]),
            cursor=cursor,
            exhausted=bool(row["exhausted"]),
            result_count=int(row["result_count"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            last_error=(str(row["last_error"]) if row["last_error"] is not None else None),
            scope=SearchScope(str(row["scope"])),
        )

    @staticmethod
    def _result_from_row(row: sqlite3.Row) -> SearchResult:
        grouped = row["grouped_id"]
        expected = row["expected_size"]
        return SearchResult(
            id=str(row["id"]),
            search_id=str(row["search_id"]),
            account_id=str(row["account_id"]),
            peer_ref=str(row["peer_ref"]),
            message_id=int(row["message_id"]),
            grouped_id=int(grouped) if grouped is not None else None,
            media_id=str(row["media_id"]),
            media_kind=MediaKind(str(row["media_kind"])),
            original_name=str(row["original_name"]),
            expected_size=int(expected) if expected is not None else None,
            message_date_utc=datetime.fromisoformat(str(row["message_date_utc"])),
            excerpt=str(row["excerpt"]),
            thumbnail_key=str(row["thumbnail_key"]),
            selected=bool(row["selected"]),
            available=bool(row["available"]),
            queued=bool(row["queued"]),
            source_title=str(row["source_title"]),
            source_kind=ContentSourceKind(str(row["source_kind"])),
        )

    @staticmethod
    def _subscription_from_row(row: sqlite3.Row) -> SubscriptionRule:
        last_message_id = row["last_message_id"]
        backfill_through_id = row["backfill_through_id"]
        try:
            criteria = SubscriptionCriteria(
                tuple(json.loads(str(row["include_keywords_json"]))),
                tuple(json.loads(str(row["exclude_keywords_json"]))),
                SubscriptionMatchMode(str(row["match_mode"])),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise CatalogError("订阅规则数据无效") from error
        if str(row["matcher_fingerprint"]) != criteria.fingerprint:
            raise CatalogError("订阅规则指纹不一致")
        return SubscriptionRule(
            id=str(row["id"]),
            account_id=str(row["account_id"]),
            peer_ref=str(row["peer_ref"]),
            dialog_title=str(row["dialog_title"]),
            criteria=criteria,
            media_kinds=frozenset(
                MediaKind(value) for value in str(row["media_kinds"]).split(",") if value
            ),
            interval_minutes=int(row["interval_minutes"]),
            history_days=int(row["history_days"]),
            enabled=bool(row["enabled"]),
            state=SubscriptionState(str(row["state"])),
            last_message_id=(int(last_message_id) if last_message_id is not None else None),
            backfill_from_utc=CatalogRepository._datetime_from_row(row["backfill_from_utc"]),
            backfill_through_id=(
                int(backfill_through_id) if backfill_through_id is not None else None
            ),
            next_run_at=CatalogRepository._datetime_from_row(row["next_run_at"]),
            last_run_at=CatalogRepository._datetime_from_row(row["last_run_at"]),
            last_error=(str(row["last_error"]) if row["last_error"] is not None else None),
            failure_count=int(row["failure_count"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _keywords_json(values: tuple[str, ...]) -> str:
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _subscription_run_from_row(row: sqlite3.Row) -> SubscriptionRun:
        return SubscriptionRun(
            id=str(row["id"]),
            rule_id=str(row["rule_id"]),
            account_id=str(row["account_id"]),
            started_at=datetime.fromisoformat(str(row["started_at"])),
            finished_at=datetime.fromisoformat(str(row["finished_at"])),
            status=SubscriptionRunStatus(str(row["status"])),
            inspected=int(row["inspected"]),
            keyword_hits=int(row["keyword_hits"]),
            matched=int(row["matched"]),
            queued=int(row["queued"]),
            duplicate=int(row["duplicate"]),
            error=str(row["error"]) if row["error"] is not None else None,
        )

    @staticmethod
    def _datetime_value(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _datetime_from_row(value: object) -> datetime | None:
        return datetime.fromisoformat(str(value)) if value is not None else None
