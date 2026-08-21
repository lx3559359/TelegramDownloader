# Responsive Application Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make task, content, subscription, diagnostics, login, and settings actions acknowledge immediately and keep blocking database/file work off the Qt event loop.

**Architecture:** Extend `AsyncActionBridge` with explicit deduplicate and replace-latest policies. Add bulk repository/scheduler entry points for task commands, an async coalesced task-view refresh, and keyed Qt model updates. Convert the remaining file/database-heavy controller actions to async methods that use `asyncio.to_thread`, while all Qt mutations stay on the event-loop thread.

**Tech Stack:** Python 3.12, asyncio, SQLite, PySide6, qasync, pytest, pytest-asyncio, pytest-qt, Ruff.

---

## File map

- Modify `src/telegram_downloader/ui/async_actions.py`: action policies and safe replacement cleanup.
- Modify `src/telegram_downloader/repository.py`: bulk task lookup/status updates.
- Modify `src/telegram_downloader/scheduler.py`: batch pause/resume entry points.
- Modify `src/telegram_downloader/controller.py`: async task commands, coalesced view refresh, and blocking-operation offloads.
- Modify `src/telegram_downloader/app.py`: route every costly signal through the bridge with the correct policy.
- Modify `src/telegram_downloader/ui/models.py`: keyed task updates without resets when order is unchanged.
- Modify `src/telegram_downloader/ui/subscription_models.py`: keyed rule updates.
- Modify focused tests in `tests/test_repository.py`, `tests/test_scheduler.py`, `tests/test_controller.py`, `tests/test_app.py`, `tests/ui/test_async_actions.py`, `tests/ui/test_main_window.py`, and `tests/ui/test_subscriptions.py`.

### Task 1: Add explicit async action policies

**Files:**
- Modify: `src/telegram_downloader/ui/async_actions.py`
- Modify: `tests/ui/test_async_actions.py`

- [ ] **Step 1: Write failing replace-latest and immediate-feedback tests**

```python
# append to tests/ui/test_async_actions.py
from telegram_downloader.ui.async_actions import ActionPolicy


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
async def test_replace_latest_cancels_old_without_clearing_new_busy_state() -> None:
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
    bridge.start("content.search", first, hooks=hooks)
    await first_entered.wait()
    bridge.start(
        "content.search", second, hooks=hooks,
        policy=ActionPolicy.REPLACE_LATEST,
    )
    await asyncio.sleep(0)
    assert events.count("busy:off") == 0
    second_release.set()
    await bridge.wait_idle()
    assert events[-1] == "busy:off"
    assert events.count("busy:off") == 1
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ui\test_async_actions.py -q
```

Expected: `ActionPolicy` or the `policy` argument is missing.

- [ ] **Step 3: Implement deduplicate and replace-latest**

```python
from enum import StrEnum


class ActionPolicy(StrEnum):
    DEDUPLICATE = "deduplicate"
    REPLACE_LATEST = "replace_latest"


def start(
    self,
    key: str,
    action: ActionFactory,
    *,
    hooks: ActionHooks = _NO_HOOKS,
    policy: ActionPolicy = ActionPolicy.DEDUPLICATE,
) -> bool:
    existing = self._tasks.get(key)
    if existing is not None and not existing.done():
        if policy is ActionPolicy.DEDUPLICATE:
            return False
        existing.cancel()
    self._invoke(hooks.started, key)
    task = asyncio.create_task(self._run(key, action, hooks), name=f"ui:{key}")
    self._tasks[key] = task
    return True
```

Add `policy` to `connect` and `connect_payload` and pass it through. In `_run.finally`, invoke `finished` and pop the key only when `self._tasks.get(key) is asyncio.current_task()`. A superseded task still invokes `cancelled`, but cannot clear the replacement's busy state.

- [ ] **Step 4: Run bridge tests and Ruff**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ui\test_async_actions.py -q
.\.venv\Scripts\python.exe -m ruff check src\telegram_downloader\ui\async_actions.py tests\ui\test_async_actions.py
```

Expected: all selected checks pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/telegram_downloader/ui/async_actions.py tests/ui/test_async_actions.py
git commit -m "feat: add async action policies"
```

### Task 2: Add bulk task repository and scheduler operations

