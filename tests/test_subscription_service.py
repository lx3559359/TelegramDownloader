from __future__ import annotations

import asyncio
from dataclasses import replace
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
from telegram_downloader.subscription_matching import (
    SubscriptionCriteria,
    SubscriptionMatchMode,
)
from telegram_downloader.subscription_service import SubscriptionService
from telegram_downloader.subscriptions import (
    SubscriptionDraft,
    SubscriptionProbeProgress,
    SubscriptionState,
)

NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def criteria(value: str) -> SubscriptionCriteria:
    return SubscriptionCriteria((value,))


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
        self.latest_calls = 0
        self.latest_error: Exception | None = None
        self.boundary_id = 0
        self.boundary_calls: list[tuple[str, datetime]] = []
        self.messages: tuple[RemoteMessage, ...] = ()
        self.incremental_error: Exception | None = None
        self.albums: dict[int, tuple[RemoteSearchHit, ...]] = {}
        self.incremental_calls: list[tuple[int, int, int]] = []
        self.incremental_started: asyncio.Event | None = None
        self.incremental_release: asyncio.Event | None = None
        self.recent: tuple[RemoteMessage, ...] = ()
        self.recent_calls: list[tuple[str, int]] = []
        self.recent_started: asyncio.Event | None = None
        self.recent_release: asyncio.Event | None = None

    async def latest_message_id(self, _peer_ref: str) -> int:
        self.latest_calls += 1
        if self.latest_error is not None:
            raise self.latest_error
        return self.latest_id

    async def message_id_before(
        self,
        peer_ref: str,
        before_utc: datetime,
    ) -> int:
        self.boundary_calls.append((peer_ref, before_utc))
        return self.boundary_id

    async def incremental_messages(
        self,
        _peer_ref: str,
        *,
        after_id: int,
        through_id: int,
        limit: int,
    ) -> tuple[RemoteMessage, ...]:
        self.incremental_calls.append((after_id, through_id, limit))
        if self.incremental_started is not None:
            self.incremental_started.set()
        if self.incremental_release is not None:
            await self.incremental_release.wait()
        if self.incremental_error is not None:
            raise self.incremental_error
        return tuple(item for item in self.messages if after_id < item.message_id <= through_id)[
            :limit
        ]

    async def recent_messages(
        self,
        peer_ref: str,
        *,
        limit: int,
    ) -> tuple[RemoteMessage, ...]:
        self.recent_calls.append((peer_ref, limit))
        if self.recent_started is not None:
            self.recent_started.set()
        if self.recent_release is not None:
            await self.recent_release.wait()
        return self.recent[:limit]

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
            criteria("美女"),
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
async def test_create_historical_rule_locks_cutoff_and_snapshot(
    tmp_path: Path,
) -> None:
    service, gateway, catalog, tasks = build_service(tmp_path)
    gateway.latest_id = 900
    gateway.boundary_id = 300

    saved = await service.create_rule(
        SubscriptionDraft(
            "-1001",
            SubscriptionCriteria(("AI",)),
            frozenset({MediaKind.PHOTO}),
            history_days=7,
        )
    )

    assert gateway.boundary_calls == [("-1001", NOW - timedelta(days=7))]
    assert saved.last_message_id == 300
    assert saved.backfill_from_utc == NOW - timedelta(days=7)
    assert saved.backfill_through_id == 900
    assert saved.next_run_at == NOW
    assert tasks.list_tasks() == []
    assert catalog.get_subscription("a1", saved.id) == saved


@pytest.mark.asyncio
async def test_failed_history_baseline_retries_original_cutoff(
    tmp_path: Path,
) -> None:
    service, gateway, _catalog, _tasks = build_service(tmp_path)
    gateway.latest_error = TransientNetworkError("offline")
    draft = SubscriptionDraft(
        "-1001",
        SubscriptionCriteria(("AI",)),
        frozenset({MediaKind.PHOTO}),
        history_days=3,
    )

    with pytest.raises(TransientNetworkError):
        await service.create_rule(draft)

    failed = service.list_rules()[0]
    assert failed.backfill_from_utc == NOW - timedelta(days=3)
    gateway.latest_error = None
    gateway.latest_id = 500
    gateway.boundary_id = 100

    await service.run_rule(failed.id)

    assert gateway.boundary_calls[-1] == ("-1001", NOW - timedelta(days=3))


