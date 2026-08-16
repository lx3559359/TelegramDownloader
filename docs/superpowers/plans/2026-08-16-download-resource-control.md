# Download Resource Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build TelegramDownloader v0.11.0 with one predictable active task, persistent FIFO/priority ordering, live-adjustable file concurrency, a shared total download speed limit, visible queue feedback, and a data-safe dual-source Windows release.

**Architecture:** `DownloadScheduler` remains the single public scheduling boundary and gains a task-admission queue ahead of its existing file downloads. Small asyncio-only resource primitives provide adjustable concurrency and aggregate byte pacing; SQLite persists only a numeric queue priority, while the controller and Qt models expose a read-only scheduler snapshot. Existing task/media identities, `.part` resume behavior, encrypted session data, content browsing, subscriptions, integrity repair, diagnostics, and signed updater remain intact.

**Tech Stack:** Python 3.12, asyncio, SQLite, PySide6/qasync, Telethon, pytest/pytest-asyncio/pytest-qt, Ruff, PyInstaller, Inno Setup, Ed25519 signed GitHub + ModelScope release pipeline.

---

## File map

- Create `src/telegram_downloader/resource_control.py`: asyncio-only adjustable permit limiter and aggregate bandwidth limiter.
- Create `tests/test_resource_control.py`: deterministic primitive tests using controlled clocks and events.
- Modify `src/telegram_downloader/settings.py`: persist and validate `speed_limit_kib` without breaking old JSON or positional constructors.
- Modify `src/telegram_downloader/downloader.py`: apply the shared byte limiter before each media chunk is written.
- Modify `src/telegram_downloader/domain.py`: add `queue_priority` to `TaskRecord` with a backward-compatible default.
- Modify `src/telegram_downloader/repository.py`: migrate/read/write priority and expose stable dispatch ordering.
- Modify `src/telegram_downloader/scheduler.py`: task admission, reprioritization, queue snapshots, live resource settings, waiting pause, and safe shutdown.
- Modify `src/telegram_downloader/controller.py`: restore in dispatch order, prioritize tasks, expose positions/summary, suppress duplicate waiting refresh loops, and apply download settings live.
- Modify `src/telegram_downloader/app.py`: construct the shared limiter, pass full resource settings, and wire the priority action.
- Modify `src/telegram_downloader/ui/models.py`: queue position in `TaskSummary` and status rendering.
- Modify `src/telegram_downloader/ui/main.py`: priority button/signal and scheduler summary.
- Modify `src/telegram_downloader/ui/settings.py`: total speed presets and clearer file-concurrency wording.
- Modify focused existing tests under `tests/` and `tests/ui/`; create `tests/test_download_queue_e2e.py` for the complete local flow.
- Modify version/package/docs files and create `docs/releases/v0.11.0.md` plus `docs/verification/v0.11.0-download-resource-control.md`.

### Task 1: Add deterministic adjustable resource primitives

**Files:**
- Create: `src/telegram_downloader/resource_control.py`
- Create: `tests/test_resource_control.py`

- [ ] **Step 1: Write failing tests for bandwidth pacing**

Add deterministic tests with a monotonic fake clock. The core expectations are:

```python
@pytest.mark.asyncio
async def test_unlimited_bandwidth_never_sleeps() -> None:
    sleeps: list[float] = []
    limiter = AsyncBandwidthLimiter(0, sleeper=_recording_sleep(sleeps))
    await limiter.acquire(512 * 1024)
    assert sleeps == []


@pytest.mark.asyncio
async def test_concurrent_bytes_share_one_rate() -> None:
    clock = FakeClock()
    limiter = AsyncBandwidthLimiter(1024, clock=clock, sleeper=clock.sleep)
    await asyncio.gather(limiter.acquire(512 * 1024), limiter.acquire(512 * 1024))
    assert clock.now == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_speed_change_resets_future_reservations() -> None:
    clock = FakeClock()
    limiter = AsyncBandwidthLimiter(512, clock=clock, sleeper=clock.sleep)
    await limiter.acquire(512 * 1024)
    limiter.set_speed_limit_kib(2048)
    await limiter.acquire(512 * 1024)
    assert clock.sleeps == pytest.approx([1.0, 0.25])
```