**Files:**
- Modify: `src/telegram_downloader/repository.py:241-268, 426-439`
- Modify: `src/telegram_downloader/scheduler.py:45-84, 132-162`
- Modify: `tests/test_repository.py`
- Modify: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing bulk repository tests**

```python
# append to tests/test_repository.py
def test_get_tasks_and_update_statuses_use_bulk_contract(tmp_path: Path) -> None:
    repository = TaskRepository(tmp_path / "tasks.sqlite3")
    repository.initialize()
    first_task, first_item = records(tmp_path)
    second_task = replace(first_task, id="task-2", source_ref="peer-2")
    second_item = replace(
        first_item,
        id="item-2",
        task_id=second_task.id,
        peer_ref="peer-2",
        message_id=8,
        media_id="media-8",
        target_path=tmp_path / "y.mp4",
    )
    repository.create_task(first_task, [first_item])
    repository.create_task(second_task, [second_item])
    selected = [first_task.id, second_task.id, "missing"]
    found = repository.get_tasks(selected)
    assert [task.id for task in found] == selected[:2]
    updated = repository.update_task_statuses(
        selected,
        TaskStatus.PAUSED,
        allowed={TaskStatus.QUEUED, TaskStatus.DOWNLOADING},
    )
    assert updated == set(selected[:2])
    assert all(repository.get_task(task_id).status is TaskStatus.PAUSED for task_id in updated)
```

- [ ] **Step 2: Run the test and verify RED**

Expected: `get_tasks` and `update_task_statuses` are missing.

- [ ] **Step 3: Implement one-query lookup and one-transaction update**

```python
def get_tasks(self, task_ids: list[str]) -> list[TaskRecord]:
    ids = tuple(dict.fromkeys(task_ids))
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    with self._connection() as connection:
        rows = connection.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id IN ({marks})",
            ids,
        ).fetchall()
    by_id = {str(row["id"]): self._task_from_row(row) for row in rows}
    return [by_id[task_id] for task_id in ids if task_id in by_id]


def update_task_statuses(
    self,
    task_ids: list[str],
    status: TaskStatus,
    *,
    allowed: set[TaskStatus],
    error: str | None = None,
) -> set[str]:
    ids = tuple(dict.fromkeys(task_ids))
    if not ids or not allowed:
        return set()
    id_marks = ",".join("?" for _ in ids)
    states = tuple(sorted(value.value for value in allowed))
    state_marks = ",".join("?" for _ in states)
    now = datetime.now(UTC).isoformat()
    with self._connection() as connection:
        rows = connection.execute(
            f"SELECT id FROM tasks WHERE id IN ({id_marks}) "
            f"AND status IN ({state_marks}) AND archived_at IS NULL",
            (*ids, *states),
        ).fetchall()
        accepted = {str(row[0]) for row in rows}
        if accepted:
            ordered = tuple(sorted(accepted))
            marks = ",".join("?" for _ in ordered)
            connection.execute(
                f"UPDATE tasks SET status=?, updated_at=?, last_error=? WHERE id IN ({marks})",
                (status.value, now, error, *ordered),
            )
    return accepted
```

Add both methods to `SchedulerRepository`.

- [ ] **Step 4: Add scheduler batch tests and methods**

Test that `pause_tasks([a, b, a])` sets both flags, removes queued operations, and calls `update_task_statuses` once; test that `resume_tasks([a, b])` clears flags, performs one bulk `QUEUED` update, and schedules each accepted task once.

Implement `pause_tasks` by deduplicating IDs, updating in-memory flags/operations with the existing `pause_task` logic but without per-ID persistence, then calling `repository.update_task_statuses(..., PAUSED, allowed={QUEUED, DOWNLOADING, WAITING_RETRY})`. Implement async `resume_tasks` analogously with one `QUEUED` update and `await asyncio.gather(*(run_task(id) ...))`. Keep single-item methods as wrappers.

