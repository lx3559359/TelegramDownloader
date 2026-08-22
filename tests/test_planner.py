from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_downloader.content import (
    ALL_DIALOGS_SCOPE_REF,
    ALL_DIALOGS_TITLE,
    ContentSearchQuery,
)
from telegram_downloader.domain import (
    MediaItem,
    MediaKind,
    ScanFilters,
    SourceKind,
    TaskRecord,
    TaskStatus,
)
from telegram_downloader.files import DownloadNamingSettings
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
        self.existing.update((item.peer_ref, item.message_id, item.media_id) for item in accepted)
        self.saved = (task, accepted)
        return accepted


@pytest.mark.asyncio
async def test_preview_summarizes_without_persisting_until_commit(tmp_path: Path) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    media = [
        RemoteMedia("peer", "频道", 9, None, "m9", MediaKind.VIDEO, "a.mp4", 100, now),
        RemoteMedia("peer", "频道", 8, None, "m8", MediaKind.DOCUMENT, "b.pdf", None, now),
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
    committed = planner.commit(preview)
    assert committed.task.status is TaskStatus.QUEUED
    assert committed.accepted_keys == frozenset({("peer", 9, "m9"), ("peer", 8, "m8")})
    assert committed.skipped_count == 0
    assert repo.saved[0] == committed.task
    assert [item.message_id for item in repo.saved[1]] == [9, 8]


@pytest.mark.asyncio
async def test_planner_uses_and_reconfigures_download_naming_for_new_previews(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    remote = RemoteMedia(
        "peer",
        "资料群",
        42,
        None,
        "m42",
        MediaKind.VIDEO,
        "clip.mp4",
        100,
        now,
    )
    planner = TaskPlanner(
        FakeGateway([remote]),
        FakeRepository(),
        tmp_path,
        uuid_factory=iter(("first-task", "first-item", "second-task", "second-item")).__next__,
        clock=lambda: now,
        naming=DownloadNamingSettings(
            "{year}/{month}/{source}/{media_type}",
            "{stem}_{message_id}{extension}",
        ),
    )
    filters = ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 20)

    first = await planner.scan(parse_telegram_link("https://t.me/example"), filters)
    planner.configure_naming(
        DownloadNamingSettings(
            "{source}/{message_date}",
            "{message_id}_{original_name}",
        )
    )
    second = await planner.scan(parse_telegram_link("https://t.me/example"), filters)

    assert first.items[0].target_path == (
        tmp_path / "2026" / "08" / "资料群" / "video" / "clip_42.mp4"
    )
    assert second.items[0].target_path == (
        tmp_path / "资料群" / "2026-08-13" / "42_clip.mp4"
    )
    assert first.items[0].target_path != second.items[0].target_path


def test_planner_configure_downloads_changes_only_future_targets(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    now = datetime(2026, 8, 22, tzinfo=UTC)
    base = RemoteMedia(
        "peer",
        "来源",
        1,
        None,
        "media-1",
        MediaKind.VIDEO,
        "clip.mp4",
        3,
        now,
    )
    planner = TaskPlanner(FakeGateway([]), FakeRepository(), first)
    query = ContentSearchQuery(
        "测试",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 10),
    )

    old = planner.plan_selected("peer", "来源", query, [base]).items[0].target_path
    planner.configure_downloads(second, DownloadNamingSettings())
    new = planner.plan_selected(
        "peer",
        "来源",
        query,
        [replace(base, message_id=2, media_id="media-2")],
    ).items[0].target_path

    assert old.is_relative_to(first)
    assert new.is_relative_to(second)


def test_planner_probes_current_root_once_per_preview(tmp_path: Path) -> None:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    calls: list[Path] = []
    planner = TaskPlanner(
        FakeGateway([]),
        FakeRepository(),
        tmp_path,
        download_root_provider=lambda: calls.append(tmp_path.resolve()) or tmp_path,
    )
    query = ContentSearchQuery(
        "测试",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 10),
    )
    selected = [
        RemoteMedia(
            "peer",
            "来源",
            index,
            None,
            f"media-{index}",
            MediaKind.VIDEO,
            f"clip-{index}.mp4",
            3,
            now,
        )
        for index in (1, 2)
    ]

    planner.plan_selected("peer", "来源", query, selected)

    assert calls == [tmp_path.resolve()]


