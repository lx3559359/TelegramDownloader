import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

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
from telegram_downloader.downloader import MediaDownloader
from telegram_downloader.paths import PortablePaths
from telegram_downloader.repository import TaskRepository
from telegram_downloader.resource_control import AsyncBandwidthLimiter
from telegram_downloader.scheduler import DownloadScheduler


async def wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not reached")


class ControlledGateway:
    def __init__(self, payloads: dict[int, tuple[bytes, ...]]) -> None:
        self.payloads = payloads
        self.gates = {
            message_id: tuple(asyncio.Event() for _ in chunks)
            for message_id, chunks in payloads.items()
        }
        self.started: list[int] = []
        self.offsets: list[tuple[int, int]] = []
        self.consumed: list[tuple[int, int]] = []

    async def stream_media(self, _peer_ref, message_id, offset):
        self.started.append(message_id)
        self.offsets.append((message_id, offset))
        position = 0
        for chunk_index, (gate, chunk) in enumerate(
            zip(self.gates[message_id], self.payloads[message_id], strict=True)
        ):
            next_position = position + len(chunk)
            if next_position <= offset:
                position = next_position
                continue
            await gate.wait()
            yield chunk[offset - position :] if offset > position else chunk
            self.consumed.append((message_id, chunk_index))
            position = next_position

    def release(self, message_id: int, chunk_index: int) -> None:
        self.gates[message_id][chunk_index].set()

    def release_all(self, message_id: int) -> None:
        for gate in self.gates[message_id]:
            gate.set()


def queue_fixture(
    paths: PortablePaths,
    task_id: str,
    message_id: int,
    created_at: datetime,
    payload: bytes,
) -> tuple[TaskRecord, MediaItem]:
    filters = ScanFilters(
        created_at - timedelta(days=1),
        created_at,
        frozenset({MediaKind.DOCUMENT}),
        10,
    )
    task = TaskRecord(
        task_id,
        SourceKind.CHANNEL_OR_GROUP,
        f"synthetic-{task_id}",
        f"Synthetic {task_id}",
        f"https://t.me/synthetic/{message_id}",
        filters,
        TaskStatus.QUEUED,
        created_at,
        created_at,
    )
    item = MediaItem(
        f"{task_id}-item",
        task_id,
        task.source_ref,
        message_id,
        None,
        f"media-{message_id}",
        MediaKind.DOCUMENT,
        f"{task_id}.bin",
        paths.downloads / task_id / f"{task_id}.bin",
        len(payload),
        created_at,
    )
    return task, item


@pytest.mark.asyncio
async def test_real_queue_prioritizes_pauses_restarts_and_stays_portable(tmp_path) -> None:
    paths = PortablePaths(tmp_path / "application")
    paths.ensure_layout()
    repository = TaskRepository(paths.database)
    repository.initialize()
    now = datetime(2026, 8, 16, tzinfo=UTC)
    payloads = {
        101: (b"link-", b"payload"),
        102: (b"search-", b"payload"),
        103: (b"sub-", b"partial-", b"payload"),
    }
    link_task, link_item = queue_fixture(
        paths, "link-task", 101, now, b"".join(payloads[101])
    )
    search_task, search_item = queue_fixture(
        paths, "search-task", 102, now + timedelta(seconds=1), b"".join(payloads[102])
    )
    subscription_task, subscription_item = queue_fixture(
        paths,
        "subscription-task",
        103,
        now + timedelta(seconds=2),
        b"".join(payloads[103]),
    )
    for task, item in (
        (link_task, link_item),
        (search_task, search_item),
        (subscription_task, subscription_item),
    ):
        repository.create_task(task, [item])

    gateway = ControlledGateway(payloads)
    bandwidth = AsyncBandwidthLimiter(0)
    downloader = MediaDownloader(
        gateway,
        repository,
        paths,
        free_bytes=lambda _path: 10**9,
        reserve_bytes=0,
        progress_interval=0,
        bandwidth=bandwidth,
    )
    scheduler = DownloadScheduler(
        repository,
        downloader,
        concurrency=2,
        bandwidth=bandwidth,
    )

    operations = [
        asyncio.create_task(scheduler.run_task(task.id))
        for task in (link_task, search_task, subscription_task)
    ]
    await wait_until(
        lambda: scheduler.active_task_ids == (link_task.id, search_task.id)
    )
    assert scheduler.queue_positions() == {subscription_task.id: 1}
    assert repository.prioritize_task(subscription_task.id) is True
    assert scheduler.prioritize_task(subscription_task.id) is True
    assert scheduler.queue_positions() == {subscription_task.id: 1}

    gateway.release_all(101)
    await wait_until(
        lambda: scheduler.active_task_ids == (search_task.id, subscription_task.id)
    )
    gateway.release(103, 0)
    part = subscription_item.target_path.with_suffix(".bin.part")
    await wait_until(lambda: (103, 0) in gateway.consumed)
    scheduler.pause_task(subscription_task.id)
    gateway.release(103, 1)
    await wait_until(lambda: subscription_task.id not in scheduler.active_task_ids)
    assert part.read_bytes() == b"sub-"
    gateway.release_all(102)
    await asyncio.gather(*operations)
    await scheduler.shutdown()

    restarted_repository = TaskRepository(paths.database)
    restarted_repository.initialize()
    restarted_downloader = MediaDownloader(
        gateway,
        restarted_repository,
        paths,
        free_bytes=lambda _path: 10**9,
        reserve_bytes=0,
        progress_interval=0,
        bandwidth=bandwidth,
    )
    restarted_scheduler = DownloadScheduler(
        restarted_repository,
        restarted_downloader,
        concurrency=3,
        bandwidth=bandwidth,
    )
    resumed = asyncio.create_task(restarted_scheduler.resume_task(subscription_task.id))
    await wait_until(lambda: restarted_scheduler.active_task_id == subscription_task.id)
    gateway.release(103, 2)
    await resumed
    await restarted_scheduler.shutdown()

    assert gateway.started == [101, 102, 103, 103]
    assert gateway.offsets[-1] == (103, len(b"sub-"))
    all_items = [
        restarted_repository.get_item(item.id)
        for item in (link_item, search_item, subscription_item)
    ]
    assert len({item.media_id for item in all_items}) == 3
    for expected, item in zip(
        (b"".join(payloads[101]), b"".join(payloads[102]), b"".join(payloads[103])),
        all_items,
        strict=True,
    ):
        assert item.status is ItemStatus.COMPLETED
        assert item.integrity_status is IntegrityStatus.VERIFIED
        assert item.downloaded_bytes == len(expected)
        assert item.expected_size == len(expected)
        assert item.content_sha256 == hashlib.sha256(expected).hexdigest()
        assert item.target_path.read_bytes() == expected
        assert not item.target_path.with_suffix(item.target_path.suffix + ".part").exists()
        assert item.target_path.is_relative_to(paths.root)
    assert paths.database.is_relative_to(paths.root)
    assert paths.settings.is_relative_to(paths.root)
