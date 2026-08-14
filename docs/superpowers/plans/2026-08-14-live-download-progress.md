# Live Download Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make active Telegram downloads visibly transition from queued to downloading and update progress, speed, remaining time, and the statistics rail every 0.5 seconds.

**Architecture:** Keep SQLite as the progress source of truth. While a scheduler task is active, `AppController` periodically rebuilds `TaskSummary` snapshots, calculates speed from adjacent byte samples, and throttles concurrent refresh loops to one UI refresh per interval. `MainWindow` renders numeric summary fields without owning download state.

**Tech Stack:** Python 3.12, asyncio/qasync, PySide6, SQLite, pytest/pytest-asyncio/pytest-qt, Ruff, PyInstaller, Inno Setup

---

## File structure

- Modify `src/telegram_downloader/controller.py`: active-task refresh loop, refresh throttling, speed/ETA sampling, enriched summaries.
- Modify `src/telegram_downloader/ui/models.py`: numeric progress fields carried with each `TaskSummary`.
- Modify `src/telegram_downloader/ui/main.py`: statistics rail and current-task rendering.
- Modify `tests/test_controller.py`: controller refresh, speed/ETA, final refresh, and throttle regression tests.
- Modify `tests/ui/test_main_window.py`: statistics rail rendering regression test and version assertion.
- Modify `tests/test_packaging_contract.py`: expected 0.2.3 package metadata.
- Modify `pyproject.toml`, `src/telegram_downloader/__init__.py`, and `installer/TelegramDownloader.iss`: synchronized 0.2.3 version.
- Create `docs/releases/v0.2.3.md`: local candidate release notes.

### Task 1: Controller live snapshots and refresh lifecycle

**Files:**
- Modify: `tests/test_controller.py`
- Modify: `src/telegram_downloader/controller.py`
- Modify: `src/telegram_downloader/ui/models.py`

- [ ] **Step 1: Write the failing active-download refresh test**

Append a pytest-asyncio test that uses a real controller refresh path and mutable repository state:

```python
@pytest.mark.asyncio
async def test_running_task_refreshes_window_before_download_finishes() -> None:
    release = asyncio.Event()
    started = asyncio.Event()
    task = SimpleNamespace(
        id="task-1",
        source_title="示例频道",
        status=TaskStatus.QUEUED,
        last_error=None,
    )
    item = SimpleNamespace(
        status=ItemStatus.QUEUED,
        expected_size=100,
        downloaded_bytes=0,
        last_error=None,
    )

    class Repository:
        def list_tasks(self):
            return [task]

        def list_items(self, task_id):
            assert task_id == "task-1"
            return [item]

    class Scheduler:
        async def run_task(self, task_id):
            assert task_id == "task-1"
            task.status = TaskStatus.DOWNLOADING
            item.status = ItemStatus.DOWNLOADING
            item.downloaded_bytes = 25
            started.set()
            await release.wait()
            item.downloaded_bytes = 100
            item.status = ItemStatus.COMPLETED
            task.status = TaskStatus.COMPLETED

    class Window:
        def __init__(self):
            self.snapshots = []
            self.downloading = asyncio.Event()

        def set_task_summaries(self, summaries):
            self.snapshots.append(summaries)
            if summaries and summaries[0].status is TaskStatus.DOWNLOADING:
                self.downloading.set()

    window = Window()
    controller = AppController.for_test(
        repository=Repository(),
        scheduler=Scheduler(),
        window=window,
        progress_refresh_interval=0.01,
    )

    running = asyncio.create_task(controller._run_and_refresh("task-1"))
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(window.downloading.wait(), timeout=1)

    assert running.done() is False
    assert window.snapshots[-1][0].downloaded_bytes == 25

    release.set()
    await running

    assert window.snapshots[-1][0].status is TaskStatus.COMPLETED
    assert window.snapshots[-1][0].completed_items == 1
```

Add these imports beside the existing controller test imports:

```python
from types import SimpleNamespace

from telegram_downloader.domain import ItemStatus, MediaKind, TaskStatus
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_controller.py::test_running_task_refreshes_window_before_download_finishes -q
```

Expected: FAIL because `AppController.for_test()` does not accept `progress_refresh_interval`, and `TaskSummary` does not expose numeric progress fields.

- [ ] **Step 3: Write failing deterministic speed and throttle tests**