@pytest.mark.asyncio
async def test_history_catchup_uses_fixed_snapshot_and_clears_last_page(
    tmp_path: Path,
) -> None:
    service, gateway, catalog, _tasks = build_service(tmp_path)
    gateway.latest_id = 1200
    gateway.boundary_id = 0
    saved = await service.create_rule(
        SubscriptionDraft(
            "-1001",
            SubscriptionCriteria(("AI",)),
            frozenset({MediaKind.PHOTO}),
            history_days=7,
        )
    )
    gateway.latest_id = 1300
    gateway.messages = tuple(message(number, "AI", remote(number)) for number in range(1, 1301))

    first = await service.run_rule(saved.id)
    second = await service.run_rule(saved.id)
    third = await service.run_rule(saved.id)

    assert gateway.incremental_calls == [
        (0, 1200, 500),
        (500, 1200, 500),
        (1000, 1200, 500),
    ]
    assert first.has_more is True
    assert second.has_more is True
    assert third.has_more is False
    completed = catalog.get_subscription("a1", saved.id)
    assert completed.last_message_id == 1200
    assert completed.backfill_from_utc is None
    assert completed.backfill_through_id is None

    await service.run_rule(saved.id)
    assert gateway.incremental_calls[-1] == (1200, 1300, 500)


@pytest.mark.asyncio
async def test_history_catchup_failure_cancel_and_restart_preserve_cursor(
    tmp_path: Path,
) -> None:
    service, gateway, catalog, tasks = build_service(tmp_path)
    gateway.latest_id = 1200
    gateway.boundary_id = 0
    saved = await service.create_rule(
        SubscriptionDraft(
            "-1001",
            SubscriptionCriteria(("AI",)),
            frozenset({MediaKind.PHOTO}),
            history_days=7,
        )
    )
    gateway.messages = tuple(message(number, "AI", remote(number)) for number in range(1, 1201))
    await service.run_rule(saved.id)
    assert catalog.get_subscription("a1", saved.id).last_message_id == 500

    gateway.incremental_error = TransientNetworkError("offline")
    with pytest.raises(TransientNetworkError):
        await service.run_rule(saved.id)
    failed = catalog.get_subscription("a1", saved.id)
    assert failed.last_message_id == 500
    assert failed.backfill_through_id == 1200

    gateway.incremental_error = None
    gateway.incremental_started = asyncio.Event()
    gateway.incremental_release = asyncio.Event()
    running = asyncio.create_task(service.run_rule(saved.id))
    await gateway.incremental_started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    cancelled = catalog.get_subscription("a1", saved.id)
    assert cancelled.last_message_id == 500
    assert cancelled.backfill_through_id == 1200

    gateway.incremental_started = None
    gateway.incremental_release = None
    reopened_catalog = CatalogRepository(catalog.database)
    reopened_catalog.initialize()
    reopened_planner = TaskPlanner(
        gateway,
        tasks,
        tmp_path / "downloads",
        uuid_factory=ids("reopened-task"),
        clock=lambda: NOW,
    )
    reopened = SubscriptionService(
        reopened_catalog,
        uuid_factory=ids("reopened-subscription"),
        clock=lambda: NOW,
    )
    reopened.bind_online(gateway, reopened_planner)
    reopened.set_account(AccountProfile("a1", "账号"))

    assert reopened.get_rule(saved.id).last_message_id == 500
    await reopened.run_rule(saved.id)
    assert gateway.incremental_calls[-1] == (500, 1200, 500)


