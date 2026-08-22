from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt

from telegram_downloader.catalog import CatalogRepository
from telegram_downloader.content import AccountProfile, ContentDialog, DialogKind
from telegram_downloader.controller import AppController
from telegram_downloader.domain import ItemStatus, MediaKind, TaskStatus
from telegram_downloader.gateway import RemoteMedia, RemoteMessage
from telegram_downloader.paths import PortablePaths
from telegram_downloader.planner import TaskPlanner
from telegram_downloader.repository import TaskRepository
from telegram_downloader.scheduler import DownloadScheduler
from telegram_downloader.subscription_matching import SubscriptionCriteria
from telegram_downloader.subscription_service import SubscriptionService
from telegram_downloader.subscriptions import (
    SubscriptionDraft,
    SubscriptionProbeReport,
    SubscriptionProbeSample,
    SubscriptionRule,
    SubscriptionState,
)
from telegram_downloader.ui.subscriptions import SubscriptionPage

NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def ids(prefix: str):
    number = 0

    def next_id() -> str:
        nonlocal number
        number += 1
        return f"{prefix}-{number}"

    return next_id


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


def message(message_id: int, text: str) -> RemoteMessage:
    media = remote(message_id)
    return RemoteMessage(message_id, None, media.message_date_utc, text, media)


class Gateway:
    def __init__(self) -> None:
        self.latest_id = 10
        self.messages: tuple[RemoteMessage, ...] = ()
        self.recent: tuple[RemoteMessage, ...] = ()

    async def latest_message_id(self, _peer_ref: str) -> int:
        return self.latest_id

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

    async def recent_messages(
        self,
        _peer_ref: str,
        *,
        limit: int,
    ) -> tuple[RemoteMessage, ...]:
        return self.recent[:limit]

    async def expand_album(self, *_args) -> tuple[object, ...]:
        return ()


class Downloader:
    async def download(self, _item, should_pause) -> None:
        assert should_pause() is False
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_probe_formal_run_download_and_restart_stay_project_local(
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
            SubscriptionCriteria(("资料",)),
            frozenset({MediaKind.PHOTO}),
        )
    )

    seeded = planner.plan_subscription("-1001", "测试群", "预置", [remote(11)])
    planner.commit_selected(seeded)
    gateway.recent = (
        message(11, "资料一"),
        message(12, "资料二"),
        message(13, "普通内容"),
    )
    before_tasks = tasks.list_tasks()
    before_runs = catalog.list_subscription_runs("a1", rule.id)

    probe = await service.probe_rule(rule.id)

    assert (probe.inspected, probe.keyword_hits, probe.matched, probe.duplicate) == (
        3,
        2,
        2,
        1,
    )
    assert tasks.list_tasks() == before_tasks
    assert service.get_rule(rule.id).last_message_id == 10
    assert catalog.list_subscription_runs("a1", rule.id) == before_runs

    gateway.latest_id = 13
    gateway.messages = gateway.recent
    formal = await service.run_rule(rule.id)
    assert (
        formal.run.keyword_hits,
        formal.run.matched,
        formal.run.queued,
        formal.run.duplicate,
    ) == (2, 2, 1, 1)
    assert len(tasks.list_tasks()) == 2

    scheduler = DownloadScheduler(tasks, Downloader(), concurrency=1)
    await scheduler.run_task(formal.task_ids[0])
    assert tasks.get_task(formal.task_ids[0]).status is TaskStatus.COMPLETED
    assert all(item.status is ItemStatus.COMPLETED for item in tasks.list_items(formal.task_ids[0]))

    restarted_catalog = CatalogRepository(paths.catalog_database)
    restarted_catalog.initialize()
    restarted_tasks = TaskRepository(paths.database)
    restarted_tasks.initialize()
    restarted_planner = TaskPlanner(gateway, restarted_tasks, paths.downloads)
    restarted = SubscriptionService(restarted_catalog, clock=lambda: NOW)
    restarted.bind_online(gateway, restarted_planner)
    restarted.set_account(profile)

    [saved_run] = restarted.list_runs(rule.id)
    assert saved_run.keyword_hits == 2
    assert restarted.get_rule(rule.id).last_message_id == 13
    before_second_probe = restarted_tasks.list_tasks()
    second_probe = await restarted.probe_rule(rule.id)
    assert second_probe.duplicate == 2
    assert restarted_tasks.list_tasks() == before_second_probe

    for path in (
        paths.database,
        paths.catalog_database,
        *(item.target_path for item in restarted_tasks.list_items(formal.task_ids[0])),
    ):
        assert path.resolve().is_relative_to(paths.root)


