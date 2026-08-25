import asyncio

import pytest

from telegram_downloader.task_refresh import TaskRefreshCoordinator


@pytest.mark.asyncio
async def test_progress_dirty_ids_share_one_fixed_window_without_deadline_extension() -> None:
    full_calls = 0
    id_batches: list[tuple[str, ...]] = []
    applied = asyncio.Event()
    first_marked_at = 0.0
    loaded_at = 0.0

    async def load_full() -> str:
        nonlocal full_calls
        full_calls += 1
        return "full"

    async def load_ids(task_ids: tuple[str, ...]) -> tuple[str, ...]:
        nonlocal loaded_at
        loaded_at = asyncio.get_running_loop().time()
        id_batches.append(task_ids)
        return task_ids

    coordinator = TaskRefreshCoordinator(
        load_full=load_full,
        load_ids=load_ids,
        apply_full=lambda _value: None,
        apply_patch=lambda _value: applied.set(),
        progress_interval=0.02,
        reconcile_interval=0.2,
    )
    await coordinator.activate()
    for _ in range(500):
        if first_marked_at == 0.0:
            first_marked_at = asyncio.get_running_loop().time()
        coordinator.mark_progress(["a"])
    for _ in range(3):
        await asyncio.sleep(0.004)
        coordinator.mark_progress(["a"])

    await asyncio.wait_for(applied.wait(), timeout=1)

    assert id_batches == [("a",)]
    assert full_calls == 1
    assert loaded_at - first_marked_at < 0.06
    await coordinator.close()


@pytest.mark.asyncio
async def test_refresh_now_waits_for_apply_and_reconcile_now_replaces_full_state() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    applied: list[tuple[str, object]] = []
    full_revision = 0

    async def load_full() -> str:
        nonlocal full_revision
        full_revision += 1
        return f"full-{full_revision}"

    async def load_ids(task_ids: tuple[str, ...]) -> tuple[str, ...]:
        started.set()
        await release.wait()
        return task_ids

    coordinator = TaskRefreshCoordinator(
        load_full=load_full,
        load_ids=load_ids,
        apply_full=lambda value: applied.append(("full", value)),
        apply_patch=lambda value: applied.append(("patch", value)),
        progress_interval=0.02,
        reconcile_interval=0.2,
    )
    await coordinator.activate()
    refresh = asyncio.create_task(coordinator.refresh_now(["a", "b"]))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert refresh.done() is False
    release.set()
    await refresh
    assert applied[-1] == ("patch", ("a", "b"))

    await coordinator.reconcile_now()

    assert applied[-1] == ("full", "full-2")
    await coordinator.close()


@pytest.mark.asyncio
async def test_deactivate_stops_periodic_full_load_and_reactivate_reconciles() -> None:
    full_applied = asyncio.Event()
    full_calls = 0

    async def load_full() -> int:
        nonlocal full_calls
        full_calls += 1
        return full_calls

    coordinator = TaskRefreshCoordinator(
        load_full=load_full,
        load_ids=lambda _ids: asyncio.sleep(0, result=()),
        apply_full=lambda _value: full_applied.set(),
        apply_patch=lambda _value: None,
        progress_interval=0.01,
        reconcile_interval=0.03,
    )
    await coordinator.activate()
    full_applied.clear()
    await asyncio.wait_for(full_applied.wait(), timeout=0.5)
    coordinator.deactivate()
    calls_when_hidden = full_calls
    full_applied.clear()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(full_applied.wait(), timeout=0.08)
    assert full_calls == calls_when_hidden

    await coordinator.activate()

    assert full_calls == calls_when_hidden + 1
    await coordinator.close()


@pytest.mark.asyncio
async def test_replace_generation_retrieves_but_drops_stale_load_result() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    applied: list[object] = []

    async def load_ids(task_ids: tuple[str, ...]) -> tuple[str, ...]:
        started.set()
        await release.wait()
        finished.set()
        return task_ids

    coordinator = TaskRefreshCoordinator(
        load_full=lambda: asyncio.sleep(0, result="full"),
        load_ids=load_ids,
        apply_full=lambda _value: None,
        apply_patch=applied.append,
        progress_interval=0.01,
        reconcile_interval=0.2,
    )
    refresh = asyncio.create_task(coordinator.refresh_now(["old"]))
    await asyncio.wait_for(started.wait(), timeout=1)
    coordinator.replace_generation()
    release.set()

    with pytest.raises(RuntimeError, match="代次"):
        await refresh
    await asyncio.wait_for(finished.wait(), timeout=1)
    assert applied == []
    await coordinator.close()


@pytest.mark.parametrize("failure_stage", ["load", "apply"])
@pytest.mark.asyncio
async def test_refresh_error_reaches_waiter_is_sanitized_and_periodically_retries(
    failure_stage: str,
) -> None:
    attempts = 0
    apply_attempts = 0
    errors: list[str] = []
    retried = asyncio.Event()

    async def load_ids(task_ids: tuple[str, ...]) -> tuple[str, ...]:
        nonlocal attempts
        attempts += 1
        if failure_stage == "load" and attempts == 1:
            raise ValueError("private task title")
        return task_ids

    def apply_patch(value: tuple[str, ...]) -> None:
        nonlocal apply_attempts
        apply_attempts += 1
        if failure_stage == "apply" and apply_attempts == 1:
            raise ValueError("private task title")
        retried.set()

    coordinator = TaskRefreshCoordinator(
        load_full=lambda: asyncio.sleep(0, result="full"),
        load_ids=load_ids,
        apply_full=lambda _value: None,
        apply_patch=apply_patch,
        progress_interval=0.01,
        reconcile_interval=0.2,
        on_error=lambda error: errors.append(type(error).__name__),
    )

    with pytest.raises(ValueError, match="private task title"):
        await coordinator.refresh_now(["a"])
    await asyncio.wait_for(retried.wait(), timeout=1)

    assert errors == ["ValueError"]
    assert attempts >= 2
    await coordinator.close()


@pytest.mark.asyncio
async def test_close_waits_for_inflight_load_is_idempotent_and_rejects_new_work() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    applied: list[object] = []

    async def load_ids(task_ids: tuple[str, ...]) -> tuple[str, ...]:
        started.set()
        await release.wait()
        return task_ids

    coordinator = TaskRefreshCoordinator(
        load_full=lambda: asyncio.sleep(0, result="full"),
        load_ids=load_ids,
        apply_full=lambda _value: None,
        apply_patch=applied.append,
        progress_interval=0.01,
        reconcile_interval=0.2,
    )
    refresh = asyncio.create_task(coordinator.refresh_now(["a"]))
    await asyncio.wait_for(started.wait(), timeout=1)
    closing = asyncio.create_task(coordinator.close())
    await asyncio.sleep(0)
    assert closing.done() is False
    release.set()
    await closing

    with pytest.raises(RuntimeError, match="关闭"):
        await refresh
    await coordinator.close()
    with pytest.raises(RuntimeError, match="关闭"):
        coordinator.mark_progress(["a"])
    with pytest.raises(RuntimeError, match="关闭"):
        await coordinator.refresh_now(["a"])
    with pytest.raises(RuntimeError, match="关闭"):
        await coordinator.reconcile_now()
    assert applied == []
