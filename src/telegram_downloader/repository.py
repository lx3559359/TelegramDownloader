from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path

from telegram_downloader.domain import (
    ItemStatus,
    MediaItem,
    MediaKind,
    ScanFilters,
    SourceKind,
    TaskRecord,
    TaskStatus,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_title TEXT NOT NULL,
    source_url TEXT NOT NULL,
    date_from_utc TEXT NOT NULL,
    date_to_utc TEXT NOT NULL,
    media_kinds TEXT NOT NULL,
    item_limit INTEGER NOT NULL CHECK(item_limit > 0),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT,
    display_title TEXT,
    archived_at TEXT
);
CREATE TABLE IF NOT EXISTS media_items (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    peer_ref TEXT NOT NULL,
    message_id INTEGER NOT NULL CHECK(message_id > 0),
    grouped_id INTEGER,
    media_id TEXT NOT NULL,
    media_kind TEXT NOT NULL,
    original_name TEXT NOT NULL,
    target_path TEXT NOT NULL,
    expected_size INTEGER CHECK(expected_size IS NULL OR expected_size >= 0),
    message_date_utc TEXT NOT NULL,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0 CHECK(downloaded_bytes >= 0),
    status TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
    last_error TEXT,
    UNIQUE(peer_ref, message_id, media_id)
);
CREATE INDEX IF NOT EXISTS idx_items_task_status ON media_items(task_id, status);
"""

_TASK_COLUMNS = """
id, source_kind, source_ref, source_title, source_url,
date_from_utc, date_to_utc, media_kinds, item_limit, status,
created_at, updated_at, last_error, display_title, archived_at
"""

_QUALIFIED_TASK_COLUMNS = """
t.id AS id, t.source_kind AS source_kind, t.source_ref AS source_ref,
t.source_title AS source_title, t.source_url AS source_url,
t.date_from_utc AS date_from_utc, t.date_to_utc AS date_to_utc,
t.media_kinds AS media_kinds, t.item_limit AS item_limit,
t.status AS status, t.created_at AS created_at, t.updated_at AS updated_at,
t.last_error AS last_error, t.display_title AS display_title,
t.archived_at AS archived_at
"""

_ITEM_COLUMNS = """
id, task_id, peer_ref, message_id, grouped_id, media_id, media_kind,
original_name, target_path, expected_size, message_date_utc,
downloaded_bytes, status, retry_count, last_error
"""


class AllMediaAlreadyExists(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    task: TaskRecord
    total_items: int
    completed_items: int
    downloaded_bytes: int
    known_size: int
    unknown_size_count: int
    item_error: str | None


class TaskRepository:
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
            connection.executescript(_SCHEMA)
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "display_title" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN display_title TEXT")
            if "archived_at" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN archived_at TEXT")

    def create_task(self, task: TaskRecord, items: list[MediaItem]) -> None:
        with self._connection() as connection:
            self._insert_task(connection, task)
            for item in items:
                if item.task_id != task.id:
                    raise ValueError("媒体项不属于当前任务")
                self._insert_item(connection, item)

    def create_task_deduplicating(
        self,
        task: TaskRecord,
        items: list[MediaItem],
    ) -> list[MediaItem]:
        accepted: list[MediaItem] = []
        with self._connection() as connection:
            self._insert_task(connection, task)
            for item in items:
                if item.task_id != task.id:
                    raise ValueError("媒体项不属于当前任务")
                try:
                    self._insert_item(connection, item)
                except sqlite3.IntegrityError:
                    duplicate = connection.execute(
                        """
                        SELECT 1
                        FROM media_items
                        WHERE peer_ref = ? AND message_id = ? AND media_id = ?
                        """,
                        (item.peer_ref, item.message_id, item.media_id),
                    ).fetchone()
                    if duplicate is None:
                        raise
                    continue
                accepted.append(item)
            if not accepted:
                raise AllMediaAlreadyExists
        return accepted

    def existing_media_keys(
        self,
        keys: set[tuple[str, int, str]],
    ) -> set[tuple[str, int, str]]:
        if not keys:
            return set()
        found: set[tuple[str, int, str]] = set()
        ordered = sorted(keys)
        with self._connection() as connection:
            for chunk in batched(ordered, 200):
                where = " OR ".join(
                    "(peer_ref=? AND message_id=? AND media_id=?)" for _ in chunk
                )
                parameters = [value for key in chunk for value in key]
                rows = connection.execute(
                    "SELECT peer_ref, message_id, media_id FROM media_items WHERE "
                    + where,
                    parameters,
                ).fetchall()
                found.update(
                    (str(row[0]), int(row[1]), str(row[2])) for row in rows
                )
        return found

    def insert_item_if_new(self, item: MediaItem) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                f"INSERT OR IGNORE INTO media_items ({_ITEM_COLUMNS}) "
                f"VALUES ({','.join('?' for _ in range(15))})",
                self._item_values(item),
            )
            return cursor.rowcount == 1

    def get_task(self, task_id: str) -> TaskRecord:
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task_from_row(row)

    def get_item(self, item_id: str) -> MediaItem:
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {_ITEM_COLUMNS} FROM media_items WHERE id = ?",
                (item_id,),
            ).fetchone()
        if row is None:
            raise KeyError(item_id)
        return self._item_from_row(row)

    def list_tasks(self, *, include_archived: bool = False) -> list[TaskRecord]:
        where = "" if include_archived else "WHERE archived_at IS NULL"
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks {where} "
                "ORDER BY created_at DESC, id"
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def list_task_snapshots(
        self,
        *,
        include_archived: bool = False,
    ) -> list[TaskSnapshot]:
        where = "" if include_archived else "WHERE t.archived_at IS NULL"
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT {_QUALIFIED_TASK_COLUMNS},
                       COUNT(i.id) AS total_items,
                       COALESCE(
                           SUM(CASE WHEN i.status = ? THEN 1 ELSE 0 END),
                           0
                       ) AS completed_items,
                       COALESCE(SUM(i.downloaded_bytes), 0) AS downloaded_bytes,
                       COALESCE(SUM(COALESCE(i.expected_size, 0)), 0) AS known_size,
                       COALESCE(
                           SUM(
                               CASE
                                   WHEN i.id IS NOT NULL AND i.expected_size IS NULL
                                   THEN 1 ELSE 0
                               END
                           ),
                           0
                       ) AS unknown_size_count,
                       (
                           SELECT e.last_error
                           FROM media_items AS e
                           WHERE e.task_id = t.id AND e.last_error IS NOT NULL
                           ORDER BY e.message_date_utc DESC, e.message_id DESC, e.id
                           LIMIT 1
                       ) AS item_error
                FROM tasks AS t
                LEFT JOIN media_items AS i ON i.task_id = t.id
                {where}
                GROUP BY t.id
                ORDER BY t.created_at DESC, t.id
                """,
                (ItemStatus.COMPLETED.value,),
            ).fetchall()
        return [self._snapshot_from_row(row) for row in rows]

    def archive_tasks(self, task_ids: list[str]) -> set[str]:
        ids = tuple(dict.fromkeys(task_ids))
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT id FROM tasks WHERE id IN ({placeholders}) "
                "AND status = ? AND archived_at IS NULL",
                (*ids, TaskStatus.COMPLETED.value),
            ).fetchall()
            accepted = {str(row[0]) for row in rows}
            if accepted:
                selected = tuple(sorted(accepted))
                marks = ",".join("?" for _ in selected)
                connection.execute(
                    f"UPDATE tasks SET archived_at = ?, updated_at = ? "
                    f"WHERE id IN ({marks}) AND status = ? "
                    "AND archived_at IS NULL",
                    (now, now, *selected, TaskStatus.COMPLETED.value),
                )
        return accepted

    def restore_tasks(self, task_ids: list[str]) -> set[str]:
        ids = tuple(dict.fromkeys(task_ids))
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT id FROM tasks WHERE id IN ({placeholders}) "
                "AND archived_at IS NOT NULL",
                ids,
            ).fetchall()
            accepted = {str(row[0]) for row in rows}
            if accepted:
                selected = tuple(sorted(accepted))
                marks = ",".join("?" for _ in selected)
                connection.execute(
                    f"UPDATE tasks SET archived_at = NULL, updated_at = ? "
                    f"WHERE id IN ({marks}) AND archived_at IS NOT NULL",
                    (now, *selected),
                )
        return accepted

    def list_items(
        self,
        task_id: str,
        statuses: set[ItemStatus] | None = None,
    ) -> list[MediaItem]:
        if statuses is not None and not statuses:
            return []
        parameters: list[object] = [task_id]
        where = "task_id = ?"
        if statuses is not None:
            ordered = sorted(status.value for status in statuses)
            where += f" AND status IN ({','.join('?' for _ in ordered)})"
            parameters.extend(ordered)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {_ITEM_COLUMNS} FROM media_items "
                f"WHERE {where} ORDER BY message_date_utc DESC, message_id DESC, id",
                parameters,
            ).fetchall()
        return [self._item_from_row(row) for row in rows]

    def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        error: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, last_error = ? WHERE id = ?",
                (status.value, now, error, task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)

    def update_item_progress(
        self,
        item_id: str,
        downloaded_bytes: int,
        status: ItemStatus,
        error: str | None = None,
        retry_count: int | None = None,
    ) -> None:
        if downloaded_bytes < 0:
            raise ValueError("下载字节数不能为负数")
        if retry_count is not None and retry_count < 0:
            raise ValueError("重试次数不能为负数")

        assignments = "downloaded_bytes = ?, status = ?, last_error = ?"
        parameters: list[object] = [downloaded_bytes, status.value, error]
        if retry_count is not None:
            assignments += ", retry_count = ?"
            parameters.append(retry_count)
        parameters.append(item_id)
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE media_items SET {assignments} WHERE id = ?",
                parameters,
            )
            if cursor.rowcount != 1:
                raise KeyError(item_id)

    def recover_interrupted(self) -> None:
        now = datetime.now(UTC).isoformat()
        task_states = (
            TaskStatus.SCANNING.value,
            TaskStatus.DOWNLOADING.value,
            TaskStatus.WAITING_RETRY.value,
        )
        item_states = (ItemStatus.DOWNLOADING.value, ItemStatus.WAITING_RETRY.value)
        with self._connection() as connection:
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? "
                "WHERE status IN (?, ?, ?)",
                (TaskStatus.QUEUED.value, now, *task_states),
            )
            connection.execute(
                "UPDATE media_items SET status = ? WHERE status IN (?, ?)",
                (ItemStatus.QUEUED.value, *item_states),
            )

    @staticmethod
    def _insert_task(connection: sqlite3.Connection, task: TaskRecord) -> None:
        connection.execute(
            f"INSERT INTO tasks ({_TASK_COLUMNS}) "
            f"VALUES ({','.join('?' for _ in range(15))})",
            TaskRepository._task_values(task),
        )

    @staticmethod
    def _insert_item(connection: sqlite3.Connection, item: MediaItem) -> None:
        connection.execute(
            f"INSERT INTO media_items ({_ITEM_COLUMNS}) "
            f"VALUES ({','.join('?' for _ in range(15))})",
            TaskRepository._item_values(item),
        )

    @staticmethod
    def _item_values(item: MediaItem) -> tuple[object, ...]:
        return (
            item.id,
            item.task_id,
            item.peer_ref,
            item.message_id,
            item.grouped_id,
            item.media_id,
            item.media_kind.value,
            item.original_name,
            str(item.target_path),
            item.expected_size,
            item.message_date_utc.isoformat(),
            item.downloaded_bytes,
            item.status.value,
            item.retry_count,
            item.last_error,
        )

    @staticmethod
    def _task_values(task: TaskRecord) -> tuple[object, ...]:
        return (
            task.id,
            task.source_kind.value,
            task.source_ref,
            task.source_title,
            task.source_url,
            task.filters.date_from_utc.isoformat(),
            task.filters.date_to_utc.isoformat(),
            ",".join(sorted(kind.value for kind in task.filters.media_kinds)),
            task.filters.item_limit,
            task.status.value,
            task.created_at.isoformat(),
            task.updated_at.isoformat(),
            task.last_error,
            task.display_title,
            task.archived_at.isoformat() if task.archived_at is not None else None,
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskRecord:
        kinds = frozenset(
            MediaKind(value) for value in row["media_kinds"].split(",") if value
        )
        filters = ScanFilters(
            datetime.fromisoformat(row["date_from_utc"]),
            datetime.fromisoformat(row["date_to_utc"]),
            kinds,
            row["item_limit"],
        )
        return TaskRecord(
            row["id"],
            SourceKind(row["source_kind"]),
            row["source_ref"],
            row["source_title"],
            row["source_url"],
            filters,
            TaskStatus(row["status"]),
            datetime.fromisoformat(row["created_at"]),
            datetime.fromisoformat(row["updated_at"]),
            row["last_error"],
            row["display_title"],
            (
                datetime.fromisoformat(row["archived_at"])
                if row["archived_at"] is not None
                else None
            ),
        )

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> TaskSnapshot:
        return TaskSnapshot(
            TaskRepository._task_from_row(row),
            int(row["total_items"]),
            int(row["completed_items"]),
            int(row["downloaded_bytes"]),
            int(row["known_size"]),
            int(row["unknown_size_count"]),
            row["item_error"],
        )

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> MediaItem:
        return MediaItem(
            row["id"],
            row["task_id"],
            row["peer_ref"],
            row["message_id"],
            row["grouped_id"],
            row["media_id"],
            MediaKind(row["media_kind"]),
            row["original_name"],
            Path(row["target_path"]),
            row["expected_size"],
            datetime.fromisoformat(row["message_date_utc"]),
            row["downloaded_bytes"],
            ItemStatus(row["status"]),
            row["retry_count"],
            row["last_error"],
        )
