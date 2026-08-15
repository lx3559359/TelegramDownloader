from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from telegram_downloader.domain import MediaKind
from telegram_downloader.gateway import (
    AccessDeniedError,
    FloodWaitError,
    SessionExpiredError,
    TransientNetworkError,
)
from telegram_downloader.subscription_scheduler import SubscriptionScheduler
from telegram_downloader.subscriptions import (
    SubscriptionRule,
    SubscriptionRun,
    SubscriptionRunReport,
    SubscriptionRunStatus,
    SubscriptionState,
)

NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def rule(
    rule_id: str,
    *,
    next_run_at: datetime | None = NOW,
    failure_count: int = 0,
) -> SubscriptionRule:
    return SubscriptionRule(
        rule_id,
        "a1",
        f"peer:{rule_id}",
        f"群-{rule_id}",
        "美女",
        frozenset({MediaKind.PHOTO}),
        30,
        True,
        SubscriptionState.WAITING,
        10,
        next_run_at,
        None,
        None,
        failure_count,
        NOW,
        NOW,
    )


def report(value: SubscriptionRule) -> SubscriptionRunReport:
    run = SubscriptionRun(
        f"run-{value.id}",
        value.id,
        value.account_id,
        NOW,
        NOW,
        SubscriptionRunStatus.COMPLETED,
        1,
        1,
        1,
        0,
    )
    return SubscriptionRunReport(run, (f"task-{value.id}",), 11, False)