- [ ] **Step 5: Run repository and scheduler tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_repository.py tests\test_scheduler.py tests\test_download_queue_stress.py -q
```

Expected: all selected tests pass and bulk-call spies observe one status update per command.

- [ ] **Step 6: Commit Task 2**

```powershell
git add src/telegram_downloader/repository.py src/telegram_downloader/scheduler.py tests/test_repository.py tests/test_scheduler.py
git commit -m "perf: batch task state commands"
```

### Task 3: Refresh task views asynchronously and incrementally

**Files:**
- Modify: `src/telegram_downloader/controller.py:1545-1645, 1903-2000, 2240-2256`
- Modify: `src/telegram_downloader/ui/models.py:125-211`
- Modify: `src/telegram_downloader/ui/main.py:633-679`
- Modify: `tests/test_controller.py`
- Modify: `tests/ui/test_main_window.py`

- [ ] **Step 1: Write failing event-loop and no-reset tests**

```python
# append to tests/test_controller.py
@pytest.mark.asyncio
async def test_async_task_refresh_does_not_block_event_loop() -> None:
    import time

    entered = asyncio.Event()
    heartbeat = asyncio.Event()

    class Repository:
        def list_task_snapshots(self, *, include_archived=False):
            entered.set()
            time.sleep(0.05)
            return []

    controller = AppController.for_test(repository=Repository())
    refresh = asyncio.create_task(controller.refresh_tasks_async())
    await entered.wait()
    asyncio.get_running_loop().call_soon(heartbeat.set)
    await asyncio.wait_for(heartbeat.wait(), timeout=0.02)
    await refresh


# append to tests/ui/test_main_window.py
def test_progress_only_task_update_does_not_reset_model(qtbot) -> None:
    window = MainWindow()
    first = task_summary("t1", downloaded_bytes=1)
    resets = QSignalSpy(window.task_model.modelReset)
    changes = QSignalSpy(window.task_model.dataChanged)
    window.set_task_summaries([first])
    resets.clear()
    window.set_task_summaries([replace(first, downloaded_bytes=2, progress_text="2 / 10")])
    assert len(resets) == 0
    assert len(changes) >= 1
```

- [ ] **Step 2: Run the tests and verify RED**

Expected: `refresh_tasks_async` is missing and `TaskTableModel.set_tasks` emits `modelReset`.

- [ ] **Step 3: Add coalesced async snapshot loading**

Add `_task_refresh_task` and `_task_refresh_pending` fields. Move summary construction into a pure `_summaries_from_snapshots(snapshots, scheduler_state, queue_positions, sampled_at)` helper. Implement:

```python
async def refresh_tasks_async(self, *, now: float | None = None) -> None:
    current = asyncio.current_task()
    active = self._task_refresh_task
    if active is not None and active is not current and not active.done():
        self._task_refresh_pending = True
        await asyncio.shield(active)
        return
    self._task_refresh_task = current
    try:
        while True:
            self._task_refresh_pending = False
            sampled_at = monotonic_clock() if now is None else now
            snapshots = await asyncio.to_thread(
                self.repository.list_task_snapshots,
                include_archived=True,
            )
            scheduler_state = self.scheduler.snapshot()
            queue_positions = self.scheduler.queue_positions()
            summaries = self._summaries_from_snapshots(
                snapshots, scheduler_state, queue_positions, sampled_at,
            )
            self.window.set_task_summaries(summaries)
            self.window.set_scheduler_summary(
                active=1 if scheduler_state.active_task_id else 0,
                queued=scheduler_state.queued_count,
                concurrency=scheduler_state.concurrency,
                speed_limit_kib=scheduler_state.speed_limit_kib,
            )
            if not self._task_refresh_pending:
                break
    finally:
        if self._task_refresh_task is current:
            self._task_refresh_task = None
```

Use this method in download polling and every async task command. Keep the synchronous `refresh_tasks` compatibility wrapper only for startup/tests that already hold in-memory fakes; no Qt signal handler may call it after Task 5.

- [ ] **Step 4: Update rows without reset when IDs and order match**

In `TaskTableModel.set_tasks`, compute the filtered target list. If current and target IDs match in the same order, replace `_all_tasks`/`_tasks` and emit `dataChanged` only for rows whose summaries changed. If IDs/order differ, retain the existing reset path. This handles progress/status refresh without resetting, while priority reorder and filter membership changes remain safe.

- [ ] **Step 5: Run controller and main-window tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_controller.py tests\ui\test_main_window.py -q
```

Expected: all selected tests pass; the new heartbeat and no-reset tests pass.

- [ ] **Step 6: Commit Task 3**

```powershell
git add src/telegram_downloader/controller.py src/telegram_downloader/ui/models.py src/telegram_downloader/ui/main.py tests/test_controller.py tests/ui/test_main_window.py
git commit -m "perf: refresh task views incrementally"
```

