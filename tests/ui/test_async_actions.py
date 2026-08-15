import asyncio

import pytest

from telegram_downloader.ui.async_actions import ActionHooks, AsyncActionBridge


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
