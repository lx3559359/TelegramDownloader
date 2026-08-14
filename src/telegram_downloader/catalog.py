from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from telegram_downloader.content import AccountProfile, ContentDialog, DialogKind


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
