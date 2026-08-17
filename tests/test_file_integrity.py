import hashlib
import os
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest

import telegram_downloader.file_integrity as integrity_module
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
from telegram_downloader.file_integrity import FileIntegrityService
from telegram_downloader.paths import PathOutsideRootError, PortablePaths
from telegram_downloader.repository import TaskRepository


def integrity_fixture(
    tmp_path: Path,
    payloads: tuple[bytes, ...] = (b"good",),
) -> tuple[FileIntegrityService, TaskRepository, PortablePaths, list[MediaItem]]:
    paths = PortablePaths(tmp_path / "app")
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
        ScanFilters(now, now, frozenset({MediaKind.DOCUMENT}), 20),
        TaskStatus.COMPLETED,
        now,
        now,
    )
    items: list[MediaItem] = []
    for index, payload in enumerate(payloads):
        target = paths.downloads / f"file-{index}.bin"
        target.write_bytes(payload)
        items.append(
            MediaItem(
                f"item-{index}",
                task.id,
                "peer",
                index + 1,
                None,
                f"media-{index}",
                MediaKind.DOCUMENT,
                target.name,
                target,
                len(payload),
                now,
                len(payload),
                ItemStatus.COMPLETED,
            )
        )
    repository.create_task(task, items)
    return FileIntegrityService(repository, paths), repository, paths, items


@pytest.mark.asyncio
async def test_unverified_file_gets_baseline_then_verifies(tmp_path: Path) -> None:
    service, repository, _, items = integrity_fixture(tmp_path)

    baseline = await service.verify([items[0].id])
    verified = await service.verify([items[0].id])

    saved = repository.get_item(items[0].id)
    assert baseline.baselined == 1
    assert verified.verified == 1
    assert saved.integrity_status is IntegrityStatus.VERIFIED
    assert saved.content_sha256 == hashlib.sha256(b"good").hexdigest()
    assert saved.verified_at is not None


@pytest.mark.asyncio
async def test_missing_file_is_persisted_as_safe_failure(tmp_path: Path) -> None:
    service, repository, _, items = integrity_fixture(tmp_path)
    items[0].target_path.unlink()

    summary = await service.verify([items[0].id])

    saved = repository.get_item(items[0].id)
    assert summary.missing == 1
    assert saved.integrity_status is IntegrityStatus.MISSING
    assert saved.status is ItemStatus.FAILED
    assert saved.last_error == "本地文件缺失"


@pytest.mark.asyncio
async def test_known_size_change_is_size_mismatch(tmp_path: Path) -> None:
    service, repository, _, items = integrity_fixture(tmp_path)
    items[0].target_path.write_bytes(b"too-long")

    summary = await service.verify([items[0].id])

    assert summary.size_mismatch == 1
    assert (
        repository.get_item(items[0].id).integrity_status
        is IntegrityStatus.SIZE_MISMATCH
    )


@pytest.mark.asyncio
async def test_same_size_change_is_hash_mismatch(tmp_path: Path) -> None:
    service, repository, _, items = integrity_fixture(tmp_path)
    await service.verify([items[0].id])
    items[0].target_path.write_bytes(b"evil")

    summary = await service.verify([items[0].id])

    assert summary.hash_mismatch == 1
    assert (
        repository.get_item(items[0].id).integrity_status
        is IntegrityStatus.HASH_MISMATCH
    )


@pytest.mark.asyncio
async def test_read_error_is_safe_and_does_not_expose_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, _, items = integrity_fixture(tmp_path)

    def fail_read(path: Path, cancelled: Event) -> str:
        raise OSError(f"secret path: {path}")

    monkeypatch.setattr(integrity_module, "_hash_file", fail_read)

    summary = await service.verify([items[0].id])

    saved = repository.get_item(items[0].id)
    assert summary.read_error == 1
    assert saved.integrity_status is IntegrityStatus.READ_ERROR
    assert saved.last_error == "无法读取本地文件"
    assert str(items[0].target_path) not in saved.last_error


@pytest.mark.asyncio
async def test_path_escape_is_rejected_before_file_access(tmp_path: Path) -> None:
    service, repository, _, items = integrity_fixture(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"good")
    escaped = replace(items[0], target_path=outside)
    with sqlite3.connect(repository.database) as connection:
        connection.execute(
            "UPDATE media_items SET target_path = ? WHERE id = ?",
            (str(outside), escaped.id),
        )

    with pytest.raises(PathOutsideRootError):
        await service.verify([escaped.id])

    assert outside.read_bytes() == b"good"


