import asyncio
import time
from types import SimpleNamespace

import pytest

from telegram_downloader.domain import ItemStatus, PauseReason, TaskStatus
from telegram_downloader.download_persistence import DownloadPersistenceCoordinator
from telegram_downloader.downloader import DownloadPaused, InsufficientSpaceError
from telegram_downloader.gateway import FloodWaitError, TransientNetworkError
from telegram_downloader.maintenance_activity import (
    ActivityKind,
    MaintenanceBusyError,
    OperationActivityRegistry,
)
from telegram_downloader.notifications import EventKind
from telegram_downloader.resource_control import AsyncBandwidthLimiter
from telegram_downloader.scheduler import (
    DownloadScheduler,
    RetryPolicy,
    SchedulerSnapshot,
)


class Repo:
    def __init__(self, count: int = 1):
        self.items = [
            SimpleNamespace(
                id=f"i{index}",
                task_id="t",
                retry_count=0,
                downloaded_bytes=0,
                status=ItemStatus.QUEUED,
                last_error=None,
            )
            for index in range(count)
        ]
        self.item_updates = []
        self.task_updates = []
        self.bulk_task_updates = []
        self.task_errors = []
        self.task_pause_reasons = {}
        self.recovered = False
        self.list_item_calls = 0
        self.get_item_calls = []
        self.recomputed = []

    def list_items(self, task_id, statuses=None):
        self.list_item_calls += 1
        if statuses is None:
            return self.items
        return [item for item in self.items if item.status in statuses]

    def get_item(self, item_id):
        self.get_item_calls.append(item_id)
        return next(item for item in self.items if item.id == item_id)

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

    def update_task_status(self, task_id, status, error=None, *, pause_reason=None):
        self.task_updates.append(status)
        self.task_errors.append(error)
        self.task_pause_reasons[task_id] = (
            pause_reason or PauseReason.USER if status is TaskStatus.PAUSED else None
        )

    def update_task_statuses(
        self,
        task_ids,
        status,
        *,
        allowed,
        error=None,
        pause_reason=None,
    ):
        ordered = tuple(dict.fromkeys(task_ids))
        self.bulk_task_updates.append((ordered, status, allowed, error))
        for task_id in ordered:
            self.task_pause_reasons[task_id] = (
                pause_reason or PauseReason.USER if status is TaskStatus.PAUSED else None
            )
        return set(ordered)

    def recover_interrupted(self):
        self.recovered = True

    def recompute_task_status(self, task_id):
        self.recomputed.append(task_id)
        statuses = {item.status for item in self.items}
        if ItemStatus.FAILED in statuses:
            return TaskStatus.PARTIAL_FAILURE
        if ItemStatus.PAUSED in statuses:
            return TaskStatus.PAUSED
        if statuses == {ItemStatus.COMPLETED}:
            return TaskStatus.COMPLETED
        return TaskStatus.QUEUED


