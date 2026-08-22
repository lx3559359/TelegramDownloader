# Tray Background, Notifications, and Download Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep TelegramDownloader running safely in the Windows tray, deliver privacy-safe system notifications, support opt-in startup, and pause/resume downloads according to a persistent weekly schedule.

**Architecture:** Keep one Qt/asyncio process and separate lifecycle, notification, activation, autostart, and schedule responsibilities into focused modules. Extend the existing scheduler with an admission gate and persisted pause reasons instead of redesigning cross-task concurrency; integrate all effects through narrow callbacks constructed in `app.py`.

**Tech Stack:** Python 3.12, PySide6/QtNetwork, qasync, asyncio, SQLite, Windows `winreg`, pytest, pytest-asyncio, pytest-qt, Ruff, PyInstaller, Inno Setup.

---

## File map

**Create:**

- `src/telegram_downloader/download_schedule.py` — weekly time-window evaluation and async boundary controller.
- `src/telegram_downloader/notifications.py` — application events, privacy-safe aggregation, and notification routing.
- `src/telegram_downloader/autostart.py` — current-user Windows startup integration.
- `src/telegram_downloader/activation.py` — local named-pipe activation server/client.
- `src/telegram_downloader/background.py` — tray adapter and application lifecycle controller.
- `tests/test_download_schedule.py`
- `tests/test_notifications.py`
- `tests/test_autostart.py`
- `tests/test_activation.py`
- `tests/test_background.py`
- `tests/test_background_runtime_e2e.py`
- `docs/releases/v0.13.0.md`
- `docs/verification/v0.13.0-tray-background-notifications-scheduling.md`

**Modify:**

- `src/telegram_downloader/settings.py` — backward-compatible background and schedule settings.
- `src/telegram_downloader/domain.py` — `PauseReason` and task pause provenance.
- `src/telegram_downloader/repository.py` — `pause_reason` migration and queries.
- `src/telegram_downloader/scheduler.py` — schedule admission gate and terminal event callback.
- `src/telegram_downloader/subscription_scheduler.py` — enriched subscription-created event.
- `src/telegram_downloader/instance_guard.py` — duplicate-instance activation fallback.
- `src/telegram_downloader/controller.py` — event publication and runtime settings delegation.
- `src/telegram_downloader/app.py` — construct and connect new components.
- `src/telegram_downloader/__main__.py` — `--background` launch mode.
- `src/telegram_downloader/ui/settings.py` — background/notification/schedule settings tab.
- `src/telegram_downloader/ui/main.py` — navigation targets used by notification clicks.
- `tests/test_settings.py`
- `tests/test_repository.py`
- `tests/test_scheduler.py`
- `tests/test_subscription_scheduler.py`
- `tests/test_instance_guard.py`
- `tests/test_controller.py`
- `tests/test_app.py`
- `tests/test_packaging_contract.py`
- `tests/ui/test_settings_dialog.py`
- `README.md`
- `pyproject.toml`
- `src/telegram_downloader/__init__.py`
- `installer/TelegramDownloader.iss`

## Task 1: Add backward-compatible settings types

**Files:**
- Modify: `src/telegram_downloader/settings.py`
- Modify: `tests/test_settings.py`

- [ ] **Step 1: Write failing schedule-settings tests**

```python
def test_background_settings_have_safe_compatible_defaults(tmp_path: Path) -> None:
    loaded = SettingsStore(tmp_path / "missing.json").load()
    assert loaded.close_to_tray is True
    assert loaded.notifications_enabled is True
    assert loaded.autostart_enabled is False
    assert loaded.tray_hint_shown is False
    assert loaded.download_schedule == DownloadScheduleSettings()


@pytest.mark.parametrize(
    "value",
    [
        {"weekdays": []},
        {"weekdays": [0, 7]},
        {"start_minute": -1},
        {"end_minute": 1440},
    ],
)
def test_download_schedule_rejects_invalid_values(value) -> None:
    with pytest.raises(SettingsError):
        DownloadScheduleSettings(**value)


def test_old_settings_json_loads_with_new_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"api_id":123,"concurrency":3}', encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert loaded.api_id == 123
    assert loaded.download_schedule.enabled is False
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_settings.py -q`

Expected: collection/import failure because `DownloadScheduleSettings` and the new fields do not exist.

- [ ] **Step 3: Implement immutable validated settings**

```python
@dataclass(frozen=True, slots=True)
class DownloadScheduleSettings:
    enabled: bool = False
    weekdays: tuple[int, ...] = tuple(range(7))
    start_minute: int = 0
    end_minute: int = 0

    def __post_init__(self) -> None:
        normalized = tuple(dict.fromkeys(self.weekdays))
        if not normalized or any(
            not isinstance(day, int) or isinstance(day, bool) or not 0 <= day <= 6
            for day in normalized
        ):
            raise SettingsError("下载星期必须是周一到周日的非空集合")
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 1439
            for value in (self.start_minute, self.end_minute)
        ):
            raise SettingsError("下载时段分钟必须在 0 到 1439 之间")
        object.__setattr__(self, "weekdays", normalized)


@dataclass(frozen=True, slots=True)
class AppSettings:
    api_id: int = 0
    concurrency: int = 3
    proxy: ProxySettings = ProxySettings()
    check_updates_on_startup: bool = True
    speed_limit_kib: int = 0
    close_to_tray: bool = True
    notifications_enabled: bool = True
    autostart_enabled: bool = False
    tray_hint_shown: bool = False
    download_schedule: DownloadScheduleSettings = DownloadScheduleSettings()
```

In `SettingsStore.load`, convert the nested object before `AppSettings(**values)`:

```python
schedule_raw = raw.get("download_schedule", {})
if not isinstance(schedule_raw, dict):
    raise SettingsError("下载时段设置必须是对象")
values["download_schedule"] = DownloadScheduleSettings(
    **schedule_raw,
)
```

- [ ] **Step 4: Run settings tests and Ruff**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_settings.py -q`

Expected: all settings tests pass.

Run: `.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/settings.py tests/test_settings.py`

Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/settings.py tests/test_settings.py
git commit -m "feat: add background and schedule settings"
```

