import asyncio
from datetime import UTC, datetime

import pytest

from telegram_downloader.background import BackgroundModeController
from telegram_downloader.domain import (
    MediaItem,
    MediaKind,
    PauseReason,
    ScanFilters,
    SourceKind,
    TaskRecord,
    TaskStatus,
)
from telegram_downloader.download_schedule import DownloadScheduleController
from telegram_downloader.downloader import DownloadPaused
from telegram_downloader.notifications import NotificationBatcher, NotificationPayload
from telegram_downloader.repository import TaskRepository
from telegram_downloader.scheduler import DownloadScheduler
from telegram_downloader.settings import DownloadScheduleSettings


def seed_task(repository: TaskRepository, tmp_path) -> TaskRecord:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    task = TaskRecord(
        "task-1",
        SourceKind.CHANNEL_OR_GROUP,
        "peer",
        "来源",
        "https://t.me/peer",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 1),
        TaskStatus.QUEUED,
        now,
        now,
    )
    item = MediaItem(
        "item-1",
        task.id,
        "peer",
        1,
        None,
        "media-1",
        MediaKind.VIDEO,
        "video.mp4",
        tmp_path / "downloads" / "video.mp4",
        1,
        now,
    )
    repository.create_task(task, [item])
    return task


@pytest.mark.asyncio
async def test_closed_schedule_blocks_restored_queue_until_open(tmp_path) -> None:
    repository = TaskRepository(tmp_path / "tasks.sqlite3")
    repository.initialize()
    task = seed_task(repository, tmp_path)
    calls: list[str] = []

    class Downloader:
        async def download(self, item, should_pause):
            calls.append(item.id)

    scheduler = DownloadScheduler(repository, Downloader())
    schedule = DownloadScheduleController(
        lambda: scheduler,
        DownloadScheduleSettings(True, (0,), 9 * 60, 17 * 60),
        now=lambda: datetime(2026, 8, 24, 8, tzinfo=UTC),
    )
    await schedule.start()
    queued = asyncio.create_task(scheduler.run_task(task.id))
    await wait_until(lambda: scheduler.snapshot().queued_task_ids == (task.id,))

    assert calls == []

    await schedule.reconfigure(DownloadScheduleSettings())
    await queued
    await schedule.shutdown()

    assert calls == ["item-1"]
    assert repository.get_task(task.id).status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_schedule_pause_reason_survives_repository_restart(tmp_path) -> None:
    repository = TaskRepository(tmp_path / "tasks.sqlite3")
    repository.initialize()
    task = seed_task(repository, tmp_path)
    entered = asyncio.Event()

    class Downloader:
        async def download(self, item, should_pause):
            entered.set()
            while not should_pause():
                await asyncio.sleep(0)
            raise DownloadPaused("paused")

    scheduler = DownloadScheduler(repository, Downloader())
    schedule = DownloadScheduleController(
        lambda: scheduler,
        DownloadScheduleSettings(),
        now=lambda: datetime(2026, 8, 24, 8, tzinfo=UTC),
    )
    await schedule.start()
    active = asyncio.create_task(scheduler.run_task(task.id))
    await entered.wait()

    await schedule.reconfigure(
        DownloadScheduleSettings(True, (0,), 9 * 60, 17 * 60)
    )
    await active
    await schedule.shutdown()

    reopened = TaskRepository(repository.database)
    saved = reopened.get_task(task.id)
    assert saved.status is TaskStatus.PAUSED
    assert saved.pause_reason is PauseReason.SCHEDULE


@pytest.mark.asyncio
async def test_background_schedule_notification_restart_contract(tmp_path) -> None:
    repository = TaskRepository(tmp_path / "tasks.sqlite3")
    repository.initialize()
    task = seed_task(repository, tmp_path)
    download_calls: list[str] = []
    batcher = NotificationBatcher(window_seconds=5)

    def publish(event) -> None:
        batcher.record(event, now=0)

    class Downloader:
        async def download(self, item, should_pause):
            assert should_pause() is False
            download_calls.append(item.id)

    scheduler = DownloadScheduler(repository, Downloader(), publish=publish)
    schedule = DownloadScheduleController(
        lambda: scheduler,
        DownloadScheduleSettings(True, (0,), 9 * 60, 17 * 60),
        now=lambda: datetime(2026, 8, 24, 8, tzinfo=UTC),
    )

    class Window:
        visible = True

        def hide(self) -> None:
            self.visible = False

        def show(self) -> None:
            self.visible = True

        def raise_(self) -> None:
            pass

        def activateWindow(self) -> None:
            pass

        def show_route(self, _route) -> None:
            pass

    class Tray:
        available = True

        def __init__(self) -> None:
            self.payloads: list[NotificationPayload] = []
            self.hidden = False

        def show_close_hint(self) -> None:
            pass

        def show_notification(self, payload: NotificationPayload) -> None:
            self.payloads.append(payload)

        def hide(self) -> None:
            self.hidden = True

    window = Window()
    tray = Tray()
    exits: list[str] = []
    background = BackgroundModeController(window, tray, lambda: exits.append("exit"))

    await schedule.start()
    assert background.handle_window_close() is True
    queued = asyncio.create_task(scheduler.run_task(task.id))
    await wait_until(lambda: scheduler.snapshot().queued_task_ids == (task.id,))

    assert window.visible is False
    assert exits == []
    assert download_calls == []

    await schedule.reconfigure(DownloadScheduleSettings())
    await queued
    for payload in batcher.flush_due(now=5):
        background.show_notification(payload)

    assert download_calls == ["item-1"]
    assert tray.payloads[-1].body == "1 个下载任务已完成"

    await schedule.shutdown()
    background.request_exit()
    reopened = TaskRepository(repository.database)

    assert tray.hidden is True
    assert exits == ["exit"]
    assert reopened.get_task(task.id).status is TaskStatus.COMPLETED


async def wait_until(predicate, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")