@pytest.mark.asyncio
async def test_failed_initial_baseline_is_retryable_without_recreating_rule(
    tmp_path: Path,
) -> None:
    service, gateway, catalog, tasks = build_service(tmp_path)
    gateway.latest_error = TransientNetworkError("offline")

    with pytest.raises(TransientNetworkError):
        await service.create_rule(
            SubscriptionDraft("-1001", criteria("美女"), frozenset({MediaKind.PHOTO}))
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
            criteria("美女"),
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
        SubscriptionDraft("-1001", criteria("美女"), frozenset({MediaKind.PHOTO}))
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
        SubscriptionDraft("-1001", criteria("美女"), frozenset({MediaKind.PHOTO}))
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
        SubscriptionDraft("-1001", criteria("相册"), frozenset({MediaKind.PHOTO}))
    )
    gateway.latest_id = 44
    second_report = await service.run_rule(overlapping.id)
    assert second_report.task_ids == ()
    assert second_report.run.queued == 0
    assert second_report.run.duplicate == 2


@pytest.mark.asyncio
async def test_probe_rule_reports_matches_without_side_effects(tmp_path: Path) -> None:
    service, gateway, catalog, tasks = build_service(tmp_path)
    saved = await service.create_rule(
        SubscriptionDraft("-1001", criteria("美女"), frozenset({MediaKind.PHOTO}))
    )
    gateway.recent = (
        message(40, "普通", remote(40)),
        message(41, "美女写真", remote(41)),
        message(42, "美女视频", remote(42, MediaKind.VIDEO)),
    )
    before_rule = service.get_rule(saved.id)
    before_runs = catalog.list_subscription_runs("a1", saved.id)
    before_tasks = tasks.list_tasks()
    progress: list[SubscriptionProbeProgress] = []

    report = await service.probe_rule(saved.id, on_progress=progress.append)

    assert (report.inspected, report.keyword_hits, report.matched) == (3, 2, 1)
    assert report.duplicate == 0
    assert [sample.message_id for sample in report.samples] == [41]
    assert gateway.recent_calls == [("-1001", 100)]
    assert [item.inspected for item in progress] == sorted(item.inspected for item in progress)
    assert progress[0].inspected == 0
    assert progress[-1].phase == "测试完成"
    assert service.get_rule(saved.id) == before_rule
    assert catalog.list_subscription_runs("a1", saved.id) == before_runs
    assert tasks.list_tasks() == before_tasks


@pytest.mark.asyncio
async def test_probe_and_scheduled_run_share_advanced_matcher(tmp_path: Path) -> None:
    service, gateway, _catalog, _tasks = build_service(tmp_path)
    saved = await service.create_rule(
        SubscriptionDraft(
            "-1001",
            SubscriptionCriteria(
                ("AI", "模型"),
                ("广告",),
                SubscriptionMatchMode.ALL,
            ),
            frozenset({MediaKind.PHOTO}),
        )
    )
    messages = (
        message(43, "AI 模型", remote(43)),
        message(44, "AI 模型 广告", remote(44)),
        message(45, "只有 AI", remote(45)),
    )
    gateway.recent = messages
    gateway.messages = messages
    gateway.latest_id = 45

    probe = await service.probe_rule(saved.id)
    run = await service.run_rule(saved.id)

    assert (probe.inspected, probe.keyword_hits, probe.matched) == (3, 1, 1)
    assert (run.run.inspected, run.run.keyword_hits, run.run.matched) == (3, 1, 1)