Add a synchronous speed test that calls `refresh_tasks(now=...)` twice and asserts a 512 B/s delta plus a one-second ETA:

```python
def test_refresh_tasks_calculates_speed_and_remaining_time() -> None:
    task = SimpleNamespace(
        id="task-1",
        source_title="频道",
        status=TaskStatus.DOWNLOADING,
        last_error=None,
    )
    item = SimpleNamespace(
        status=ItemStatus.DOWNLOADING,
        expected_size=1024,
        downloaded_bytes=0,
        last_error=None,
    )

    class Repository:
        def list_tasks(self):
            return [task]

        def list_items(self, _task_id):
            return [item]

    class Window:
        def __init__(self):
            self.tasks = []

        def set_task_summaries(self, summaries):
            self.tasks = summaries

    window = Window()
    controller = AppController.for_test(repository=Repository(), window=window)
    controller.refresh_tasks(now=10.0)
    item.downloaded_bytes = 512
    controller.refresh_tasks(now=11.0)

    summary = window.tasks[0]
    assert summary.speed_bps == 512
    assert summary.speed_text == "512 B/s"
    assert summary.remaining_seconds == 1
    assert summary.remaining_text == "1 秒"
```

Add a throttle test:

```python
def test_progress_refresh_is_throttled_across_concurrent_callers() -> None:
    class Window:
        def __init__(self):
            self.refreshes = 0

        def set_task_summaries(self, _summaries):
            self.refreshes += 1

    window = Window()
    controller = AppController.for_test(window=window, progress_refresh_interval=0.5)

    controller._refresh_tasks_if_due(20.0)
    controller._refresh_tasks_if_due(20.1)
    controller._refresh_tasks_if_due(20.5)

    assert window.refreshes == 2
```

- [ ] **Step 4: Run the new tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_controller.py -k "running_task_refreshes or calculates_speed or throttled" -q
```

Expected: three failures for the missing interval dependency, numeric fields, `now` argument, or throttle method.

- [ ] **Step 5: Enrich `TaskSummary` with numeric fields**

Add defaults after the existing text fields in `src/telegram_downloader/ui/models.py` so existing call sites remain valid:

```python
    completed_items: int = 0
    total_items: int = 0
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed_bps: float = 0.0
    remaining_seconds: int | None = None
```

- [ ] **Step 6: Implement controller sampling, throttling, and periodic refresh**

In `src/telegram_downloader/controller.py`, import `Awaitable` and a monotonic clock:

```python
from collections.abc import Awaitable, Callable
from time import monotonic as monotonic_clock
```

Extend `AppController.__init__` with injectable timing dependencies:

```python
        progress_refresh_interval: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
```

Validate and initialize state:

```python
        if progress_refresh_interval <= 0:
            raise ValueError("进度刷新间隔必须大于零")
        self._progress_refresh_interval = progress_refresh_interval
        self._sleep = sleep
        self._next_progress_refresh = 0.0
        self._progress_samples: dict[str, tuple[float, int]] = {}
```

Pass both optional values through `AppController.for_test()`.

Replace `refresh_tasks()` with a keyword-only deterministic time argument and numeric summary construction:

```python
    def refresh_tasks(self, *, now: float | None = None) -> None:
        sampled_at = monotonic_clock() if now is None else now
        summaries: list[TaskSummary] = []
        active_ids: set[str] = set()
        for task in self.repository.list_tasks():
            items = self.repository.list_items(task.id)
            completed = sum(item.status is ItemStatus.COMPLETED for item in items)
            downloaded = sum(item.downloaded_bytes for item in items)
            known_size = sum(item.expected_size or 0 for item in items)
            unknown = any(item.expected_size is None for item in items)
            total_bytes = None if unknown else known_size
            speed = self._sample_speed(task.id, task.status, downloaded, sampled_at)
            remaining_seconds = None
            if total_bytes is not None and speed > 0:
                remaining_seconds = max(0, round((total_bytes - downloaded) / speed))
            error_text = task.last_error or next(
                (item.last_error for item in items if item.last_error),
                "—",
            )
            summaries.append(
                TaskSummary(
                    task.id,
                    task.source_title,
                    task.status,
                    f"{completed} / {len(items)}",
                    self._format_bytes(known_size) + (" + 未知" if unknown else ""),
                    self._format_rate(speed),
                    self._format_duration(remaining_seconds),
                    error_text,
                    completed,
                    len(items),
                    downloaded,
                    total_bytes,
                    speed,
                    remaining_seconds,
                )
            )
            if task.status is TaskStatus.DOWNLOADING:
                active_ids.add(task.id)
        for task_id in set(self._progress_samples) - active_ids:
            self._progress_samples.pop(task_id, None)
        self.window.set_task_summaries(summaries)