- [ ] **Step 2: Run the bandwidth tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_resource_control.py -k bandwidth -v
```

Expected: collection fails because `telegram_downloader.resource_control` does not exist.

- [ ] **Step 3: Implement `AsyncBandwidthLimiter`**

Implement the complete public contract in `resource_control.py`:

```python
class AsyncBandwidthLimiter:
    def __init__(self, speed_limit_kib: int = 0, *, clock=time.monotonic, sleeper=asyncio.sleep):
        self._clock = clock
        self._sleeper = sleeper
        self._lock = asyncio.Lock()
        self._speed_limit_kib = 0
        self._next_available = clock()
        self.set_speed_limit_kib(speed_limit_kib)

    @property
    def speed_limit_kib(self) -> int:
        return self._speed_limit_kib

    def set_speed_limit_kib(self, value: int) -> None:
        validate_speed_limit_kib(value)
        self._speed_limit_kib = value
        self._next_available = self._clock()

    async def acquire(self, byte_count: int) -> None:
        if byte_count < 0:
            raise ValueError("字节数不能为负数")
        if byte_count == 0 or self._speed_limit_kib == 0:
            return
        async with self._lock:
            rate = self._speed_limit_kib * 1024
            now = self._clock()
            finish = max(now, self._next_available) + byte_count / rate
            delay = max(0.0, finish - now)
            self._next_available = finish
        if delay:
            await self._sleeper(delay)
```

Also add `validate_speed_limit_kib(value)` accepting integer values from 0 through 1,048,576 and rejecting booleans.

- [ ] **Step 4: Write failing tests for adjustable FIFO permits**

Cover limit enforcement, increase, decrease, FIFO, and cancellation:

```python
@pytest.mark.asyncio
async def test_reducing_limit_does_not_cancel_active_holders() -> None:
    limiter = AdjustableConcurrencyLimiter(2)
    await limiter.acquire()
    await limiter.acquire()
    limiter.set_limit(1)
    waiter = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)
    assert waiter.done() is False
    limiter.release()
    await asyncio.sleep(0)
    assert waiter.done() is False
    limiter.release()
    await waiter
    assert limiter.active == 1


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
```

- [ ] **Step 5: Implement `AdjustableConcurrencyLimiter` and verify GREEN**

Use a FIFO `deque[Future[None]]`, explicit `active` count, `set_limit(1..5)`, cancellation removal, `_wake_waiters()`, and async context-manager methods. A permit is assigned before a waiter future is resolved; `release()` rejects underflow.

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_resource_control.py -v
.\.venv\Scripts\python.exe -m ruff check src\telegram_downloader\resource_control.py tests\test_resource_control.py
```

Expected: all resource-control tests pass and Ruff exits 0.

- [ ] **Step 6: Commit the primitives**

```powershell
git add src/telegram_downloader/resource_control.py tests/test_resource_control.py
git commit -m "feat: add adjustable download resource limiters"
```

### Task 2: Persist speed settings and throttle media chunks

**Files:**
- Modify: `src/telegram_downloader/settings.py`
- Modify: `src/telegram_downloader/downloader.py`
- Modify: `tests/test_settings.py`
- Modify: `tests/test_downloader.py`

- [ ] **Step 1: Write failing settings compatibility tests**

Add tests proving an old four-field JSON loads as unlimited, a new value round-trips atomically, booleans/negative/over-maximum values fail, and the existing first four positional arguments retain their meaning:

```python
def test_old_settings_default_to_unlimited_speed(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"api_id":1,"concurrency":3,"proxy":{},"check_updates_on_startup":true}', encoding="utf-8")
    assert SettingsStore(path).load().speed_limit_kib == 0


def test_speed_limit_round_trip(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    expected = AppSettings(1, 4, ProxySettings(), False, speed_limit_kib=2048)
    store.save(expected)
    assert store.load() == expected
```

- [ ] **Step 2: Run settings tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_settings.py -v
```

Expected: failures report missing `speed_limit_kib`.

- [ ] **Step 3: Add the backward-compatible settings field**

Append, rather than insert, the field so existing positional construction remains valid:

```python
@dataclass(frozen=True, slots=True)
class AppSettings:
    api_id: int = 0
    concurrency: int = 3
    proxy: ProxySettings = ProxySettings()
    check_updates_on_startup: bool = True
    speed_limit_kib: int = 0

    def __post_init__(self) -> None:
        # retain current API/proxy/concurrency/update validation
        validate_speed_limit_kib(self.speed_limit_kib)
