from __future__ import annotations

import asyncio

import pytest

from telegram_downloader.resource_control import (
    AdjustableConcurrencyLimiter,
    AsyncBandwidthLimiter,
    validate_speed_limit_kib,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


@pytest.mark.parametrize("value", [-1, 1_048_577, True, False, 1.5, "1024"])
def test_speed_limit_rejects_unsafe_values(value: object) -> None:
    with pytest.raises(ValueError, match="限速"):
        validate_speed_limit_kib(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, 1, 256, 1_048_576])
def test_speed_limit_accepts_safe_integer_range(value: int) -> None:
    validate_speed_limit_kib(value)


@pytest.mark.asyncio
async def test_unlimited_bandwidth_never_sleeps() -> None:
    clock = FakeClock()
    limiter = AsyncBandwidthLimiter(0, clock=clock, sleeper=clock.sleep)

    await limiter.acquire(512 * 1024)

    assert clock.sleeps == []
    assert limiter.speed_limit_kib == 0


@pytest.mark.asyncio
async def test_bandwidth_accounts_bytes_at_configured_rate() -> None:
    clock = FakeClock()
    limiter = AsyncBandwidthLimiter(1024, clock=clock, sleeper=clock.sleep)

    await limiter.acquire(256 * 1024)
    await limiter.acquire(768 * 1024)

    assert clock.sleeps == pytest.approx([0.25, 0.75])
    assert clock.now == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_concurrent_bytes_share_one_bandwidth_rate() -> None:
    clock = FakeClock()
    limiter = AsyncBandwidthLimiter(1024, clock=clock, sleeper=clock.sleep)

    await asyncio.gather(
        limiter.acquire(512 * 1024),
        limiter.acquire(512 * 1024),
    )

    assert clock.now == pytest.approx(1.0)
    assert sum(clock.sleeps) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_speed_change_resets_future_reservations() -> None:
    clock = FakeClock()
    limiter = AsyncBandwidthLimiter(512, clock=clock, sleeper=clock.sleep)
    await limiter.acquire(512 * 1024)

    limiter.set_speed_limit_kib(2048)
    await limiter.acquire(512 * 1024)

    assert clock.sleeps == pytest.approx([1.0, 0.25])
    assert limiter.speed_limit_kib == 2048


@pytest.mark.asyncio
async def test_switching_to_unlimited_wakes_an_existing_bandwidth_wait() -> None:
    sleep_started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocking_sleep(_delay: float) -> None:
        sleep_started.set()
        await never_release.wait()

    limiter = AsyncBandwidthLimiter(1, sleeper=blocking_sleep)
    waiting = asyncio.create_task(limiter.acquire(1024))
    await sleep_started.wait()

    limiter.set_speed_limit_kib(0)
    await asyncio.wait_for(waiting, timeout=0.1)

    assert limiter.speed_limit_kib == 0


@pytest.mark.asyncio
async def test_external_cancellation_wins_when_configuration_changes_too() -> None:
    sleep_started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocking_sleep(_delay: float) -> None:
        sleep_started.set()
        await never_release.wait()

    limiter = AsyncBandwidthLimiter(1, sleeper=blocking_sleep)
    waiting = asyncio.create_task(limiter.acquire(1024))
    await sleep_started.wait()

    waiting.cancel()
    limiter.set_speed_limit_kib(0)

    with pytest.raises(asyncio.CancelledError):
        await waiting


@pytest.mark.asyncio
async def test_cancelled_bandwidth_reservation_does_not_delay_the_next_chunk() -> None:
    clock = FakeClock()
    first_started = asyncio.Event()
    never_release = asyncio.Event()
    delays: list[float] = []

    async def controlled_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 1:
            first_started.set()
            await never_release.wait()
            return
        clock.now += delay

    limiter = AsyncBandwidthLimiter(1, clock=clock, sleeper=controlled_sleep)
    cancelled = asyncio.create_task(limiter.acquire(1024))
    await first_started.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    await limiter.acquire(1024)

    assert delays == pytest.approx([1.0, 1.0])
    assert clock.now == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_adjustable_limiter_never_exceeds_configured_limit() -> None:
    limiter = AdjustableConcurrencyLimiter(2)
    await limiter.acquire()
    await limiter.acquire()
    waiter = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)

    assert limiter.active == 2
    assert limiter.waiting == 1
    assert waiter.done() is False

    limiter.release()
    await waiter
    assert limiter.active == 2
    limiter.release()
    limiter.release()
    assert limiter.active == 0


@pytest.mark.asyncio
async def test_increasing_limit_wakes_waiters_in_fifo_order() -> None:
    limiter = AdjustableConcurrencyLimiter(1)
    await limiter.acquire()
    entered: list[str] = []

    async def wait_for_permit(name: str) -> None:
        await limiter.acquire()
        entered.append(name)

    first = asyncio.create_task(wait_for_permit("first"))
    second = asyncio.create_task(wait_for_permit("second"))
    await asyncio.sleep(0)

    limiter.set_limit(2)
    await first
    assert entered == ["first"]
    assert second.done() is False

    limiter.release()
    await second
    assert entered == ["first", "second"]
    limiter.release()
    limiter.release()


@pytest.mark.asyncio
async def test_reducing_limit_does_not_cancel_active_holders() -> None:
    limiter = AdjustableConcurrencyLimiter(2)
    await limiter.acquire()
    await limiter.acquire()
    limiter.set_limit(1)
    waiter = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)

    limiter.release()
    await asyncio.sleep(0)
    assert waiter.done() is False

    limiter.release()
    await waiter
    assert limiter.active == 1
    limiter.release()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_consume_permit() -> None:
    limiter = AdjustableConcurrencyLimiter(1)
    await limiter.acquire()
    waiter = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    limiter.release()

    assert limiter.active == 0
    assert limiter.waiting == 0


@pytest.mark.asyncio
async def test_adjustable_limiter_supports_async_context_manager() -> None:
    limiter = AdjustableConcurrencyLimiter(1)

    async with limiter:
        assert limiter.active == 1

    assert limiter.active == 0


@pytest.mark.parametrize("value", [0, 6, True, 1.5])
def test_adjustable_limiter_rejects_invalid_limits(value: object) -> None:
    with pytest.raises(ValueError, match="并发"):
        AdjustableConcurrencyLimiter(value)  # type: ignore[arg-type]
