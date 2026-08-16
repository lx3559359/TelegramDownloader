import asyncio
from datetime import UTC, datetime, timedelta

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
from telegram_downloader.downloader import DownloadPaused
from telegram_downloader.paths import PortablePaths
from telegram_downloader.repository import TaskRepository
from telegram_downloader.resource_control import AsyncBandwidthLimiter
from telegram_downloader.scheduler import DownloadScheduler


async def wait_until(predicate, attempts: int = 500) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def seed_queue(repository: TaskRepository, paths: PortablePaths) -> list[str]:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    filters = ScanFilters(
        now - timedelta(days=1),
        now,
        frozenset({MediaKind.DOCUMENT}),
        500,
    )
    task_ids: list[str] = []
    for task_index in range(50):
        task_id = f"task-{task_index:02d}"
        task_ids.append(task_id)
        created_at = now + timedelta(seconds=task_index)
        task = TaskRecord(
            task_id,
            SourceKind.CHANNEL_OR_GROUP,
            f"peer-{task_index}",
            f"Synthetic {task_index}",
            f"https://t.me/synthetic/{task_index}",
            filters,
            TaskStatus.QUEUED,
            created_at,
            created_at,
        )
        items = [
            MediaItem(
                f"{task_id}-item-{item_index:02d}",
                task_id,
                task.source_ref,
                task_index * 100 + item_index + 1,
                None,
                f"media-{task_index}-{item_index}",
                MediaKind.DOCUMENT,
                f"item-{item_index}.bin",
                paths.downloads / task_id / f"item-{item_index}.bin",
                1,
                created_at,
            )
            for item_index in range(10)
        ]
        repository.create_task(task, items)
    return task_ids


@pytest.mark.asyncio
async def test_fifty_task_queue_survives_priority_duplicates_pauses_and_live_limits(
    tmp_path,
) -> None:
    paths = PortablePaths(tmp_path / "stress-root")
    paths.ensure_layout()
    repository = TaskRepository(paths.database)
    repository.initialize()
    task_ids = seed_queue(repository, paths)
    first_release = asyncio.Event()
    wave_full = asyncio.Event()
    wave_release = asyncio.Event()
    first_blocked = False
    active_files = 0
    max_active_files = 0
    active_tasks: set[str] = set()
    max_active_tasks = 0
    started_tasks: list[str] = []

    class StressDownloader:
        async def download(self, item, should_pause):
            nonlocal first_blocked
            nonlocal active_files
            nonlocal max_active_files
            nonlocal max_active_tasks
            if item.task_id not in started_tasks:
                started_tasks.append(item.task_id)
            active_files += 1
            active_tasks.add(item.task_id)
            max_active_files = max(max_active_files, active_files)
            max_active_tasks = max(max_active_tasks, len(active_tasks))
            try:
                if item.task_id == task_ids[0] and not first_blocked:
                    first_blocked = True
                    await first_release.wait()
                elif not wave_release.is_set():
                    if active_files >= 5:
                        wave_full.set()
                    await wave_release.wait()
                else:
                    await asyncio.sleep(0)
                if should_pause():
                    raise DownloadPaused("paused")
            finally:
                active_files -= 1
                if active_files == 0 or all(
                    task_id != item.task_id for task_id in active_tasks - {item.task_id}
                ):
                    active_tasks.discard(item.task_id)

    bandwidth = AsyncBandwidthLimiter(256)
    scheduler = DownloadScheduler(
        repository,
        StressDownloader(),
        concurrency=1,
        bandwidth=bandwidth,
    )
    operations = [
        asyncio.create_task(scheduler.run_task(task_id)) for task_id in task_ids
    ]
    await wait_until(lambda: scheduler.active_task_id == task_ids[0])
    await wait_until(lambda: first_blocked)
    duplicates = [
        asyncio.create_task(scheduler.run_task(task_ids[0])) for _ in range(5)
    ]

    prioritized = task_ids[-10:]
    for task_id in prioritized:
        assert repository.prioritize_task(task_id) is True
        assert scheduler.prioritize_task(task_id) is True
    assert scheduler.snapshot().queued_task_ids[0] == prioritized[-1]
    queued_paused = task_ids[20]
    scheduler.pause_task(queued_paused)
    scheduler.pause_task(task_ids[0])
    scheduler.configure_resources(5, 0)
    first_release.set()

    await wave_full.wait()
    scheduler.configure_resources(1, 256)
    assert scheduler.snapshot().concurrency == 1
    assert scheduler.snapshot().speed_limit_kib == 256
    wave_release.set()
    await wait_until(lambda: active_files <= 1)
    scheduler.configure_resources(5, 0)
    await asyncio.gather(*operations, *duplicates)

    assert started_tasks[1] == prioritized[-1]
    assert max_active_tasks == 1
    assert max_active_files == 5
    assert repository.get_task(task_ids[0]).status is TaskStatus.PAUSED
    assert repository.get_task(queued_paused).status is TaskStatus.PAUSED

    await asyncio.gather(
        scheduler.resume_task(task_ids[0]),
        scheduler.resume_task(queued_paused),
    )
    await scheduler.shutdown()

    tasks = repository.list_tasks()
    assert len(tasks) == 50
    assert all(task.status is TaskStatus.COMPLETED for task in tasks)
    all_items = [item for task_id in task_ids for item in repository.list_items(task_id)]
    assert len(all_items) == 500
    assert len({item.id for item in all_items}) == 500
    assert all(item.status is ItemStatus.COMPLETED for item in all_items)
    assert paths.database.is_relative_to(paths.root)