## Task 2: Implement deterministic weekly schedule evaluation

**Files:**
- Create: `src/telegram_downloader/download_schedule.py`
- Create: `tests/test_download_schedule.py`

- [ ] **Step 1: Write failing schedule-boundary tests**

```python
MONDAY = datetime(2026, 8, 24, 10, 0).astimezone()


def schedule(start: int, end: int, days=(0,)) -> DownloadScheduleSettings:
    return DownloadScheduleSettings(True, days, start, end)


def test_same_day_window_and_next_boundary() -> None:
    value = evaluate_download_schedule(schedule(9 * 60, 17 * 60), MONDAY)
    assert value.allowed is True
    assert value.next_boundary.hour == 17


def test_cross_midnight_end_segment_belongs_to_start_day() -> None:
    tuesday_0100 = MONDAY.replace(day=25, hour=1)
    value = evaluate_download_schedule(schedule(22 * 60, 2 * 60), tuesday_0100)
    assert value.allowed is True
    assert value.next_boundary == tuesday_0100.replace(hour=2)


def test_equal_times_mean_full_selected_day() -> None:
    assert evaluate_download_schedule(schedule(0, 0), MONDAY).allowed is True
    assert evaluate_download_schedule(
        schedule(0, 0), MONDAY.replace(day=25)
    ).allowed is False


def test_disabled_schedule_is_always_open() -> None:
    value = evaluate_download_schedule(DownloadScheduleSettings(), MONDAY)
    assert value.allowed is True
    assert value.next_boundary is None
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_download_schedule.py -q`

Expected: import failure because `download_schedule.py` does not exist.

- [ ] **Step 3: Implement schedule evaluation without timers or Qt**

```python
@dataclass(frozen=True, slots=True)
class DownloadScheduleState:
    allowed: bool
    next_boundary: datetime | None


def _minute_of_day(value: datetime) -> int:
    return value.hour * 60 + value.minute


def _allowed_at(settings: DownloadScheduleSettings, now: datetime) -> bool:
    if not settings.enabled:
        return True
    minute = _minute_of_day(now)
    selected = set(settings.weekdays)
    if settings.start_minute == settings.end_minute:
        return now.weekday() in selected
    if settings.start_minute < settings.end_minute:
        return now.weekday() in selected and settings.start_minute <= minute < settings.end_minute
    previous_day = (now.weekday() - 1) % 7
    return (
        now.weekday() in selected and minute >= settings.start_minute
    ) or (
        previous_day in selected and minute < settings.end_minute
    )


def evaluate_download_schedule(
    settings: DownloadScheduleSettings,
    now: datetime,
) -> DownloadScheduleState:
    if now.utcoffset() is None:
        raise ValueError("下载时段计算要求本地时区时间")
    allowed = _allowed_at(settings, now)
    if not settings.enabled:
        return DownloadScheduleState(True, None)
    cursor = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = cursor + timedelta(days=8)
    while cursor <= limit:
        if _allowed_at(settings, cursor) != allowed:
            return DownloadScheduleState(allowed, cursor)
        cursor += timedelta(minutes=1)
    raise RuntimeError("无法计算下载时段的下一边界")
```

- [ ] **Step 4: Add Sunday wrap, timezone-aware, and transition-table cases**

```python
@pytest.mark.parametrize(
    ("day", "hour", "expected"),
    [(0, 8, False), (0, 9, True), (0, 16, True), (0, 17, False)],
)
def test_same_day_transition_table(day, hour, expected) -> None:
    now = MONDAY + timedelta(days=day)
    assert evaluate_download_schedule(
        schedule(9 * 60, 17 * 60), now.replace(hour=hour)
    ).allowed is expected


def test_naive_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="时区"):
        evaluate_download_schedule(schedule(0, 0), datetime(2026, 8, 24))
```