class QueueRepo:
    def __init__(self, task_ids: tuple[str, ...]) -> None:
        self.items = {
            task_id: SimpleNamespace(
                id=f"{task_id}-item",
                task_id=task_id,
                retry_count=0,
                downloaded_bytes=0,
                status=ItemStatus.QUEUED,
                last_error=None,
            )
            for task_id in task_ids
        }
        self.order = {task_id: index for index, task_id in enumerate(task_ids)}
        self.priorities = dict.fromkeys(task_ids, 0)
        self.task_statuses = dict.fromkeys(task_ids, TaskStatus.QUEUED)
        self.pause_reasons = dict.fromkeys(task_ids)
        self.task_updates: list[tuple[str, TaskStatus]] = []
        self.bulk_task_updates = []
        self.cleared: list[str] = []

    def list_items(self, task_id, statuses=None):
        selected = [self.items[task_id]]
        if statuses is None:
            return selected
        return [item for item in selected if item.status in statuses]

    def get_item(self, item_id):
        return next(item for item in self.items.values() if item.id == item_id)

    def update_item_progress(
        self,
        item_id,
        downloaded_bytes,
        status,
        error=None,
        retry_count=None,
    ):
        item = self.get_item(item_id)
        item.downloaded_bytes = downloaded_bytes
        item.status = status
        item.last_error = error
        if retry_count is not None:
            item.retry_count = retry_count

    def update_task_status(self, task_id, status, error=None, *, pause_reason=None):
        self.task_statuses[task_id] = status
        self.pause_reasons[task_id] = (
            pause_reason or PauseReason.USER if status is TaskStatus.PAUSED else None
        )
        self.task_updates.append((task_id, status))

    def update_task_statuses(
        self,
        task_ids,
        status,
        *,
        allowed,
        error=None,
        pause_reason=None,
    ):
        ordered = tuple(dict.fromkeys(task_ids))
        accepted = {
            task_id
            for task_id in ordered
            if self.task_statuses.get(task_id) in allowed
        }
        for task_id in accepted:
            self.task_statuses[task_id] = status
            self.pause_reasons[task_id] = (
                pause_reason or PauseReason.USER if status is TaskStatus.PAUSED else None
            )
        self.bulk_task_updates.append((ordered, status, allowed, error))
        return accepted

    def list_paused_by_reason(self, reason):
        return [
            SimpleNamespace(id=task_id)
            for task_id, status in self.task_statuses.items()
            if status is TaskStatus.PAUSED and self.pause_reasons[task_id] is reason
        ]

    def recover_interrupted(self):
        return None

    def recompute_task_status(self, task_id):
        status = self.items[task_id].status
        if status is ItemStatus.COMPLETED:
            result = TaskStatus.COMPLETED
        elif status is ItemStatus.PAUSED:
            result = TaskStatus.PAUSED
        elif status is ItemStatus.FAILED:
            result = TaskStatus.PARTIAL_FAILURE
        else:
            result = TaskStatus.QUEUED
        self.task_statuses[task_id] = result
        return result

    def task_dispatch_key(self, task_id):
        return (-self.priorities[task_id], self.order[task_id], task_id)

    def clear_task_priority(self, task_id):
        self.priorities[task_id] = 0
        self.cleared.append(task_id)
        return True


class SlowQueueRepo(QueueRepo):
    def __init__(self, task_ids: tuple[str, ...], delay: float = 0.05) -> None:
        super().__init__(task_ids)
        self.delay = delay

    def _wait(self) -> None:
        time.sleep(self.delay)

    def list_items(self, task_id, statuses=None):
        self._wait()
        return super().list_items(task_id, statuses)

    def get_item(self, item_id):
        self._wait()
        return super().get_item(item_id)

    def update_item_progress(
        self,
        item_id,
        downloaded_bytes,
        status,
        error=None,
        retry_count=None,
    ):
        self._wait()
        return super().update_item_progress(
            item_id,
            downloaded_bytes,
            status,
            error,
            retry_count,
        )

    def update_item_progresses(self, updates):
        self._wait()
        for update in updates:
            QueueRepo.update_item_progress(
                self,
                update.item_id,
                update.downloaded_bytes,
                update.status,
                update.error,
                update.retry_count,
            )

    def update_task_status(self, task_id, status, error=None, *, pause_reason=None):
        self._wait()
        return super().update_task_status(
            task_id,
            status,
            error,
            pause_reason=pause_reason,
        )

    def recompute_task_status(self, task_id):
        self._wait()
        return super().recompute_task_status(task_id)

    def task_dispatch_key(self, task_id):
        self._wait()
        return super().task_dispatch_key(task_id)

    def clear_task_priority(self, task_id):
        self._wait()
        return super().clear_task_priority(task_id)


class RecordingPersistence:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.fault_handler = None

    def set_fault_handler(self, handler) -> None:
        self.fault_handler = handler

    async def record_progress(self, update) -> None:
        raise AssertionError(f"unexpected progress: {update}")

    async def execute(self, operation, **_kwargs):
        return operation()

    async def drain(self) -> None:
        self.events.append("drain")

    async def close(self) -> None:
        self.events.append("close")


class MultiQueueRepo(QueueRepo):
    def __init__(self, counts: dict[str, int]) -> None:
        super().__init__(tuple(counts))
        self.items = {
            f"{task_id}-item-{index}": SimpleNamespace(
                id=f"{task_id}-item-{index}",
                task_id=task_id,
                retry_count=0,
                downloaded_bytes=0,
                status=ItemStatus.QUEUED,
                last_error=None,
            )
            for task_id, count in counts.items()
            for index in range(count)
        }

    def list_items(self, task_id, statuses=None):
        selected = [item for item in self.items.values() if item.task_id == task_id]
        if statuses is None:
            return selected
        return [item for item in selected if item.status in statuses]

    def get_item(self, item_id):
        return self.items[item_id]

    def recompute_task_status(self, task_id):
        statuses = {item.status for item in self.list_items(task_id)}
        if ItemStatus.FAILED in statuses:
            result = TaskStatus.PARTIAL_FAILURE
        elif ItemStatus.PAUSED in statuses:
            result = TaskStatus.PAUSED
        elif statuses == {ItemStatus.COMPLETED}:
            result = TaskStatus.COMPLETED
        else:
            result = TaskStatus.QUEUED
        self.task_statuses[task_id] = result
        return result