### Task 4: Convert task commands to asynchronous batches

**Files:**
- Modify: `src/telegram_downloader/controller.py:1545-1645`
- Modify: `src/telegram_downloader/app.py:552-576, 765-775`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write failing single-refresh batch tests**

```python
@pytest.mark.asyncio
async def test_pause_tasks_uses_bulk_lookup_command_and_one_refresh() -> None:
    events: list[str] = []

    class Repository:
        def get_tasks(self, ids):
            events.append("lookup")
            return [queued_task(task_id) for task_id in ids]

    class Scheduler:
        def pause_tasks(self, ids):
            events.append("pause:" + ",".join(ids))
            return set(ids)

    controller = AppController.for_test(repository=Repository(), scheduler=Scheduler())
    controller.refresh_tasks_async = AsyncMock(side_effect=lambda: events.append("refresh"))
    await controller.pause_tasks(["a", "b", "a"])
    assert events == ["lookup", "pause:a,b", "refresh"]
```

Add these four explicit tests beside the pause test:

```python
def task_record(task_id: str, status: TaskStatus) -> TaskRecord:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    return TaskRecord(
        task_id,
        SourceKind.CHANNEL_OR_GROUP,
        f"peer-{task_id}",
        f"任务 {task_id}",
        f"https://t.me/{task_id}",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 10),
        status,
        now,
        now,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "initial", "scheduler_call"),
    [
        ("resume_tasks", TaskStatus.PAUSED, "resume_tasks"),
        ("retry_failed_tasks", TaskStatus.PARTIAL_FAILURE, "resume_tasks"),
    ],
)
async def test_resume_commands_use_one_bulk_lookup_and_refresh(
    method, initial, scheduler_call
) -> None:
    repository = SimpleNamespace(
        get_tasks=Mock(return_value=[task_record("a", initial), task_record("b", initial)])
    )
    scheduler = SimpleNamespace(
        resume_tasks=AsyncMock(return_value={"a", "b"}),
        snapshot=Mock(return_value=SchedulerSnapshot(None, (), 3, 0)),
        queue_positions=Mock(return_value={}),
    )
    controller = AppController.for_test(repository=repository, scheduler=scheduler)
    controller.refresh_tasks_async = AsyncMock()
    await getattr(controller, method)(["a", "b", "a"])
    repository.get_tasks.assert_called_once_with(["a", "b"])
    getattr(scheduler, scheduler_call).assert_awaited_once_with(["a", "b"])
    controller.refresh_tasks_async.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["archive_tasks", "restore_tasks"])
async def test_archive_commands_write_and_refresh_once(method) -> None:
    repository = SimpleNamespace()
    setattr(repository, method, Mock(return_value={"a", "b"}))
    controller = AppController.for_test(repository=repository)
    controller.refresh_tasks_async = AsyncMock()
    await getattr(controller, method)(["a", "b", "a"])
    getattr(repository, method).assert_called_once_with(["a", "b"])
    controller.refresh_tasks_async.assert_awaited_once()
```

The prioritize test uses one queued `task_record`, asserts one `repository.prioritize_task("a")`, one `scheduler.prioritize_task("a")`, and one awaited refresh.

- [ ] **Step 2: Run the tests and verify RED**

Expected: `pause_tasks`, archive, restore, or prioritize are synchronous or use per-ID reads.

- [ ] **Step 3: Implement async batch controller methods**

Convert pause, prioritize, archive, and restore methods to `async def`. Use `await asyncio.to_thread(repository.get_tasks, unique)` for eligibility, event-loop scheduler methods for in-memory changes, and `await asyncio.to_thread` for repository archive/restore/prioritize writes. Finish each command with exactly one `await refresh_tasks_async()` and one status message. Resume/retry use `scheduler.resume_tasks(accepted)` from Task 2.

Update `app.py` wrappers to async functions and connect pause/prioritize/archive/restore through `AsyncActionBridge.connect_payload` with deduplicate keys `tasks.pause`, `tasks.prioritize`, `tasks.archive`, and `tasks.restore`. Existing resume/retry bindings remain deduplicated.