- [ ] **Step 5: Run focused tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_download_schedule.py -q`

Expected: all schedule tests pass.

```powershell
git add src/telegram_downloader/download_schedule.py tests/test_download_schedule.py
git commit -m "feat: evaluate weekly download schedules"
```

## Task 3: Persist user and schedule pause provenance

**Files:**
- Modify: `src/telegram_downloader/domain.py`
- Modify: `src/telegram_downloader/repository.py`
- Modify: `tests/test_repository.py`

- [ ] **Step 1: Write failing migration and round-trip tests**

```python
def test_pause_reason_migrates_and_round_trips(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, item = records(tmp_path)
    repo.create_task(task, [item])

    repo.update_task_status(task.id, TaskStatus.PAUSED)
    assert repo.get_task(task.id).pause_reason is PauseReason.USER

    repo.update_task_status(
        task.id,
        TaskStatus.PAUSED,
        pause_reason=PauseReason.SCHEDULE,
    )
    assert repo.get_task(task.id).pause_reason is PauseReason.SCHEDULE

    repo.update_task_status(task.id, TaskStatus.QUEUED)
    assert repo.get_task(task.id).pause_reason is None


def test_existing_paused_task_migrates_as_user_pause(tmp_path: Path) -> None:
    database = tmp_path / "tasks.sqlite3"
    create_pre_pause_reason_database(database, status="paused")
    repo = TaskRepository(database)
    repo.initialize()
    assert repo.list_paused_by_reason(PauseReason.USER)[0].pause_reason is PauseReason.USER
```

- [ ] **Step 2: Run repository tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_repository.py -q`

Expected: failure because `PauseReason`, `pause_reason`, and the repository API do not exist.

- [ ] **Step 3: Add the domain value and schema migration**

```python
class PauseReason(StrEnum):
    USER = "user"
    SCHEDULE = "schedule"


@dataclass(frozen=True, slots=True)
class TaskRecord:
    # existing fields stay in their current order
    pause_reason: PauseReason | None = None
```

Add `pause_reason TEXT` to new schemas and `_TASK_COLUMNS`/`_QUALIFIED_TASK_COLUMNS`. In `initialize()` perform the idempotent migration:

```python
if "pause_reason" not in columns:
    connection.execute("ALTER TABLE tasks ADD COLUMN pause_reason TEXT")
    connection.execute(
        "UPDATE tasks SET pause_reason = ? WHERE status = ?",
        (PauseReason.USER.value, TaskStatus.PAUSED.value),
    )
```

- [ ] **Step 4: Make status writes preserve provenance**

```python
@staticmethod
def _pause_value(
    status: TaskStatus,
    pause_reason: PauseReason | None,
) -> str | None:
    if status is not TaskStatus.PAUSED:
        return None
    return (pause_reason or PauseReason.USER).value


def list_paused_by_reason(self, reason: PauseReason) -> list[TaskRecord]:
    with self._connection() as connection:
        rows = connection.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks "
            "WHERE status = ? AND pause_reason = ? AND archived_at IS NULL "
            "ORDER BY created_at, id",
            (TaskStatus.PAUSED.value, reason.value),
        ).fetchall()
    return [self._task_from_row(row) for row in rows]
```

Extend both status update methods with keyword-only `pause_reason`; write the computed value in the same transaction and decode it in `_task_from_row`.

- [ ] **Step 5: Run repository and packaging migration tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_repository.py tests/test_packaging_contract.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/domain.py src/telegram_downloader/repository.py tests/test_repository.py
git commit -m "feat: persist task pause reasons"
```

## Task 4: Add the scheduler admission gate and schedule pause behavior

**Files:**
- Modify: `src/telegram_downloader/scheduler.py`
- Modify: `tests/test_scheduler.py`
- Modify: `tests/test_download_queue_e2e.py`

- [ ] **Step 1: Write failing admission and resume tests**

```python
@pytest.mark.asyncio
async def test_closed_admission_keeps_tasks_queued_until_opened() -> None:
    repo = QueueRepo(("a", "b"))
    scheduler = DownloadScheduler(repo, ImmediateDownloader())
    scheduler.set_admission_open(False)
    first = asyncio.create_task(scheduler.run_task("a"))
    await asyncio.sleep(0)
    assert scheduler.active_task_id is None
    assert scheduler.snapshot().queued_task_ids == ("a",)
    scheduler.set_admission_open(True)
    await first
    assert repo.task_statuses["a"] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_schedule_open_resumes_only_schedule_paused_tasks() -> None:
    repo = ScheduleRepo(user_paused=("user",), schedule_paused=("clock",))
    scheduler = DownloadScheduler(repo, ImmediateDownloader())
    await scheduler.set_schedule_open(True)
    assert repo.resumed == ["clock"]
    assert repo.task_statuses["user"] is TaskStatus.PAUSED
```

- [ ] **Step 2: Run focused scheduler tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_scheduler.py -q`

Expected: attribute failures for admission and schedule APIs.

- [ ] **Step 3: Add gate state and pause-reason-aware repository protocol**

```python
class SchedulerRepository(Protocol):
    def list_paused_by_reason(self, reason: PauseReason) -> list[TaskRecord]: ...


class DownloadScheduler:
    def __init__(...):
        # existing initialization
        self._admission_open = True
        self._pause_reasons: dict[str, PauseReason] = {}

    def set_admission_open(self, opened: bool) -> None:
        self._admission_open = bool(opened)
        if self._admission_open:
            self._admit_next()

    def _admit_next(self) -> None:
        if (
            self._shutting_down
            or not self._admission_open
            or self._active_operation is not None
            or not self._pending
        ):
            return
        self._pending.sort(key=self._operation_sort_key)
        operation = self._pending.pop(0)
        self._active_operation = operation
        operation.runner = asyncio.create_task(self._perform(operation))
        operation.runner.add_done_callback(
            lambda runner, selected=operation: self._finish_operation(selected, runner)
        )
```

- [ ] **Step 4: Implement schedule close/open semantics**

```python
async def set_schedule_open(self, opened: bool) -> set[str]:
    self.set_admission_open(opened)
    if not opened:
        active = self.active_task_id
        if active is not None:
            self._pause_reasons[active] = PauseReason.SCHEDULE
            self._pause_flag(active).set()
        return set()
    tasks = self.repository.list_paused_by_reason(PauseReason.SCHEDULE)
    return await self.resume_tasks([task.id for task in tasks])
```

When `_execute_task()` observes paused media, persist the recorded reason and remove it after use:

```python
reason = self._pause_reasons.pop(task_id, PauseReason.USER)
self.repository.update_task_status(
    task_id,
    TaskStatus.PAUSED,
    pause_reason=reason,
)
```

Manual `pause_tasks()` must explicitly write `PauseReason.USER`; resuming or terminal statuses clear the column through Task 3's repository contract.

- [ ] **Step 5: Run scheduler, queue E2E, and stress tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_scheduler.py tests/test_download_queue_e2e.py tests/test_download_queue_stress.py -q`

Expected: all tests pass; existing one-active-task ordering remains unchanged.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/scheduler.py tests/test_scheduler.py tests/test_download_queue_e2e.py
git commit -m "feat: gate downloads by schedule"
```

## Task 5: Build privacy-safe event aggregation

**Files:**
- Create: `src/telegram_downloader/notifications.py`
- Create: `tests/test_notifications.py`

- [ ] **Step 1: Write failing privacy, deduplication, and batching tests**

```python
def test_task_terminal_event_is_deduplicated_and_contains_no_private_text() -> None:
    batcher = NotificationBatcher(window_seconds=5.0)
    private = "private-channel secret-file.mp4 D:\\private"
    event = ApplicationEvent(
        EventKind.DOWNLOAD_COMPLETED,
        identity="task-1",
        count=1,
        route=NotificationRoute.TASKS,
        private_context=private,
    )
    assert batcher.record(event, now=10.0) is True
    assert batcher.record(event, now=11.0) is False
    payload = batcher.flush_due(now=15.0)[0]
    serialized = f"{payload.title} {payload.body}"
    assert "private-channel" not in serialized
    assert "secret-file.mp4" not in serialized
    assert "D:\\private" not in serialized


def test_three_completion_events_are_coalesced() -> None:
    batcher = NotificationBatcher(window_seconds=5.0)
    for index in range(3):
        batcher.record(completed_event(f"task-{index}"), now=float(index))
    payload = batcher.flush_due(now=7.0)[0]
    assert payload.body == "3 个下载任务已完成"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_notifications.py -q`

Expected: import failure because the notification module does not exist.

- [ ] **Step 3: Implement events, payloads, formatter, and batcher**

```python
class EventKind(StrEnum):
    DOWNLOAD_COMPLETED = "download-completed"
    DOWNLOAD_FAILED = "download-failed"
    SUBSCRIPTION_MATCH = "subscription-match"
    AUTH_REQUIRED = "auth-required"
    DISK_FULL = "disk-full"
    SCHEDULE_OPENED = "schedule-opened"
    SCHEDULE_CLOSED = "schedule-closed"
    UPDATE_AVAILABLE = "update-available"


class NotificationRoute(StrEnum):
    TASKS = "tasks"
    SUBSCRIPTIONS = "subscriptions"
    LOGIN = "login"
    UPDATE = "update"


@dataclass(frozen=True, slots=True)
class ApplicationEvent:
    kind: EventKind
    identity: str
    count: int
    route: NotificationRoute
    private_context: str = ""


@dataclass(frozen=True, slots=True)
class NotificationPayload:
    title: str
    body: str
    route: NotificationRoute


_TEXT = {
    EventKind.DOWNLOAD_COMPLETED: ("下载完成", "{count} 个下载任务已完成"),
    EventKind.DOWNLOAD_FAILED: ("下载需要处理", "{count} 个下载任务部分失败"),
    EventKind.SUBSCRIPTION_MATCH: ("订阅发现新媒体", "已加入 {count} 个媒体到下载队列"),
    EventKind.AUTH_REQUIRED: ("需要重新登录", "Telegram 登录已失效，请打开应用处理"),
    EventKind.DISK_FULL: ("磁盘空间不足", "下载已安全暂停，请释放应用所在磁盘空间"),
    EventKind.SCHEDULE_OPENED: ("下载时段已开始", "时段暂停的任务正在恢复"),
    EventKind.SCHEDULE_CLOSED: ("下载时段已结束", "活动下载正在安全暂停"),
    EventKind.UPDATE_AVAILABLE: ("发现正式版更新", "打开应用可查看并确认更新"),
}
```

`NotificationBatcher` stores `(kind, route)` batches until `deadline`, keeps a permanent terminal identity set for task completion/failure, ignores `private_context` while formatting, and returns only due payloads from `flush_due()`.

- [ ] **Step 4: Run tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_notifications.py -q`

Expected: all tests pass.

```powershell
git add src/telegram_downloader/notifications.py tests/test_notifications.py
git commit -m "feat: aggregate privacy-safe notifications"
```

## Task 6: Implement current-user autostart safely

**Files:**
- Create: `src/telegram_downloader/autostart.py`
- Create: `tests/test_autostart.py`

- [ ] **Step 1: Write failing fake-registry tests**

```python
def test_enabling_autostart_writes_fixed_background_command(tmp_path: Path) -> None:
    executable = tmp_path / "TelegramDownloader.exe"
    executable.write_bytes(b"exe")
    registry = FakeRegistry()
    service = CurrentUserAutostart(registry, executable, frozen=True)
    service.reconcile(True)
    assert registry.values[RUN_VALUE_NAME] == subprocess.list2cmdline(
        [str(executable.resolve()), "--background"]
    )


def test_disabling_autostart_removes_only_owned_value(tmp_path: Path) -> None:
    registry = FakeRegistry({RUN_VALUE_NAME: "old", "Unrelated": "keep"})
    CurrentUserAutostart(registry, tmp_path / "app.exe", frozen=True).reconcile(False)
    assert RUN_VALUE_NAME not in registry.values
    assert registry.values["Unrelated"] == "keep"


def test_source_mode_rejects_enabling_autostart(tmp_path: Path) -> None:
    with pytest.raises(AutostartUnavailableError):
        CurrentUserAutostart(FakeRegistry(), tmp_path / "python.exe", frozen=False).reconcile(True)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_autostart.py -q`

Expected: import failure because `autostart.py` does not exist.

- [ ] **Step 3: Implement an injectable registry adapter**

```python
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "TelegramDownloader"


class RegistryPort(Protocol):
    def set_value(self, key: str, name: str, value: str) -> None: ...
    def delete_value(self, key: str, name: str) -> None: ...
    def get_value(self, key: str, name: str) -> str | None: ...


class CurrentUserAutostart:
    def __init__(self, registry: RegistryPort, executable: Path, *, frozen: bool) -> None:
        self.registry = registry
        self.executable = executable.resolve()
        self.frozen = frozen

    def command(self) -> str:
        return subprocess.list2cmdline([str(self.executable), "--background"])

    def reconcile(self, enabled: bool) -> None:
        if enabled:
            if not self.frozen or not self.executable.is_file():
                raise AutostartUnavailableError("开机启动只支持正式打包程序")
            self.registry.set_value(RUN_KEY, RUN_VALUE_NAME, self.command())
        else:
            self.registry.delete_value(RUN_KEY, RUN_VALUE_NAME)
```

The production adapter uses `winreg.HKEY_CURRENT_USER`, catches missing values only, and wraps other `OSError` values in a fixed Chinese `AutostartError` without logging the executable path.

- [ ] **Step 4: Run tests, Ruff, and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_autostart.py -q`

Expected: all tests pass without touching the real registry.

```powershell
git add src/telegram_downloader/autostart.py tests/test_autostart.py
git commit -m "feat: manage opt-in Windows autostart"
```

## Task 7: Replace duplicate-instance message with local activation

**Files:**
- Create: `src/telegram_downloader/activation.py`
- Create: `tests/test_activation.py`
- Modify: `src/telegram_downloader/instance_guard.py`
- Modify: `tests/test_instance_guard.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write failing activation tests**

```python
def test_activation_server_accepts_only_fixed_command(qtbot) -> None:
    activated: list[bool] = []
    server = LocalActivationServer(unique_channel(), lambda: activated.append(True))
    assert server.start() is True
    assert request_activation(server.channel, command=b"activate", timeout_ms=500) is True
    qtbot.waitUntil(lambda: activated == [True])
    assert request_activation(server.channel, command=b"private-data", timeout_ms=500) is False


def test_duplicate_run_requests_activation_before_fallback(tmp_path, monkeypatch) -> None:
    guard = DuplicateGuard()
    monkeypatch.setattr(app, "request_activation", lambda *_args, **_kwargs: True)
    assert app.run(tmp_path, instance_guard=guard) == 2
    assert guard.fallback_notified is False
```

- [ ] **Step 2: Run activation and app tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_activation.py tests/test_instance_guard.py tests/test_app.py::test_duplicate_run_requests_activation_before_fallback -q`

Expected: failures because activation APIs do not exist.

- [ ] **Step 3: Implement a fixed-message local server/client**

```python
ACTIVATION_CHANNEL = "TelegramDownloader.Activation.v1"
ACTIVATE_COMMAND = b"activate"


class LocalActivationServer(QObject):
    def __init__(self, channel: str, activate: Callable[[], None]) -> None:
        super().__init__()
        self.channel = channel
        self.activate = activate
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._read_connection)

    def start(self) -> bool:
        QLocalServer.removeServer(self.channel)
        return self.server.listen(self.channel)

    def _read_connection(self) -> None:
        socket = self.server.nextPendingConnection()
        if socket is None or not socket.waitForReadyRead(500):
            return
        accepted = bytes(socket.readAll()) == ACTIVATE_COMMAND
        socket.write(b"ok" if accepted else b"rejected")
        socket.flush()
        if accepted:
            self.activate()
```

`request_activation()` connects with `QLocalSocket`, sends only `ACTIVATE_COMMAND`, waits for `b"ok"`, and returns `False` on timeout or socket error.

- [ ] **Step 4: Wire duplicate launch and preserve fallback**

```python
if not guard.acquire():
    activated = request_activation(ACTIVATION_CHANNEL, timeout_ms=1000)
    if not activated:
        guard.notify_already_running()
    _startup_close(startup_indicator)
    return 2
```

The primary process starts `LocalActivationServer` only after owning the mutex and closes it during true shutdown.

- [ ] **Step 5: Run tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_activation.py tests/test_instance_guard.py tests/test_app.py -q`

Expected: all tests pass.

```powershell
git add src/telegram_downloader/activation.py src/telegram_downloader/instance_guard.py tests/test_activation.py tests/test_instance_guard.py tests/test_app.py
git commit -m "feat: activate the existing app instance"
```

## Task 8: Implement tray lifecycle and explicit exit

**Files:**
- Create: `src/telegram_downloader/background.py`
- Create: `tests/test_background.py`
- Modify: `src/telegram_downloader/app.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write failing lifecycle tests with fake ports**

```python
def test_close_hides_without_shutting_down_when_tray_is_available() -> None:
    window, tray, exits = FakeWindow(), FakeTray(available=True), []
    runtime = BackgroundModeController(window, tray, exits.append)
    runtime.configure(close_to_tray=True, notifications_enabled=True)
    assert runtime.handle_window_close() is True
    assert window.hidden is True
    assert exits == []


def test_explicit_exit_is_idempotent_and_bypasses_tray() -> None:
    exits: list[str] = []
    runtime = BackgroundModeController(FakeWindow(), FakeTray(), lambda: exits.append("exit"))
    runtime.request_exit()
    runtime.request_exit()
    assert exits == ["exit"]


def test_no_tray_falls_back_to_true_exit() -> None:
    exits: list[str] = []
    runtime = BackgroundModeController(FakeWindow(), FakeTray(available=False), lambda: exits.append("exit"))
    assert runtime.handle_window_close() is False
    assert exits == ["exit"]


def test_first_close_hint_is_persisted_and_not_repeated() -> None:
    hints: list[bool] = []
    runtime = background_controller(
        tray_hint_shown=False,
        persist_tray_hint=lambda: hints.append(True),
    )
    runtime.handle_window_close()
    runtime.show_window()
    runtime.handle_window_close()
    assert hints == [True]


def test_notification_adapter_failure_does_not_escape() -> None:
    runtime = background_controller(tray=RaisingNotificationTray())
    runtime.show_notification(generic_payload())
    assert runtime.redacted_log == ["系统通知不可用"]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_background.py -q`

Expected: import failure because `background.py` does not exist.

- [ ] **Step 3: Implement a lifecycle controller independent of concrete menus**

```python
class BackgroundModeController:
    def __init__(
        self,
        window: WindowPort,
        tray: TrayPort,
        exit_app: Callable[[], None],
        *,
        tray_hint_shown: bool = False,
        persist_tray_hint: Callable[[], None] | None = None,
    ) -> None:
        self.window = window
        self.tray = tray
        self.exit_app = exit_app
        self.tray_hint_shown = tray_hint_shown
        self.persist_tray_hint = persist_tray_hint or (lambda: None)
        self.close_to_tray = True
        self.notifications_enabled = True
        self._exit_requested = False

    def handle_window_close(self) -> bool:
        if self.close_to_tray and self.tray.available:
            self.window.hide()
            if not self.tray_hint_shown:
                self.tray.show_close_hint()
                self.persist_tray_hint()
                self.tray_hint_shown = True
            return True
        self.request_exit()
        return False

    def show_window(self, route: NotificationRoute | None = None) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        if route is not None:
            self.window.show_route(route)

    def request_exit(self) -> None:
        if not self._exit_requested:
            self._exit_requested = True
            self.exit_app()
```

- [ ] **Step 4: Build the Qt tray adapter and refactor the close filter**

The adapter owns `QSystemTrayIcon`, `QMenu`, and fixed `QAction` instances. Connect tray double-click and “显示主窗口” to `show_window`; connect “彻底退出” to `request_exit`; expose callbacks for pause/resume, subscription wake, and opening downloads. `show_notification()` catches Qt/OS notification failures, records only the fixed text “系统通知不可用”, and never propagates into download, subscription, update, or authentication code. The one-time close hint persists `tray_hint_shown=True` through `SettingsStore` immediately after the first successful hide.

Replace `_install_graceful_shutdown()` close behavior with:

```python
if event.type() == QEvent.Type.Close and not shutdown.completed:
    event.ignore()
    background.handle_window_close()
    return True
```

Online update and Qt session shutdown call `background.request_exit()` rather than closing the window.

- [ ] **Step 5: Run lifecycle and app tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_background.py tests/test_app.py -q`

Expected: all tests pass; existing `_GracefulShutdown` ordering remains `actions → controller → quit`.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/background.py src/telegram_downloader/app.py tests/test_background.py tests/test_app.py
git commit -m "feat: keep the app running in the system tray"
```

## Task 9: Run schedule boundaries against the existing scheduler

**Files:**
- Modify: `src/telegram_downloader/download_schedule.py`
- Modify: `tests/test_download_schedule.py`
- Create: `tests/test_background_runtime_e2e.py`
- Modify: `src/telegram_downloader/app.py`

- [ ] **Step 1: Write failing controller tests with injected clock/sleep**

```python
@pytest.mark.asyncio
async def test_schedule_controller_applies_initial_gate_before_queue_restore() -> None:
    scheduler = FakeScheduler()
    events: list[ApplicationEvent] = []
    controller = DownloadScheduleController(
        scheduler,
        DownloadScheduleSettings(True, (0,), 9 * 60, 17 * 60),
        now=lambda: aware_monday(hour=8),
        sleep=cancelled_sleep,
        publish=events.append,
    )
    await controller.start()
    assert scheduler.schedule_states == [False]
    assert events[-1].kind is EventKind.SCHEDULE_CLOSED


@pytest.mark.asyncio
async def test_reconfigure_recalculates_immediately() -> None:
    scheduler = FakeScheduler()
    controller = schedule_controller(scheduler, hour=8)
    await controller.reconfigure(DownloadScheduleSettings())
    assert scheduler.schedule_states[-1] is True
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_download_schedule.py -q`

Expected: failure because `DownloadScheduleController` does not exist.

- [ ] **Step 3: Implement a recomputing async controller**

```python
class DownloadScheduleController:
    def __init__(self, scheduler, settings, *, now, sleep, publish) -> None:
        self.scheduler = scheduler
        self.settings = settings
        self.now = now
        self.sleep = sleep
        self.publish = publish
        self._task: asyncio.Task[None] | None = None
        self._last_allowed: bool | None = None

    async def start(self) -> None:
        await self.refresh()
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def refresh(self) -> None:
        state = evaluate_download_schedule(self.settings, self.now())
        if state.allowed != self._last_allowed:
            await self.scheduler.set_schedule_open(state.allowed)
            self.publish(schedule_event(state.allowed))
            self._last_allowed = state.allowed

    async def reconfigure(self, settings: DownloadScheduleSettings) -> None:
        self.settings = settings
        await self.refresh()

    async def _run(self) -> None:
        while True:
            await self.sleep(60)
            await self.refresh()

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
```

- [ ] **Step 4: Wire initial ordering in `app.py` and add E2E evidence**

Construct the schedule controller with `datetime.now().astimezone()`. In application startup call `await download_schedule.start()` before `await controller.start()`, because controller session restoration dispatches queued tasks. In true shutdown stop the schedule controller before `DownloadScheduler.shutdown()`.

The E2E test creates a real `TaskRepository`, one queued task, a closed schedule, and an injected downloader. It proves zero downloader calls outside the window, one call after opening, and persisted `PauseReason.SCHEDULE` across a repository restart.

- [ ] **Step 5: Run schedule, scheduler, and E2E tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_download_schedule.py tests/test_scheduler.py tests/test_background_runtime_e2e.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/download_schedule.py src/telegram_downloader/app.py tests/test_download_schedule.py tests/test_background_runtime_e2e.py
git commit -m "feat: enforce download time windows"
```

## Task 10: Publish real download, subscription, auth, and disk events

**Files:**
- Modify: `src/telegram_downloader/scheduler.py`
- Modify: `src/telegram_downloader/subscription_scheduler.py`
- Modify: `src/telegram_downloader/controller.py`
- Modify: `src/telegram_downloader/app.py`
- Modify: `tests/test_scheduler.py`
- Modify: `tests/test_subscription_scheduler.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write failing event-source tests**

```python
@pytest.mark.asyncio
async def test_scheduler_emits_one_terminal_event_per_task() -> None:
    events: list[ApplicationEvent] = []
    scheduler = DownloadScheduler(Repo(), SuccessfulDownloader(), publish=events.append)
    await scheduler.run_task("t")
    assert [(event.kind, event.identity) for event in events] == [
        (EventKind.DOWNLOAD_COMPLETED, "t")
    ]


@pytest.mark.asyncio
async def test_subscription_event_reports_count_without_private_context() -> None:
    events: list[ApplicationEvent] = []
    scheduler = subscription_scheduler_fixture(publish=events.append, queued=3)
    await scheduler.run_due()
    event = events[-1]
    assert event.kind is EventKind.SUBSCRIPTION_MATCH
    assert event.count == 3
    assert event.private_context == ""


@pytest.mark.asyncio
async def test_session_expiry_emits_auth_event_once() -> None:
    controller, events = controller_with_event_sink()
    await controller._handle_session_expired(expired_error())
    await controller._handle_session_expired(expired_error())
    assert [event.kind for event in events] == [EventKind.AUTH_REQUIRED]
```

- [ ] **Step 2: Run source-specific tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_scheduler.py tests/test_subscription_scheduler.py tests/test_controller.py -q`

Expected: constructor/callback failures because event publication is not wired.

- [ ] **Step 3: Add narrow publication callbacks**

Add `publish: Callable[[ApplicationEvent], None] = lambda _event: None` to scheduler and controller construction. Publish only after the terminal task status transaction succeeds:

```python
if terminal is TaskStatus.COMPLETED:
    self.publish(task_event(EventKind.DOWNLOAD_COMPLETED, task_id))
elif terminal is TaskStatus.PARTIAL_FAILURE:
    self.publish(task_event(EventKind.DOWNLOAD_FAILED, task_id))
```

In the `InsufficientSpaceError` branch publish `DISK_FULL` once per task before returning paused. Extend subscription completion output to provide its queued count and publish one `SUBSCRIPTION_MATCH` event only when `queued > 0`. Publish `AUTH_REQUIRED` inside the existing `_session_expiry_handled` lock after the guard flips to true.

- [ ] **Step 4: Connect batching to the tray**

Create one `NotificationBatcher` in `app.py`. After recording an event, arm a single-shot `QTimer` only when no flush timer is active, using the earliest batch deadline; later events must not postpone that deadline. On timeout, call `flush_due(monotonic())`, pass each payload to the tray adapter, then re-arm for the next remaining deadline if any. Notification clicks route through `BackgroundModeController.show_window(payload.route)`.

- [ ] **Step 5: Run event, controller, subscription E2E, and privacy tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_notifications.py tests/test_scheduler.py tests/test_subscription_scheduler.py tests/test_subscription_e2e.py tests/test_controller.py tests/test_app.py -q`

Expected: all tests pass and private-context assertions remain zero.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/scheduler.py src/telegram_downloader/subscription_scheduler.py src/telegram_downloader/controller.py src/telegram_downloader/app.py tests/test_scheduler.py tests/test_subscription_scheduler.py tests/test_controller.py tests/test_app.py
git commit -m "feat: notify terminal background events"
```

## Task 11: Add background, notification, autostart, and schedule settings UI

**Files:**
- Modify: `src/telegram_downloader/ui/settings.py`
- Modify: `tests/ui/test_settings_dialog.py`
- Modify: `src/telegram_downloader/controller.py`
- Modify: `tests/test_controller.py`
- Modify: `src/telegram_downloader/app.py`

- [ ] **Step 1: Write failing settings-page tests**

```python
def test_background_tab_round_trips_all_values(qtbot) -> None:
    settings = AppSettings(
        close_to_tray=False,
        notifications_enabled=False,
        autostart_enabled=True,
        download_schedule=DownloadScheduleSettings(True, (0, 2, 4), 22 * 60, 2 * 60),
    )
    dialog = SettingsDialog(settings, autostart_available=True)
    qtbot.addWidget(dialog)
    assert dialog.values() == settings


def test_schedule_controls_disable_without_changing_values(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(), autostart_available=False)
    qtbot.addWidget(dialog)
    assert dialog.autostart.isEnabled() is False
    assert all(not widget.isEnabled() for widget in dialog.schedule_detail_widgets)
```

- [ ] **Step 2: Run UI tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ui/test_settings_dialog.py -q`

Expected: constructor/attribute failures for the new controls.

- [ ] **Step 3: Split settings into two tabs and serialize exact values**

Use `QTabWidget` with “常规” for existing controls and “后台与通知” for:

```python
self.close_to_tray = QCheckBox("关闭主窗口后继续在托盘运行")
self.notifications = QCheckBox("显示下载、订阅和登录状态通知")
self.autostart = QCheckBox("登录 Windows 后后台启动")
self.schedule_enabled = QCheckBox("限制下载时段")
self.weekdays = tuple(QCheckBox(label) for label in "一二三四五六日")
self.schedule_start = QTimeEdit()
self.schedule_end = QTimeEdit()
```

`values()` constructs `DownloadScheduleSettings` from checked weekday indexes and `hour * 60 + minute`. Empty weekdays is rejected in `_save()` with the model's fixed Chinese error.

- [ ] **Step 4: Apply external effects transactionally**

Inject a runtime effects port into `AppController`:

```python
class RuntimeSettingsEffects(Protocol):
    async def apply(self, previous: AppSettings, current: AppSettings) -> None: ...
```

In `apply_settings`, call the effect before assigning `self.settings`; the effect reconciles autostart, saves settings, configures background mode, and reconfigures the schedule. If settings persistence fails after changing the startup value, reconcile the previous autostart value before re-raising. Existing API/proxy reconnect behavior executes only after persistence succeeds.

- [ ] **Step 5: Run UI/controller/settings tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_settings.py tests/ui/test_settings_dialog.py tests/test_controller.py tests/test_app.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/ui/settings.py src/telegram_downloader/controller.py src/telegram_downloader/app.py tests/ui/test_settings_dialog.py tests/test_controller.py tests/test_app.py
git commit -m "feat: configure background operation in settings"
```

## Task 12: Support hidden startup, update routing, and session shutdown

**Files:**
- Modify: `src/telegram_downloader/__main__.py`
- Modify: `src/telegram_downloader/app.py`
- Modify: `src/telegram_downloader/controller.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_bootstrap.py`
- Modify: `tests/update/test_update_coordinator.py`

- [ ] **Step 1: Write failing CLI and hidden-update tests**

```python
def test_background_argument_skips_startup_indicator(monkeypatch, tmp_path) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(main_module, "_run_gui", lambda root, **kw: calls.append(kw["background"]) or 0)
    monkeypatch.setattr(sys, "argv", ["TelegramDownloader", "--background"])
    assert main_module.main() == 0
    assert calls == [True]


@pytest.mark.asyncio
async def test_hidden_update_check_notifies_then_defers_dialog() -> None:
    prompt = BackgroundUpdatePrompt(hidden=True)
    accepted = await prompt(manifest_fixture())
    assert accepted is False
    assert prompt.events[-1].kind is EventKind.UPDATE_AVAILABLE
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_bootstrap.py tests/update/test_update_coordinator.py -q`

Expected: parser/signature failures for background launch and prompt routing.

- [ ] **Step 3: Parse and propagate hidden launch mode**

```python
parser.add_argument("--background", action="store_true")
# self-test handling remains first
return _run_gui(root, background=arguments.background)
```

`_run_gui()` creates no startup indicator in background mode. `run(..., background=False)` starts the tray before services; `start_application()` calls `controller.window.show()` only when `background` is false. If the tray adapter reports unavailable, show the window and a fixed status message.

- [ ] **Step 4: Route updates and system session termination**

Make the update prompt awaitable. When the window is hidden it publishes `UPDATE_AVAILABLE` and returns `False`; clicking that route shows the window and starts a new update check, which displays the existing confirmation dialog.

Connect Qt session termination to true exit:

```python
application.commitDataRequest.connect(lambda _manager: background.request_exit())
controller.update_shutdown = background.request_exit
```

The close event remains hide-only; explicit exit, update, fatal startup failure, and session termination remain true shutdown.

- [ ] **Step 5: Run app/update/bootstrap tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_bootstrap.py tests/update/test_update_coordinator.py -q`

Expected: all tests pass.

```powershell
git add src/telegram_downloader/__main__.py src/telegram_downloader/app.py src/telegram_downloader/controller.py tests/test_app.py tests/test_bootstrap.py tests/update/test_update_coordinator.py
git commit -m "feat: launch safely in background mode"
```

## Task 13: Complete integration, documentation, versioning, and release gates

**Files:**
- Modify: `tests/test_background_runtime_e2e.py`
- Modify: `tests/test_packaging_contract.py`
- Modify: `README.md`
- Modify: `pyproject.toml`
- Modify: `src/telegram_downloader/__init__.py`
- Modify: `installer/TelegramDownloader.iss`
- Create: `docs/releases/v0.13.0.md`
- Create: `docs/verification/v0.13.0-tray-background-notifications-scheduling.md`

- [ ] **Step 1: Add the end-to-end acceptance test before version changes**

The E2E test must assert the complete lifecycle in one isolated application root:

```python
@pytest.mark.asyncio
async def test_background_schedule_notification_restart_contract(tmp_path: Path) -> None:
    runtime = await background_runtime_fixture(tmp_path, schedule_open=False)
    runtime.close_main_window()
    assert runtime.window.visible is False
    assert runtime.controller.shutdown_calls == 0
    await runtime.queue_task("task-1")
    assert runtime.downloader.calls == []
    await runtime.open_schedule()
    await runtime.wait_completed("task-1")
    assert runtime.notifications.payloads[-1].body == "1 个下载任务已完成"
    await runtime.explicit_exit()
    assert runtime.shutdown_order == ["actions", "schedule", "subscriptions", "downloads", "gateway", "quit"]
```

- [ ] **Step 2: Run the E2E test and fix only integration contract defects**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_background_runtime_e2e.py -q`

Expected: PASS with no real Telegram credentials, registry writes, or system notifications.

- [ ] **Step 3: Update version and public documentation**

Set version `0.13.0` in all three authoritative metadata files. Document:

- close-to-tray and explicit exit;
- second-launch activation;
- privacy-safe notification classes;
- opt-in current-user startup and its single OS integration value;
- weekday, same-day, all-day, and cross-midnight download schedules;
- schedule pause versus manual pause;
- subscriptions continuing to queue outside the download window;
- fallback when Windows tray/notifications are unavailable;
- no Windows service and no execution while the user is logged out.

Add packaging assertions:

```python
assert project_version() == "0.13.0"
assert "关闭到托盘" in README
assert "pause_reason" in repository_source
assert "--background" in main_source
```

- [ ] **Step 4: Run the complete source verification**

Run: `.\scripts\test.ps1`

Expected: all pytest tests pass and Ruff prints `All checks passed!`.

- [ ] **Step 5: Build and smoke-test the frozen application and installer**

Run: `.\scripts\build-installer.ps1`

Expected: `PACKAGED_SMOKE_OK`, `INSTALLER_SMOKE_OK`, and a generated `TelegramDownloader-0.13.0-win-x64-setup.exe`.

- [ ] **Step 6: Perform Windows interactive acceptance**

Use the packaged runtime and record exact evidence in `docs/verification/v0.13.0-tray-background-notifications-scheduling.md`:

1. close hides to a visible tray icon and does not stop an active download;
2. tray double-click restores the same process and task state;
3. a second executable launch activates that hidden window;
4. explicit tray exit safely settles an active `.part` file;
5. autostart enabled then disabled changes only the owned HKCU value;
6. a short same-day window pauses and resumes only schedule-paused work;
7. a cross-midnight window evaluates correctly;
8. Windows notifications enabled and disabled do not alter business outcomes;
9. notification screenshots contain no account, source, message, search term, filename, or path;
10. online update from hidden mode displays only after the notification is clicked.

- [ ] **Step 7: Commit the candidate**

```powershell
git add README.md pyproject.toml src/telegram_downloader/__init__.py installer/TelegramDownloader.iss tests/test_packaging_contract.py tests/test_background_runtime_e2e.py docs/releases/v0.13.0.md docs/verification/v0.13.0-tray-background-notifications-scheduling.md
git commit -m "release: prepare TelegramDownloader 0.13.0"
```

- [ ] **Step 8: Run final clean-tree verification**

Run: `git status --short`

Expected: no output.

Run: `.\scripts\test.ps1`

Expected: all tests pass and Ruff prints `All checks passed!` on the committed candidate.

## Plan self-review checklist

- Every requirement in the approved design maps to Tasks 1–13.
- No Task 5 advanced subscription behavior, Task 6 batch links, Task 7 global media scheduling, or Task 8 naming template work is included.
- `DownloadScheduleSettings`, `PauseReason`, `ApplicationEvent`, `NotificationRoute`, `BackgroundModeController`, and `DownloadScheduleController` names are consistent throughout.
- Schedule gating is installed before queued-task restoration.
- Manual pause and schedule pause remain distinguishable across restart.
- Update and session shutdown paths bypass close-to-tray.
- The first close-to-tray hint persists once, and notification failures never reach business producers.
- Continuous event arrival cannot postpone the oldest notification batch past its five-second deadline.
- All registry, tray, notification, clock, and activation tests use injected/fake adapters; only explicit Windows acceptance touches real integration state.