async def wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if predicate():
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"condition was not reached within {timeout:.1f}s")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_closed_admission_keeps_task_queued_until_opened() -> None:
    repo = QueueRepo(("a",))
    entered: list[str] = []

    class Downloader:
        async def download(self, item, should_pause):
            entered.append(item.task_id)

    scheduler = DownloadScheduler(repo, Downloader(), concurrency=1)
    scheduler.set_admission_open(False)
    queued = asyncio.create_task(scheduler.run_task("a"))
    await wait_until(lambda: scheduler.snapshot().queued_task_ids == ("a",))

    assert scheduler.active_task_id is None
    assert entered == []

    scheduler.set_admission_open(True)
    await queued

    assert entered == ["a"]
    assert repo.task_statuses["a"] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_maintenance_busy_preserves_queued_download_state() -> None:
    repo = QueueRepo(("a",))
    entered: list[str] = []

    class Downloader:
        async def download(self, item, should_pause):
            entered.append(item.task_id)

    activity = OperationActivityRegistry()
    token = activity.try_track_maintenance(ActivityKind.STORAGE_CLEANUP)
    assert token is not None
    scheduler = DownloadScheduler(repo, Downloader(), activity=activity)

    try:
        with pytest.raises(MaintenanceBusyError, match="存储维护正在收尾"):
            await scheduler.run_task("a")
    finally:
        token.release()

    assert entered == []
    assert repo.cleared == []
    assert repo.task_statuses["a"] is TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_schedule_open_resumes_only_schedule_paused_tasks() -> None:
    repo = QueueRepo(("user", "clock"))
    repo.task_statuses.update(
        {"user": TaskStatus.PAUSED, "clock": TaskStatus.PAUSED}
    )
    repo.pause_reasons.update(
        {"user": PauseReason.USER, "clock": PauseReason.SCHEDULE}
    )
    entered: list[str] = []

    class Downloader:
        async def download(self, item, should_pause):
            entered.append(item.task_id)

    scheduler = DownloadScheduler(repo, Downloader())

    resumed = await scheduler.set_schedule_open(True)

    assert resumed == {"clock"}
    assert entered == ["clock"]
    assert repo.task_statuses["user"] is TaskStatus.PAUSED
    assert repo.pause_reasons["user"] is PauseReason.USER


@pytest.mark.asyncio
async def test_schedule_close_pauses_active_task_with_schedule_reason() -> None:
    repo = QueueRepo(("active",))
    entered = asyncio.Event()

    class Downloader:
        async def download(self, item, should_pause):
            entered.set()
            while not should_pause():
                await asyncio.sleep(0)
            raise DownloadPaused("paused")

    scheduler = DownloadScheduler(repo, Downloader())
    active = asyncio.create_task(scheduler.run_task("active"))
    await entered.wait()

    await scheduler.set_schedule_open(False)
    await active

    assert repo.task_statuses["active"] is TaskStatus.PAUSED
    assert repo.pause_reasons["active"] is PauseReason.SCHEDULE


@pytest.mark.asyncio
async def test_schedule_close_pauses_every_active_task() -> None:
    repo = QueueRepo(("first", "second"))
    entered: set[str] = set()

    class Downloader:
        async def download(self, item, should_pause):
            entered.add(item.task_id)
            while not should_pause():
                await asyncio.sleep(0)
            raise DownloadPaused("paused")

    scheduler = DownloadScheduler(repo, Downloader(), concurrency=2)
    runs = [
        asyncio.create_task(scheduler.run_task(task_id))
        for task_id in ("first", "second")
    ]
    await wait_until(lambda: entered == {"first", "second"})

    await scheduler.set_schedule_open(False)
    await asyncio.gather(*runs)

    assert repo.task_statuses == {
        "first": TaskStatus.PAUSED,
        "second": TaskStatus.PAUSED,
    }
    assert repo.pause_reasons == {
        "first": PauseReason.SCHEDULE,
        "second": PauseReason.SCHEDULE,
    }


