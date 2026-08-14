from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_downloader.content import ContentSearchQuery
from telegram_downloader.domain import (
    MediaItem,
    MediaKind,
    ScanFilters,
    SourceKind,
    TaskRecord,
    TaskStatus,
)
from telegram_downloader.gateway import RemoteMedia
from telegram_downloader.links import parse_telegram_link
from telegram_downloader.planner import EmptyScanError, TaskPlanner
from telegram_downloader.repository import AllMediaAlreadyExists, TaskRepository


class FakeGateway:
    def __init__(self, media):
        self.media = media

    async def scan(self, source, filters):
        for item in self.media:
            yield item


class FakeRepository:
    def __init__(self):
        self.saved = None
        self.existing = set()

    def create_task(self, task, items):
        self.saved = (task, items)

    def existing_media_keys(self, keys):
        return keys & self.existing

    def create_task_deduplicating(self, task, items):
        accepted = [
            item
            for item in items
            if (item.peer_ref, item.message_id, item.media_id) not in self.existing
        ]
        if not accepted:
            raise AllMediaAlreadyExists
        self.existing.update(
            (item.peer_ref, item.message_id, item.media_id) for item in accepted
        )
        self.saved = (task, accepted)
        return accepted


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


def test_plan_selected_uses_search_title_but_archives_under_source(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = FakeRepository()
    repo.existing = {("-1001", 8, "m8")}
    planner = TaskPlanner(
        FakeGateway([]),
        repo,
        tmp_path,
        uuid_factory=iter(["task", "item-9"]).__next__,
        clock=lambda: now,
    )
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 500),
    )
    selected = [
        RemoteMedia(
            "-1001", "资料群", 9, None, "m9", MediaKind.VIDEO, "a.mp4", 10, now
        ),
        RemoteMedia(
            "-1001", "资料群", 8, None, "m8", MediaKind.VIDEO, "b.mp4", 20, now
        ),
    ]

    preview = planner.plan_selected("-1001", "资料群", query, selected)

    assert preview.task.display_title == "资料群（搜索：安装）"
    assert [item.message_id for item in preview.items] == [9]
    assert preview.items[0].target_path.is_relative_to(tmp_path / "资料群")
    assert repo.saved is None


def test_plan_selected_rejects_empty_and_fully_existing_input(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 500),
    )
    remote = RemoteMedia(
        "-1001", "资料群", 9, None, "m9", MediaKind.VIDEO, "a.mp4", 10, now
    )
    repo = FakeRepository()
    planner = TaskPlanner(FakeGateway([]), repo, tmp_path, clock=lambda: now)

    with pytest.raises(EmptyScanError, match="所选媒体已全部存在于下载队列"):
        planner.plan_selected("-1001", "资料群", query, [])

    repo.existing = {("-1001", 9, "m9")}
    with pytest.raises(EmptyScanError, match="所选媒体已全部存在于下载队列"):
        planner.plan_selected("-1001", "资料群", query, [remote])

    assert repo.saved is None


def test_commit_selected_accepts_remaining_items_after_a_race(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    planner = TaskPlanner(
        FakeGateway([]),
        repo,
        tmp_path / "downloads",
        uuid_factory=iter(["selected", "item-9", "item-8"]).__next__,
        clock=lambda: now,
    )
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 500),
    )
    selected = [
        RemoteMedia(
            "-1001", "资料群", 9, None, "m9", MediaKind.VIDEO, "a.mp4", 10, now
        ),
        RemoteMedia(
            "-1001", "资料群", 8, None, "m8", MediaKind.VIDEO, "b.mp4", 20, now
        ),
    ]
    preview = planner.plan_selected("-1001", "资料群", query, selected)
    occupied_task = TaskRecord(
        "occupied",
        SourceKind.CHANNEL_OR_GROUP,
        "-1001",
        "资料群",
        "telegram://peer/-1001",
        query.filters,
        TaskStatus.QUEUED,
        now,
        now,
    )
    occupied_item = MediaItem(
        "occupied-item",
        occupied_task.id,
        "-1001",
        8,
        None,
        "m8",
        MediaKind.VIDEO,
        "b.mp4",
        tmp_path / "occupied.mp4",
        20,
        now,
    )
    repo.create_task(occupied_task, [occupied_item])

    committed = planner.commit_selected(preview)

    assert committed.task.status is TaskStatus.QUEUED
    assert committed.accepted_keys == frozenset({("-1001", 9, "m9")})
    assert committed.skipped_count == 1
    assert [item.message_id for item in repo.list_items(preview.task.id)] == [9]


def test_commit_selected_rolls_back_when_every_item_loses_race(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    planner = TaskPlanner(
        FakeGateway([]),
        repo,
        tmp_path / "downloads",
        uuid_factory=iter(["selected", "selected-item"]).__next__,
        clock=lambda: now,
    )
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 500),
    )
    remote = RemoteMedia(
        "-1001", "资料群", 9, None, "m9", MediaKind.VIDEO, "a.mp4", 10, now
    )
    preview = planner.plan_selected("-1001", "资料群", query, [remote])
    occupied_task = TaskRecord(
        "occupied",
        SourceKind.CHANNEL_OR_GROUP,
        "-1001",
        "资料群",
        "telegram://peer/-1001",
        query.filters,
        TaskStatus.QUEUED,
        now,
        now,
    )
    occupied_item = MediaItem(
        "occupied-item",
        occupied_task.id,
        "-1001",
        9,
        None,
        "m9",
        MediaKind.VIDEO,
        "a.mp4",
        tmp_path / "occupied.mp4",
        10,
        now,
    )
    repo.create_task(occupied_task, [occupied_item])

    with pytest.raises(EmptyScanError, match="所选媒体已全部存在于下载队列"):
        planner.commit_selected(preview)

    with pytest.raises(KeyError):
        repo.get_task(preview.task.id)