- [ ] **Step 4: Run task command and app wiring tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_controller.py -k "pause or resume or retry or prioritize or archive or restore" tests\test_app.py tests\ui\test_async_actions.py -q
```

Expected: all selected tests pass and every command is represented in `active_keys` while running.

- [ ] **Step 5: Commit Task 4**

```powershell
git add src/telegram_downloader/controller.py src/telegram_downloader/app.py tests/test_controller.py tests/test_app.py
git commit -m "perf: run task controls as batches"
```

### Task 5: Offload remaining database and file-heavy UI actions

**Files:**
- Modify: `src/telegram_downloader/controller.py:1238-1267, 1341-1410, 1434-1543`
- Modify: `src/telegram_downloader/app.py:633-710, 725-836`
- Modify: `src/telegram_downloader/ui/subscription_models.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_app.py`
- Modify: `tests/ui/test_subscriptions.py`

- [ ] **Step 1: Add blocking-boundary tests**

For each category below, inject a synchronous fake that sets a `threading.Event`, blocks for 50 ms, and record `threading.get_ident()`. Start the controller action, verify an asyncio heartbeat within 20 ms, release the fake, and assert UI callbacks ran on the test/event-loop thread:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("action_name", [
    "delete_content_history",
    "clear_content_history",
    "clear_thumbnail_cache",
    "activate_diagnostics",
    "export_diagnostics",
    "apply_settings",
    "set_subscription_enabled",
    "delete_subscription",
])
async def test_blocking_ui_actions_leave_event_loop_responsive(action_name):
    controller, action, release, heartbeat, worker_ids, ui_ids = action_fixture(action_name)
    operation = asyncio.create_task(action())
    await asyncio.wait_for(heartbeat.wait(), timeout=0.02)
    release.set()
    await operation
    assert worker_ids and worker_ids[0] != threading.get_ident()
    assert all(value == threading.get_ident() for value in ui_ids)
```

Implement `action_fixture` with explicit fakes for each method; do not inspect private data or use real Telegram values.

- [ ] **Step 2: Run the tests and verify RED**

Expected: synchronous methods cannot be awaited or block the heartbeat.

- [ ] **Step 3: Convert the exact blocking sections**

- `delete_content_history` / `clear_content_history`: await the service method in `asyncio.to_thread`, then reload page history on the event loop.
- `clear_thumbnail_cache`: await `thumbnails.clear` and `thumbnails.total_bytes` in a worker, then update the settings dialog and status on the event loop.
- `activate_diagnostics`: await `diagnostic_store.load_latest` in a worker, then set the report.
- `run_diagnostics`: keep probes async; move `register_secrets` plus `save(report)` into one worker closure.
- `export_diagnostics`: move registration plus `export(report)` into one worker closure, then show only `package.name`.
- `apply_settings`: move `settings_store.save(settings)` and `vault.save(updated_secrets)` into one worker closure; only after success assign controller state and reconfigure the scheduler on the event loop.
- `set_subscription_enabled`, `run_subscription_now`, and `delete_subscription`: convert to async; move service repository calls to workers, then wake the scheduler/reload models on the event loop.
- `_reload_subscriptions`: obtain `list_rules` and `latest_runs` together through one worker-backed service snapshot method; apply the returned immutable values on the event loop.

Do not offload Gateway coroutine calls, scheduler asyncio mutations, Qt calls, or status callbacks.

- [ ] **Step 4: Update app action bindings**

Bind content-history deletion/clear, cache clear, diagnostics activation/export, settings save, subscription enable/delete/run, content search, and load-more through `AsyncActionBridge`. Use `REPLACE_LATEST` only for content search and page activation; use `DEDUPLICATE` for the rest. Add immediate busy hooks to settings cache clear/save and diagnostics export.

- [ ] **Step 5: Update subscription rows without unnecessary reset**

In `SubscriptionTableModel.set_rules`, use `dataChanged` when current and target rule IDs have the same order; reset only when rows are added, removed, or reordered. Add a `QSignalSpy(model.modelReset)` regression showing an enabled/progress-only change emits `dataChanged` without reset.