@pytest.mark.asyncio
async def test_batch_preview_reports_all_duplicate_layers_and_builds_one_task(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)

    def media(peer: str, title: str, number: int) -> RemoteMedia:
        return RemoteMedia(
            peer,
            title,
            number,
            None,
            f"m{number}",
            MediaKind.VIDEO,
            f"{number}.mp4",
            number,
            now,
        )

    duplicate = media("shared", "共享来源", 2)
    existing = media("second", "第二来源", 4)

    class BatchGateway:
        async def scan(self, source, _filters):
            batches = {
                "first_channel": [media("first", "第一来源", 1), duplicate],
                "second_channel": [duplicate, media("second", "第二来源", 3), existing],
            }
            for item in batches[source.entity_ref]:
                yield item

    repo = FakeRepository()
    repo.existing = {("second", 4, "m4")}
    planner = TaskPlanner(
        BatchGateway(),
        repo,
        tmp_path,
        uuid_factory=iter(("batch-task", "item-1", "item-2", "item-3")).__next__,
        clock=lambda: now,
    )
    progress = []

    batch = await planner.scan_batch(
        (
            "https://t.me/first_channel",
            "HTTPS://WWW.T.ME/first_channel/",
            "invalid",
            "https://t.me/second_channel",
        ),
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 20),
        on_progress=progress.append,
    )

    assert batch.input_count == 4
    assert batch.unique_link_count == 2
    assert batch.invalid_link_count == 1
    assert batch.duplicate_link_count == 1
    assert batch.scanned_media_count == 5
    assert batch.internal_duplicate_count == 1
    assert batch.existing_media_count == 1
    assert len(batch.preview.items) == 3
    assert batch.preview.task.source_kind is SourceKind.BATCH_IMPORT
    assert batch.preview.task.display_title == "批量链接导入（2 个链接）"
    assert [item.completed for item in progress] == [1, 2]
    assert repo.saved is None

    committed = planner.commit(batch.preview)

    assert committed.task.id == "batch-task"
    assert len(committed.accepted_keys) == 3
    assert repo.saved[0].source_kind is SourceKind.BATCH_IMPORT


