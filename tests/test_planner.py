from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_downloader.domain import MediaKind, ScanFilters, TaskStatus
from telegram_downloader.gateway import RemoteMedia
from telegram_downloader.links import parse_telegram_link
from telegram_downloader.planner import EmptyScanError, TaskPlanner


class FakeGateway:
    def __init__(self, media):
        self.media = media

    async def scan(self, source, filters):
        for item in self.media:
            yield item


class FakeRepository:
    def __init__(self):
        self.saved = None

    def create_task(self, task, items):
        self.saved = (task, items)


@pytest.mark.asyncio
async def test_preview_summarizes_without_persisting_until_commit(tmp_path: Path) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    media = [
        RemoteMedia(
            "peer", "频道", 9, None, "m9", MediaKind.VIDEO, "a.mp4", 100, now
        ),
        RemoteMedia(
            "peer", "频道", 8, None, "m8", MediaKind.DOCUMENT, "b.pdf", None, now
        ),
    ]
    repo = FakeRepository()
    ids = iter(["task", "i1", "i2"])
    planner = TaskPlanner(
        FakeGateway(media),
        repo,
        tmp_path,
        uuid_factory=ids.__next__,
        clock=lambda: now,
    )
    filters = ScanFilters(now, now, frozenset(MediaKind), 20)

    preview = await planner.scan(parse_telegram_link("https://t.me/channel"), filters)

    assert preview.known_bytes == 100
    assert preview.unknown_size_count == 1
    assert repo.saved is None
    queued = planner.commit(preview)
    assert queued.status is TaskStatus.QUEUED
    assert repo.saved[0] == queued
    assert [item.message_id for item in repo.saved[1]] == [9, 8]


@pytest.mark.asyncio
async def test_planner_deduplicates_source_items_and_avoids_existing_files(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    remote = RemoteMedia(
        "peer", "频道", 9, None, "m9", MediaKind.VIDEO, "same.mp4", 100, now
    )
    existing = tmp_path / "频道" / "2026-08" / "video" / "same.mp4"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing")
    (existing.parent / "same_9.mp4").write_bytes(b"another")
    planner = TaskPlanner(
        FakeGateway([remote, remote]),
        FakeRepository(),
        tmp_path,
        uuid_factory=iter(["task", "item"]).__next__,
        clock=lambda: now,
    )

    preview = await planner.scan(
        parse_telegram_link("https://t.me/channel"),
        ScanFilters(now, now, frozenset(MediaKind), 20),
    )

    assert len(preview.items) == 1
    assert preview.items[0].target_path.name == "same_9_2.mp4"


@pytest.mark.asyncio
async def test_empty_scan_is_rejected(tmp_path: Path) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    planner = TaskPlanner(FakeGateway([]), FakeRepository(), tmp_path)

    with pytest.raises(EmptyScanError, match="没有找到"):
        await planner.scan(
            parse_telegram_link("https://t.me/channel"),
            ScanFilters(now, now, frozenset(MediaKind), 20),
        )