@pytest.mark.asyncio
async def test_schedule_reason_is_cleared_when_active_task_finishes_at_boundary() -> None:
    repo = QueueRepo(("active",))
    entered = asyncio.Event()
    release = asyncio.Event()

    class Downloader:
        async def download(self, item, should_pause):
            entered.set()
            await release.wait()

    scheduler = DownloadScheduler(repo, Downloader())
    active = asyncio.create_task(scheduler.run_task("active"))
    await entered.wait()

    await scheduler.set_schedule_open(False)
    release.set()
    await active

    assert repo.task_statuses["active"] is TaskStatus.COMPLETED
    assert "active" not in scheduler._pause_reasons


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
async def test_retry_state_is_durable_before_sleep_and_resume() -> None:
    events: list[str] = []

    class RecordingRepo(Repo):
        def update_item_progress(
            self,
            item_id,
            downloaded_bytes,
            status,
            error=None,
            retry_count=None,
        ):
            super().update_item_progress(
                item_id,
                downloaded_bytes,
                status,
                error,
                retry_count,
            )
            events.append(f"item:{status.value}")

        def update_task_status(
            self,
            task_id,
            status,
            error=None,
            *,
            pause_reason=None,
        ):
            super().update_task_status(
                task_id,
                status,
                error,
                pause_reason=pause_reason,
            )
            events.append(f"task:{status.value}")

    attempts = 0

    class Downloader:
        async def download(self, item, should_pause):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TransientNetworkError("offline")

    async def record_sleep(_seconds):
        events.append("sleep")

    scheduler = DownloadScheduler(
        RecordingRepo(),
        Downloader(),
        concurrency=1,
        retry=RetryPolicy(2, 1),
        sleep=record_sleep,
    )

    await scheduler.run_task("t")

    assert events == [
        "task:downloading",
        "item:waiting_retry",
        "task:waiting_retry",
        "sleep",
        "task:downloading",
        "item:completed",
        "task:completed",
    ]


@pytest.mark.asyncio
async def test_scheduler_emits_one_terminal_event_after_completed_status() -> None:
    events = []

    class Downloader:
        async def download(self, item, should_pause):
            return None

    scheduler = DownloadScheduler(Repo(), Downloader(), publish=events.append)

    await scheduler.run_task("t")

    assert [(event.kind, event.identity) for event in events] == [
        (EventKind.DOWNLOAD_COMPLETED, "t")
    ]


@pytest.mark.asyncio
async def test_disk_full_emits_once_for_multi_item_task_without_terminal_event() -> None:
    events = []

    class Downloader:
        async def download(self, item, should_pause):
            raise InsufficientSpaceError("private disk path")

    scheduler = DownloadScheduler(
        Repo(count=3),
        Downloader(),
        publish=events.append,
    )

    await scheduler.run_task("t")

    assert [event.kind for event in events] == [EventKind.DISK_FULL]
    assert events[0].identity == "t"
    assert events[0].private_context == ""


@pytest.mark.asyncio
async def test_successful_item_state_updates_use_direct_item_lookup() -> None:
    class Downloader:
        async def download(self, item, should_pause):
            pass

    repo = Repo(count=3)
    scheduler = DownloadScheduler(repo, Downloader(), concurrency=1)

    await scheduler.run_task("t")

    assert repo.list_item_calls == 1
    assert repo.get_item_calls == ["i0", "i1", "i2"]


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
    await wait_until(lambda: peak == 2)
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


@pytest.mark.asyncio
async def test_recover_delegates_to_repository() -> None:
    repo = Repo()
    scheduler = DownloadScheduler(repo, object())

    await scheduler.recover()

    assert repo.recovered is True


def test_shutdown_grace_defaults_to_thirty_seconds_and_must_be_positive() -> None:
    scheduler = DownloadScheduler(Repo(), object())

    assert scheduler.shutdown_grace_seconds == 30.0
    with pytest.raises(ValueError, match="关闭等待时间"):
        DownloadScheduler(Repo(), object(), shutdown_grace_seconds=0)