```

Translate a limiter `ValueError` to `SettingsError("总下载限速设置无效")` so configuration errors remain in the existing settings error family.

- [ ] **Step 4: Write failing downloader integration tests**

Inject a recording limiter and prove it receives every non-empty chunk before completion; also prove limiter cancellation preserves `.part` progress and does not create the final file:

```python
@pytest.mark.asyncio
async def test_downloader_accounts_every_chunk_to_shared_limiter(tmp_path) -> None:
    limiter = RecordingLimiter()
    downloader = MediaDownloader(gateway_for([b"abc", b"de"]), repo, paths, bandwidth=limiter)
    await downloader.download(item)
    assert limiter.byte_counts == [3, 2]
    assert item.target_path.read_bytes() == b"abcde"
```

- [ ] **Step 5: Apply pacing before disk writes and verify GREEN**

Extend `MediaDownloader.__init__` with `bandwidth: AsyncBandwidthLimiter | None = None`, default to an unlimited instance, and add:

```python
async for chunk in self.gateway.stream_media(
    item.peer_ref,
    item.message_id,
    offset,
):
    await self.bandwidth.acquire(len(chunk))
    if pause_requested():
        os.fsync(stream.fileno())
        self.repository.update_item_progress(item.id, downloaded, ItemStatus.PAUSED)
        raise DownloadPaused("下载已暂停")
    stream.write(chunk)
```

Keep the existing post-write pause check as well, so pause remains responsive regardless of limiter mode.

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_settings.py tests\test_downloader.py -v
.\.venv\Scripts\python.exe -m ruff check src\telegram_downloader\settings.py src\telegram_downloader\downloader.py tests\test_settings.py tests\test_downloader.py
```

Expected: all focused tests pass.

- [ ] **Step 6: Commit settings and download pacing**

```powershell
git add src/telegram_downloader/settings.py src/telegram_downloader/downloader.py tests/test_settings.py tests/test_downloader.py
git commit -m "feat: throttle aggregate media downloads"
```

### Task 3: Add data-safe persistent dispatch priority

**Files:**
- Modify: `src/telegram_downloader/domain.py`
- Modify: `src/telegram_downloader/repository.py`
- Modify: `tests/test_repository.py`
- Modify: `tests/test_task_management_e2e.py`

- [ ] **Step 1: Write failing old-schema migration and ordering tests**

Build a database with the exact v0.10.0 `tasks` schema, insert queued tasks with deliberately non-lexical IDs/timestamps, initialize the current repository, and assert:

```python
assert "queue_priority" in table_columns(database, "tasks")
assert before_existing_columns == after_existing_columns
assert [task.id for task in repo.list_queued_for_dispatch()] == ["oldest", "newest"]
assert repo.prioritize_task("newest") is True
assert [task.id for task in repo.list_queued_for_dispatch()] == ["newest", "oldest"]
assert repo.clear_task_priority("newest") is True
assert repo.get_task("newest").queue_priority == 0
```

Also assert paused/completed/archived/missing tasks cannot be prioritized and do not appear in dispatch results.

- [ ] **Step 2: Run repository tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_repository.py tests\test_task_management_e2e.py -k "priority or dispatch or migration" -v
```

Expected: failures report missing field and repository methods.

- [ ] **Step 3: Implement schema migration and record mapping**

Add `queue_priority INTEGER NOT NULL DEFAULT 0` to `_SCHEMA`, `_TASK_COLUMNS`, `_QUALIFIED_TASK_COLUMNS`, insert values, and row mapping. Append the domain field after `archived_at`:

```python
@dataclass(frozen=True, slots=True)
class TaskRecord:
    # existing fields unchanged
    archived_at: datetime | None = None
    queue_priority: int = 0
```

In `initialize()`, use:

```python
if "queue_priority" not in columns:
    connection.execute(
        "ALTER TABLE tasks ADD COLUMN queue_priority INTEGER NOT NULL DEFAULT 0"
    )
