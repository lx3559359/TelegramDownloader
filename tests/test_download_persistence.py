import asyncio
import threading

import pytest

from telegram_downloader.domain import ItemStatus
from telegram_downloader.download_persistence import ThreadedDownloadPersistence
from telegram_downloader.repository import ItemProgressUpdate


class ThreadRecordingRepository:
    def __init__(self) -> None:
        self.progress: list[ItemProgressUpdate] = []

    def update_item_progress(
        self,
        item_id: str,
        downloaded_bytes: int,
        status: ItemStatus,
        error: str | None = None,
        retry_count: int | None = None,
    ) -> None:
        self.progress.append(
            ItemProgressUpdate(
                item_id,
                downloaded_bytes,
                status,
                error,
                retry_count,
            )
        )

    @staticmethod
    def record_thread() -> int:
        return threading.get_ident()


@pytest.mark.asyncio
async def test_threaded_persistence_runs_repository_work_off_loop() -> None:
    repository = ThreadRecordingRepository()
    persistence = ThreadedDownloadPersistence(repository)
    loop_thread = threading.get_ident()

    worker_thread = await persistence.execute(repository.record_thread)

    assert worker_thread != loop_thread


@pytest.mark.asyncio
async def test_threaded_persistence_accepts_future_returning_runner() -> None:
    repository = ThreadRecordingRepository()
    loop = asyncio.get_running_loop()

    def runner(operation):
        future = loop.create_future()
        future.set_result(operation())
        return future

    persistence = ThreadedDownloadPersistence(repository, runner=runner)

    assert await persistence.execute(lambda: "done") == "done"


@pytest.mark.asyncio
async def test_threaded_persistence_serializes_concurrent_operations() -> None:
    repository = ThreadRecordingRepository()
    persistence = ThreadedDownloadPersistence(repository)
    started = threading.Event()
    release = threading.Event()
    events: list[str] = []

    def first() -> None:
        events.append("first-started")
        started.set()
        release.wait(timeout=1)
        events.append("first-finished")

    def second() -> None:
        events.append("second")

    first_task = asyncio.create_task(persistence.execute(first))
    assert await asyncio.to_thread(started.wait, 1) is True
    second_task = asyncio.create_task(persistence.execute(second))
    await asyncio.sleep(0.02)

    assert events == ["first-started"]
    release.set()
    await asyncio.gather(first_task, second_task)
    assert events == ["first-started", "first-finished", "second"]


@pytest.mark.asyncio
async def test_threaded_persistence_finishes_started_write_before_cancelling() -> None:
    repository = ThreadRecordingRepository()
    persistence = ThreadedDownloadPersistence(repository)
    started = threading.Event()
    release = threading.Event()
    write_finished = False

    def blocking_write() -> None:
        nonlocal write_finished
        started.set()
        release.wait(timeout=1)
        write_finished = True

    operation = asyncio.create_task(persistence.execute(blocking_write))
    assert await asyncio.to_thread(started.wait, 1) is True
    operation.cancel()
    await asyncio.sleep(0)

    assert write_finished is False
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert write_finished is True


@pytest.mark.asyncio
async def test_threaded_persistence_records_progress_and_rejects_after_close() -> None:
    repository = ThreadRecordingRepository()
    persistence = ThreadedDownloadPersistence(repository)
    update = ItemProgressUpdate("media", 7, ItemStatus.DOWNLOADING)

    await persistence.record_progress(update)
    await persistence.close()

    assert repository.progress == [update]
    with pytest.raises(RuntimeError, match="已关闭"):
        await persistence.record_progress(update)
    with pytest.raises(RuntimeError, match="已关闭"):
        await persistence.execute(repository.record_thread)