@pytest.mark.asyncio
async def test_shutdown_lets_pause_aware_download_settle_within_grace() -> None:
    entered = asyncio.Event()
    cancelled = False

    class Downloader:
        async def download(self, item, should_pause):
            nonlocal cancelled
            entered.set()
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                cancelled = True
                raise
            if should_pause():
                raise DownloadPaused("paused")

    repo = Repo()
    scheduler = DownloadScheduler(
        repo,
        Downloader(),
        shutdown_grace_seconds=0.1,
    )
    task = asyncio.create_task(scheduler.run_task("t"))
    await entered.wait()

    await scheduler.shutdown()
    await task

    assert cancelled is False
    assert repo.items[0].status is ItemStatus.PAUSED
    assert repo.task_updates[-1] is TaskStatus.PAUSED


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
    events = []

    class Downloader:
        async def download(self, item, should_pause):
            raise TransientNetworkError("Telegram 网络连接失败")

    repo = Repo()
    scheduler = DownloadScheduler(
        repo,
        Downloader(),
        retry=RetryPolicy(attempts=1, base_delay=0),
        publish=events.append,
    )

    await scheduler.run_task("t")

    assert repo.task_updates[-1] is TaskStatus.PARTIAL_FAILURE
    assert repo.task_errors[-1] == "Telegram 网络连接失败"
    assert [event.kind for event in events] == [EventKind.DOWNLOAD_FAILED]


@pytest.mark.asyncio
async def test_notification_callback_failure_does_not_change_download_outcome() -> None:
    class Downloader:
        async def download(self, item, should_pause):
            return None

    def reject_event(_event) -> None:
        raise RuntimeError("private notification failure")

    repo = Repo()
    scheduler = DownloadScheduler(repo, Downloader(), publish=reject_event)

    await scheduler.run_task("t")

    assert repo.task_updates[-1] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_run_items_downloads_only_deduplicated_requested_media() -> None:
    downloaded = []

    class Downloader:
        async def download(self, item, should_pause):
            downloaded.append(item.id)

    repo = Repo(count=3)
    scheduler = DownloadScheduler(repo, Downloader(), concurrency=1)

    await scheduler.run_items("t", ["i1", "i1"])

    assert downloaded == ["i1"]
    assert repo.items[0].status is ItemStatus.QUEUED
    assert repo.items[1].status is ItemStatus.COMPLETED
    assert repo.items[2].status is ItemStatus.QUEUED
    assert repo.recomputed == ["t"]


@pytest.mark.asyncio
async def test_run_items_validates_every_item_before_starting() -> None:
    downloaded = []

    class Downloader:
        async def download(self, item, should_pause):
            downloaded.append(item.id)

    repo = Repo(count=2)
    repo.items[1].task_id = "other"
    scheduler = DownloadScheduler(repo, Downloader())

    with pytest.raises(ValueError, match="不属于"):
        await scheduler.run_items("t", ["i0", "i1"])

    assert downloaded == []
    assert repo.task_updates == []


@pytest.mark.asyncio
async def test_run_items_rejects_empty_and_nonqueued_selections() -> None:
    repo = Repo()
    scheduler = DownloadScheduler(repo, object())

    with pytest.raises(ValueError, match="至少"):
        await scheduler.run_items("t", [])

    repo.items[0].status = ItemStatus.FAILED
    with pytest.raises(ValueError, match="等待下载"):
        await scheduler.run_items("t", ["i0"])


@pytest.mark.asyncio
async def test_selected_run_reuses_active_task_guard_without_duplicate_downloads() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    downloaded = []

    class Downloader:
        async def download(self, item, should_pause):
            downloaded.append(item.id)
            entered.set()
            await release.wait()

    repo = Repo()
    scheduler = DownloadScheduler(repo, Downloader())
    full_run = asyncio.create_task(scheduler.run_task("t"))
    await entered.wait()
    selected_run = asyncio.create_task(scheduler.run_items("t", ["i0"]))
    await asyncio.sleep(0)

    release.set()
    await asyncio.gather(full_run, selected_run)

    assert downloaded == ["i0"]


@pytest.mark.asyncio
async def test_selected_run_executes_newly_queued_item_after_active_task() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    downloaded = []

    class Downloader:
        async def download(self, item, should_pause):
            downloaded.append(item.id)
            if item.id == "i0":
                entered.set()
                await release.wait()

    repo = Repo(count=2)
    repo.items[1].status = ItemStatus.COMPLETED
    scheduler = DownloadScheduler(repo, Downloader(), concurrency=1)
    full_run = asyncio.create_task(scheduler.run_task("t"))
    await entered.wait()
    repo.items[1].status = ItemStatus.QUEUED
    selected_run = asyncio.create_task(scheduler.run_items("t", ["i1"]))
    await asyncio.sleep(0)

    release.set()
    await asyncio.gather(full_run, selected_run)

    assert downloaded == ["i0", "i1"]


