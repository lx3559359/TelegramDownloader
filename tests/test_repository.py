import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_downloader.domain import (
    IntegrityStatus,
    ItemStatus,
    MediaItem,
    MediaKind,
    ScanFilters,
    SourceKind,
    TaskRecord,
    TaskStatus,
)
from telegram_downloader.repository import AllMediaAlreadyExists, TaskRepository


def records(tmp_path: Path) -> tuple[TaskRecord, MediaItem]:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    filters = ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 10)
    task = TaskRecord(
        "task-1",
        SourceKind.CHANNEL_OR_GROUP,
        "peer",
        "频道",
        "https://t.me/peer",
        filters,
        TaskStatus.QUEUED,
        now,
        now,
    )
    item = MediaItem(
        "item-1",
        task.id,
        "peer",
        7,
        None,
        "media-7",
        MediaKind.VIDEO,
        "x.mp4",
        tmp_path / "x.mp4",
        8,
        now,
    )
    return task, item


def test_round_trip_and_unique_source_item(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "database" / "tasks.sqlite3")
    repo.initialize()
    task, item = records(tmp_path)

    repo.create_task(task, [item])
    repo.update_item_progress(item.id, 4, ItemStatus.DOWNLOADING)

    assert repo.get_task(task.id) == task
    assert repo.list_items(task.id)[0].downloaded_bytes == 4
    assert repo.insert_item_if_new(replace(item, id="duplicate")) is False