```

Update placeholder counts and every selected column list together.

- [ ] **Step 4: Implement atomic priority methods and stable dispatch query**

Add these repository contracts, using the existing `_connection()` and `_task_from_row()` helpers:

```python
def list_queued_for_dispatch(self) -> list[TaskRecord]:
    with self._connection() as connection:
        rows = connection.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks "
            "WHERE status = ? AND archived_at IS NULL "
            "ORDER BY queue_priority DESC, created_at ASC, id ASC",
            (TaskStatus.QUEUED.value,),
        ).fetchall()
    return [self._task_from_row(row) for row in rows]

def prioritize_task(self, task_id: str) -> bool:
    with self._connection() as connection:
        eligible = connection.execute(
            "SELECT 1 FROM tasks WHERE id = ? AND status = ? AND archived_at IS NULL",
            (task_id, TaskStatus.QUEUED.value),
        ).fetchone()
        if eligible is None:
            return False
        highest = int(
            connection.execute(
                "SELECT COALESCE(MAX(queue_priority), 0) FROM tasks "
                "WHERE status = ? AND archived_at IS NULL",
                (TaskStatus.QUEUED.value,),
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE tasks SET queue_priority = ? WHERE id = ?",
            (highest + 1, task_id),
        )
    return True

def clear_task_priority(self, task_id: str) -> bool:
    with self._connection() as connection:
        cursor = connection.execute(
            "UPDATE tasks SET queue_priority = 0 "
            "WHERE id = ? AND queue_priority <> 0",
            (task_id,),
        )
    return cursor.rowcount == 1

def task_dispatch_key(self, task_id: str) -> tuple[int, datetime, str]:
    task = self.get_task(task_id)
    return (-task.queue_priority, task.created_at, task.id)
```

Do not alter `updated_at` for priority-only changes, so display recency and speed sampling remain stable.

- [ ] **Step 5: Verify migration data preservation and commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_repository.py tests\test_task_management_e2e.py -v
.\.venv\Scripts\python.exe -m ruff check src\telegram_downloader\domain.py src\telegram_downloader\repository.py tests\test_repository.py tests\test_task_management_e2e.py
```

Expected: focused suites pass; migration test proves every pre-existing column value is identical.

```powershell
git add src/telegram_downloader/domain.py src/telegram_downloader/repository.py tests/test_repository.py tests/test_task_management_e2e.py
git commit -m "feat: persist download queue priority"
```

### Task 4: Turn the scheduler into a single-active-task queue

**Files:**
- Modify: `src/telegram_downloader/scheduler.py`
- Modify: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing admission and order tests**

Use three repositories/downloaders controlled by events and start all calls concurrently. Assert only the first task enters, the remaining positions are 1 and 2, completing the first starts the second, and a reprioritized third runs next:

```python
first = asyncio.create_task(scheduler.run_task("oldest"))
second = asyncio.create_task(scheduler.run_task("middle"))
third = asyncio.create_task(scheduler.run_task("newest"))
await wait_until(lambda: scheduler.active_task_id == "oldest")
assert scheduler.queue_positions() == {"middle": 1, "newest": 2}
repo.prioritize_task("newest")
assert scheduler.prioritize_task("newest") is True
assert scheduler.queue_positions() == {"newest": 1, "middle": 2}
```

Add separate tests for duplicate `run_task`, queued pause never entering the downloader, active pause releasing the next task, `run_items` waiting behind another task, startup dispatch keys, and clean shutdown resolving all waiting callers.

- [ ] **Step 2: Run scheduler queue tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_scheduler.py -k "queue or priority or waiting or shutdown" -v
```

Expected: failures report missing queue snapshot/priority behavior and overlapping task execution.

- [ ] **Step 3: Add queued operation and snapshot types**

Add private `_QueuedOperation` with `task_id`, optional `item_ids`, stable dispatch key, enqueue sequence, ready/completion futures, cancellation flag and runner. Add the public immutable snapshot:

```python
@dataclass(frozen=True, slots=True)
class SchedulerSnapshot:
    active_task_id: str | None
    queued_task_ids: tuple[str, ...]
    concurrency: int
    speed_limit_kib: int

    @property
    def queued_count(self) -> int:
        return len(self.queued_task_ids)
```

Store `_pending`, `_operations`, `_active_operation`, `_sequence`, `AdjustableConcurrencyLimiter`, and the shared `AsyncBandwidthLimiter`. The repository dispatch key is used when available; test doubles without it fall back to enqueue sequence.

- [ ] **Step 4: Implement queue admission without a permanent worker task**

`run_task()` and `run_items()` enqueue an operation and await its shielded completion. `_admit_next()` pops the sorted first request only when no operation is active, clears persisted priority, creates one `_perform()` task and registers a done callback. The callback consumes the runner exception, resolves completion, removes the operation, clears the active slot and admits the next request.

Use this state transition skeleton:

```python
def _admit_next(self) -> None:
    if self._shutting_down or self._active_operation is not None or not self._pending:
        return
    self._pending.sort(key=lambda request: (request.dispatch_key, request.sequence))
    request = self._pending.pop(0)
    self._active_operation = request
    clear_priority = getattr(self.repository, "clear_task_priority", None)
    if clear_priority is not None:
        clear_priority(request.task_id)
    request.runner = asyncio.create_task(self._perform(request))
    request.runner.add_done_callback(lambda runner: self._finish_operation(request, runner))
```

Do not set `DOWNLOADING` until `_perform()` begins. Keep existing retry/item state machinery inside `_execute_task()` and `_execute_items()`.

- [ ] **Step 5: Implement pause, priority, runtime resources and shutdown**

Add:

```python
def snapshot(self) -> SchedulerSnapshot:
    active = self._active_operation
    return SchedulerSnapshot(
        active.task_id if active is not None else None,
        tuple(request.task_id for request in self._sorted_pending()),
        self._permits.limit,
        self._bandwidth.speed_limit_kib,
    )

def queue_positions(self) -> dict[str, int]:
    return {
        request.task_id: position
        for position, request in enumerate(self._sorted_pending(), start=1)
    }

def is_active(self, task_id: str) -> bool:
    return self._active_operation is not None and self._active_operation.task_id == task_id

def prioritize_task(self, task_id: str) -> bool:
    request = self._operations.get(task_id)
    if request is None or request is self._active_operation or request not in self._pending:
        return False
    request.dispatch_key = self._dispatch_key(task_id, request.sequence)
    self._pending.sort(key=lambda queued: (queued.dispatch_key, queued.sequence))
    return True

def configure_resources(self, concurrency: int, speed_limit_kib: int) -> None:
    self._permits.set_limit(concurrency)
    self._bandwidth.set_speed_limit_kib(speed_limit_kib)
```

For a pending pause, remove the request, mark it cancelled, complete its future normally, remove it from `_operations`, then set repository status to paused. For active pause, retain the existing event behavior. During shutdown, mark all pending requests cancelled and complete them before waiting up to `shutdown_grace_seconds` for the active runner.

- [ ] **Step 6: Run the complete scheduler/resource regression**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_scheduler.py tests\test_resource_control.py -v
.\.venv\Scripts\python.exe -m ruff check src\telegram_downloader\scheduler.py tests\test_scheduler.py
```

Expected: every old retry/resume/concurrency test and every new queue test passes.

- [ ] **Step 7: Commit scheduler queueing**

```powershell
git add src/telegram_downloader/scheduler.py tests/test_scheduler.py
git commit -m "feat: schedule one predictable download task"
```

### Task 5: Expose queue position, priority action, and resource settings in Qt

**Files:**
- Modify: `src/telegram_downloader/ui/models.py`
- Modify: `src/telegram_downloader/ui/main.py`
- Modify: `src/telegram_downloader/ui/settings.py`
- Modify: `tests/ui/test_main_window.py`
- Modify: `tests/ui/test_settings_dialog.py`
- Modify: `tests/test_ui_models.py`

- [ ] **Step 1: Write failing model and task-workbench tests**

Append `queue_position: int | None = None` to `TaskSummary`. Assert a queued row renders `等待中 · 第 2 位`, a positionless queued row remains `等待中`, and non-queued statuses ignore the position. In the window tests, assert `prioritize_task_requested` emits the sole queued task ID and the button is disabled for multi-selection, active, paused, completed, failed, and archived tasks.

Also test:

```python
window.set_scheduler_summary(active=1, queued=3, concurrency=3, speed_limit_kib=0)
assert window.scheduler_summary.text() == "调度：1 个下载中 · 3 个等待 · 文件并发 3 · 不限速"
window.set_scheduler_summary(active=0, queued=0, concurrency=3, speed_limit_kib=2048)
assert window.scheduler_summary.text() == "调度：空闲 · 文件并发 3 · 限速 2.0 MiB/s"
```

- [ ] **Step 2: Write failing settings-dialog preset tests**

Assert the speed combo contains data values `(0, 256, 512, 1024, 2048, 5120, 10240, 20480, 51200)`, round-trips `speed_limit_kib`, and the form label is `文件并发` rather than `并发下载`.

- [ ] **Step 3: Run Qt/model tests and verify RED**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests\test_ui_models.py tests\ui\test_main_window.py tests\ui\test_settings_dialog.py -v
```

Expected: failures identify the absent fields, controls, signal and summary label.

- [ ] **Step 4: Implement the compact UI without widening the table**

Render task status through a helper that appends the queue position only for `TaskStatus.QUEUED`. Add `prioritize_task_requested = Signal(str)`, a `优先下载` button beside continue/retry, and exact single-selection eligibility. Add a muted scheduler summary label to the existing real-time overview and the typed setter used by tests.

In settings, use the fixed preset combo and return:

```python
return AppSettings(
    self.api_id.value(),
    self.concurrency.value(),
    self.proxy_values(),
    self.check_updates.isChecked(),
    speed_limit_kib=int(self.speed_limit.currentData()),
)
```

- [ ] **Step 5: Verify Qt behavior and commit**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests\test_ui_models.py tests\ui\test_main_window.py tests\ui\test_settings_dialog.py -v
.\.venv\Scripts\python.exe -m ruff check src\telegram_downloader\ui tests\ui tests\test_ui_models.py
```

Expected: all focused Qt/model tests pass.

```powershell
git add src/telegram_downloader/ui/models.py src/telegram_downloader/ui/main.py src/telegram_downloader/ui/settings.py tests/test_ui_models.py tests/ui/test_main_window.py tests/ui/test_settings_dialog.py
git commit -m "feat: show and control download queue resources"
```

### Task 6: Integrate scheduler state through controller and application

**Files:**
- Modify: `src/telegram_downloader/controller.py`
- Modify: `src/telegram_downloader/app.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write failing controller integration tests**

Cover these exact behaviors:

- restore calls `recover_interrupted()` and submits `list_queued_for_dispatch()` order;
- `prioritize_task()` updates the repository first, then the scheduler, refreshes, and reports the new position;
- a state race returning `False` shows a safe “任务状态已变化” message;
- `refresh_tasks()` passes queue positions into each `TaskSummary` and updates one scheduler summary;
- waiting `_run_and_refresh()` operations do not call periodic `refresh_tasks()`, while the active operation does;
- `apply_settings()` invokes `configure_resources()` after both atomic stores succeed and distinguishes proxy/API changes;
- a settings-store or vault failure does not partially mutate in-memory settings or scheduler limits.

Use scheduler snapshots rather than inspecting private fields:

```python
scheduler.snapshot.return_value = SchedulerSnapshot("active", ("next",), 3, 2048)
controller.refresh_tasks()
window.set_scheduler_summary.assert_called_once_with(
    active=1, queued=1, concurrency=3, speed_limit_kib=2048
)
```

- [ ] **Step 2: Run controller/app tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_controller.py tests\test_app.py -k "priority or dispatch or scheduler or apply_settings or restore" -v
```

Expected: failures report missing controller action, snapshot integration and full settings wiring.

- [ ] **Step 3: Integrate full resource settings at service creation**

Change the service builder to receive `AppSettings`, construct one limiter, and pass the same instance through downloader and scheduler:

```python
def build_services(gateway: TelethonGateway, resource_settings: AppSettings):
    planner = TaskPlanner(gateway, repository, paths.downloads)
    bandwidth = AsyncBandwidthLimiter(resource_settings.speed_limit_kib)
    downloader = MediaDownloader(
        gateway,
        repository,
        paths,
        bandwidth=bandwidth,
    )
    scheduler = DownloadScheduler(
        repository,
        downloader,
        concurrency=resource_settings.concurrency,
        bandwidth=bandwidth,
    )
    content_browser.bind_online(gateway, planner)
    subscriptions.bind_online(gateway, planner)
    return planner, scheduler, content_browser
```

Update initial construction plus credential and reconnect builder calls to pass the complete `AppSettings`; do not create separate limiters for tasks.

Wire `window.prioritize_task_requested` to `controller.prioritize_task`. Keep the slot synchronous because both repository priority update and in-memory reorder are local operations.

- [ ] **Step 4: Implement atomic live settings and queue-aware refresh**

In `apply_settings()`, save settings and secrets before mutating controller state. Then call:

```python
configure = getattr(self.scheduler, "configure_resources", None)
if configure is not None:
    configure(settings.concurrency, settings.speed_limit_kib)
```

Compare old/new API and proxy fields for the reconnect suffix. During restore, use `list_queued_for_dispatch()` when available. During summary creation, use one `snapshot = scheduler.snapshot()` and one positions map for the entire refresh.

In `_run_and_refresh()`, perform timed refresh only when `scheduler.is_active(task_id)`; completion still triggers an unconditional final refresh.

- [ ] **Step 5: Run controller/application regression and commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_controller.py tests\test_app.py -v
.\.venv\Scripts\python.exe -m ruff check src\telegram_downloader\controller.py src\telegram_downloader\app.py tests\test_controller.py tests\test_app.py
```

Expected: all existing login, reconnection, content, subscription, integrity and diagnostics controller tests still pass.

```powershell
git add src/telegram_downloader/controller.py src/telegram_downloader/app.py tests/test_controller.py tests/test_app.py
git commit -m "feat: integrate live download resource controls"
```

### Task 7: Prove the unified queue end to end and prepare v0.11.0

**Files:**
- Create: `tests/test_download_queue_e2e.py`
- Modify: `tests/test_packaging_contract.py`
- Modify: `tests/test_self_test.py`
- Modify: `src/telegram_downloader/__init__.py`
- Modify: `pyproject.toml`
- Modify: `installer/TelegramDownloader.iss`
- Modify: `README.md`
- Create: `docs/releases/v0.11.0.md`
- Create: `docs/verification/v0.11.0-download-resource-control.md`

- [ ] **Step 1: Write the failing end-to-end queue test**

Create a project-local temporary root, initialize the real repository, planner-compatible fake tasks from link/search/subscription origins, a chunked fake gateway, real `MediaDownloader`, shared limiter and real scheduler. The test must assert:

```python
assert scheduler.active_task_id == link_task.id
assert scheduler.queue_positions() == {search_task.id: 1, subscription_task.id: 2}
repo.prioritize_task(subscription_task.id)
scheduler.prioritize_task(subscription_task.id)
assert scheduler.queue_positions()[subscription_task.id] == 1
```

Release the fake gateway task by task, pause/resume one operation, restart scheduler/repository after a partial file, and assert final bytes, media uniqueness, SHA-256 fields, task order and every recorded path is inside the temporary application root.

- [ ] **Step 2: Run E2E and verify RED, then fix only exposed integration defects**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_download_queue_e2e.py -v
```

Expected before fixes: at least one assertion exposes any remaining integration mismatch. Apply the smallest production correction required by the failing assertion, rerun until PASS, and keep all fake content synthetic.

- [ ] **Step 3: Update release contracts first and verify RED**

Change packaging tests to require version `0.11.0`, release notes, README terms `优先下载`, `总下载限速`, `等待中 · 第`, `文件并发`, and the import/wiring of `AsyncBandwidthLimiter`. Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_packaging_contract.py tests\test_self_test.py -v
```

Expected: failures show old version metadata and missing docs.

- [ ] **Step 4: Set version 0.11.0 and document user behavior**

Set `0.11.0` in `pyproject.toml`, `src/telegram_downloader/__init__.py`, and `installer/TelegramDownloader.iss`. Update README with queue ordering, non-preemptive priority, live file concurrency, aggregate media-only speed limit, restart persistence, and non-C data guarantees. Write `docs/releases/v0.11.0.md` with user-facing changes and compatibility notes. Seed the verification document with commands/acceptance fields but no fabricated test counts, hashes, sizes, URLs or remote claims.

- [ ] **Step 5: Run the first complete development verification**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1
```

Expected: Pytest and Ruff pass. Record the actual test count and duration in `docs/verification/v0.11.0-download-resource-control.md` using `apply_patch`.

- [ ] **Step 6: Commit the integrated release candidate**

```powershell
git add src tests pyproject.toml installer/TelegramDownloader.iss README.md docs/releases/v0.11.0.md docs/verification/v0.11.0-download-resource-control.md
git commit -m "chore: prepare v0.11.0 resource control release"
```

### Task 8: Review, perform three-pass QA, integrate, package, and publish

**Files:**
- Modify: production/tests exposed by review
- Modify: `docs/verification/v0.11.0-download-resource-control.md`

- [ ] **Step 1: Perform structured review against the committed design**

Review the full branch diff for scheduler liveness, future completion, cancellation, limiter accounting, SQLite transaction boundaries, old positional settings, error privacy, Qt selection state, controller refresh amplification, and all out-of-root writes. Classify findings by severity with exact file/line evidence. Write a regression test before each fix, run the focused suite, and commit fixes by cohesive concern.

- [ ] **Step 2: Run verification pass 2 from a clean worktree**

Run:

```powershell
git status --short
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1
```

Expected: clean worktree before execution, full test/Ruff success after all review fixes. Add the actual result to the verification document and commit the evidence.

- [ ] **Step 3: Run synthetic stress and isolated real-data migration QA**

Use project-root `.build-temp` copies only. Exercise at least 50 tasks/500 media items with repeated priority changes, concurrent duplicate submissions, queued/active pauses, live 1↔5 concurrency changes, 256 KiB/s↔unlimited changes, shutdown during limiter wait, and restart recovery. Copy v0.10.0 direct-run `data` and `downloads` to an isolated F-drive root; capture SHA-256 and row counts before/after initialization and self-test. Assert all old settings, DPAPI credentials, both databases, downloaded files and `.part` bytes are preserved except the intended additive `queue_priority` schema migration in the copy. Never output secrets, Telegram names, links or filenames.

- [ ] **Step 4: Drive source and frozen GUI like a user**

With the isolated data copy, launch the source GUI and then frozen GUI. Verify session reuse without QR, navigation, group refresh feedback, search progress/cancel, preview open, selective queue, priority button eligibility, queue positions, 256 KiB/s/2 MiB/s/unlimited settings, pause/resume/restart, integrity verify/repair, diagnostics, update check and clean shutdown. Capture screenshots only if they contain no private content; otherwise record redacted observations and process exit status.

- [ ] **Step 5: Build and smoke test all Windows artifacts**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-installer.ps1 -SkipAppBuild
```

Expected: `PACKAGED_SMOKE_OK` and `INSTALLER_SMOKE_OK`. Verify the portable ZIP contains no `data/`, `downloads/`, settings, credentials, sessions, databases, logs, diagnostics packages, `.part`, `.corrupt*`, update backups or release secrets. Run frozen `--self-test`; every writable path must resolve inside the selected D/F application root.

- [ ] **Step 6: Integrate to local main and run verification pass 3**

Fast-forward local `main` from the reviewed feature branch, ensure no unrelated files changed, and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1
```

Create `dist/release/v0.11.0-portable` from the clean v0.11.0 runtime and copy only v0.10.0 `data`/`downloads` for direct-run QA. Keep v0.10.0 immutable. Recheck pre/post hashes, row counts, session reuse, queue migration and all paths.

- [ ] **Step 7: Publish the signed dual-source release**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/release/release.ps1 -Version 0.11.0
```

Expected: `RELEASE_PUBLISHED v0.11.0`. Independently verify GitHub `main`, ModelScope `source`, peeled `v0.11.0` tags, all expected GitHub assets, all expected ModelScope version assets, both stable `latest.json` pointers, Ed25519 signature, runtime/installer SHA-256, and exact remote byte matches. Run the real `UpdateCoordinator` with current version 0.10.0 and require discovery of 0.11.0 from both sources with `blocked=False`.

- [ ] **Step 8: Record public evidence and finalize source refs**

Append only measured test counts, stress results, GUI observations, path roots, hashes, sizes, release URLs, tag commit and remote verification evidence to `docs/verification/v0.11.0-download-resource-control.md`. Commit after the tag, push GitHub `main` and ModelScope `source`, and prove both source branches contain the evidence commit while both version tags still resolve to the release code commit.
