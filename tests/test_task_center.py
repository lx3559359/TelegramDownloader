from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from telegram_downloader.domain import (
    MediaKind,
    ScanFilters,
    SourceKind,
    TaskRecord,
    TaskStatus,
)
from telegram_downloader.repository import TaskSnapshot
from telegram_downloader.scheduler import SchedulerSnapshot
from telegram_downloader.task_center import (
    ProgressSample,
    build_task_patch,
    build_task_view,
    patch_task_view,
)


def snapshot(
    task_id: str,
    status: TaskStatus,
    *,
    created_at: datetime,
    completed: int,
    total: int,
    downloaded: int = 0,
    known_size: int = 0,
    archived: bool = False,
) -> TaskSnapshot:
    filters = ScanFilters(
        created_at,
        created_at,
        frozenset({MediaKind.VIDEO}),
        max(1, total),
    )
    task = TaskRecord(
        task_id,
        SourceKind.CHANNEL_OR_GROUP,
        task_id,
        task_id.title(),
        f"https://t.me/{task_id}",
        filters,
        status,
        created_at,
        created_at,
        archived_at=created_at if archived else None,
    )
    return TaskSnapshot(
        task,
        total,
        completed,
        downloaded,
        known_size,
        0,
        None,
    )


def test_build_task_view_formats_speed_queue_and_dashboard_without_mutation() -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    snapshots = [
        snapshot(
            "active",
            TaskStatus.DOWNLOADING,
            created_at=now + timedelta(seconds=1),
            completed=1,
            total=2,
            downloaded=150,
            known_size=200,
        ),
        snapshot(
            "queued",
            TaskStatus.QUEUED,
            created_at=now,
            completed=2,
            total=3,
            known_size=300,
        ),
    ]
    original_ids = [value.task.id for value in snapshots]
    scheduler = SchedulerSnapshot(("active",), ("queued",), 3, 0)

    result = build_task_view(
        snapshots,
        scheduler_state=scheduler,
        queue_positions={"queued": 2},
        sampled_at=11.0,
        previous_samples={"active": ProgressSample(10.0, 100)},
    )

    assert result.by_id["active"].speed_bps == 50.0
    assert result.by_id["active"].speed_text == "50 B/s"
    assert result.by_id["active"].remaining_seconds == 1
    assert result.by_id["queued"].queue_position == 2
    assert result.dashboard.completed_items == 3
    assert result.dashboard.remaining_items == 2
    assert result.dashboard.total_speed_bps == 50.0
    assert result.dashboard.current_task_id == "active"
    assert result.progress_samples["active"] == ProgressSample(11.0, 150)
    assert [value.task.id for value in snapshots] == original_ids
    with pytest.raises(TypeError):
        result.by_id["other"] = result.by_id["active"]  # type: ignore[index]


def test_patch_task_view_replaces_removes_and_inserts_by_explicit_order_key() -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    scheduler = SchedulerSnapshot((), ("keep", "old"), 3, 0)
    previous_snapshots = [
        snapshot(
            "old",
            TaskStatus.QUEUED,
            created_at=now + timedelta(seconds=2),
            completed=0,
            total=4,
        ),
        snapshot(
            "gone",
            TaskStatus.COMPLETED,
            created_at=now + timedelta(seconds=1),
            completed=2,
            total=2,
        ),
        snapshot(
            "keep",
            TaskStatus.QUEUED,
            created_at=now,
            completed=1,
            total=3,
        ),
    ]
    previous = build_task_view(
        previous_snapshots,
        scheduler_state=scheduler,
        queue_positions={"old": 2, "keep": 1},
        sampled_at=10.0,
        previous_samples={},
    )
    replacement = replace(previous_snapshots[0], completed_items=3)
    inserted = snapshot(
        "new",
        TaskStatus.COMPLETED,
        created_at=now + timedelta(seconds=3),
        completed=5,
        total=5,
    )

    patch = build_task_patch(
        [replacement, inserted],
        requested_ids=("old", "gone", "new"),
        scheduler_state=scheduler,
        queue_positions={"old": 2, "keep": 1},
        sampled_at=11.0,
        previous_samples=previous.progress_samples,
    )
    result = patch_task_view(previous, patch)
    expected = build_task_view(
        [inserted, replacement, previous_snapshots[2]],
        scheduler_state=scheduler,
        queue_positions={"old": 2, "keep": 1},
        sampled_at=11.0,
        previous_samples=previous.progress_samples,
    )

    assert patch.removed_ids == frozenset({"gone"})
    assert set(patch.replacements) == {"old", "new"}
    assert [summary.id for summary in result.ordered] == ["new", "old", "keep"]
    assert result.by_id["keep"] is previous.by_id["keep"]
    assert result.dashboard == expected.dashboard
    assert result.order_keys == expected.order_keys
