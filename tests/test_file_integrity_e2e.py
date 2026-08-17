import hashlib
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
from telegram_downloader.downloader import MediaDownloader
from telegram_downloader.file_integrity import FileIntegrityService
from telegram_downloader.paths import PortablePaths
from telegram_downloader.repository import TaskRepository
from telegram_downloader.scheduler import DownloadScheduler


class ControlledGateway:
    def __init__(self, payloads: dict[int, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, int, int]] = []

    async def stream_media(self, peer_ref: str, message_id: int, offset: int):
        self.calls.append((peer_ref, message_id, offset))
        yield self.payloads[message_id][offset:]


@pytest.mark.asyncio
async def test_same_size_corruption_repairs_only_selected_media_end_to_end(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path / "application")
    paths.ensure_layout()
    repository = TaskRepository(paths.database)
    repository.initialize()
    now = datetime(2026, 8, 16, tzinfo=UTC)
    task = TaskRecord(
        "task",
        SourceKind.CHANNEL_OR_GROUP,
        "peer",
        "测试群",
        "https://t.me/test",
        ScanFilters(now, now, frozenset({MediaKind.DOCUMENT}), 10),
        TaskStatus.COMPLETED,
        now,
        now,
    )
    first_target = paths.downloads / "group" / "first.bin"
    second_target = paths.downloads / "group" / "second.bin"
    first_target.parent.mkdir(parents=True)
    first_target.write_bytes(b"good")
    second_target.write_bytes(b"keep")
    first = MediaItem(
        "first",
        task.id,
        task.source_ref,
        1,
        None,
        "media-1",
        MediaKind.DOCUMENT,
        first_target.name,
        first_target,
        4,
        now,
        4,
        ItemStatus.COMPLETED,
    )
    second = replace(
        first,
        id="second",
        message_id=2,
        media_id="media-2",
        original_name=second_target.name,
        target_path=second_target,
        retry_count=4,
    )
    repository.create_task(task, [first, second])
    integrity = FileIntegrityService(repository, paths)

    baseline = await integrity.verify([first.id, second.id])
    untouched_before = repository.get_item(second.id)
    second_hash_before = hashlib.sha256(second_target.read_bytes()).hexdigest()
    first_target.write_bytes(b"evil")

    mismatch = await integrity.verify([first.id])
    prepared = integrity.prepare_repairs([first.id])
    gateway = ControlledGateway({1: b"good"})
    downloader = MediaDownloader(
        gateway,
        repository,
        paths,
        free_bytes=lambda _path: 10**9,
        reserve_bytes=0,
        progress_interval=0,
    )
    scheduler = DownloadScheduler(repository, downloader, concurrency=1)
    await scheduler.run_items(task.id, list(prepared.accepted_ids))

    repaired = repository.get_item(first.id)
    untouched_after = repository.get_item(second.id)
    assert baseline.baselined == 2
    assert mismatch.hash_mismatch == 1
    assert prepared.accepted_ids == (first.id,)
    assert gateway.calls == [(task.source_ref, first.message_id, 0)]
    assert first_target.read_bytes() == b"good"
    assert first_target.with_suffix(".bin.corrupt").read_bytes() == b"evil"
    assert repaired.status is ItemStatus.COMPLETED
    assert repaired.integrity_status is IntegrityStatus.VERIFIED
    assert repaired.content_sha256 == hashlib.sha256(b"good").hexdigest()
    assert untouched_after == untouched_before
    assert hashlib.sha256(second_target.read_bytes()).hexdigest() == second_hash_before
    assert repository.get_task(task.id).status is TaskStatus.COMPLETED
    for managed in (
        paths.database,
        first_target,
        second_target,
        first_target.with_suffix(".bin.corrupt"),
    ):
        managed.resolve().relative_to(paths.root)