@pytest.mark.asyncio
async def test_planner_deduplicates_source_items_and_avoids_existing_files(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    remote = RemoteMedia("peer", "频道", 9, None, "m9", MediaKind.VIDEO, "same.mp4", 100, now)
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


@pytest.mark.asyncio
async def test_link_preview_filters_media_already_in_any_task(tmp_path: Path) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    media = [
        RemoteMedia("peer", "频道", 9, None, "m9", MediaKind.VIDEO, "a.mp4", 100, now),
        RemoteMedia("peer", "频道", 8, None, "m8", MediaKind.DOCUMENT, "b.pdf", 20, now),
    ]
    repo = FakeRepository()
    repo.existing = {("peer", 9, "m9")}
    planner = TaskPlanner(
        FakeGateway(media),
        repo,
        tmp_path,
        uuid_factory=iter(["task", "item-8"]).__next__,
        clock=lambda: now,
    )

    preview = await planner.scan(
        parse_telegram_link("https://t.me/channel"),
        ScanFilters(now, now, frozenset(MediaKind), 20),
    )

    assert [item.message_id for item in preview.items] == [8]
    assert preview.known_bytes == 20
    assert preview.unknown_size_count == 0


@pytest.mark.asyncio
async def test_link_preview_distinguishes_fully_existing_from_empty_scan(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    remote = RemoteMedia("peer", "频道", 9, None, "m9", MediaKind.VIDEO, "a.mp4", 100, now)
    repo = FakeRepository()
    repo.existing = {("peer", 9, "m9")}
    planner = TaskPlanner(FakeGateway([remote]), repo, tmp_path, clock=lambda: now)

    with pytest.raises(EmptyScanError, match="扫描媒体已全部存在于下载队列"):
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
        RemoteMedia("-1001", "资料群", 9, None, "m9", MediaKind.VIDEO, "a.mp4", 10, now),
        RemoteMedia("-1001", "资料群", 8, None, "m8", MediaKind.VIDEO, "b.mp4", 20, now),
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
    remote = RemoteMedia("-1001", "资料群", 9, None, "m9", MediaKind.VIDEO, "a.mp4", 10, now)
    repo = FakeRepository()
    planner = TaskPlanner(FakeGateway([]), repo, tmp_path, clock=lambda: now)

    with pytest.raises(EmptyScanError, match="所选媒体已全部存在于下载队列"):
        planner.plan_selected("-1001", "资料群", query, [])

    repo.existing = {("-1001", 9, "m9")}
    with pytest.raises(EmptyScanError, match="所选媒体已全部存在于下载队列"):
        planner.plan_selected("-1001", "资料群", query, [remote])

    assert repo.saved is None


def test_plan_account_search_uses_one_task_and_real_source_directories(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 20),
    )

    def remote_media(
        peer_ref: str,
        source_title: str,
        message_id: int,
    ) -> RemoteMedia:
        return RemoteMedia(
            peer_ref,
            source_title,
            message_id,
            None,
            f"m{message_id}",
            MediaKind.VIDEO,
            f"{message_id}.mp4",
            12,
            now,
        )

    selected = [
        remote_media("-1001", "资料群", 10),
        remote_media("42", "联系人", 11),
    ]
    repository = TaskRepository(tmp_path / "tasks.sqlite3")
    repository.initialize()
    planner = TaskPlanner(
        object(),
        repository,
        tmp_path / "downloads",
        uuid_factory=iter(("task", "item-1", "item-2")).__next__,
        clock=lambda: now,
    )

    preview = planner.plan_account_search(query, selected)

    assert preview.task.source_kind is SourceKind.ACCOUNT_SEARCH
    assert preview.task.source_ref == ALL_DIALOGS_SCOPE_REF
    assert preview.task.source_title == ALL_DIALOGS_TITLE
    assert preview.task.display_title == "全部会话（搜索：安装）"
    assert {item.peer_ref for item in preview.items} == {"-1001", "42"}
    assert {item.target_path.parts[-4] for item in preview.items} == {
        "资料群",
        "联系人",
    }


def test_plan_subscription_uses_automatic_title_and_archive_layout(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    remote = RemoteMedia(
        "-1001",
        "资料群",
        12,
        None,
        "m12",
        MediaKind.PHOTO,
        "photo.jpg",
        20,
        now,
    )
    planner = TaskPlanner(
        FakeGateway([]),
        FakeRepository(),
        tmp_path,
        uuid_factory=iter(["task", "item"]).__next__,
        clock=lambda: now,
    )

    preview = planner.plan_subscription(
        "-1001",
        "资料群",
        "全部：美女、写真；排除：广告",
        [remote],
    )

    assert preview.task.display_title == ("资料群（自动订阅：全部：美女、写真；排除：广告）")
    assert preview.task.source_url == "telegram://peer/-1001"
    assert preview.task.filters.media_kinds == frozenset({MediaKind.PHOTO})
    assert preview.items[0].target_path.is_relative_to(tmp_path / "资料群")


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
        RemoteMedia("-1001", "资料群", 9, None, "m9", MediaKind.VIDEO, "a.mp4", 10, now),
        RemoteMedia("-1001", "资料群", 8, None, "m8", MediaKind.VIDEO, "b.mp4", 20, now),
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
    remote = RemoteMedia("-1001", "资料群", 9, None, "m9", MediaKind.VIDEO, "a.mp4", 10, now)
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


@pytest.mark.asyncio
async def test_link_commit_accepts_remaining_items_after_confirmation_race(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    media = [
        RemoteMedia("-1001", "资料群", 9, None, "m9", MediaKind.VIDEO, "a.mp4", 10, now),
        RemoteMedia("-1001", "资料群", 8, None, "m8", MediaKind.VIDEO, "b.mp4", 20, now),
    ]
    planner = TaskPlanner(
        FakeGateway(media),
        repo,
        tmp_path / "downloads",
        uuid_factory=iter(["link", "item-9", "item-8"]).__next__,
        clock=lambda: now,
    )
    filters = ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 500)
    preview = await planner.scan(parse_telegram_link("https://t.me/example"), filters)
    occupied_task = TaskRecord(
        "occupied",
        SourceKind.CHANNEL_OR_GROUP,
        "-1001",
        "资料群",
        "telegram://peer/-1001",
        filters,
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

    committed = planner.commit(preview)

    assert committed.task.status is TaskStatus.QUEUED
    assert committed.accepted_keys == frozenset({("-1001", 9, "m9")})
    assert committed.skipped_count == 1
    assert [item.message_id for item in repo.list_items(preview.task.id)] == [9]


@pytest.mark.asyncio
async def test_link_commit_rolls_back_when_every_item_loses_confirmation_race(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    remote = RemoteMedia("-1001", "资料群", 9, None, "m9", MediaKind.VIDEO, "a.mp4", 10, now)
    planner = TaskPlanner(
        FakeGateway([remote]),
        repo,
        tmp_path / "downloads",
        uuid_factory=iter(["link", "link-item"]).__next__,
        clock=lambda: now,
    )
    filters = ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 500)
    preview = await planner.scan(parse_telegram_link("https://t.me/example"), filters)
    occupied_task = TaskRecord(
        "occupied",
        SourceKind.CHANNEL_OR_GROUP,
        "-1001",
        "资料群",
        "telegram://peer/-1001",
        filters,
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

    with pytest.raises(EmptyScanError, match="扫描媒体已全部存在于下载队列"):
        planner.commit(preview)

    with pytest.raises(KeyError):
        repo.get_task(preview.task.id)
