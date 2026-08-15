from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_downloader.catalog import CatalogRepository
from telegram_downloader.content import AccountProfile, ContentDialog, DialogKind
from telegram_downloader.domain import MediaKind
from telegram_downloader.gateway import (
    RemoteMedia,
    RemoteMessage,
    RemoteSearchHit,
    TransientNetworkError,
)
from telegram_downloader.planner import TaskPlanner
from telegram_downloader.repository import TaskRepository
from telegram_downloader.subscription_service import SubscriptionService
from telegram_downloader.subscriptions import SubscriptionDraft, SubscriptionState

NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def ids(prefix: str):
    number = 0

    def next_id() -> str:
        nonlocal number
        number += 1
        return f"{prefix}-{number}"

    return next_id


def remote(message_id: int, kind: MediaKind = MediaKind.PHOTO) -> RemoteMedia:
    suffix = "jpg" if kind is MediaKind.PHOTO else "mp4"
    return RemoteMedia(
        "-1001",
        "资料群",
        message_id,
        None,
        f"media-{message_id}",
        kind,
        f"file-{message_id}.{suffix}",
        100,
        NOW + timedelta(minutes=message_id),
    )


def message(
    message_id: int,
    text: str,
    media: RemoteMedia | None,
    *,
    grouped_id: int | None = None,
) -> RemoteMessage:
    return RemoteMessage(
        message_id,
        grouped_id,
        NOW + timedelta(minutes=message_id),
        text,
        media,
    )


class Gateway:
    def __init__(self) -> None:
        self.latest_id = 42
        self.latest_error: Exception | None = None
        self.messages: tuple[RemoteMessage, ...] = ()
        self.incremental_error: Exception | None = None
        self.albums: dict[int, tuple[RemoteSearchHit, ...]] = {}
        self.incremental_calls: list[tuple[int, int, int]] = []

    async def latest_message_id(self, _peer_ref: str) -> int:
        if self.latest_error is not None:
            raise self.latest_error
        return self.latest_id

    async def incremental_messages(
        self,
        _peer_ref: str,
        *,
        after_id: int,
        through_id: int,
        limit: int,
    ) -> tuple[RemoteMessage, ...]:
        self.incremental_calls.append((after_id, through_id, limit))
        if self.incremental_error is not None:
            raise self.incremental_error
        return tuple(
            item
            for item in self.messages
            if after_id < item.message_id <= through_id
        )[:limit]

    async def expand_album(
        self,
        _peer_ref: str,
        _message_id: int,
        grouped_id: int,
    ) -> tuple[RemoteSearchHit, ...]:
        return self.albums[grouped_id]


def build_service(tmp_path: Path):
    catalog = CatalogRepository(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_account(AccountProfile("a1", "账号"), NOW)
    catalog.replace_dialogs(
        "a1",
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
        ],
        NOW,
    )
    tasks = TaskRepository(tmp_path / "tasks.sqlite3")
    tasks.initialize()
    gateway = Gateway()
    planner = TaskPlanner(
        gateway,
        tasks,
        tmp_path / "downloads",
        uuid_factory=ids("task"),
        clock=lambda: NOW,
    )
    service = SubscriptionService(
        catalog,
        uuid_factory=ids("subscription"),
        clock=lambda: NOW,
    )
    service.bind_online(gateway, planner)
    service.set_account(AccountProfile("a1", "账号"))
    return service, gateway, catalog, tasks


@pytest.mark.asyncio
async def test_create_rule_establishes_baseline_without_queueing(
    tmp_path: Path,
) -> None:
    service, gateway, catalog, tasks = build_service(tmp_path)

    saved = await service.create_rule(
        SubscriptionDraft(
            "-1001",
            "美女",
            frozenset({MediaKind.PHOTO, MediaKind.VIDEO}),
            30,
        )
    )

    assert saved.last_message_id == 42
    assert saved.state is SubscriptionState.WAITING
    assert saved.next_run_at == NOW + timedelta(minutes=30)
    assert tasks.list_tasks() == []
    assert catalog.get_subscription("a1", saved.id) == saved
    assert gateway.incremental_calls == []


@pytest.mark.asyncio
async def test_failed_initial_baseline_is_retryable_without_recreating_rule(
    tmp_path: Path,
) -> None:
    service, gateway, catalog, tasks = build_service(tmp_path)
    gateway.latest_error = TransientNetworkError("offline")

    with pytest.raises(TransientNetworkError):
        await service.create_rule(
            SubscriptionDraft("-1001", "美女", frozenset({MediaKind.PHOTO}))
        )

    [failed] = service.list_rules()
    assert failed.state is SubscriptionState.FAILED
    assert failed.last_message_id is None
    assert failed.next_run_at == NOW
    assert failed.failure_count == 1

    gateway.latest_error = None
    report = await service.run_rule(failed.id)

    assert report.run.queued == 0
    assert tasks.list_tasks() == []
    recovered = catalog.get_subscription("a1", failed.id)
    assert recovered.state is SubscriptionState.WAITING
    assert recovered.last_message_id == 42


