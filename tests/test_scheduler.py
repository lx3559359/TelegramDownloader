import asyncio
from types import SimpleNamespace

import pytest

from telegram_downloader.domain import ItemStatus, TaskStatus
from telegram_downloader.downloader import DownloadPaused, InsufficientSpaceError
from telegram_downloader.gateway import FloodWaitError, TransientNetworkError
from telegram_downloader.scheduler import DownloadScheduler, RetryPolicy


class Repo:
    def __init__(self, count: int = 1):
        self.items = [
            SimpleNamespace(
                id=f"i{index}",
                retry_count=0,
                downloaded_bytes=0,
                status=ItemStatus.QUEUED,
                last_error=None,
            )
            for index in range(count)
        ]
        self.item_updates = []
        self.task_updates = []
        self.task_errors = []
        self.recovered = False

    def list_items(self, task_id, statuses=None):
        if statuses is None:
            return self.items
        return [item for item in self.items if item.status in statuses]

    def update_item_progress(
        self,
        item_id,
        downloaded_bytes,
        status,
        error=None,
        retry_count=None,
    ):
        self.item_updates.append((item_id, status, retry_count, error))
        selected = next(item for item in self.items if item.id == item_id)
        selected.downloaded_bytes = downloaded_bytes
        selected.status = status
        selected.last_error = error
        if retry_count is not None:
            selected.retry_count = retry_count

    def update_task_status(self, task_id, status, error=None):
        self.task_updates.append(status)
        self.task_errors.append(error)

    def recover_interrupted(self):
        self.recovered = True


@pytest.mark.asyncio
async def test_flood_wait_sleeps_exact_seconds_then_retries() -> None:
    attempts, sleeps = 0, []

    class Downloader:
        async def download(self, item, should_pause):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise FloodWaitError(4)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    repo = Repo()
    scheduler = DownloadScheduler(
        repo,
        Downloader(),
        concurrency=1,
        retry=RetryPolicy(3, 1),
        sleep=fake_sleep,
    )

    await scheduler.run_task("t")

    assert attempts == 2
    assert sleeps == [4]
    assert repo.items[0].retry_count == 0
    assert repo.task_updates[-1] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_transient_error_uses_exponential_backoff_then_marks_failed() -> None:
    class Downloader:
        async def download(self, item, should_pause):
            raise TransientNetworkError("offline")

    sleeps = []
    repo = Repo()
    scheduler = DownloadScheduler(
        repo,
        Downloader(),
        concurrency=1,
        retry=RetryPolicy(3, 2),
        sleep=lambda value: _record_sleep(sleeps, value),
    )

    await scheduler.run_task("t")

    assert sleeps == [2, 4]
    assert repo.item_updates[-1][1] is ItemStatus.FAILED
    assert repo.item_updates[-1][2] == 3
    assert repo.task_updates[-1] is TaskStatus.PARTIAL_FAILURE


async def _record_sleep(values, value):
    values.append(value)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_scheduler_never_exceeds_configured_concurrency() -> None:
    active = 0
    peak = 0
    release = asyncio.Event()

    class Downloader:
        async def download(self, item, should_pause):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await release.wait()
            active -= 1

    repo = Repo(count=4)
    scheduler = DownloadScheduler(repo, Downloader(), concurrency=2)
    task = asyncio.create_task(scheduler.run_task("t"))
    for _ in range(20):
        if peak == 2:
            break
        await asyncio.sleep(0)
    assert peak == 2
    release.set()
    await task


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [DownloadPaused("paused"), InsufficientSpaceError("disk")])
async def test_pause_conditions_do_not_retry(error) -> None:
    attempts = 0

    class Downloader:
        async def download(self, item, should_pause):
            nonlocal attempts
            attempts += 1
            raise error

    repo = Repo()
    scheduler = DownloadScheduler(repo, Downloader())

    await scheduler.run_task("t")

    assert attempts == 1
    assert repo.task_updates[-1] is TaskStatus.PAUSED


def test_recover_delegates_to_repository() -> None:
    repo = Repo()
    scheduler = DownloadScheduler(repo, object())

    scheduler.recover()

    assert repo.recovered is True


@pytest.mark.asyncio
async def test_unknown_download_error_does_not_persist_exception_text() -> None:
    class Downloader:
        async def download(self, item, should_pause):
            raise RuntimeError("api-secret-in-third-party-error")

    repo = Repo()
    scheduler = DownloadScheduler(repo, Downloader())

    await scheduler.run_task("t")

    stored_error = repo.item_updates[-1][3]
    assert stored_error == "RuntimeError"
    assert "api-secret" not in stored_error


@pytest.mark.asyncio
async def test_partial_failure_preserves_safe_item_error_on_task() -> None:
    class Downloader:
        async def download(self, item, should_pause):
            raise TransientNetworkError("Telegram 网络连接失败")

    repo = Repo()
    scheduler = DownloadScheduler(
        repo,
        Downloader(),
        retry=RetryPolicy(attempts=1, base_delay=0),
    )

    await scheduler.run_task("t")

    assert repo.task_updates[-1] is TaskStatus.PARTIAL_FAILURE
    assert repo.task_errors[-1] == "Telegram 网络连接失败"