@pytest.mark.asyncio
async def test_scheduler_runs_one_task_and_prioritizes_waiting_work() -> None:
    task_ids = ("oldest", "middle", "newest")
    repo = QueueRepo(task_ids)
    gates = {task_id: asyncio.Event() for task_id in task_ids}
    entered: list[str] = []

    class Downloader:
        async def download(self, item, should_pause):
            entered.append(item.task_id)
            await gates[item.task_id].wait()

    scheduler = DownloadScheduler(repo, Downloader(), concurrency=2)
    oldest = asyncio.create_task(scheduler.run_task("oldest"))
    await wait_until(lambda: entered == ["oldest"])
    middle = asyncio.create_task(scheduler.run_task("middle"))
    newest = asyncio.create_task(scheduler.run_task("newest"))
    await wait_until(lambda: scheduler.queue_positions() == {"newest": 1})

    assert scheduler.active_task_id == "oldest"
    assert scheduler.snapshot() == SchedulerSnapshot(
        ("oldest", "middle"),
        ("newest",),
        2,
        0,
    )
    repo.priorities["newest"] = 1
    assert await scheduler.prioritize_task("newest") is True
    assert scheduler.queue_positions() == {"newest": 1}

    gates["oldest"].set()
    await wait_until(lambda: entered == ["oldest", "middle", "newest"])
    assert scheduler.active_task_ids == ("middle", "newest")
    gates["newest"].set()
    gates["middle"].set()
    await asyncio.gather(oldest, middle, newest)

    assert repo.cleared == ["oldest", "middle", "newest"]
    assert scheduler.snapshot().active_task_id is None
    assert scheduler.queue_positions() == {}


@pytest.mark.asyncio
async def test_global_slots_run_multiple_tasks_without_exceeding_limit() -> None:
    task_ids = ("first", "second", "third", "fourth")
    repo = QueueRepo(task_ids)
    release = {task_id: asyncio.Event() for task_id in task_ids}
    entered: list[str] = []
    active = 0
    maximum = 0

    class Downloader:
        async def download(self, item, should_pause):
            nonlocal active, maximum
            entered.append(item.task_id)
            active += 1
            maximum = max(maximum, active)
            await release[item.task_id].wait()
            active -= 1

    scheduler = DownloadScheduler(repo, Downloader(), concurrency=3)
    runs = [asyncio.create_task(scheduler.run_task(task_id)) for task_id in task_ids]
    await wait_until(lambda: len(entered) == 3)

    assert scheduler.active_task_ids == ("first", "second", "third")
    assert scheduler.queue_positions() == {"fourth": 1}
    assert maximum == 3

    release["first"].set()
    await wait_until(lambda: "fourth" in entered)
    for event in release.values():
        event.set()
    await asyncio.gather(*runs)

    assert maximum == 3
    assert set(entered) == set(task_ids)


@pytest.mark.asyncio
async def test_new_single_file_task_rotates_ahead_of_large_task_backlog() -> None:
    repo = MultiQueueRepo({"large": 4, "small": 1})
    gates = {item_id: asyncio.Event() for item_id in repo.items}
    entered: list[str] = []

    class Downloader:
        async def download(self, item, should_pause):
            entered.append(item.id)
            await gates[item.id].wait()

    scheduler = DownloadScheduler(repo, Downloader(), concurrency=2)
    large = asyncio.create_task(scheduler.run_task("large"))
    await wait_until(lambda: entered == ["large-item-0", "large-item-1"])
    small = asyncio.create_task(scheduler.run_task("small"))
    await wait_until(lambda: scheduler.active_task_ids == ("large", "small"))
    await wait_until(lambda: scheduler._permits.waiting == 3)

    gates["large-item-0"].set()
    await wait_until(lambda: "small-item-0" in entered)

    assert entered[:3] == ["large-item-0", "large-item-1", "small-item-0"]
    for gate in gates.values():
        gate.set()
    await asyncio.gather(large, small)


