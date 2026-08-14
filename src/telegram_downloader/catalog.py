from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from telegram_downloader.content import (
    AccountProfile,
    ContentDialog,
    ContentSearchQuery,
    DialogKind,
    SearchCursor,
    SearchResult,
    SearchSession,
    SearchStatus,
)
from telegram_downloader.domain import MediaKind, ScanFilters


class CatalogError(RuntimeError):
    pass


class StaleSearchError(CatalogError):
    pass


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


class CatalogRepository:
    def __init__(self, database: Path) -> None:
        self.database = database.resolve()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
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
            elif version != 1:
                raise CatalogError(f"不支持的内容目录版本：{version}")

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

    def begin_search(
        self,
        search_id: str,
        account_id: str,
        peer_ref: str,
        dialog_title: str,
        query: ContentSearchQuery,
        now: datetime,
    ) -> SearchSession:
        kinds = ",".join(sorted(kind.value for kind in query.filters.media_kinds))
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO search_sessions(id, account_id, peer_ref, dialog_title, "
                "keyword, normalized_keyword, date_from_utc, date_to_utc, "
                "media_kinds, item_limit, filters_fingerprint, status, generation, "
                "next_offset_id, exhausted, result_count, created_at, updated_at, "
                "last_error) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, "
                "0, 0, ?, ?, NULL) ON CONFLICT(account_id, peer_ref, "
                "normalized_keyword, filters_fingerprint) DO UPDATE SET "
                "dialog_title=excluded.dialog_title, keyword=excluded.keyword, "
                "status=excluded.status, generation=search_sessions.generation+1, "
                "next_offset_id=NULL, exhausted=0, result_count=0, "
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
                "SELECT * FROM search_sessions WHERE account_id=? "
                "ORDER BY updated_at DESC, id",
                (account_id,),
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def save_search_page(
        self,
        account_id: str,
        search_id: str,
        generation: int,
        results: list[SearchResult],
    ) -> None:
        if any(
            item.search_id != search_id or item.account_id != account_id
            for item in results
        ):
            raise ValueError("搜索结果不属于当前搜索")
        with self._connection() as connection:
            session = connection.execute(
                "SELECT peer_ref, generation FROM search_sessions "
                "WHERE account_id=? AND id=?",
                (account_id, search_id),
            ).fetchone()
            if session is None or int(session["generation"]) != generation:
                raise StaleSearchError("搜索结果已被更新的搜索代次取代")
            if any(item.peer_ref != str(session["peer_ref"]) for item in results):
                raise ValueError("搜索结果不属于当前会话")
            for item in results:
                connection.execute(
                    "INSERT INTO search_results(id, search_id, account_id, peer_ref, "
                    "message_id, grouped_id, media_id, media_kind, original_name, "
                    "expected_size, message_date_utc, excerpt, thumbnail_key, "
                    "selected, available, queued, generation) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(search_id, peer_ref, message_id, media_id) "
                    "DO UPDATE SET grouped_id=excluded.grouped_id, "
                    "media_kind=excluded.media_kind, "
                    "original_name=excluded.original_name, "
                    "expected_size=excluded.expected_size, "
                    "message_date_utc=excluded.message_date_utc, "
                    "excerpt=excluded.excerpt, thumbnail_key=excluded.thumbnail_key, "
                    "available=excluded.available, generation=excluded.generation",
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
                "SELECT 1 FROM search_sessions "
                "WHERE account_id=? AND id=? AND generation=?",
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
            connection.execute(
                "UPDATE search_sessions SET status=?, next_offset_id=?, exhausted=?, "
                "result_count=?, updated_at=?, last_error=? "
                "WHERE account_id=? AND id=? AND generation=?",
                (
                    status.value,
                    cursor.offset_id if cursor else None,
                    int(exhausted),
                    count,
                    now.isoformat(),
                    error,
                    account_id,
                    search_id,
                    generation,
                ),
            )
            if status is SearchStatus.COMPLETED:
                connection.execute(
                    "DELETE FROM search_results WHERE account_id=? AND search_id=? "
                    "AND generation<>?",
                    (account_id, search_id, generation),
                )

    def list_results(
        self,
        account_id: str,
        search_id: str,
        *,
        selected_only: bool = False,
    ) -> list[SearchResult]:
        selected = " AND result.selected=1" if selected_only else ""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT result.* FROM search_results AS result "
                "JOIN search_sessions AS session ON session.id=result.search_id "
                "WHERE session.account_id=? AND session.id=? "
                "AND result.generation=session.generation"
                f"{selected} ORDER BY result.message_date_utc DESC, "
                "result.message_id DESC, result.id",
                (account_id, search_id),
            ).fetchall()
        return [self._result_from_row(row) for row in rows]

    def set_selected(
        self,
        account_id: str,
        search_id: str,
        result_id: str,
        selected: bool,
    ) -> None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT result.available, result.queued FROM search_results AS result "
                "JOIN search_sessions AS session ON session.id=result.search_id "
                "WHERE session.account_id=? AND session.id=? AND result.id=? "
                "AND result.generation=session.generation",
                (account_id, search_id, result_id),
            ).fetchone()
            if row is None:
                raise KeyError(result_id)
            if selected and (not bool(row["available"]) or bool(row["queued"])):
                raise ValueError("该媒体当前不可选择")
            connection.execute(
                "UPDATE search_results SET selected=? "
                "WHERE account_id=? AND search_id=? AND id=?",
                (int(selected), account_id, search_id, result_id),
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
            frozenset(
                MediaKind(value)
                for value in str(row["media_kinds"]).split(",")
                if value
            ),
            int(row["item_limit"]),
        )
        cursor_value = row["next_offset_id"]
        return SearchSession(
            id=str(row["id"]),
            account_id=str(row["account_id"]),
            peer_ref=str(row["peer_ref"]),
            dialog_title=str(row["dialog_title"]),
            query=ContentSearchQuery(str(row["keyword"]), filters),
            status=SearchStatus(str(row["status"])),
            generation=int(row["generation"]),
            cursor=SearchCursor(int(cursor_value)) if cursor_value is not None else None,
            exhausted=bool(row["exhausted"]),
            result_count=int(row["result_count"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            last_error=(
                str(row["last_error"]) if row["last_error"] is not None else None
            ),
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
        )
