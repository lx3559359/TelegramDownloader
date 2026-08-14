import asyncio

import pytest

from telegram_downloader.connectivity import ConnectionRecovery
from telegram_downloader.gateway import SessionExpiredError, TransientNetworkError


async def _record_sleep(values: list[float], value: float) -> None:
    values.append(value)


@pytest.mark.asyncio
async def test_transient_failures_use_bounded_delays_then_recover() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        async def connect(self) -> None:
            self.calls += 1
            if self.calls < 3:
                raise TransientNetworkError("Telegram 网络连接失败")

    gateway = Gateway()
    sleeps: list[float] = []
    attempts: list[tuple[int, int]] = []
    recovery = ConnectionRecovery(
        delays=(0.0, 1.0, 3.0),
        sleeper=lambda value: _record_sleep(sleeps, value),
    )

    await recovery.ensure_connected(gateway, attempts.append)

    assert gateway.calls == 3
    assert sleeps == [1.0, 3.0]
    assert attempts == [(1, 3), (2, 3), (3, 3)]


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_connect_attempt() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        async def connect(self) -> None:
            self.calls += 1
            entered.set()
            await release.wait()

    gateway = Gateway()
    recovery = ConnectionRecovery(delays=(0.0,))
    first = asyncio.create_task(recovery.ensure_connected(gateway))
    second = asyncio.create_task(recovery.ensure_connected(gateway))
    await entered.wait()
    release.set()
    await asyncio.gather(first, second)

    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_session_expiry_is_not_retried() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        async def connect(self) -> None:
            self.calls += 1
            raise SessionExpiredError("登录已失效")

    gateway = Gateway()
    recovery = ConnectionRecovery(delays=(0.0, 1.0, 3.0))

    with pytest.raises(SessionExpiredError):
        await recovery.ensure_connected(gateway)

    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_cancel_stops_the_shared_connect_attempt() -> None:
    entered = asyncio.Event()

    class Gateway:
        async def connect(self) -> None:
            entered.set()
            await asyncio.Event().wait()

    recovery = ConnectionRecovery(delays=(0.0,))
    waiter = asyncio.create_task(recovery.ensure_connected(Gateway()))
    await entered.wait()

    await recovery.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter


@pytest.mark.parametrize("delays", [(), (1.0,), (0.0, -1.0)])
def test_retry_delays_must_start_at_zero_and_stay_non_negative(
    delays: tuple[float, ...],
) -> None:
    with pytest.raises(ValueError, match="重连延迟"):
        ConnectionRecovery(delays=delays)