```

Add helpers:

```python
    def _sample_speed(
        self,
        task_id: str,
        status: TaskStatus,
        downloaded: int,
        now: float,
    ) -> float:
        if status is not TaskStatus.DOWNLOADING:
            self._progress_samples.pop(task_id, None)
            return 0.0
        previous = self._progress_samples.get(task_id)
        self._progress_samples[task_id] = (now, downloaded)
        if previous is None:
            return 0.0
        elapsed = now - previous[0]
        delta = downloaded - previous[1]
        return delta / elapsed if elapsed > 0 and delta > 0 else 0.0

    def _refresh_tasks_if_due(self, now: float | None = None) -> None:
        sampled_at = monotonic_clock() if now is None else now
        if sampled_at < self._next_progress_refresh:
            return
        self._next_progress_refresh = sampled_at + self._progress_refresh_interval
        self.refresh_tasks(now=sampled_at)

    @classmethod
    def _format_rate(cls, value: float) -> str:
        return "—" if value <= 0 else f"{cls._format_bytes(round(value))}/s"

    @staticmethod
    def _format_duration(value: int | None) -> str:
        if value is None:
            return "—"
        if value < 60:
            return f"{value} 秒"
        minutes, seconds = divmod(value, 60)
        return f"{minutes} 分 {seconds} 秒"
```

Replace `_run_and_refresh()` with an independently awaited scheduler task and timeout-based refresh loop:

```python
    async def _run_and_refresh(self, task_id: str) -> None:
        operation = asyncio.create_task(self.scheduler.run_task(task_id))
        try:
            while not operation.done():
                self._refresh_tasks_if_due()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(operation),
                        timeout=self._progress_refresh_interval,
                    )
                except TimeoutError:
                    continue
            await operation
        finally:
            self.refresh_tasks()
```

- [ ] **Step 7: Run Task 1 tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_controller.py tests\ui\test_main_window.py -q
```

Expected: all selected tests pass with no unhandled asyncio task warnings.

- [ ] **Step 8: Commit Task 1**

```powershell
git add src/telegram_downloader/controller.py src/telegram_downloader/ui/models.py tests/test_controller.py
git commit -m "fix: refresh active download progress"
```

### Task 2: Render live statistics and current task

**Files:**
- Modify: `tests/ui/test_main_window.py`
- Modify: `src/telegram_downloader/ui/main.py`

- [ ] **Step 1: Write the failing statistics-rail test**

Add:

```python
def test_live_summary_updates_statistics_and_current_task(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_task_summaries(
        [
            TaskSummary(
                "task-1",
                "示例频道",
                TaskStatus.DOWNLOADING,
                "1 / 2",
                "1.0 KB",
                "512 B/s",
                "1 秒",
                "—",
                completed_items=1,
                total_items=2,
                downloaded_bytes=512,
                total_bytes=1024,
                speed_bps=512,
                remaining_seconds=1,
            )
        ]
    )

    assert window.speed_value.text() == "512 B/s"
    assert window.completed_value.text() == "1"
    assert window.remaining_value.text() == "1"
    assert window.current_task_label.text() == "示例频道"
    assert window.current_progress.value() == 50
    assert "1 / 2" in window.current_detail.text()
    assert "剩余 1 秒" in window.current_detail.text()
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'; .\.venv\Scripts\python.exe -m pytest tests\ui\test_main_window.py::test_live_summary_updates_statistics_and_current_task -q
```

Expected: FAIL because `set_task_summaries()` currently updates only the table model.

- [ ] **Step 3: Implement statistics rendering**

First change the domain import to:

```python
from telegram_downloader.domain import MediaKind, TaskStatus
```

Then extend `MainWindow.set_task_summaries()`:

```python
    def set_task_summaries(self, tasks: list[TaskSummary]) -> None:
        self.task_model.set_tasks(tasks)
        self._update_action_state()

        total_speed = sum(
            task.speed_bps for task in tasks if task.status is TaskStatus.DOWNLOADING
        )
        completed = sum(task.completed_items for task in tasks)
        remaining = sum(max(0, task.total_items - task.completed_items) for task in tasks)
        self.speed_value.setText(self._format_rate(total_speed))
        self.completed_value.setText(str(completed))
        self.remaining_value.setText(str(remaining))

        active = next(
            (
                task
                for task in tasks
                if task.status in {TaskStatus.DOWNLOADING, TaskStatus.WAITING_RETRY}
            ),
            None,
        )
        if active is None:
            self.current_task_label.setText("暂无活动任务")
            self.current_progress.setValue(0)
            self.current_detail.setText("等待任务进入队列")
            return

        if active.total_bytes is not None and active.total_bytes > 0:
            progress = round(active.downloaded_bytes * 100 / active.total_bytes)
        elif active.total_items > 0:
            progress = round(active.completed_items * 100 / active.total_items)
        else:
            progress = 0
        self.current_task_label.setText(active.title)
        self.current_progress.setValue(max(0, min(100, progress)))
        detail = f"{active.progress_text} · {active.speed_text}"
        if active.remaining_text != "—":
            detail += f" · 剩余 {active.remaining_text}"
        self.current_detail.setText(detail)
```

Add a formatter near the other static UI helpers:

```python
    @staticmethod
    def _format_rate(value: float) -> str:
        if value <= 0:
            return "0 B/s"
        amount = value
        for unit in ("B/s", "KB/s", "MB/s", "GB/s", "TB/s"):
            if amount < 1024 or unit == "TB/s":
                return f"{amount:.0f} {unit}" if unit == "B/s" else f"{amount:.1f} {unit}"
            amount /= 1024
        return "0 B/s"
```

- [ ] **Step 4: Run UI tests and verify GREEN**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'; .\.venv\Scripts\python.exe -m pytest tests\ui\test_main_window.py -q
```

Expected: all main-window tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/telegram_downloader/ui/main.py tests/ui/test_main_window.py
git commit -m "fix: show active task statistics"
```

### Task 3: Prepare the 0.2.3 local candidate

**Files:**
- Modify: `tests/test_packaging_contract.py`
- Modify: `tests/ui/test_main_window.py`
- Modify: `pyproject.toml`
- Modify: `src/telegram_downloader/__init__.py`
- Modify: `installer/TelegramDownloader.iss`
- Create: `docs/releases/v0.2.3.md`

- [ ] **Step 1: Change version assertions first and verify RED**

Rename `test_v022_version_and_qr_runtime_contract_are_consistent` to `test_v023_version_and_qr_runtime_contract_are_consistent`, change its three 0.2.2 assertions, and change the main-window version label assertion to 0.2.3, then run:

```python
def test_v023_version_and_qr_runtime_contract_are_consistent() -> None:
    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    package_init = (root / "src/telegram_downloader/__init__.py").read_text(
        encoding="utf-8"
    )
    gateway = (root / "src/telegram_downloader/gateway.py").read_text(
        encoding="utf-8"
    )
    main = (root / "src/telegram_downloader/ui/main.py").read_text(
        encoding="utf-8"
    )
    installer = (root / "installer/TelegramDownloader.iss").read_text(
        encoding="utf-8"
    )
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    spec = (root / "TelegramDownloader.spec").read_text(encoding="utf-8")

    assert project["project"]["version"] == "0.2.3"
    assert '__version__ = "0.2.3"' in package_init
    assert '#define AppVersion "0.2.3"' in installer
    assert "qrcode==8.2" in requirements
    assert '"qrcode"' in spec
    assert "app_version=__version__" in gateway
    assert 'f"v{__version__} · stable"' in main
    assert "v0.1.0 · stable" not in main
```

In `test_workbench_contains_required_controls`, replace only the version assertion:

```diff
-    assert window.version_label.text() == "v0.2.2 · stable"
+    assert window.version_label.text() == "v0.2.3 · stable"
```

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_packaging_contract.py::test_v023_version_and_qr_runtime_contract_are_consistent tests\ui\test_main_window.py::test_workbench_contains_required_controls -q
```

Expected: FAIL because production metadata remains 0.2.2.

- [ ] **Step 2: Synchronize production metadata**

Apply these exact values:

```toml
# pyproject.toml
version = "0.2.3"
```

```python
# src/telegram_downloader/__init__.py
__version__ = "0.2.3"
```

```iss
; installer/TelegramDownloader.iss
#define AppVersion "0.2.3"
```

Create `docs/releases/v0.2.3.md`:

```markdown
# TelegramDownloader 0.2.3