@pytest.mark.asyncio
async def test_probe_expands_album_marks_duplicates_and_matches_formal_run(
    tmp_path: Path,
) -> None:
    service, gateway, catalog, tasks = build_service(tmp_path)
    first_rule = await service.create_rule(
        SubscriptionDraft("-1001", criteria("美女"), frozenset({MediaKind.PHOTO}))
    )
    trigger = replace(remote(43), grouped_id=900)
    second = replace(remote(44), grouped_id=900)
    excluded = replace(remote(45, MediaKind.VIDEO), grouped_id=900)
    album_message = message(43, "美女相册", trigger, grouped_id=900)
    gateway.latest_id = 45
    gateway.messages = (album_message,)
    gateway.albums[900] = (
        RemoteSearchHit(trigger, "美女相册", "t1"),
        RemoteSearchHit(second, "", "t2"),
        RemoteSearchHit(excluded, "", "t3"),
        RemoteSearchHit(second, "重复", "t2-duplicate"),
    )
    first_run = await service.run_rule(first_rule.id)
    assert first_run.run.matched == 2

    gateway.latest_id = 42
    parity_rule = await service.create_rule(
        SubscriptionDraft("-1001", criteria("相册"), frozenset({MediaKind.PHOTO}))
    )
    gateway.latest_id = 45
    gateway.recent = (album_message,)
    before_rule = service.get_rule(parity_rule.id)
    before_runs = catalog.list_subscription_runs("a1", parity_rule.id)
    before_tasks = tasks.list_tasks()

    probe = await service.probe_rule(parity_rule.id)

    assert (probe.keyword_hits, probe.matched, probe.duplicate) == (1, 2, 2)
    assert [sample.message_id for sample in probe.samples] == [44, 43]
    assert all(sample.already_queued for sample in probe.samples)
    assert service.get_rule(parity_rule.id) == before_rule
    assert catalog.list_subscription_runs("a1", parity_rule.id) == before_runs
    assert tasks.list_tasks() == before_tasks

    formal = await service.run_rule(parity_rule.id)
    assert (
        formal.run.keyword_hits,
        formal.run.matched,
        formal.run.duplicate,
    ) == (probe.keyword_hits, probe.matched, probe.duplicate)


@pytest.mark.asyncio
async def test_probe_limits_samples_and_excerpt_length(tmp_path: Path) -> None:
    service, gateway, _catalog, _tasks = build_service(tmp_path)
    saved = await service.create_rule(
        SubscriptionDraft("-1001", criteria("资料"), frozenset({MediaKind.PHOTO}))
    )
    gateway.recent = tuple(
        message(value, "资料" + "长" * 100, remote(value)) for value in range(43, 68)
    )

    report = await service.probe_rule(saved.id)

    assert report.matched == 25
    assert len(report.samples) == 20
    assert all(len(sample.excerpt) <= 80 for sample in report.samples)


@pytest.mark.asyncio
async def test_probe_cancellation_leaves_rule_cursor_history_and_tasks_unchanged(
    tmp_path: Path,
) -> None:
    service, gateway, catalog, tasks = build_service(tmp_path)
    saved = await service.create_rule(
        SubscriptionDraft("-1001", criteria("资料"), frozenset({MediaKind.PHOTO}))
    )
    gateway.recent_started = asyncio.Event()
    gateway.recent_release = asyncio.Event()
    before_rule = service.get_rule(saved.id)
    before_runs = catalog.list_subscription_runs("a1", saved.id)
    before_tasks = tasks.list_tasks()
    running = asyncio.create_task(service.probe_rule(saved.id))
    await gateway.recent_started.wait()

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert service.get_rule(saved.id) == before_rule
    assert catalog.list_subscription_runs("a1", saved.id) == before_runs
    assert tasks.list_tasks() == before_tasks


@pytest.mark.asyncio
async def test_list_runs_is_account_scoped_and_bounded(tmp_path: Path) -> None:
    service, gateway, _catalog, _tasks = build_service(tmp_path)
    saved = await service.create_rule(
        SubscriptionDraft("-1001", criteria("资料"), frozenset({MediaKind.PHOTO}))
    )
    gateway.latest_id = 43
    gateway.messages = (message(43, "资料", remote(43)),)
    await service.run_rule(saved.id)

    assert len(service.list_runs(saved.id, limit=1)) == 1
    with pytest.raises(ValueError, match="1 到 100"):
        service.list_runs(saved.id, limit=101)
    with pytest.raises(KeyError):
        service.list_runs("missing")