@pytest.mark.asyncio
async def test_verify_deduplicates_ids_and_reports_progress_in_order(
    tmp_path: Path,
) -> None:
    service, _, _, items = integrity_fixture(tmp_path, (b"one", b"two"))
    progress = []

    summary = await service.verify(
        [items[1].id, items[0].id, items[1].id],
        progress=progress.append,
    )

    assert summary.baselined == 2
    assert [(value.completed, value.total, value.item_id) for value in progress] == [
        (1, 2, items[1].id),
        (2, 2, items[0].id),
    ]


@pytest.mark.asyncio
async def test_verify_cancellation_keeps_finished_results(tmp_path: Path) -> None:
    service, repository, _, items = integrity_fixture(tmp_path, (b"one", b"two"))
    cancelled = Event()
    progress = []

    def on_progress(value) -> None:
        progress.append(value)
        cancelled.set()

    summary = await service.verify(
        [items[0].id, items[1].id],
        progress=on_progress,
        cancelled=cancelled,
    )

    assert summary.baselined == 1
    assert summary.cancelled == 1
    assert len(progress) == 1
    assert (
        repository.get_item(items[1].id).integrity_status
        is IntegrityStatus.UNVERIFIED
    )


@pytest.mark.asyncio
async def test_ineligible_item_is_counted_as_skipped(tmp_path: Path) -> None:
    service, repository, _, items = integrity_fixture(tmp_path)
    repository.update_item_progress(items[0].id, 0, ItemStatus.QUEUED)

    summary = await service.verify([items[0].id])

    assert summary.skipped == 1
    assert summary.baselined == 0


@pytest.mark.asyncio
async def test_prepare_repair_quarantines_final_and_part_collision_safely(
    tmp_path: Path,
) -> None:
    service, repository, _, items = integrity_fixture(tmp_path)
    target = items[0].target_path
    await service.verify([items[0].id])
    target.write_bytes(b"evil")
    await service.verify([items[0].id])
    target.with_suffix(".bin.corrupt").write_bytes(b"older")
    part = target.with_suffix(".bin.part")
    part.write_bytes(b"partial")

    result = service.prepare_repairs([items[0].id, items[0].id])

    assert result.accepted_ids == (items[0].id,)
    assert result.skipped == 0
    assert not target.exists()
    assert target.with_suffix(".bin.corrupt").read_bytes() == b"older"
    assert target.with_suffix(".bin.corrupt.2").read_bytes() == b"evil"
    assert target.with_suffix(".bin.part.corrupt").read_bytes() == b"partial"
    queued = repository.get_item(items[0].id)
    assert queued.status is ItemStatus.QUEUED
    assert queued.integrity_status is IntegrityStatus.UNVERIFIED
    assert queued.content_sha256 is None


@pytest.mark.asyncio
async def test_prepare_repair_restores_first_move_when_second_move_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, _, items = integrity_fixture(tmp_path)
    target = items[0].target_path
    await service.verify([items[0].id])
    target.write_bytes(b"evil")
    await service.verify([items[0].id])
    part = target.with_suffix(".bin.part")
    part.write_bytes(b"partial")
    real_replace = os.replace

    def fail_part_move(source, destination) -> None:
        if Path(source) == part:
            raise OSError("simulated move failure")
        real_replace(source, destination)

    monkeypatch.setattr(integrity_module.os, "replace", fail_part_move)

    result = service.prepare_repairs([items[0].id])

    assert result.accepted_ids == ()
    assert result.skipped == 1
    assert target.read_bytes() == b"evil"
    assert part.read_bytes() == b"partial"
    assert not target.with_suffix(".bin.corrupt").exists()
    assert (
        repository.get_item(items[0].id).integrity_status
        is IntegrityStatus.HASH_MISMATCH
    )


@pytest.mark.asyncio
async def test_prepare_repair_rejects_verified_item_without_moving_it(
    tmp_path: Path,
) -> None:
    service, _, _, items = integrity_fixture(tmp_path)
    await service.verify([items[0].id])

    result = service.prepare_repairs([items[0].id])

    assert result.accepted_ids == ()
    assert result.skipped == 1
    assert items[0].target_path.read_bytes() == b"good"
