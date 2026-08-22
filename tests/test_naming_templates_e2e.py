from datetime import UTC, datetime

import pytest

from telegram_downloader.domain import MediaKind, ScanFilters
from telegram_downloader.files import DownloadNamingSettings
from telegram_downloader.gateway import RemoteMedia
from telegram_downloader.links import parse_telegram_link
from telegram_downloader.paths import PortablePaths
from telegram_downloader.planner import TaskPlanner
from telegram_downloader.repository import TaskRepository
from telegram_downloader.settings import AppSettings, SettingsStore


class MutableGateway:
    def __init__(self, media: RemoteMedia) -> None:
        self.media = media

    async def scan(self, _source, _filters):
        yield self.media


@pytest.mark.asyncio
async def test_changed_template_only_affects_future_tasks_after_restart(tmp_path) -> None:
    now = datetime(2026, 8, 22, 9, 30, tzinfo=UTC)
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    store = SettingsStore(paths.settings)
    repository = TaskRepository(paths.database)
    repository.initialize()
    first_naming = DownloadNamingSettings(
        "{year}/{month}/{source}/{media_type}",
        "{stem}_{message_id}{extension}",
    )
    store.save(AppSettings(download_naming=first_naming))
    gateway = MutableGateway(
        RemoteMedia(
            "peer",
            "资料群",
            41,
            None,
            "m41",
            MediaKind.VIDEO,
            "clip.mp4",
            100,
            now,
        )
    )
    planner = TaskPlanner(
        gateway,
        repository,
        paths.downloads,
        uuid_factory=iter(("task-1", "item-1", "task-2", "item-2")).__next__,
        clock=lambda: now,
        naming=store.load().download_naming,
    )
    filters = ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 20)
    source = parse_telegram_link("https://t.me/example")

    first = planner.commit(await planner.scan(source, filters))
    first_target = repository.list_items(first.task.id)[0].target_path

    second_naming = DownloadNamingSettings(
        "{source}/{message_date}",
        "{message_id}_{original_name}",
    )
    store.save(AppSettings(download_naming=second_naming))
    planner.configure_naming(store.load().download_naming)
    gateway.media = RemoteMedia(
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
    second = planner.commit(await planner.scan(source, filters))

    reopened = TaskRepository(paths.database)
    reopened.initialize()
    assert store.load().download_naming == second_naming
    assert reopened.list_items(first.task.id)[0].target_path == first_target
    assert first_target == (
        paths.downloads / "2026" / "08" / "资料群" / "video" / "clip_41.mp4"
    )
    assert reopened.list_items(second.task.id)[0].target_path == (
        paths.downloads / "资料群" / "2026-08-22" / "42_clip.mp4"
    )