@pytest.mark.asyncio
async def test_rule_edit_pause_resume_due_and_delete_lifecycle(
    tmp_path: Path,
) -> None:
    service, gateway, _catalog, _tasks = build_service(tmp_path)
    saved = await service.create_rule(
        SubscriptionDraft("-1001", criteria("美女"), frozenset({MediaKind.PHOTO}), 30)
    )
    assert service.list_rules() == [saved]
    assert service.list_due_rules(NOW) == []
    assert service.list_due_rules(NOW + timedelta(minutes=30)) == [saved]

    gateway.latest_id = 99
    changed = await service.update_rule(
        saved.id,
        SubscriptionDraft("-1001", criteria("视频"), frozenset({MediaKind.VIDEO}), 60),
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


@pytest.mark.asyncio
async def test_only_semantic_edits_reestablish_history_baseline(tmp_path: Path) -> None:
    service, gateway, _catalog, _tasks = build_service(tmp_path)
    original_draft = SubscriptionDraft(
        "-1001",
        SubscriptionCriteria(("AI",)),
        frozenset({MediaKind.PHOTO}),
        30,
        0,
    )
    saved = await service.create_rule(original_draft)
    assert gateway.latest_calls == 1

    interval_only = await service.update_rule(
        saved.id,
        replace(original_draft, interval_minutes=60),
    )

    assert interval_only.last_message_id == saved.last_message_id
    assert gateway.latest_calls == 1

    gateway.latest_id = 900
    gateway.boundary_id = 300
    changed = await service.update_rule(
        saved.id,
        replace(
            original_draft,
            criteria=SubscriptionCriteria(("模型",)),
            history_days=7,
        ),
    )

    assert gateway.latest_calls == 2
    assert changed.last_message_id == 300
    assert changed.backfill_through_id == 900
    assert changed.next_run_at == NOW


@pytest.mark.asyncio
async def test_rebaselined_rule_does_not_duplicate_existing_media(tmp_path: Path) -> None:
    service, gateway, _catalog, tasks = build_service(tmp_path)
    saved = await service.create_rule(
        SubscriptionDraft(
            "-1001",
            SubscriptionCriteria(("AI",)),
            frozenset({MediaKind.PHOTO}),
        )
    )
    gateway.latest_id = 43
    gateway.messages = (message(43, "AI 模型", remote(43)),)
    first = await service.run_rule(saved.id)
    assert first.run.queued == 1

    gateway.boundary_id = 42
    changed = await service.update_rule(
        saved.id,
        SubscriptionDraft(
            "-1001",
            SubscriptionCriteria(("AI", "模型")),
            frozenset({MediaKind.PHOTO}),
            history_days=7,
        ),
    )
    second = await service.run_rule(changed.id)

    assert second.run.queued == 0
    assert second.run.duplicate == 1
    assert len(tasks.list_tasks()) == 1


@pytest.mark.asyncio
async def test_successful_reconnection_makes_blocked_rule_due_immediately(
    tmp_path: Path,
) -> None:
    service, _gateway, catalog, _tasks = build_service(tmp_path)
    saved = await service.create_rule(
        SubscriptionDraft("-1001", criteria("美女"), frozenset({MediaKind.PHOTO}))
    )
    catalog.save_subscription(
        replace(
            saved,
            state=SubscriptionState.AUTH_REQUIRED,
            next_run_at=None,
            last_error="Telegram 登录已失效",
        )
    )

    assert service.resume_after_connection() == 1

    [resumed] = service.list_due_rules(NOW)
    assert resumed.id == saved.id
    assert resumed.state is SubscriptionState.WAITING
    assert resumed.next_run_at == NOW
    assert resumed.last_error == "Telegram 登录已失效"


@pytest.mark.asyncio
async def test_editing_paused_rule_does_not_resume_it(tmp_path: Path) -> None:
    service, _gateway, _catalog, _tasks = build_service(tmp_path)
    saved = await service.create_rule(
        SubscriptionDraft("-1001", criteria("美女"), frozenset({MediaKind.PHOTO}), 30)
    )
    service.set_enabled(saved.id, False)

    changed = await service.update_rule(
        saved.id,
        SubscriptionDraft("-1001", criteria("美女"), frozenset({MediaKind.VIDEO}), 60),
    )

    assert changed.enabled is False
    assert changed.state is SubscriptionState.PAUSED
    assert changed.next_run_at is None
