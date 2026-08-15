import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_downloader.domain import (
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
