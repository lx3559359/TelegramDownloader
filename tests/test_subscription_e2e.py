from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_downloader.catalog import CatalogRepository
from telegram_downloader.content import AccountProfile, ContentDialog, DialogKind
from telegram_downloader.domain import ItemStatus, MediaKind, TaskStatus
from telegram_downloader.gateway import RemoteMedia, RemoteMessage
from telegram_downloader.paths import PortablePaths
from telegram_downloader.planner import TaskPlanner
from telegram_downloader.repository import TaskRepository
from telegram_downloader.scheduler import DownloadScheduler
from telegram_downloader.subscription_matching import SubscriptionCriteria
from telegram_downloader.subscription_scheduler import SubscriptionScheduler
from telegram_downloader.subscription_service import SubscriptionService
from telegram_downloader.subscriptions import SubscriptionDraft

NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def ids(prefix: str):
    number = 0

    def next_id() -> str:
        nonlocal number
        number += 1
        return f"{prefix}-{number}"

    return next_id


class Gateway:
    def __init__(self) -> None:
        self.latest_id = 10
        self.boundary_id = 0
        self.messages: tuple[RemoteMessage, ...] = ()

    async def latest_message_id(self, _peer_ref: str) -> int:
        return self.latest_id

    async def message_id_before(
        self,
        _peer_ref: str,
        _before_utc: datetime,
    ) -> int:
        return self.boundary_id

    async def incremental_messages(
        self,
        _peer_ref: str,
        *,
        after_id: int,
        through_id: int,
        limit: int,
    ) -> tuple[RemoteMessage, ...]:
        return tuple(item for item in self.messages if after_id < item.message_id <= through_id)[
            :limit
        ]

    async def expand_album(self, *_args) -> tuple[object, ...]:
        return ()


class Downloader:
    async def download(self, _item, should_pause) -> None:
        assert should_pause() is False
        await asyncio.sleep(0)


def remote(message_id: int) -> RemoteMedia:
    return RemoteMedia(
        "-1001",
        "测试群",
        message_id,
        None,
        f"media-{message_id}",
        MediaKind.PHOTO,
        f"photo-{message_id}.jpg",
        100,
        NOW + timedelta(minutes=message_id),
    )


async def wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_project_local_subscription_to_completed_download_and_restart(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    catalog = CatalogRepository(paths.catalog_database)
    catalog.initialize()
    profile = AccountProfile("a1", "测试账号")
    catalog.upsert_account(profile, NOW)
    catalog.replace_dialogs(
        "a1",
        [
            ContentDialog(
                "a1",
                "-1001",
                "测试群",
                "",
                DialogKind.GROUP,
                False,
                True,
                NOW,
            )
        ],
        NOW,
    )
    tasks = TaskRepository(paths.database)
    tasks.initialize()
    gateway = Gateway()
    planner = TaskPlanner(
        gateway,
        tasks,
        paths.downloads,
        uuid_factory=ids("task"),
        clock=lambda: NOW,
    )
    service = SubscriptionService(
        catalog,
        uuid_factory=ids("subscription"),
        clock=lambda: NOW,
    )
    service.bind_online(gateway, planner)
    service.set_account(profile)

    rule = await service.create_rule(
        SubscriptionDraft(
            "-1001",
            SubscriptionCriteria(("测试关键词",)),
            frozenset({MediaKind.PHOTO}),
        )
    )
    assert rule.last_message_id == 10
    assert tasks.list_tasks() == []

    gateway.latest_id = 12
    gateway.messages = tuple(
        RemoteMessage(item.message_id, None, item.message_date_utc, "测试关键词", item)
        for item in (remote(11), remote(12))
    )
    created: list[str] = []
    subscription_scheduler = SubscriptionScheduler(
        service,
        clock=lambda: NOW,
        foreground_busy=lambda: False,
        on_task_created=created.append,
        idle_delay=0.01,
    )
    subscription_scheduler.set_account("a1")
    subscription_scheduler.wake(rule.id)
    subscription_scheduler.start()
    await wait_until(lambda: len(created) == 1)
    await subscription_scheduler.shutdown()

    download_scheduler = DownloadScheduler(tasks, Downloader(), concurrency=2)
    await download_scheduler.run_task(created[0])
    assert tasks.get_task(created[0]).status is TaskStatus.COMPLETED
    items = tasks.list_items(created[0])
    assert len(items) == 2
    assert all(item.status is ItemStatus.COMPLETED for item in items)
    assert all(item.target_path.is_relative_to(paths.root) for item in items)

    restarted_catalog = CatalogRepository(paths.catalog_database)
    restarted_catalog.initialize()
    restarted = SubscriptionService(
        restarted_catalog,
        uuid_factory=ids("restart"),
        clock=lambda: NOW,
    )
    restarted.bind_online(gateway, planner)
    restarted.set_account(profile)
    assert restarted.get_rule(rule.id).last_message_id == 12
    duplicate = await restarted.run_rule(rule.id)
    assert duplicate.task_ids == ()
    assert len(tasks.list_tasks()) == 1

    for path in (
        paths.database,
        paths.catalog_database,
        *(item.target_path for item in items),
    ):
        assert path.resolve().is_relative_to(paths.root)
