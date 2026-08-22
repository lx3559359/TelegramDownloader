import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from telegram_downloader.settings import StorageMaintenanceSettings
from telegram_downloader.storage_models import (
    StorageMaintenanceState,
    StorageResultCode,
    StorageTrigger,
)
from telegram_downloader.storage_scheduler import StorageMaintenanceScheduler

BASE = datetime(2026, 8, 22, 8, tzinfo=UTC)


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def utc(self) -> datetime:
        return BASE + timedelta(seconds=self.seconds)

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.seconds + seconds, future))
        await future

    async def advance(self, seconds: float) -> None:
        self.seconds += seconds
        for deadline, future in tuple(self.sleepers):
            if deadline <= self.seconds and not future.done():
                future.set_result(None)
        self.sleepers = [
            (deadline, future) for deadline, future in self.sleepers if not future.done()
        ]
        await settle()


class FakeActivity:
    def __init__(self) -> None:
        self.idle_calls: list[float] = []
        self.idle_gate: asyncio.Event | None = None

    async def wait_for_continuous_idle(self, seconds: float) -> bool:
        self.idle_calls.append(seconds)
        if self.idle_gate is not None:
            await self.idle_gate.wait()
        return True


class FakeService:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.settings = StorageMaintenanceSettings()
        self.state = StorageMaintenanceState()
        self.results: list[StorageResultCode] = [StorageResultCode.COMPLETED]
        self.calls: list[StorageTrigger] = []
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.shutdown_calls = 0

    def load_state(self) -> StorageMaintenanceState:
        return self.state

    async def clean_safe(self, trigger: StorageTrigger):
        self.calls.append(trigger)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        code = self.results.pop(0) if self.results else StorageResultCode.COMPLETED
        if code in {
            StorageResultCode.COMPLETED,
            StorageResultCode.NOTHING_TO_CLEAN,
        }:
            self.state = replace(
                self.state,
                next_due_at=self.clock.utc() + timedelta(seconds=86400),
            )
        return SimpleNamespace(result_code=code)

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


def enabled() -> StorageMaintenanceSettings:
    return replace(StorageMaintenanceSettings(), automatic_enabled=True)


def make_scheduler(
    clock: FakeClock,
    service: FakeService,
    activity: FakeActivity,
    settings: StorageMaintenanceSettings,
) -> StorageMaintenanceScheduler:
    current = {"settings": settings}
    scheduler = StorageMaintenanceScheduler(
        service,
        activity,
        lambda: current["settings"],
        utc_clock=clock.utc,
        sleep=clock.sleep,
    )
    scheduler.test_settings = current
    return scheduler


@pytest.mark.asyncio
async def test_enabled_scheduler_waits_startup_and_continuous_idle() -> None:
    clock = FakeClock()
    service = FakeService(clock)
    activity = FakeActivity()
    scheduler = make_scheduler(clock, service, activity, enabled())
    scheduler.start()
    await settle()

    await clock.advance(299)
    assert service.calls == []
    await clock.advance(1)

    assert service.calls == [StorageTrigger.AUTOMATIC]
    assert activity.idle_calls == [60]
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_disabled_to_enabled_transition_gets_full_startup_delay() -> None:
    clock = FakeClock()
    service = FakeService(clock)
    activity = FakeActivity()
    scheduler = make_scheduler(clock, service, activity, StorageMaintenanceSettings())
    scheduler.start()
    await settle()
    await clock.advance(1000)
    assert service.calls == []

    new_settings = enabled()
    scheduler.test_settings["settings"] = new_settings
    scheduler.reconfigure(new_settings)
    await settle()
    await clock.advance(299)
    assert service.calls == []
    await clock.advance(1)

    assert service.calls == [StorageTrigger.AUTOMATIC]
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_scheduler_waits_formal_due_after_startup_delay() -> None:
    clock = FakeClock()
    service = FakeService(clock)
    service.state = replace(
        service.state,
        next_due_at=BASE + timedelta(seconds=600),
    )
    activity = FakeActivity()
    scheduler = make_scheduler(clock, service, activity, enabled())
    scheduler.start()
    await settle()

    await clock.advance(300)
    assert service.calls == []
    await clock.advance(299)
    assert service.calls == []
    await clock.advance(1)

    assert service.calls == [StorageTrigger.AUTOMATIC]
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_busy_result_retries_after_fixed_delay() -> None:
    clock = FakeClock()
    service = FakeService(clock)
    service.results = [
        StorageResultCode.BUSY_DEFERRED,
        StorageResultCode.COMPLETED,
    ]
    activity = FakeActivity()
    scheduler = make_scheduler(clock, service, activity, enabled())
    scheduler.start()
    await settle()
    await clock.advance(300)
    assert len(service.calls) == 1

    await clock.advance(899)
    assert len(service.calls) == 1
    await clock.advance(1)

    assert len(service.calls) == 2
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_disabling_during_cleanup_does_not_cancel_current_run() -> None:
    clock = FakeClock()
    service = FakeService(clock)
    service.release = asyncio.Event()
    activity = FakeActivity()
    scheduler = make_scheduler(clock, service, activity, enabled())
    scheduler.start()
    await settle()
    await clock.advance(300)
    await service.started.wait()

    disabled = StorageMaintenanceSettings()
    scheduler.test_settings["settings"] = disabled
    scheduler.reconfigure(disabled)
    await settle()
    assert len(service.calls) == 1
    assert service.release.is_set() is False

    service.release.set()
    await settle()
    await clock.advance(86400)
    assert len(service.calls) == 1
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_disabling_interrupts_continuous_idle_wait() -> None:
    clock = FakeClock()
    service = FakeService(clock)
    activity = FakeActivity()
    activity.idle_gate = asyncio.Event()
    scheduler = make_scheduler(clock, service, activity, enabled())
    scheduler.start()
    await settle()
    await clock.advance(300)
    assert activity.idle_calls == [60]

    disabled = StorageMaintenanceSettings()
    scheduler.test_settings["settings"] = disabled
    scheduler.reconfigure(disabled)
    await settle()

    assert service.calls == []
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_wait_and_converges_service() -> None:
    clock = FakeClock()
    service = FakeService(clock)
    activity = FakeActivity()
    scheduler = make_scheduler(clock, service, activity, enabled())
    scheduler.start()
    await settle()

    await scheduler.shutdown()

    assert service.calls == []
    assert service.shutdown_calls == 1


@pytest.mark.asyncio
async def test_shutdown_cancels_active_runner_before_service_convergence() -> None:
    clock = FakeClock()
    service = FakeService(clock)
    service.release = asyncio.Event()
    activity = FakeActivity()
    scheduler = make_scheduler(clock, service, activity, enabled())
    scheduler.start()
    await settle()
    await clock.advance(300)
    await service.started.wait()

    await scheduler.shutdown()

    assert service.calls == [StorageTrigger.AUTOMATIC]
    assert service.release.is_set() is False
    assert service.shutdown_calls == 1