@pytest.mark.asyncio
async def test_duplicate_task_submission_shares_one_operation() -> None:
    repo = QueueRepo(("task",))
    entered = 0
    release = asyncio.Event()

    class Downloader:
        async def download(self, item, should_pause):
            nonlocal entered
            entered += 1
            await release.wait()

    scheduler = DownloadScheduler(repo, Downloader())
    first = asyncio.create_task(scheduler.run_task("task"))
    await wait_until(lambda: entered == 1)
    duplicate = asyncio.create_task(scheduler.run_task("task"))
    await asyncio.sleep(0)

    assert entered == 1
    release.set()
    await asyncio.gather(first, duplicate)
    assert entered == 1


@pytest.mark.asyncio
async def test_pausing_waiting_task_removes_it_without_downloading() -> None:
    repo = QueueRepo(("active", "waiting"))
    active_release = asyncio.Event()
    entered: list[str] = []

    class Downloader:
        async def download(self, item, should_pause):
            entered.append(item.task_id)
            if item.task_id == "active":
                await active_release.wait()

    scheduler = DownloadScheduler(repo, Downloader(), concurrency=1)
    active = asyncio.create_task(scheduler.run_task("active"))
    await wait_until(lambda: entered == ["active"])
    waiting = asyncio.create_task(scheduler.run_task("waiting"))
    await wait_until(lambda: scheduler.queue_positions() == {"waiting": 1})

    await scheduler.pause_task("waiting")
    await waiting

    assert entered == ["active"]
    assert repo.task_statuses["waiting"] is TaskStatus.PAUSED
    assert scheduler.queue_positions() == {}
    active_release.set()
    await active


@pytest.mark.asyncio
async def test_shutdown_resolves_waiting_callers_and_settles_active_task() -> None:
    repo = QueueRepo(("active", "waiting"))
    entered = asyncio.Event()

    class Downloader:
        async def download(self, item, should_pause):
            if item.task_id == "active":
                entered.set()
                while not should_pause():
                    await asyncio.sleep(0)
                raise DownloadPaused("paused")

    scheduler = DownloadScheduler(
        repo,
        Downloader(),
        concurrency=1,
        shutdown_grace_seconds=0.1,
    )
    active = asyncio.create_task(scheduler.run_task("active"))
    await entered.wait()
    waiting = asyncio.create_task(scheduler.run_task("waiting"))
    await wait_until(lambda: scheduler.queue_positions() == {"waiting": 1})

    await scheduler.shutdown()
    await asyncio.gather(active, waiting)

    assert repo.task_statuses["active"] is TaskStatus.PAUSED
    assert entered.is_set()
    assert scheduler.snapshot().queued_task_ids == ()


@pytest.mark.asyncio
async def test_shutdown_settles_all_active_task_callers() -> None:
    repo = QueueRepo(("first", "second"))
    entered: set[str] = set()

    class Downloader:
        async def download(self, item, should_pause):
            entered.add(item.task_id)
            while not should_pause():
                await asyncio.sleep(0)
            raise DownloadPaused("paused")

    scheduler = DownloadScheduler(
        repo,
        Downloader(),
        concurrency=2,
        shutdown_grace_seconds=0.1,
    )
    runs = [
        asyncio.create_task(scheduler.run_task(task_id))
        for task_id in ("first", "second")
    ]
    await wait_until(lambda: entered == {"first", "second"})

    await scheduler.shutdown()
    await asyncio.gather(*runs)

    assert repo.task_statuses == {
        "first": TaskStatus.PAUSED,
        "second": TaskStatus.PAUSED,
    }
    assert scheduler.active_task_ids == ()


def test_runtime_resource_configuration_is_visible_in_snapshot() -> None:
    repo = QueueRepo(("task",))
    bandwidth = AsyncBandwidthLimiter()
    scheduler = DownloadScheduler(
        repo,
        object(),
        concurrency=2,
        bandwidth=bandwidth,
    )

    scheduler.configure_resources(5, 2048)

    assert scheduler.snapshot() == SchedulerSnapshot((), (), 5, 2048)
    assert bandwidth.speed_limit_kib == 2048


@pytest.mark.asyncio
async def test_pause_tasks_deduplicates_flags_queue_and_persistence() -> None:
    repo = QueueRepo(("active", "waiting"))
    entered = asyncio.Event()

    class Downloader:
        async def download(self, _item, should_pause):
            entered.set()
            while not should_pause():
                await asyncio.sleep(0)
            raise DownloadPaused("paused")

    scheduler = DownloadScheduler(repo, Downloader(), concurrency=1)
    active = asyncio.create_task(scheduler.run_task("active"))
    await entered.wait()
    waiting = asyncio.create_task(scheduler.run_task("waiting"))
    await wait_until(lambda: scheduler.queue_positions() == {"waiting": 1})

    accepted = await scheduler.pause_tasks(["active", "waiting", "active"])
    await asyncio.gather(active, waiting)

    assert accepted == {"active", "waiting"}
    assert scheduler._pause_flags["active"].is_set()
    assert scheduler._pause_flags["waiting"].is_set()
    assert scheduler.queue_positions() == {}
    assert len(repo.bulk_task_updates) == 1