- [ ] **Step 6: Run focused application tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_controller.py tests\test_app.py tests\ui\test_async_actions.py tests\ui\test_subscriptions.py tests\ui\test_diagnostics.py tests\ui\test_settings_dialog.py -q
```

Expected: all selected tests pass, heartbeats remain responsive, and Qt callback thread assertions pass.

- [ ] **Step 7: Commit Task 5**

```powershell
git add src/telegram_downloader/controller.py src/telegram_downloader/app.py src/telegram_downloader/ui/subscription_models.py tests/test_controller.py tests/test_app.py tests/ui/test_subscriptions.py
git commit -m "perf: offload blocking application actions"
```

### Task 6: Integrate policies across page activation, search, login, and diagnostics

**Files:**
- Modify: `src/telegram_downloader/app.py:725-836`
- Modify: `tests/test_app.py`
- Modify: `tests/ui/test_async_actions.py`

- [ ] **Step 1: Add a complete policy-map test**

Expose the action bindings through the existing application construction test seam and assert this exact map:

```python
EXPECTED_POLICIES = {
    "content.activate": ActionPolicy.REPLACE_LATEST,
    "content.search": ActionPolicy.REPLACE_LATEST,
    "content.load_more": ActionPolicy.REPLACE_LATEST,
    "dialogs.refresh": ActionPolicy.DEDUPLICATE,
    "telegram.retry": ActionPolicy.DEDUPLICATE,
    "diagnostics.run": ActionPolicy.DEDUPLICATE,
    "diagnostics.export": ActionPolicy.DEDUPLICATE,
    "login.qr.refresh": ActionPolicy.DEDUPLICATE,
    "login.phone": ActionPolicy.DEDUPLICATE,
    "settings.save": ActionPolicy.DEDUPLICATE,
    "settings.thumbnail_cache.clear": ActionPolicy.DEDUPLICATE,
}
```

- [ ] **Step 2: Run the test and verify RED**

Expected: search/load-more/settings/export are not all routed through the bridge or lack policy metadata.

- [ ] **Step 3: Centralize and apply the map**

Define `ACTION_POLICIES` in `ui/async_actions.py` with the exact keys above plus existing task/subscription keys, and have `connect` default to the map value before falling back to `DEDUPLICATE`. Update all app bindings to use stable keys from this map. Preserve immediate started/finished hooks and existing safe failure callbacks.

- [ ] **Step 4: Run app and bridge regression**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_app.py tests\ui\test_async_actions.py -q
```

Expected: all policy and lifecycle tests pass.

- [ ] **Step 5: Commit Task 6**

```powershell
git add src/telegram_downloader/app.py src/telegram_downloader/ui/async_actions.py tests/test_app.py tests/ui/test_async_actions.py
git commit -m "feat: unify responsive action routing"
```

### Task 7: Run complete regression and record performance evidence

**Files:**
- Create: `docs/verification/2026-08-21-responsive-app-actions.md`

- [ ] **Step 1: Run the complete project checks**

Run:

```powershell
.\scripts\test.ps1
git status --short
```

Expected: complete pytest and Ruff pass. The only untracked file may be the verification record while it is being written.

- [ ] **Step 2: Run focused duration evidence**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_download_queue_stress.py tests\test_account_wide_search_e2e.py tests\test_task_management_e2e.py tests\test_controller.py tests\test_app.py tests\ui\test_async_actions.py tests\ui\test_main_window.py tests\ui\test_content_browser.py tests\ui\test_subscriptions.py -q --durations=20
```

Expected: all selected tests pass; heartbeat, no-reset, single-refresh, and policy tests are present in the run.

- [ ] **Step 3: Perform Windows GUI smoke checks**

Verify these user-visible outcomes with project-local test data and no credential capture:

1. Download progress continues while navigating and using task controls.
2. Search direct hits appear before delayed albums/thumbnails and queue remains disabled until stable.
3. Task pause/resume/retry/priority/archive/restore acknowledge immediately.
4. Content refresh, preview, queue, and history actions expose local busy/final state.
5. Subscription create/update/enable/run/probe/delete remain responsive.
6. Diagnostics run/cancel/export remain responsive.
7. Login QR refresh, phone fallback, cancel, and reconnect remain responsive.
8. Settings save and thumbnail cache clear remain responsive.

- [ ] **Step 4: Write the verification record**

Record exact test counts/durations, Ruff result, focused heartbeat/no-reset/bulk-call outcomes, GUI boolean outcomes, commit SHA, and worktree status. Do not include account names, peers, messages, search keywords, links, credentials, real filenames, or absolute user paths.

- [ ] **Step 5: Commit Task 7**

```powershell
git add docs/verification/2026-08-21-responsive-app-actions.md
git commit -m "docs: verify responsive application actions"
```
