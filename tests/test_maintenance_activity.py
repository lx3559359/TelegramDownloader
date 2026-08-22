import asyncio

import pytest

from telegram_downloader.maintenance_activity import (
    ActivityKind,
    MaintenanceBusyError,
    OperationActivityRegistry,
)


class FakeMonotonicClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.timeouts: list[tuple[float, asyncio.Future[None]]] = []

    def __call__(self) -> float:
        return self.now

    async def wait_for(self, awaitable, timeout: float):
        task = asyncio.ensure_future(awaitable)
        marker = asyncio.get_running_loop().create_future()
        self.timeouts.append((self.now + timeout, marker))
        done, _pending = await asyncio.wait(
            (task, marker),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if marker in done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise TimeoutError
        marker.cancel()
        return task.result()

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        for deadline, marker in tuple(self.timeouts):
            if deadline <= self.now and not marker.done():
                marker.set_result(None)
        self.timeouts = [
            (deadline, marker)
            for deadline, marker in self.timeouts
            if not marker.done()
        ]
        await asyncio.sleep(0)


async def settle_event_loop() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


def test_activity_tokens_are_reference_counted_and_release_on_error() -> None:
    registry = OperationActivityRegistry()

    with (
        pytest.raises(RuntimeError, match="stop"),
        registry.track(ActivityKind.SEARCH),
        registry.track(ActivityKind.SEARCH),
    ):
        assert registry.active_count == 2
        assert registry.active(ActivityKind.SEARCH) == 2
        raise RuntimeError("stop")

    assert registry.active_count == 0
    assert registry.is_idle is True


@pytest.mark.asyncio
async def test_wait_for_continuous_idle_restarts_after_activity(monkeypatch) -> None:
    clock = FakeMonotonicClock()
    monkeypatch.setattr(asyncio, "wait_for", clock.wait_for)
    registry = OperationActivityRegistry(clock=clock)
    waiter = asyncio.create_task(registry.wait_for_continuous_idle(60))
    await settle_event_loop()

    await clock.advance(30)
    with registry.track(ActivityKind.DOWNLOAD):
        await clock.advance(1)
    await settle_event_loop()
    await clock.advance(59)
    assert waiter.done() is False
    await clock.advance(1)

    assert await waiter is True


def test_maintenance_token_is_exclusive_and_business_has_priority() -> None:
    registry = OperationActivityRegistry()
    maintenance = registry.try_track_maintenance(ActivityKind.STORAGE_CLEANUP)
    assert maintenance is not None

    with pytest.raises(MaintenanceBusyError, match="存储维护"):
        registry.track(ActivityKind.DOWNLOAD)
    with pytest.raises(RuntimeError, match="未释放"):
        registry.close()
    maintenance.release()

    with registry.track(ActivityKind.SUBSCRIPTION):
        assert registry.try_track_maintenance(ActivityKind.STORAGE_SCAN) is None


@pytest.mark.asyncio
async def test_close_wakes_idle_waiter_and_prevents_new_tokens() -> None:
    registry = OperationActivityRegistry()
    waiter = asyncio.create_task(registry.wait_for_continuous_idle(60))
    await asyncio.sleep(0)

    registry.close()

    assert await waiter is False
    with pytest.raises(RuntimeError, match="关闭"):
        registry.track(ActivityKind.SEARCH)
    assert registry.try_track_maintenance(ActivityKind.STORAGE_SCAN) is None
