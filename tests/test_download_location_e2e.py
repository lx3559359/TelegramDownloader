from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import telegram_downloader.controller as controller_module
from telegram_downloader.content import ContentSearchQuery
from telegram_downloader.controller import AppController
from telegram_downloader.domain import MediaKind, ScanFilters
from telegram_downloader.download_paths import DownloadPathError, DownloadPathPolicy
from telegram_downloader.downloader import MediaDownloader
from telegram_downloader.gateway import RemoteMedia
from telegram_downloader.paths import PortablePaths
from telegram_downloader.planner import TaskPlanner
from telegram_downloader.settings import (
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
async def test_custom_root_survives_restart_and_old_tasks_keep_working(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = PortablePaths(tmp_path / "app")
    paths.ensure_layout()
    external = tmp_path / "external"
    external.mkdir()
    third = tmp_path / "unknown"
    third.mkdir()
    store = SettingsStore(paths.settings)
    paths.settings.write_text(
        '{"check_updates_on_startup": true}',
        encoding="utf-8",
    )
    settings = store.load()
    assert settings.check_updates_on_startup is False
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
    old_plan = planner.plan_selected("peer", "来源", query, [remote(1, now)])
    old_item = old_plan.items[0]

    prepared = policy.prepare(DownloadStorageSettings(str(external)))
    settings = replace(settings, download_storage=prepared)
    store.save(settings)
    policy.apply(prepared)
    planner.configure_downloads(policy.current_root, settings.download_naming)
    new_plan = planner.plan_selected("peer", "来源", query, [remote(2, now)])
    new_item = new_plan.items[0]

    reloaded = store.load()
    restarted = DownloadPathPolicy(paths, reloaded.download_storage)
    restarted_planner = TaskPlanner(
        Gateway(),
        repository,
        restarted.current_root,
        download_root_provider=restarted.require_current_writable,
        naming=reloaded.download_naming,
    )
    assert restarted_planner.downloads == external.resolve()
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
    assert reloaded.check_updates_on_startup is False
    assert reloaded.download_storage.root == str(external.resolve())
    assert restarted.guard(old_item.target_path) == old_item.target_path.resolve()
    assert restarted.guard(new_item.target_path) == new_item.target_path.resolve()

    class DirectoryRepository:
        plans = {old_plan.task.id: old_plan, new_plan.task.id: new_plan}

        def get_task(self, task_id):
            return self.plans[task_id].task

        def list_items(self, task_id):
            return list(self.plans[task_id].items)

    opened: list[Path] = []
    monkeypatch.setattr(
        controller_module.os,
        "startfile",
        lambda path: opened.append(Path(path)),
        raising=False,
    )
    controller = AppController.for_test(
        repository=DirectoryRepository(),
        paths=paths,
        download_paths=restarted,
        settings=reloaded,
    )
    controller.open_task_directory(old_plan.task.id)
    controller.open_task_directory(new_plan.task.id)

    assert opened == [
        old_item.target_path.parent.resolve(),
        new_item.target_path.parent.resolve(),
    ]
    with pytest.raises(DownloadPathError):
        await media.download(replace(new_item, target_path=third / "blocked.bin"))
