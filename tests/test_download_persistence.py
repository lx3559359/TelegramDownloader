import asyncio
import threading

import pytest

from telegram_downloader.domain import ItemStatus
from telegram_downloader.download_persistence import (
    DownloadPersistenceCoordinator,
    ThreadedDownloadPersistence,
)
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


class BatchRepository(ThreadRecordingRepository):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[tuple[ItemProgressUpdate, ...]] = []
        self.events: list[str] = []
        self.batch_written = threading.Event()
        self.batch_failure: BaseException | None = None

    def update_item_progresses(
        self,
        updates: tuple[ItemProgressUpdate, ...],
    ) -> None:
        if self.batch_failure is not None:
            raise self.batch_failure
        batch = tuple(updates)
        self.batches.append(batch)
        self.events.append(
            "batch:" + ",".join(f"{update.item_id}={update.downloaded_bytes}" for update in batch)
        )
        self.batch_written.set()


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


@pytest.mark.asyncio
async def test_coordinator_keeps_only_latest_progress_until_drain() -> None:
    repository = BatchRepository()
    persistence = DownloadPersistenceCoordinator(repository)

    for downloaded in range(1, 21):
        await persistence.record_progress(
            ItemProgressUpdate("media", downloaded, ItemStatus.DOWNLOADING)
        )
    await persistence.drain()

    assert repository.batches == [
        (ItemProgressUpdate("media", 20, ItemStatus.DOWNLOADING),)
    ]
    await persistence.close()


@pytest.mark.asyncio
async def test_coordinator_does_not_extend_an_active_flush_window() -> None:
    repository = BatchRepository()
    persistence = DownloadPersistenceCoordinator(repository, flush_interval=0.15)

    await persistence.record_progress(
        ItemProgressUpdate("media", 1, ItemStatus.DOWNLOADING)
    )
    await asyncio.sleep(0.1)
    await persistence.record_progress(
        ItemProgressUpdate("media", 2, ItemStatus.DOWNLOADING)
    )

    assert await asyncio.to_thread(repository.batch_written.wait, 0.09) is True
    assert repository.batches[0] == (
        ItemProgressUpdate("media", 2, ItemStatus.DOWNLOADING),
    )
    await persistence.close()


@pytest.mark.asyncio
async def test_coordinator_batches_multiple_media_in_one_transaction() -> None:
    repository = BatchRepository()
    persistence = DownloadPersistenceCoordinator(repository)

    for index in range(5):
        await persistence.record_progress(
            ItemProgressUpdate(f"media-{index}", index, ItemStatus.DOWNLOADING)
        )
    await persistence.drain()

    assert len(repository.batches) == 1
    assert {update.item_id for update in repository.batches[0]} == {
        f"media-{index}" for index in range(5)
    }
    await persistence.close()


@pytest.mark.asyncio
async def test_coordinator_item_barrier_flushes_only_target_before_command() -> None:
    repository = BatchRepository()
    persistence = DownloadPersistenceCoordinator(repository)
    await persistence.record_progress(
        ItemProgressUpdate("a", 10, ItemStatus.DOWNLOADING)
    )
    await persistence.record_progress(
        ItemProgressUpdate("b", 20, ItemStatus.DOWNLOADING)
    )

    await persistence.execute(
        lambda: repository.events.append("terminal:a"),
        flush_item_ids=("a",),
    )

    assert repository.events == ["batch:a=10", "terminal:a"]
    await persistence.drain()
    assert repository.events == ["batch:a=10", "terminal:a", "batch:b=20"]
    await persistence.close()


@pytest.mark.asyncio
async def test_coordinator_global_barrier_flushes_all_before_read() -> None:
    repository = BatchRepository()
    persistence = DownloadPersistenceCoordinator(repository)
    await persistence.record_progress(
        ItemProgressUpdate("a", 10, ItemStatus.DOWNLOADING)
    )
    await persistence.record_progress(
        ItemProgressUpdate("b", 20, ItemStatus.DOWNLOADING)
    )

    result = await persistence.execute(
        lambda: repository.events.append("read") or "result",
        flush_all=True,
    )

    assert result == "result"
    assert repository.events == ["batch:a=10,b=20", "read"]
    await persistence.close()


@pytest.mark.asyncio
async def test_coordinator_terminal_future_waits_for_repository_command() -> None:
    repository = BatchRepository()
    persistence = DownloadPersistenceCoordinator(repository)
    terminal_started = threading.Event()
    terminal_release = threading.Event()

    def terminal() -> None:
        terminal_started.set()
        terminal_release.wait(timeout=1)
        repository.events.append("terminal")

    await persistence.record_progress(
        ItemProgressUpdate("media", 5, ItemStatus.DOWNLOADING)
    )
    operation = asyncio.create_task(
        persistence.execute(terminal, flush_item_ids=("media",))
    )
    assert await asyncio.to_thread(terminal_started.wait, 1) is True
    assert operation.done() is False

    terminal_release.set()
    await operation
    assert repository.events == ["batch:media=5", "terminal"]
    await persistence.close()


@pytest.mark.asyncio
async def test_coordinator_cancellation_does_not_cancel_terminal_command() -> None:
    repository = BatchRepository()
    persistence = DownloadPersistenceCoordinator(repository)
    terminal_started = threading.Event()
    terminal_release = threading.Event()

    def terminal() -> None:
        terminal_started.set()
        terminal_release.wait(timeout=1)
        repository.events.append("terminal")

    operation = asyncio.create_task(persistence.execute(terminal))
    assert await asyncio.to_thread(terminal_started.wait, 1) is True
    operation.cancel()
    await asyncio.sleep(0)
    terminal_release.set()

    with pytest.raises(asyncio.CancelledError):
        await operation
    assert repository.events == ["terminal"]
    await persistence.close()


@pytest.mark.asyncio
async def test_coordinator_failure_is_sticky_and_notifies_once() -> None:
    repository = BatchRepository()
    failure = RuntimeError("synthetic storage failure")
    repository.batch_failure = failure
    persistence = DownloadPersistenceCoordinator(repository)
    notified: list[BaseException] = []
    persistence.set_fault_handler(notified.append)
    update = ItemProgressUpdate("media", 1, ItemStatus.DOWNLOADING)
    await persistence.record_progress(update)

    with pytest.raises(RuntimeError) as first:
        await persistence.drain()
    with pytest.raises(RuntimeError) as second:
        await persistence.record_progress(update)
    with pytest.raises(RuntimeError) as third:
        await persistence.execute(lambda: None)

    assert first.value is failure
    assert second.value is failure
    assert third.value is failure
    assert notified == [failure]
    with pytest.raises(RuntimeError) as closing:
        await persistence.close()
    assert closing.value is failure


@pytest.mark.asyncio
async def test_coordinator_close_flushes_last_progress_and_rejects_new_work() -> None:
    repository = BatchRepository()
    persistence = DownloadPersistenceCoordinator(repository)
    update = ItemProgressUpdate("media", 9, ItemStatus.DOWNLOADING)
    await persistence.record_progress(update)

    await persistence.close()
    await persistence.close()

    assert repository.batches == [(update,)]
    with pytest.raises(RuntimeError, match="已关闭"):
        await persistence.record_progress(update)
    with pytest.raises(RuntimeError, match="已关闭"):
        await persistence.execute(lambda: None)
