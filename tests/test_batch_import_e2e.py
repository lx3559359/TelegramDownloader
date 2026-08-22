from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_downloader.domain import (
    MediaItem,
    MediaKind,
    ScanFilters,
    SourceKind,
    TaskRecord,
    TaskStatus,
)
from telegram_downloader.gateway import RemoteMedia
from telegram_downloader.paths import PortablePaths
from telegram_downloader.planner import TaskPlanner
from telegram_downloader.repository import TaskRepository

NOW = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)


def ids(prefix: str):
    number = 0

    def next_id() -> str:
        nonlocal number
        number += 1
        return f"{prefix}-{number}"

    return next_id


def remote(peer: str, title: str, number: int) -> RemoteMedia:
    return RemoteMedia(
        peer,
        title,
        number,
        None,
        f"media-{number}",
        MediaKind.PHOTO,
        f"photo-{number}.jpg",
        100,
        NOW,
    )


class Gateway:
    async def scan(self, source, _filters):
        shared = remote("shared", "共享群", 2)
        batches = {
            "first_channel": (remote("first", "第一群", 1), shared),
            "second_channel": (shared, remote("second", "第二群", 3)),
        }
        for item in batches[source.entity_ref]:
            yield item


@pytest.mark.asyncio
async def test_batch_import_creates_one_restart_safe_multi_source_task(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    repository = TaskRepository(paths.database)
    repository.initialize()
    existing_task = TaskRecord(
        "existing-task",
        SourceKind.CHANNEL_OR_GROUP,
        "second",
        "第二群",
        "https://t.me/second_channel",
        ScanFilters(NOW, NOW, frozenset({MediaKind.PHOTO}), 20),
        TaskStatus.QUEUED,
        NOW,
        NOW,
    )
    repository.create_task(
        existing_task,
        [
            MediaItem(
                "existing-item",
                existing_task.id,
                "second",
                3,
                None,
                "media-3",
                MediaKind.PHOTO,
                "photo-3.jpg",
                paths.downloads / "existing.jpg",
                100,
                NOW,
            )
        ],
    )
    planner = TaskPlanner(
        Gateway(),
        repository,
        paths.downloads,
        uuid_factory=ids("batch"),
        clock=lambda: NOW,
    )

    preview = await planner.scan_batch(
        (
            "https://t.me/first_channel",
            "HTTPS://WWW.T.ME/first_channel/",
            "invalid",
            "https://t.me/second_channel",
        ),
        ScanFilters(NOW, NOW, frozenset({MediaKind.PHOTO}), 20),
    )
    committed = planner.commit(preview.preview)

    assert preview.duplicate_link_count == 1
    assert preview.invalid_link_count == 1
    assert preview.internal_duplicate_count == 1
    assert preview.existing_media_count == 1
    assert len(committed.accepted_keys) == 2

    reopened = TaskRepository(paths.database)
    reopened.initialize()
    task = reopened.get_task(committed.task.id)
    items = reopened.list_items(task.id)
    assert task.source_kind is SourceKind.BATCH_IMPORT
    assert task.display_title == "批量链接导入（2 个链接）"
    assert {item.peer_ref for item in items} == {"first", "shared"}
    assert all(item.target_path.resolve().is_relative_to(paths.root) for item in items)
    assert len(reopened.list_tasks()) == 2