本版本修复下载实际运行时界面仍停留在“等待中”的问题。

- 下载期间每 0.5 秒刷新任务状态和媒体进度。
- 实时显示下载速度、预计剩余时间和右侧当前任务。
- 多任务刷新共享节流，避免重复高频读取项目内数据库。
- Telegram 会话、断点续传、在线更新和项目内数据目录保持不变。
```

- [ ] **Step 3: Run version tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_packaging_contract.py::test_v023_version_and_qr_runtime_contract_are_consistent tests\ui\test_main_window.py::test_workbench_contains_required_controls -q
```

Expected: 2 passed.

- [ ] **Step 4: Commit Task 3**

```powershell
git add pyproject.toml src/telegram_downloader/__init__.py installer/TelegramDownloader.iss tests/test_packaging_contract.py tests/ui/test_main_window.py docs/releases/v0.2.3.md
git commit -m "release: prepare TelegramDownloader 0.2.3"
```

### Task 4: Full verification, data preservation, and Windows artifacts

**Files:**
- Verify: all tracked source and tests
- Build: `dist/TelegramDownloader/TelegramDownloader.exe`
- Build: `dist/TelegramDownloader-0.2.3-win-x64-portable.zip`
- Build: `dist/release/TelegramDownloader-0.2.3-win-x64-setup.exe`

- [ ] **Step 1: Run the complete source verification**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test.ps1
```

Expected: all tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 2: Gracefully close the running packaged app**

Resolve only processes whose executable path equals the project-local packaged EXE, call `CloseMainWindow()`, and verify they exit. Do not force-stop an unrelated process.

```powershell
$expected = (Resolve-Path 'dist\TelegramDownloader\TelegramDownloader.exe').Path
$targets = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'TelegramDownloader.exe' -and $_.ExecutablePath -eq $expected
})
foreach ($target in $targets) {
    $process = Get-Process -Id $target.ProcessId -ErrorAction Stop
    [void]$process.CloseMainWindow()
    [void]$process.WaitForExit(10000)
}
$remaining = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'TelegramDownloader.exe' -and $_.ExecutablePath -eq $expected
})
if ($remaining.Count -ne 0) { throw 'Project-local app did not close gracefully' }
```

- [ ] **Step 3: Back up current project-local user data**

Create a timestamped directory under `.local/backups/pre-v0.2.3-*` and copy only `dist/TelegramDownloader/data` and `dist/TelegramDownloader/downloads`. Record SHA-256 hashes for settings, DPAPI secrets, SQLite database, logs, update state, and downloaded files before building.

```powershell
$project = (Resolve-Path '.').Path
$runtime = (Resolve-Path 'dist\TelegramDownloader').Path
$backup = Join-Path $project ('.local\backups\pre-v0.2.3-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force -Path $backup | Out-Null
foreach ($name in @('data', 'downloads')) {
    $source = Join-Path $runtime $name
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination $backup -Recurse -Force
    }
}
Get-ChildItem -LiteralPath $backup -Recurse -File |
    Get-FileHash -Algorithm SHA256 |
    Export-Clixml -LiteralPath (Join-Path $backup 'hashes.clixml')
Write-Output $backup
```

- [ ] **Step 4: Build portable and installer artifacts**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-installer.ps1
```

Expected: exit code 0, `PACKAGED_SMOKE_OK`, `INSTALLER_SMOKE_OK`, and three versioned 0.2.3 outputs.

- [ ] **Step 5: Verify packaged behavior and data integrity**

Run the packaged EXE with `--self-test`; assert version 0.2.3 and every writable path starts with `D:\Codex Project\Telegram下载器\dist\TelegramDownloader\`. Restore only the self-test report generated by verification, then compare all backed-up user-data hashes byte-for-byte. Confirm no `TelegramDownloader.exe` process remains.

- [ ] **Step 6: Verify repository and artifact state**

Run `git status --short`, list the three artifacts with sizes and SHA-256 hashes, and confirm no tracked changes remain. Do not push or publish in this task.
