from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_downloader.content import ContentSearchQuery
from telegram_downloader.domain import MediaKind, ScanFilters
from telegram_downloader.download_paths import DownloadPathError, DownloadPathPolicy
from telegram_downloader.downloader import MediaDownloader
from telegram_downloader.gateway import RemoteMedia
from telegram_downloader.paths import PortablePaths
from telegram_downloader.planner import TaskPlanner
from telegram_downloader.settings import (
    AppSettings,
    DownloadStorageSettings,
    SettingsStore,
)


class Gateway:
    async def stream_media(self, _peer_ref, _message_id, _offset):
        yield b"abc"


class Repository:
    def __init__(self) -> None:
        self.existing: set[tuple[str, int, str]] = set()

    def existing_media_keys(self, keys):
        return keys & self.existing

    def update_item_progress(self, *_args, **_kwargs) -> None:
        pass

    def complete_item(self, *_args, **_kwargs) -> None:
        pass


def remote(message_id: int, now: datetime) -> RemoteMedia:
    return RemoteMedia(
        "peer",
        "来源",
        message_id,
        None,
        f"media-{message_id}",
        MediaKind.VIDEO,
        f"clip-{message_id}.mp4",
        3,
        now,
    )


@pytest.mark.asyncio
async def test_custom_root_survives_restart_and_old_tasks_keep_working(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path / "app")
    paths.ensure_layout()
    external = tmp_path / "external"
    external.mkdir()
    third = tmp_path / "unknown"
    third.mkdir()
    store = SettingsStore(paths.settings)
    settings = AppSettings()
    store.save(settings)
    policy = DownloadPathPolicy(paths, settings.download_storage)
    repository = Repository()
    planner = TaskPlanner(
        Gateway(),
        repository,
        policy.current_root,
        download_root_provider=policy.require_current_writable,
    )
    now = datetime(2026, 8, 22, tzinfo=UTC)
    query = ContentSearchQuery(
        "测试",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 10),
    )
    old_item = planner.plan_selected("peer", "来源", query, [remote(1, now)]).items[0]

    prepared = policy.prepare(DownloadStorageSettings(str(external)))
    settings = replace(settings, download_storage=prepared)
    store.save(settings)
    policy.apply(prepared)
    planner.configure_downloads(policy.current_root, settings.download_naming)
    new_item = planner.plan_selected("peer", "来源", query, [remote(2, now)]).items[0]

    reloaded = store.load()
    restarted = DownloadPathPolicy(paths, reloaded.download_storage)
    media = MediaDownloader(
        Gateway(),
        repository,
        paths,
        free_bytes=lambda _path: 10**9,
        reserve_bytes=0,
        progress_interval=0,
        download_paths=restarted,
    )
    await media.download(old_item)
    await media.download(new_item)

    assert old_item.target_path.is_relative_to(paths.downloads)
    assert new_item.target_path.is_relative_to(external)
    assert old_item.target_path.read_bytes() == b"abc"
    assert new_item.target_path.read_bytes() == b"abc"
    with pytest.raises(DownloadPathError):
        await media.download(replace(new_item, target_path=third / "blocked.bin"))
