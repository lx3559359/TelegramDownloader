import asyncio

import pytest

from telegram_downloader.ui.async_actions import (
    ActionHooks,
    AsyncActionBridge,
)


class FakeSignal:
    def __init__(self) -> None:
        self.slot = None

    def connect(self, slot) -> None:
        self.slot = slot

    def emit(self, *values) -> None:
        assert self.slot is not None
        self.slot(*values)


@pytest.mark.asyncio
async def test_same_action_key_runs_once_and_restores_state() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def action() -> None:
        calls.append("action")
        started.set()
        await release.wait()

    bridge = AsyncActionBridge()
    hooks = ActionHooks(
        started=lambda: calls.append("started"),
        succeeded=lambda: calls.append("succeeded"),
        finished=lambda: calls.append("finished"),
    )

    assert bridge.start("dialogs.refresh", action, hooks=hooks) is True
    await started.wait()
    assert bridge.start("dialogs.refresh", action, hooks=hooks) is False
    release.set()
    await bridge.wait_idle()

    assert calls == ["started", "action", "succeeded", "finished"]
    assert bridge.active_keys == frozenset()


@pytest.mark.asyncio
async def test_failure_is_reported_without_leaking_from_task() -> None:
    events: list[object] = []

    async def action() -> None:
        raise RuntimeError("private response body")

    bridge = AsyncActionBridge()
    bridge.start(
        "content.activate",
        action,
        hooks=ActionHooks(
            failed=lambda error: events.append(type(error).__name__),
            finished=lambda: events.append("finished"),
        ),
    )
    await bridge.wait_idle()

    assert events == ["RuntimeError", "finished"]
    assert bridge.active_keys == frozenset()


@pytest.mark.asyncio
async def test_shutdown_cancels_running_actions_and_calls_cleanup() -> None:
    entered = asyncio.Event()
    events: list[str] = []

    async def action() -> None:
        entered.set()
        await asyncio.Event().wait()

    bridge = AsyncActionBridge()
    bridge.start(
        "login.qr.refresh",
        action,
        hooks=ActionHooks(
            cancelled=lambda: events.append("cancelled"),
            finished=lambda: events.append("finished"),
        ),
    )
    await entered.wait()
    await bridge.shutdown()

    assert events == ["cancelled", "finished"]
    assert bridge.active_keys == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["tasks.resume", "content.queue"])
async def test_payload_signal_forwards_value_and_deduplicates_running_key(
    key: str,
) -> None:
    signal = FakeSignal()
    started = asyncio.Event()
    release = asyncio.Event()
    values: list[list[str]] = []

    async def action(value) -> None:
        values.append(value)
        started.set()
        await release.wait()

    bridge = AsyncActionBridge()
    bridge.connect_payload(signal, key, action)

    signal.emit(["first"])
    await started.wait()
    signal.emit(["second"])
    release.set()
    await bridge.wait_idle()

    assert values == [["first"]]
    assert bridge.active_keys == frozenset()


@pytest.mark.asyncio
async def test_args_signal_forwards_all_values() -> None:
    signal = FakeSignal()
    values: list[tuple[str, bool]] = []

    async def action(rule_id: str, enabled: bool) -> None:
        values.append((rule_id, enabled))

    bridge = AsyncActionBridge()
    bridge.connect_args(signal, "subscriptions.enabled", action)

    signal.emit("rule-1", False)
    await bridge.wait_idle()

    assert values == [("rule-1", False)]


@pytest.mark.asyncio
async def test_started_hook_runs_before_action_coroutine_starts() -> None:
    events: list[str] = []
    entered = asyncio.Event()

    async def action() -> None:
        events.append("action")
        entered.set()

    bridge = AsyncActionBridge()
    assert bridge.start(
        "content.search",
        action,
        hooks=ActionHooks(started=lambda: events.append("started")),
    ) is True
    assert events == ["started"]
    await entered.wait()
    await bridge.wait_idle()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        "content.activate",
        "content.history.open",
        "content.search",
        "content.load_more",
    ],
)
async def test_replace_latest_policy_cancels_old_without_clearing_new_busy_state(
    key: str,
) -> None:
    first_entered = asyncio.Event()
    second_release = asyncio.Event()
    events: list[str] = []

    async def first() -> None:
        first_entered.set()
        await asyncio.Event().wait()

    async def second() -> None:
        events.append("second")
        await second_release.wait()

    bridge = AsyncActionBridge()
    hooks = ActionHooks(
        started=lambda: events.append("busy:on"),
        cancelled=lambda: events.append("cancelled"),
        finished=lambda: events.append("busy:off"),
    )
    bridge.start(key, first, hooks=hooks)
    await first_entered.wait()
    bridge.start(key, second, hooks=hooks)
    await asyncio.sleep(0)
    assert events.count("busy:off") == 0
    second_release.set()
    await bridge.wait_idle()
    assert events[-1] == "busy:off"
    assert events.count("busy:off") == 1