@pytest.mark.asyncio
async def test_resume_tasks_bulk_updates_and_schedules_each_once() -> None:
    repo = QueueRepo(("a", "b"))
    repo.task_statuses.update({"a": TaskStatus.PAUSED, "b": TaskStatus.PAUSED})
    entered: list[str] = []

    class Downloader:
        async def download(self, item, should_pause):
            entered.append(item.task_id)

    scheduler = DownloadScheduler(repo, Downloader())
    scheduler._pause_flag("a").set()
    scheduler._pause_flag("b").set()

    accepted = await scheduler.resume_tasks(["a", "b", "a"])

    assert accepted == {"a", "b"}
    assert entered == ["a", "b"]
    assert scheduler._pause_flags["a"].is_set() is False
    assert scheduler._pause_flags["b"].is_set() is False
    assert len(repo.bulk_task_updates) == 1
    assert repo.bulk_task_updates[0][1] is TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_slow_scheduler_repository_keeps_event_loop_responsive() -> None:
    repo = SlowQueueRepo(("task",))
    persistence = DownloadPersistenceCoordinator(repo)

    class Downloader:
        async def download(self, _item, _should_pause):
            return None

    scheduler = DownloadScheduler(repo, Downloader(), persistence=persistence)
    gaps: list[float] = []
    running = True

    async def heartbeat() -> None:
        previous = asyncio.get_running_loop().time()
        while running:
            await asyncio.sleep(0)
            current = asyncio.get_running_loop().time()
            gaps.append((current - previous) * 1000)
            previous = current

    pulse = asyncio.create_task(heartbeat())
    try:
        await scheduler.run_task("task")
    finally:
        running = False
        await pulse
        await scheduler.shutdown()

    assert max(gaps) <= 20.0


@pytest.mark.asyncio
async def test_scheduler_skips_duplicate_completed_item_write() -> None:
    repo = QueueRepo(("task",))
    item_writes = 0
    original_update = repo.update_item_progress

    def record_update(*args, **kwargs):
        nonlocal item_writes
        item_writes += 1
        return original_update(*args, **kwargs)

    repo.update_item_progress = record_update

    class Downloader:
        async def download(self, item, should_pause):
            del should_pause
            item.status = ItemStatus.COMPLETED

    scheduler = DownloadScheduler(repo, Downloader())

    await scheduler.run_task("task")

    assert item_writes == 0
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_persistence_fault_stops_admission_and_pauses_active_tasks() -> None:
    repo = QueueRepo(("active", "waiting"))
    persistence = RecordingPersistence()
    entered = asyncio.Event()

    class Downloader:
        async def download(self, _item, should_pause):
            entered.set()
            while not should_pause():
                await asyncio.sleep(0)
            raise DownloadPaused("paused")

    scheduler = DownloadScheduler(
        repo,
        Downloader(),
        concurrency=1,
        persistence=persistence,
    )
    operation = asyncio.create_task(scheduler.run_task("active"))
    await entered.wait()
    waiting = asyncio.create_task(scheduler.run_task("waiting"))
    await wait_until(lambda: scheduler.queue_positions() == {"waiting": 1})
    assert persistence.fault_handler is not None
    failure = RuntimeError("synthetic storage failure")

    persistence.fault_handler(failure)

    assert scheduler._admission_open is False
    assert scheduler._pause_flags["active"].is_set()
    with pytest.raises(RuntimeError) as queued_failure:
        await asyncio.wait_for(waiting, timeout=0.1)
    assert queued_failure.value is failure
    assert scheduler.queue_positions() == {}
    await operation
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_shutdown_drains_and_closes_persistence_without_active_tasks() -> None:
    persistence = RecordingPersistence()
    scheduler = DownloadScheduler(
        QueueRepo(("task",)),
        object(),
        persistence=persistence,
    )

    await scheduler.shutdown()

    assert persistence.events == ["drain", "close"]