class Service:
    def __init__(self, *rules: SubscriptionRule) -> None:
        self.rules = {item.id: item for item in rules}
        self.run_calls: list[str] = []
        self.runtime_calls: list[tuple[str, SubscriptionState, datetime | None, int]] = []
        self.outcomes: list[Exception | SubscriptionRunReport] = []
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()
        self.block: asyncio.Event | None = None

    def list_due_rules(self, now: datetime) -> list[SubscriptionRule]:
        return [
            item
            for item in self.rules.values()
            if item.enabled
            and item.next_run_at is not None
            and item.next_run_at <= now
            and item.state
            in {
                SubscriptionState.WAITING,
                SubscriptionState.WAITING_NETWORK,
                SubscriptionState.FAILED,
            }
        ]

    def list_rules(self) -> list[SubscriptionRule]:
        return list(self.rules.values())

    def get_rule(self, rule_id: str) -> SubscriptionRule:
        return self.rules[rule_id]

    async def run_rule(self, rule_id: str, *, on_progress=None):
        self.run_calls.append(rule_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.block is not None:
                await self.block.wait()
            outcome = self.outcomes.pop(0) if self.outcomes else report(self.rules[rule_id])
            if isinstance(outcome, Exception):
                raise outcome
            self.rules[rule_id] = replace(
                self.rules[rule_id],
                state=SubscriptionState.WAITING,
                next_run_at=NOW + timedelta(minutes=30),
            )
            return outcome
        finally:
            self.active -= 1

    def update_runtime(
        self,
        rule_id: str,
        *,
        state: SubscriptionState,
        next_run_at: datetime | None,
        last_run_at: datetime | None,
        last_error: str | None,
        failure_count: int,
    ) -> None:
        self.runtime_calls.append((rule_id, state, next_run_at, failure_count))
        self.rules[rule_id] = replace(
            self.rules[rule_id],
            state=state,
            next_run_at=next_run_at,
            last_run_at=last_run_at,
            last_error=last_error,
            failure_count=failure_count,
        )

    def set_enabled(self, rule_id: str, enabled: bool) -> SubscriptionRule:
        self.rules[rule_id] = replace(
            self.rules[rule_id],
            enabled=enabled,
            state=SubscriptionState.WAITING if enabled else SubscriptionState.PAUSED,
            next_run_at=NOW if enabled else None,
        )
        return self.rules[rule_id]


async def wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_scheduler_runs_due_rules_serially_and_starts_created_tasks() -> None:
    service = Service(rule("r1"), rule("r2"))
    started_tasks: list[str] = []
    scheduler = SubscriptionScheduler(
        service,
        clock=lambda: NOW,
        foreground_busy=lambda: False,
        on_task_created=started_tasks.append,
        idle_delay=0.01,
    )
    scheduler.set_account("a1")
    scheduler.start()

    await wait_until(lambda: len(service.run_calls) == 2)
    await scheduler.shutdown()

    assert service.run_calls == ["r1", "r2"]
    assert service.max_active == 1
    assert started_tasks == ["task-r1", "task-r2"]


@pytest.mark.asyncio
async def test_manual_wake_deduplicates_the_same_rule() -> None:
    service = Service(rule("r1", next_run_at=NOW + timedelta(hours=1)))
    scheduler = SubscriptionScheduler(
        service,
        clock=lambda: NOW,
        foreground_busy=lambda: False,
        idle_delay=0.01,
    )
    scheduler.set_account("a1")
    scheduler.wake("r1")
    scheduler.wake("r1")
    scheduler.start()

    await wait_until(lambda: service.run_calls == ["r1"])
    await asyncio.sleep(0.02)
    await scheduler.shutdown()

    assert service.run_calls == ["r1"]


@pytest.mark.asyncio
async def test_manual_wake_during_active_run_does_not_queue_duplicate_run() -> None:
    service = Service(rule("r1", next_run_at=NOW + timedelta(hours=1)))
    service.block = asyncio.Event()
    scheduler = SubscriptionScheduler(
        service,
        clock=lambda: NOW,
        foreground_busy=lambda: False,
        idle_delay=0.01,
    )
    scheduler.set_account("a1")
    scheduler.wake("r1")
    scheduler.start()
    await asyncio.wait_for(service.started.wait(), timeout=1)

    scheduler.wake("r1")
    scheduler.wake("r1")
    service.block.set()
    await wait_until(lambda: service.run_calls == ["r1"])
    await asyncio.sleep(0.03)
    await scheduler.shutdown()

    assert service.run_calls == ["r1"]


@pytest.mark.asyncio
async def test_foreground_busy_defers_without_recording_failure() -> None:
    busy = True
    service = Service(rule("r1"))
    scheduler = SubscriptionScheduler(
        service,
        clock=lambda: NOW,
        foreground_busy=lambda: busy,
        idle_delay=0.01,
    )
    scheduler.set_account("a1")
    scheduler.start()

    await asyncio.sleep(0.03)
    assert service.run_calls == []
    assert service.runtime_calls == []

    busy = False
    scheduler.wake()
    await wait_until(lambda: service.run_calls == ["r1"])
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_foreground_state_change_does_not_drop_manual_wake() -> None:
    checks = iter((False, True))

    def foreground_busy() -> bool:
        return next(checks, False)

    service = Service(rule("r1", next_run_at=NOW + timedelta(hours=1)))
    scheduler = SubscriptionScheduler(
        service,
        clock=lambda: NOW,
        foreground_busy=foreground_busy,
        idle_delay=0.01,
    )
    scheduler.set_account("a1")
    scheduler.wake("r1")
    scheduler.start()

    await wait_until(lambda: service.run_calls == ["r1"])
    await scheduler.shutdown()

    assert service.run_calls == ["r1"]


@pytest.mark.asyncio
async def test_transient_and_flood_failures_use_safe_backoff() -> None:
    service = Service(rule("r1"))
    service.outcomes = [
        TransientNetworkError("offline"),
        TransientNetworkError("offline"),
        FloodWaitError(90),
    ]
    scheduler = SubscriptionScheduler(
        service,
        clock=lambda: NOW,
        foreground_busy=lambda: False,
        idle_delay=0.01,
    )
    scheduler.set_account("a1")
    scheduler.start()

    await wait_until(lambda: len(service.runtime_calls) == 1)
    scheduler.wake("r1")
    await wait_until(lambda: len(service.runtime_calls) == 2)
    scheduler.wake("r1")
    await wait_until(lambda: len(service.runtime_calls) == 3)
    await scheduler.shutdown()

    assert [item[2] for item in service.runtime_calls] == [
        NOW + timedelta(minutes=1),
        NOW + timedelta(minutes=2),
        NOW + timedelta(seconds=90),
    ]
    assert [item[3] for item in service.runtime_calls] == [1, 2, 3]
    assert all(item[1] is SubscriptionState.WAITING_NETWORK for item in service.runtime_calls)


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_active_rule() -> None:
    service = Service(rule("r1"))
    service.block = asyncio.Event()
    scheduler = SubscriptionScheduler(
        service,
        clock=lambda: NOW,
        foreground_busy=lambda: False,
        idle_delay=0.01,
    )
    scheduler.set_account("a1")
    scheduler.start()
    await asyncio.wait_for(service.started.wait(), timeout=1)

    await scheduler.shutdown()

    assert service.active == 0
    assert scheduler.running is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_state", "expected_next"),
    [
        (SessionExpiredError("expired"), SubscriptionState.AUTH_REQUIRED, None),
        (AccessDeniedError("无权访问"), SubscriptionState.PAUSED, None),
        (
            RuntimeError("boom"),
            SubscriptionState.FAILED,
            NOW + timedelta(minutes=1),
        ),
    ],
)
async def test_scheduler_classifies_non_network_failures(
    error: Exception,
    expected_state: SubscriptionState,
    expected_next: datetime | None,
) -> None:
    service = Service(rule("r1"))
    service.outcomes = [error]
    scheduler = SubscriptionScheduler(
        service,
        clock=lambda: NOW,
        foreground_busy=lambda: False,
        idle_delay=0.01,
    )
    scheduler.set_account("a1")
    scheduler.start()

    await wait_until(lambda: len(service.runtime_calls) == 1)
    await scheduler.shutdown()

    assert service.runtime_calls[0][1] is expected_state
    assert service.runtime_calls[0][2] == expected_next
    if isinstance(error, AccessDeniedError):
        assert service.rules["r1"].enabled is False