def ui_rule() -> SubscriptionRule:
    return SubscriptionRule(
        id="rule-1",
        account_id="a1",
        peer_ref="-1001",
        dialog_title="资料群",
        criteria=SubscriptionCriteria(("资料",)),
        media_kinds=frozenset({MediaKind.PHOTO}),
        interval_minutes=30,
        history_days=0,
        enabled=True,
        state=SubscriptionState.WAITING,
        last_message_id=10,
        backfill_from_utc=None,
        backfill_through_id=None,
        next_run_at=NOW + timedelta(minutes=30),
        last_run_at=None,
        last_error=None,
        failure_count=0,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_real_qt_page_controller_probe_cancel_and_complete(qtbot) -> None:
    rule = ui_rule()
    sample = SubscriptionProbeSample(
        12,
        NOW,
        MediaKind.PHOTO,
        "photo.jpg",
        100,
        False,
        "资料摘要",
    )
    report = SubscriptionProbeReport("rule-1", 2, 1, 1, 0, (sample,), NOW)

    class Service:
        def __init__(self) -> None:
            self.account = AccountProfile("a1", "账号")
            self.started = asyncio.Event()
            self.block = True
            self.calls = []

        def get_rule(self, rule_id):
            if rule_id != rule.id:
                raise KeyError(rule_id)
            return rule

        def list_runs(self, _rule_id, *, limit=20):
            return []

        async def probe_rule(self, rule_id, *, on_progress=None):
            self.calls.append(rule_id)
            self.started.set()
            if self.block:
                await asyncio.Event().wait()
            return report

    page = SubscriptionPage()
    qtbot.addWidget(page)
    page.set_logged_in(True)
    page.set_dialogs(
        [
            ContentDialog(
                "a1",
                "-1001",
                "资料群",
                "",
                DialogKind.GROUP,
                False,
                True,
                NOW,
            )
        ]
    )
    page.set_rules([rule])
    window = SimpleNamespace(
        subscriptions_page=page,
        statusBar=lambda: SimpleNamespace(showMessage=lambda *_args: None),
    )
    service = Service()
    controller = AppController.for_test(subscriptions=service, window=window)
    running: list[asyncio.Task[None]] = []
    page.rule_selected.connect(controller.show_subscription_details)
    page.probe_requested.connect(
        lambda rule_id: running.append(asyncio.create_task(controller.probe_subscription(rule_id)))
    )
    page.probe_cancel_requested.connect(controller.cancel_subscription_probe)
    page.rule_table.selectRow(0)

    qtbot.mouseClick(page.probe_button, Qt.MouseButton.LeftButton)
    await service.started.wait()
    qtbot.mouseClick(page.probe_button, Qt.MouseButton.LeftButton)
    assert service.calls == ["rule-1"]

    qtbot.mouseClick(page.probe_cancel_button, Qt.MouseButton.LeftButton)
    await running[-1]
    assert "均未改变" in page.probe_result_label.text()
    assert page.probe_button.isEnabled()

    service.block = False
    service.started = asyncio.Event()
    qtbot.mouseClick(page.probe_button, Qt.MouseButton.LeftButton)
    await running[-1]

    assert service.calls == ["rule-1", "rule-1"]
    assert page.probe_sample_model.rowCount() == 1
    assert page.probe_button.isEnabled()
    assert controller._subscription_probe_task is None