def test_create_task_is_atomic_when_an_item_conflicts(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, item = records(tmp_path)
    repo.create_task(task, [item])
    second_task = replace(task, id="task-2")

    with pytest.raises(sqlite3.IntegrityError):
        repo.create_task(second_task, [replace(item, id="item-2", task_id="task-2")])

    with pytest.raises(KeyError):
        repo.get_task(second_task.id)


def test_recover_interrupted_work_in_one_pass(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, item = records(tmp_path)
    repo.create_task(
        replace(task, status=TaskStatus.DOWNLOADING),
        [replace(item, status=ItemStatus.DOWNLOADING)],
    )
    second_task = replace(task, id="task-2", status=TaskStatus.WAITING_RETRY)
    second_item = replace(
        item,
        id="item-2",
        task_id="task-2",
        message_id=8,
        media_id="media-8",
        status=ItemStatus.WAITING_RETRY,
    )
    repo.create_task(second_task, [second_item])

    repo.recover_interrupted()

    assert repo.get_task(task.id).status is TaskStatus.QUEUED
    assert repo.list_items(task.id)[0].status is ItemStatus.QUEUED
    assert repo.get_task(second_task.id).status is TaskStatus.QUEUED
    assert repo.list_items(second_task.id)[0].status is ItemStatus.QUEUED


def test_filters_items_and_updates_retry_and_error(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, item = records(tmp_path)
    failed = replace(
        item,
        id="item-2",
        message_id=8,
        media_id="media-8",
        status=ItemStatus.FAILED,
    )
    repo.create_task(task, [item, failed])

    repo.update_item_progress(
        failed.id,
        3,
        ItemStatus.WAITING_RETRY,
        error="network",
        retry_count=2,
    )

    selected = repo.list_items(task.id, {ItemStatus.WAITING_RETRY})
    assert len(selected) == 1
    assert selected[0].retry_count == 2
    assert selected[0].last_error == "network"
    assert repo.list_items(task.id, set()) == []


def test_initialize_enables_wal_and_foreign_keys_for_operations(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "tasks.sqlite3"
    repo = TaskRepository(database)
    repo.initialize()

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    task, item = records(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_item_if_new(replace(item, task_id="missing-task"))
    repo.create_task(task, [item])


def test_missing_records_and_negative_progress_are_rejected(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()

    with pytest.raises(KeyError):
        repo.get_task("missing")
    with pytest.raises(KeyError):
        repo.update_task_status("missing", TaskStatus.PAUSED)
    with pytest.raises(ValueError):
        repo.update_item_progress("missing", -1, ItemStatus.DOWNLOADING)


def test_initialize_migrates_old_tasks_and_round_trips_display_title(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tasks.sqlite3"
    now = datetime(2026, 8, 14, tzinfo=UTC).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE tasks (
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
                last_error TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy",
                SourceKind.CHANNEL_OR_GROUP.value,
                "peer",
                "旧频道",
                "https://t.me/peer",
                now,
                now,
                MediaKind.VIDEO.value,
                10,
                TaskStatus.QUEUED.value,
                now,
                now,
                None,
            ),
        )

    repo = TaskRepository(database)
    repo.initialize()

    assert repo.get_task("legacy").display_title is None
    assert repo.get_task("legacy").archived_at is None
    with sqlite3.connect(database) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
    assert "archived_at" in columns
    task, item = records(tmp_path)
    titled = replace(task, display_title="资料群（搜索：安装）")
    repo.create_task(titled, [item])

    reopened = TaskRepository(database)
    assert reopened.get_task(titled.id).display_title == "资料群（搜索：安装）"


def test_existing_media_keys_and_deduplicating_create_are_database_backed(
    tmp_path: Path,
) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, item = records(tmp_path)
    repo.create_task(task, [item])
    second_task = replace(task, id="task-2")
    duplicate = replace(item, id="duplicate", task_id=second_task.id)
    fresh = replace(
        item,
        id="fresh",
        task_id=second_task.id,
        message_id=8,
        media_id="media-8",
    )

    assert repo.existing_media_keys(set()) == set()
    assert repo.existing_media_keys(
        {
            (item.peer_ref, item.message_id, item.media_id),
            (fresh.peer_ref, fresh.message_id, fresh.media_id),
        }
    ) == {(item.peer_ref, item.message_id, item.media_id)}

    accepted = repo.create_task_deduplicating(second_task, [duplicate, fresh])

    assert accepted == [fresh]
    assert repo.list_items(second_task.id) == [fresh]


def test_deduplicating_create_rolls_back_task_when_every_item_exists(
    tmp_path: Path,
) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, item = records(tmp_path)
    repo.create_task(task, [item])
    second_task = replace(task, id="task-2")

    with pytest.raises(AllMediaAlreadyExists):
        repo.create_task_deduplicating(
            second_task,
            [replace(item, id="duplicate", task_id=second_task.id)],
        )

    with pytest.raises(KeyError):
        repo.get_task(second_task.id)


def test_task_snapshots_aggregate_items_without_hiding_unknown_sizes(
    tmp_path: Path,
) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, item = records(tmp_path)
    completed = replace(
        task,
        status=TaskStatus.COMPLETED,
        last_error="task-error",
    )
    known = replace(
        item,
        downloaded_bytes=8,
        status=ItemStatus.COMPLETED,
    )
    unknown = replace(
        item,
        id="item-2",
        message_id=8,
        media_id="media-8",
        expected_size=None,
        downloaded_bytes=3,
        status=ItemStatus.FAILED,
        last_error="item-error",
    )
    repo.create_task(completed, [known, unknown])

    snapshot = repo.list_task_snapshots()[0]

    assert snapshot.task == completed
    assert snapshot.total_items == 2
    assert snapshot.completed_items == 1
    assert snapshot.downloaded_bytes == 11
    assert snapshot.known_size == 8
    assert snapshot.unknown_size_count == 1
    assert snapshot.item_error == "item-error"


def test_completed_tasks_can_be_archived_restored_and_still_deduplicate(
    tmp_path: Path,
) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, item = records(tmp_path)
    completed = replace(task, status=TaskStatus.COMPLETED)
    active = replace(task, id="active", status=TaskStatus.DOWNLOADING)
    repo.create_task(completed, [replace(item, status=ItemStatus.COMPLETED)])
    repo.create_task(active, [])

    assert repo.archive_tasks([completed.id, active.id, completed.id]) == {
        completed.id
    }
    assert repo.get_task(completed.id).archived_at is not None
    assert [snapshot.task.id for snapshot in repo.list_task_snapshots()] == [active.id]
    assert {
        snapshot.task.id
        for snapshot in repo.list_task_snapshots(include_archived=True)
    } == {completed.id, active.id}
    assert repo.get_item(item.id).task_id == completed.id

    duplicate_task = replace(task, id="duplicate")
    with pytest.raises(AllMediaAlreadyExists):
        repo.create_task_deduplicating(
            duplicate_task,
            [replace(item, id="duplicate-item", task_id=duplicate_task.id)],
        )

    assert repo.restore_tasks([completed.id, completed.id]) == {completed.id}
    assert repo.get_task(completed.id).archived_at is None
    assert repo.restore_tasks([active.id, "missing"]) == set()


def test_get_item_rejects_unknown_id(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()

    with pytest.raises(KeyError):
        repo.get_item("missing")


def _create_v080_database(database: Path, target: Path) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC).isoformat()
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                source_title TEXT NOT NULL,
                source_url TEXT NOT NULL,
                date_from_utc TEXT NOT NULL,
                date_to_utc TEXT NOT NULL,
                media_kinds TEXT NOT NULL,
                item_limit INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_error TEXT,
                display_title TEXT,
                archived_at TEXT
            );
            CREATE TABLE media_items (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                peer_ref TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                grouped_id INTEGER,
                media_id TEXT NOT NULL,
                media_kind TEXT NOT NULL,
                original_name TEXT NOT NULL,
                target_path TEXT NOT NULL,
                expected_size INTEGER,
                message_date_utc TEXT NOT NULL,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                UNIQUE(peer_ref, message_id, media_id)
            );
            """
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-task",
                SourceKind.CHANNEL_OR_GROUP.value,
                "legacy-peer",
                "旧任务",
                "https://t.me/legacy-peer",
                now,
                now,
                MediaKind.VIDEO.value,
                10,
                TaskStatus.COMPLETED.value,
                now,
                now,
                None,
                None,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO media_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-item",
                "legacy-task",
                "legacy-peer",
                8,
                None,
                "legacy-media",
                MediaKind.VIDEO.value,
                "legacy.mp4",
                str(target),
                4,
                now,
                4,
                ItemStatus.COMPLETED.value,
                0,
                None,
            ),
        )


def test_initialize_migrates_v080_media_to_unverified(tmp_path: Path) -> None:
    database = tmp_path / "tasks.sqlite3"
    _create_v080_database(database, tmp_path / "legacy.mp4")
    repo = TaskRepository(database)

    repo.initialize()
    repo.initialize()

    item = repo.get_item("legacy-item")
    assert item.status is ItemStatus.COMPLETED
    assert item.integrity_status is IntegrityStatus.UNVERIFIED
    assert item.content_sha256 is None
    assert item.verified_at is None

    with sqlite3.connect(database) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(media_items)").fetchall()
        }
    assert {"integrity_status", "content_sha256", "verified_at"} <= columns


def test_record_integrity_success_validates_and_round_trips_digest(
    tmp_path: Path,
) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, item = records(tmp_path)
    repo.create_task(
        replace(task, status=TaskStatus.COMPLETED),
        [replace(item, status=ItemStatus.COMPLETED, downloaded_bytes=8)],
    )
    verified_at = datetime(2026, 8, 16, 3, 4, tzinfo=UTC)

    with pytest.raises(ValueError, match="SHA-256"):
        repo.record_integrity_success(item.id, "not-a-digest", verified_at)

    digest = "a" * 64
    repo.record_integrity_success(item.id, digest, verified_at)

    saved = repo.get_item(item.id)
    assert saved.integrity_status is IntegrityStatus.VERIFIED
    assert saved.content_sha256 == digest
    assert saved.verified_at == verified_at
    assert saved.status is ItemStatus.COMPLETED


def test_integrity_failure_preserves_baseline_and_marks_task_partial(
    tmp_path: Path,
) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, item = records(tmp_path)
    verified_at = datetime(2026, 8, 16, tzinfo=UTC)
    digest = "b" * 64
    repo.create_task(
        replace(task, status=TaskStatus.COMPLETED),
        [
            replace(
                item,
                status=ItemStatus.COMPLETED,
                downloaded_bytes=8,
                integrity_status=IntegrityStatus.VERIFIED,
                content_sha256=digest,
                verified_at=verified_at,
            )
        ],
    )

    repo.record_integrity_failure(
        item.id,
        IntegrityStatus.HASH_MISMATCH,
        "文件哈希不一致",
    )

    failed = repo.get_item(item.id)
    assert failed.status is ItemStatus.FAILED
    assert failed.integrity_status is IntegrityStatus.HASH_MISMATCH
    assert failed.content_sha256 == digest
    assert failed.verified_at == verified_at
    assert failed.last_error == "文件哈希不一致"
    assert repo.get_task(task.id).status is TaskStatus.PARTIAL_FAILURE

    with pytest.raises(ValueError, match="异常"):
        repo.record_integrity_failure(
            item.id,
            IntegrityStatus.UNVERIFIED,
            "invalid",
        )


def test_prepare_integrity_repair_resets_only_an_integrity_failed_item(
    tmp_path: Path,
) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, item = records(tmp_path)
    failed = replace(
        item,
        downloaded_bytes=8,
        status=ItemStatus.FAILED,
        retry_count=3,
        last_error="文件缺失",
        integrity_status=IntegrityStatus.MISSING,
        content_sha256="c" * 64,
        verified_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    repo.create_task(replace(task, status=TaskStatus.PARTIAL_FAILURE), [failed])

    previous = repo.prepare_integrity_repair(item.id)

    assert previous == failed
    queued = repo.get_item(item.id)
    assert queued.status is ItemStatus.QUEUED
    assert queued.downloaded_bytes == 0
    assert queued.retry_count == 0
    assert queued.last_error is None
    assert queued.integrity_status is IntegrityStatus.UNVERIFIED
    assert queued.content_sha256 is None
    assert queued.verified_at is None
    assert repo.get_task(task.id).status is TaskStatus.QUEUED

    with pytest.raises(ValueError, match="完整性异常"):
        repo.prepare_integrity_repair(item.id)