@pytest.mark.asyncio
async def test_run_queues_only_matching_new_media_and_advances_cursor(
    tmp_path: Path,
) -> None:
    service, gateway, catalog, tasks = build_service(tmp_path)
    saved = await service.create_rule(
        SubscriptionDraft(
            "-1001",
            "美女",
            frozenset({MediaKind.PHOTO, MediaKind.VIDEO}),
        )
    )
    gateway.latest_id = 45
    gateway.messages = (
        message(43, "普通内容", remote(43)),
        message(44, "美女写真", remote(44)),
        message(45, "美女视频", remote(45, MediaKind.VIDEO)),
    )

    report = await service.run_rule(saved.id)

    assert report.run.inspected == 3
    assert report.run.matched == 2
    assert report.run.queued == 2
    assert report.run.duplicate == 0
    assert report.has_more is False
    assert catalog.get_subscription("a1", saved.id).last_message_id == 45
    assert [item.message_id for item in tasks.list_items(report.task_ids[0])] == [
        45,
        44,
    ]


@pytest.mark.asyncio
async def test_no_match_advances_but_network_failure_does_not(
    tmp_path: Path,
) -> None:
    service, gateway, catalog, _tasks = build_service(tmp_path)
    saved = await service.create_rule(
        SubscriptionDraft("-1001", "美女", frozenset({MediaKind.PHOTO}))
    )
    gateway.latest_id = 43
    gateway.messages = (message(43, "普通内容", remote(43)),)

    report = await service.run_rule(saved.id)
    assert report.run.matched == 0
    assert catalog.get_subscription("a1", saved.id).last_message_id == 43

    gateway.latest_id = 44
    gateway.incremental_error = TransientNetworkError("offline")
    with pytest.raises(TransientNetworkError):
        await service.run_rule(saved.id)
    assert catalog.get_subscription("a1", saved.id).last_message_id == 43


@pytest.mark.asyncio
async def test_matching_album_is_expanded_and_duplicate_rerun_is_idempotent(
    tmp_path: Path,
) -> None:
    service, gateway, catalog, tasks = build_service(tmp_path)
    saved = await service.create_rule(
        SubscriptionDraft("-1001", "美女", frozenset({MediaKind.PHOTO}))
    )
    trigger = remote(43)
    trigger = RemoteMedia(
        trigger.peer_ref,
        trigger.source_title,
        trigger.message_id,
        900,
        trigger.media_id,
        trigger.kind,
        trigger.original_name,
        trigger.expected_size,
        trigger.message_date_utc,
    )
    second = remote(44)
    second = RemoteMedia(
        second.peer_ref,
        second.source_title,
        second.message_id,
        900,
        second.media_id,
        second.kind,
        second.original_name,
        second.expected_size,
        second.message_date_utc,
    )
    gateway.latest_id = 44
    gateway.messages = (message(43, "美女相册", trigger, grouped_id=900),)
    gateway.albums[900] = (
        RemoteSearchHit(trigger, "美女相册", "t1"),
        RemoteSearchHit(second, "", "t2"),
    )

    first = await service.run_rule(saved.id)
    assert first.run.queued == 2
    assert len(tasks.list_items(first.task_ids[0])) == 2

    gateway.latest_id = 42
    overlapping = await service.create_rule(
        SubscriptionDraft("-1001", "相册", frozenset({MediaKind.PHOTO}))
    )
    gateway.latest_id = 44
    second_report = await service.run_rule(overlapping.id)
    assert second_report.task_ids == ()
    assert second_report.run.queued == 0
    assert second_report.run.duplicate == 2


@pytest.mark.asyncio
async def test_rule_edit_pause_resume_due_and_delete_lifecycle(
    tmp_path: Path,
) -> None:
    service, gateway, _catalog, _tasks = build_service(tmp_path)
    saved = await service.create_rule(
        SubscriptionDraft("-1001", "美女", frozenset({MediaKind.PHOTO}), 30)
    )
    assert service.list_rules() == [saved]
    assert service.list_due_rules(NOW) == []
    assert service.list_due_rules(NOW + timedelta(minutes=30)) == [saved]

    gateway.latest_id = 99
    changed = await service.update_rule(
        saved.id,
        SubscriptionDraft("-1001", "视频", frozenset({MediaKind.VIDEO}), 60),
    )
    assert changed.last_message_id == 99
    assert changed.interval_minutes == 60

    paused = service.set_enabled(saved.id, False)
    assert paused.enabled is False
    assert paused.state is SubscriptionState.PAUSED
    assert paused.next_run_at is None
    resumed = service.set_enabled(saved.id, True)
    assert resumed.enabled is True
    assert resumed.state is SubscriptionState.WAITING
    assert resumed.next_run_at == NOW

    service.delete_rule(saved.id)
    assert service.list_rules() == []
