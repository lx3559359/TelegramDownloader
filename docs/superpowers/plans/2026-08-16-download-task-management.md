# Download Task Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade TelegramDownloader v0.7.0 with fast task filtering, multi-task actions, reversible completed-task archiving, item-level details, and guarded file opening without moving any business data outside the application directory.

**Architecture:** Extend the existing task database with a nullable archive timestamp and a one-query aggregate snapshot API. Keep filtering and item presentation in Qt models, let `MainWindow` emit intent-only signals, and keep state validation, batch scheduling, and guarded file access in `AppController`. Preserve every media row when archiving so deduplication remains effective.

**Tech Stack:** Python 3.12, SQLite WAL, PySide6 model/view, qasync, pytest, pytest-qt, Ruff, PyInstaller, Inno Setup.

---

## File map

- `src/telegram_downloader/domain.py`: append archive metadata to `TaskRecord`.
- `src/telegram_downloader/repository.py`: migrate the task schema, aggregate summaries, fetch an item, and atomically archive/restore tasks.
- `src/telegram_downloader/ui/models.py`: task filters, filtered task model, item summary, and item table model.
- `src/telegram_downloader/ui/main.py`: filter bar, extended selection, detail panel, batch actions, and stable selection refresh.
- `src/telegram_downloader/controller.py`: aggregate refresh, detail loading, batch validation, archive/restore, and guarded media opening.
- `src/telegram_downloader/app.py`: qasync adapters and signal wiring.
- `tests/test_repository.py`: migration, aggregation, archive, restore, dedup, and item lookup.
- `tests/ui/test_main_window.py`: filters, counts, multi-selection, action eligibility, details, and file intent.
- `tests/test_controller.py`: bulk actions, aggregate refresh, details, path guard, and error behavior.
- `tests/test_task_management_e2e.py`: persistence and restart workflow using real repositories and scheduler fakes.
- `tests/test_packaging_contract.py`, `tests/test_self_test.py`: v0.7.0 packaging contract.
- `README.md`, `docs/releases/v0.7.0.md`, `pyproject.toml`, `src/telegram_downloader/__init__.py`, `installer/TelegramDownloader.iss`, `TelegramDownloader.spec`: release metadata and documentation.

### Task 1: Persist reversible archives and aggregate task snapshots

**Files:**
- Modify: `src/telegram_downloader/domain.py`
- Modify: `src/telegram_downloader/repository.py`
- Test: `tests/test_repository.py`

- [ ] **Step 1: Write failing repository tests**

Add tests that initialize an old database without `archived_at`, verify migration, aggregate mixed known/unknown media sizes in one snapshot, archive only completed tasks, restore them, look up one item, and prove archived media still triggers `AllMediaAlreadyExists`.

```python
def test_initialize_migrates_archived_at_without_losing_tasks(tmp_path: Path) -> None:
    database = tmp_path / "tasks.sqlite3"
    create_legacy_task_database(database)
    repository = TaskRepository(database)

    repository.initialize()

    task = repository.get_task("legacy")
    assert task.archived_at is None
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
    assert "archived_at" in columns


def test_snapshots_archive_restore_and_keep_media_dedup(tmp_path: Path) -> None:
    repository = initialized_repository(tmp_path)
    completed = task_record("done", TaskStatus.COMPLETED)
    active = task_record("active", TaskStatus.DOWNLOADING)
    first = media_item("first", "done", expected_size=10, downloaded_bytes=10,
                       status=ItemStatus.COMPLETED)
    unknown = media_item("unknown", "done", expected_size=None, downloaded_bytes=4,
                         status=ItemStatus.FAILED, last_error="safe")
    repository.create_task(completed, [first, unknown])
    repository.create_task(active, [])

    snapshots = repository.list_task_snapshots(include_archived=True)
    done = next(value for value in snapshots if value.task.id == "done")
    assert (done.total_items, done.completed_items) == (2, 1)
    assert (done.downloaded_bytes, done.known_size, done.unknown_size_count) == (14, 10, 1)
    assert done.item_error == "safe"
    assert repository.archive_tasks(["done", "active", "done"]) == {"done"}
    assert [value.task.id for value in repository.list_task_snapshots()] == ["active"]
    assert repository.get_item("first").task_id == "done"
    assert repository.restore_tasks(["done"]) == {"done"}

    with pytest.raises(AllMediaAlreadyExists):
        repository.create_task_deduplicating(
            task_record("duplicate", TaskStatus.QUEUED),
            [replace(first, id="copy", task_id="duplicate")],
        )
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_repository.py -q
```

Expected: failures for missing `archived_at`, `TaskSnapshot`, `list_task_snapshots`, `archive_tasks`, `restore_tasks`, and `get_item`.

- [ ] **Step 3: Append archive metadata and add the aggregate repository API**

Append the field to preserve existing positional constructors:

```python
@dataclass(frozen=True, slots=True)
class TaskRecord:
    # existing fields unchanged
    display_title: str | None = None
    archived_at: datetime | None = None
```

Add the snapshot type and repository methods:

```python
@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    task: TaskRecord
    total_items: int
    completed_items: int
    downloaded_bytes: int
    known_size: int
    unknown_size_count: int
    item_error: str | None


def list_task_snapshots(self, *, include_archived: bool = False) -> list[TaskSnapshot]:
    where = "" if include_archived else "WHERE t.archived_at IS NULL"
    with self._connection() as connection:
        rows = connection.execute(
            f"""
            SELECT {_TASK_COLUMNS.replace(chr(10), ' ')},
                   COUNT(i.id) AS total_items,
                   COALESCE(SUM(i.status = ?), 0) AS completed_items,
                   COALESCE(SUM(i.downloaded_bytes), 0) AS downloaded_bytes,
                   COALESCE(SUM(COALESCE(i.expected_size, 0)), 0) AS known_size,
                   COALESCE(SUM(i.id IS NOT NULL AND i.expected_size IS NULL), 0)
                       AS unknown_size_count,
                   (SELECT last_error FROM media_items e
                    WHERE e.task_id = t.id AND e.last_error IS NOT NULL
                    ORDER BY e.message_date_utc DESC, e.message_id DESC, e.id
                    LIMIT 1) AS item_error
            FROM tasks t LEFT JOIN media_items i ON i.task_id = t.id
            {where}
            GROUP BY t.id
            ORDER BY t.created_at DESC, t.id
            """,
            (ItemStatus.COMPLETED.value,),
        ).fetchall()
    return [self._snapshot_from_row(row) for row in rows]


def archive_tasks(self, task_ids: list[str]) -> set[str]:
    ids = tuple(dict.fromkeys(task_ids))
    if not ids:
        return set()
    now = datetime.now(UTC).isoformat()
    placeholders = ",".join("?" for _ in ids)
    with self._connection() as connection:
        rows = connection.execute(
            f"SELECT id FROM tasks WHERE id IN ({placeholders}) "
            "AND status = ? AND archived_at IS NULL",
            (*ids, TaskStatus.COMPLETED.value),
        ).fetchall()
        accepted = {str(row[0]) for row in rows}
        if accepted:
            selected = tuple(sorted(accepted))
            marks = ",".join("?" for _ in selected)
            connection.execute(
                f"UPDATE tasks SET archived_at = ?, updated_at = ? WHERE id IN ({marks})",
                (now, now, *selected),
            )
    return accepted


def restore_tasks(self, task_ids: list[str]) -> set[str]:
    ids = tuple(dict.fromkeys(task_ids))
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    now = datetime.now(UTC).isoformat()
    with self._connection() as connection:
        rows = connection.execute(
            f"SELECT id FROM tasks WHERE id IN ({placeholders}) AND archived_at IS NOT NULL",
            ids,
        ).fetchall()
        accepted = {str(row[0]) for row in rows}
        if accepted:
            selected = tuple(sorted(accepted))
            marks = ",".join("?" for _ in selected)
            connection.execute(
                f"UPDATE tasks SET archived_at = NULL, updated_at = ? WHERE id IN ({marks})",
                (now, *selected),
            )
    return accepted
```

Update `_SCHEMA`, `_TASK_COLUMNS`, task values/readers, and `initialize()` so `archived_at` is added once to legacy databases. Qualify task columns with `t.` in the aggregate query instead of relying on ambiguous names.

- [ ] **Step 4: Run focused and repository regression tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_repository.py tests/test_planner.py tests/test_scheduler.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/domain.py src/telegram_downloader/repository.py tests/test_repository.py
git commit -m "feat: persist reversible task archives"
```

### Task 2: Build filtered task and media detail models

**Files:**
- Modify: `src/telegram_downloader/ui/models.py`
- Test: `tests/ui/test_main_window.py`

- [ ] **Step 1: Write failing model tests**

```python
def test_task_model_filters_search_status_and_archives() -> None:
    model = TaskTableModel()
    model.set_tasks([
        summary("a", "Alpha", TaskStatus.DOWNLOADING),
        summary("b", "Beta", TaskStatus.PAUSED),
        summary("c", "Gamma", TaskStatus.PARTIAL_FAILURE),
        summary("d", "Done", TaskStatus.COMPLETED),
        summary("e", "Old", TaskStatus.COMPLETED, archived=True),
    ])

    assert model.filter_counts() == {
        TaskFilter.ALL: 4,
        TaskFilter.ACTIVE: 1,
        TaskFilter.PAUSED: 1,
        TaskFilter.FAILED: 1,
        TaskFilter.COMPLETED: 1,
        TaskFilter.ARCHIVED: 1,
    }
    model.set_filter(TaskFilter.FAILED, "amm")
    assert [model.task_at(row).id for row in range(model.rowCount())] == ["c"]


def test_task_item_model_formats_progress_and_roles(tmp_path: Path) -> None:
    model = TaskItemTableModel()
    model.set_items([
        TaskItemSummary("i", "x.mp4", MediaKind.VIDEO, ItemStatus.COMPLETED,
                        10, 10, 2, "—")
    ])
    assert model.data(model.index(0, 2)) == "已完成"
    assert model.data(model.index(0, 3)) == "100%"
    assert model.data(model.index(0, 0), Qt.ItemDataRole.UserRole) == "i"
```

- [ ] **Step 2: Run the model tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_main_window.py -q
```

Expected: import or attribute failures for `TaskFilter`, archived summaries, and `TaskItemTableModel`.

- [ ] **Step 3: Implement model-only filtering and item presentation**

Add:

```python
class TaskFilter(StrEnum):
    ALL = "all"
    ACTIVE = "active"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class TaskItemSummary:
    id: str
    name: str
    kind: MediaKind
    status: ItemStatus
    downloaded_bytes: int
    expected_size: int | None
    retry_count: int
    error_text: str


def _task_matches(task: TaskSummary, selected: TaskFilter) -> bool:
    if selected is TaskFilter.ARCHIVED:
        return task.archived
    if task.archived:
        return False
    if selected is TaskFilter.ALL:
        return True
    if selected is TaskFilter.ACTIVE:
        return task.status in {
            TaskStatus.SCANNING, TaskStatus.QUEUED,
            TaskStatus.DOWNLOADING, TaskStatus.WAITING_RETRY,
        }
    return {
        TaskFilter.PAUSED: task.status is TaskStatus.PAUSED,
        TaskFilter.FAILED: task.status is TaskStatus.PARTIAL_FAILURE,
        TaskFilter.COMPLETED: task.status is TaskStatus.COMPLETED,
    }.get(selected, False)
```

Keep `_all_tasks`, `_tasks`, current filter, and casefolded search text in `TaskTableModel`. Extend `TaskSummary` with `archived: bool = False`. Implement `filter_counts()`, `set_filter()`, and `row_for_task_id()` using model resets. Add `TaskItemTableModel` with headers `文件/类型/状态/进度/大小/重试/错误`, item ID in `UserRole`, and status colors matching task colors.

- [ ] **Step 4: Run tests and Ruff for the model**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_main_window.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/ui/models.py tests/ui/test_main_window.py
```

Expected: both commands pass.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/ui/models.py tests/ui/test_main_window.py
git commit -m "feat: model task filters and item details"
```

### Task 3: Upgrade the task workspace UI

**Files:**
- Modify: `src/telegram_downloader/ui/main.py`
- Modify: `src/telegram_downloader/ui/theme.py`
- Test: `tests/ui/test_main_window.py`

- [ ] **Step 1: Write failing UI interaction tests**

```python
def test_task_workspace_filters_multiselects_and_emits_batch_actions(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_task_summaries([
        summary("run", "Running", TaskStatus.DOWNLOADING),
        summary("pause", "Paused", TaskStatus.PAUSED),
        summary("done", "Done", TaskStatus.COMPLETED),
    ])
    emitted: list[list[str]] = []
    window.pause_tasks_requested.connect(emitted.append)

    select_rows(window.task_table, [0, 1])
    qtbot.mouseClick(window.pause_button, Qt.MouseButton.LeftButton)

    assert emitted == [["run", "pause"]]
    assert window.open_button.isEnabled() is False
    window.task_search.setText("Done")
    assert window.task_model.rowCount() == 1


def test_single_selection_shows_details_and_double_clicks_file(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_task_summaries([summary("done", "Done", TaskStatus.COMPLETED)])
    window.task_table.selectRow(0)
    window.set_task_items("done", [item_summary("media", ItemStatus.COMPLETED)])
    opened: list[str] = []
    window.open_media_requested.connect(opened.append)

    index = window.task_item_model.index(0, 0)
    window.task_item_table.doubleClicked.emit(index)

    assert opened == ["media"]
    assert window.task_detail_title.text() == "Done"
```

- [ ] **Step 2: Run UI tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_main_window.py -q
```

Expected: missing signals and widgets for filtering, multi-selection, details, archiving, restoring, and media opening.

- [ ] **Step 3: Add intent signals and filter/detail widgets**

Add signals:

```python
task_selection_changed = Signal(object)
pause_tasks_requested = Signal(object)
resume_tasks_requested = Signal(object)
retry_tasks_requested = Signal(object)
archive_tasks_requested = Signal(object)
restore_tasks_requested = Signal(object)
open_media_requested = Signal(str)
```

Build a filter row with `QLineEdit` and `QComboBox`, a vertical `QSplitter` containing the task and item tables, and archive/restore/open-file buttons. Change the task table to `ExtendedSelection` and emit a deduplicated list in current visual order:

```python
def selected_task_ids(self) -> list[str]:
    rows = sorted({index.row() for index in self.task_table.selectionModel().selectedRows()})
    return [self.task_model.task_at(row).id for row in rows
            if self.task_model.task_at(row) is not None]


def _selection_changed(self, *_args) -> None:
    ids = self.selected_task_ids()
    self.task_selection_changed.emit(ids)
    self._update_action_state()
```

`set_task_summaries()` must remember all selected IDs, reset the model, then reselect visible IDs with `QItemSelectionModel.SelectionFlag.Select | Rows`. Update filter labels after every task refresh. `set_task_items(task_id, items)` must discard stale detail responses whose task ID is no longer the only selection.

- [ ] **Step 4: Add confirmation-safe archive/restore actions**

Use `QMessageBox.question` with text that explicitly says downloaded files remain. Emit only after confirmation. In tests, monkeypatch `QMessageBox.question` to `StandardButton.Yes`.

```python
def _confirm_archive(self) -> None:
    ids = self.selected_task_ids()
    if not ids:
        return
    answer = QMessageBox.question(
        self,
        "归档完成任务",
        f"归档所选 {len(ids)} 个任务？下载文件会保留，可随时恢复。",
    )
    if answer is QMessageBox.StandardButton.Yes:
        self.archive_tasks_requested.emit(ids)
```

- [ ] **Step 5: Run UI and layout tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_main_window.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/ui/main.py src/telegram_downloader/ui/theme.py
```

Expected: all selected tests and Ruff pass.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/ui/main.py src/telegram_downloader/ui/theme.py tests/ui/test_main_window.py
git commit -m "feat: add task management workspace"
```

### Task 4: Add controller-side batch safety and detail loading

**Files:**
- Modify: `src/telegram_downloader/controller.py`
- Test: `tests/test_controller.py`

- [ ] **Step 1: Write failing controller tests**

```python
def test_refresh_uses_one_aggregate_query_and_loads_only_selected_details(controller) -> None:
    controller.repository.snapshots = [snapshot("done", TaskStatus.COMPLETED)]
    controller.refresh_tasks(now=5.0)
    assert controller.repository.snapshot_calls == [True]
    assert controller.repository.list_items_calls == []

    controller.select_task_details(["done"])
    assert controller.repository.list_items_calls == ["done"]
    assert controller.window.detail_task_id == "done"


@pytest.mark.asyncio
async def test_bulk_actions_deduplicate_and_skip_ineligible_tasks(controller) -> None:
    controller.repository.tasks = {
        "run": task_record("run", TaskStatus.DOWNLOADING),
        "pause": task_record("pause", TaskStatus.PAUSED),
        "fail": task_record("fail", TaskStatus.PARTIAL_FAILURE),
    }
    controller.pause_tasks(["run", "pause", "run"])
    await controller.resume_tasks(["pause", "run", "pause"])
    assert controller.scheduler.paused == ["run"]
    assert controller.scheduler.resumed == ["pause"]
    await controller.retry_failed_tasks(["fail", "run"])
    assert controller.scheduler.resumed == ["pause", "fail"]


def test_open_media_rejects_missing_and_outside_paths(controller, tmp_path: Path) -> None:
    controller.repository.item = media_item(
        "outside", "task", target_path=tmp_path.parent / "escape.bin",
        status=ItemStatus.COMPLETED,
    )
    controller.open_media_file("outside")
    assert controller.startfile_calls == []
    assert "安全" in controller.window.status
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_controller.py -q
```

Expected: missing aggregate, selection, batch, archive, restore, and media-open methods.

- [ ] **Step 3: Replace N+1 refresh with snapshots**

Use `repository.list_task_snapshots(include_archived=True)` and map each snapshot directly to `TaskSummary`. Keep speed samples only for unarchived downloading tasks and exclude archived tasks from the right-side statistics.

```python
for snapshot in self.repository.list_task_snapshots(include_archived=True):
    task = snapshot.task
    total_bytes = None if snapshot.unknown_size_count else snapshot.known_size
    speed = self._sample_speed(task.id, task.status, snapshot.downloaded_bytes, sampled_at)
    summaries.append(TaskSummary(
        task.id,
        task.display_title or task.source_title,
        task.status,
        f"{snapshot.completed_items} / {snapshot.total_items}",
        self._format_bytes(snapshot.known_size)
        + (" + 未知" if snapshot.unknown_size_count else ""),
        self._format_rate(speed),
        self._format_duration(remaining_seconds),
        task.last_error or snapshot.item_error or "—",
        snapshot.completed_items,
        snapshot.total_items,
        snapshot.downloaded_bytes,
        total_bytes,
        speed,
        remaining_seconds,
        archived=task.archived_at is not None,
    ))
```

- [ ] **Step 4: Implement validated bulk actions, archive/restore, and item details**

Use stable deduplication and fetch the current task before every action:

```python
@staticmethod
def _unique_ids(task_ids: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in task_ids if value))


def pause_tasks(self, task_ids: list[str]) -> None:
    accepted = 0
    for task_id in self._unique_ids(task_ids):
        task = self.repository.get_task(task_id)
        if task.archived_at is None and task.status in {
            TaskStatus.QUEUED, TaskStatus.DOWNLOADING, TaskStatus.WAITING_RETRY,
        }:
            self.scheduler.pause_task(task_id)
            accepted += 1
    self.refresh_tasks()
    self._show_status(f"已暂停 {accepted} 个任务，跳过 {len(self._unique_ids(task_ids)) - accepted} 个")


def archive_tasks(self, task_ids: list[str]) -> None:
    ids = self._unique_ids(task_ids)
    accepted = self.repository.archive_tasks(ids)
    self.refresh_tasks()
    self._show_status(f"已归档 {len(accepted)} 个完成任务；下载文件已保留")
```

Map media records to `TaskItemSummary` in `select_task_details`. In `open_media_file`, require `ItemStatus.COMPLETED`, guard the stored target through `self.paths.guard`, require `is_file()`, and then call an injected/open-system helper. Catch `ValueError`, `OSError`, and `KeyError` and show a safe status.

- [ ] **Step 5: Run controller, scheduler, and repository tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_controller.py tests/test_scheduler.py tests/test_repository.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/controller.py tests/test_controller.py
```

Expected: all selected tests and Ruff pass.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/controller.py tests/test_controller.py
git commit -m "feat: manage download tasks safely"
```

### Task 5: Wire qasync actions and protect interaction races

**Files:**
- Modify: `src/telegram_downloader/app.py`
- Modify: `src/telegram_downloader/ui/async_actions.py`
- Test: `tests/test_app.py`
- Test: `tests/ui/test_async_actions.py`

- [ ] **Step 1: Write failing wiring tests**

```python
def test_task_management_signals_are_wired(app_harness) -> None:
    app_harness.window.pause_tasks_requested.emit(["a", "a", "b"])
    app_harness.window.task_selection_changed.emit(["a"])
    app_harness.window.archive_tasks_requested.emit(["done"])
    assert app_harness.controller.pause_batches == [["a", "a", "b"]]
    assert app_harness.controller.detail_selections == [["a"]]
    assert app_harness.controller.archive_batches == [["done"]]
```

Add async tests proving repeated resume or retry clicks use a single action key and that selection/detail loading remains available while a download action runs.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/ui/test_async_actions.py -q
```

Expected: task management signals are not connected and action keys do not exist.

- [ ] **Step 3: Add thin qasync adapters**

```python
def resume_tasks_requested(task_ids: object) -> None:
    ids = [str(value) for value in task_ids]
    async_actions.start(
        "tasks.resume",
        lambda: controller.resume_tasks(ids),
        hooks=ActionHooks(failed=lambda error: window.statusBar().showMessage(
            controller._safe_error(error)
        )),
    )


def retry_tasks_requested(task_ids: object) -> None:
    ids = [str(value) for value in task_ids]
    async_actions.start(
        "tasks.retry",
        lambda: controller.retry_failed_tasks(ids),
        hooks=ActionHooks(failed=lambda error: window.statusBar().showMessage(
            controller._safe_error(error)
        )),
    )
```

Connect all seven task management signals. Keep the adapter objects in `controller._ui_slots` so PySide does not collect them. Repeated action keys must return the existing task and never launch a second batch.

- [ ] **Step 4: Run wiring and full UI tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/ui -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/app.py src/telegram_downloader/ui/async_actions.py
```

Expected: all selected tests and Ruff pass.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/app.py src/telegram_downloader/ui/async_actions.py tests/test_app.py tests/ui/test_async_actions.py
git commit -m "feat: integrate task management actions"
```

### Task 6: Verify the complete archive and restart workflow

**Files:**
- Create: `tests/test_task_management_e2e.py`
- Modify: `tests/test_logging.py`
- Modify: `tests/test_paths.py`

- [ ] **Step 1: Write the end-to-end test**

Use real `TaskRepository`, `PortablePaths`, Qt models, and a recording scheduler. Create active, paused, failed, completed, and archived synthetic tasks under a temporary application root. Exercise filter → multi-select action → details → archive → close/reinitialize → restore → duplicate enqueue.

```python
def test_task_management_persists_archive_and_preserves_dedup(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path / "app")
    paths.ensure_layout()
    repository = TaskRepository(paths.database)
    repository.initialize()
    completed, item = completed_fixture(paths)
    repository.create_task(completed, [item])

    assert repository.archive_tasks([completed.id]) == {completed.id}
    restarted = TaskRepository(paths.database)
    restarted.initialize()
    archived = restarted.list_task_snapshots(include_archived=True)[0]
    assert archived.task.archived_at is not None
    assert restarted.restore_tasks([completed.id]) == {completed.id}

    with pytest.raises(AllMediaAlreadyExists):
        restarted.create_task_deduplicating(duplicate_task(item), [duplicate_item(item)])
    assert paths.guard(item.target_path).is_relative_to(paths.root)
```

Add logging assertions that task IDs may be logged but titles, media names, target paths, and raw errors are absent from failure events.

- [ ] **Step 2: Run the new end-to-end verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_task_management_e2e.py tests/test_logging.py tests/test_paths.py -q
```

Expected: PASS. A failure stops this task and must first be reduced to a focused regression test in the owning Task 1–5 test file before changing production code.

- [ ] **Step 3: Run focused and full regression**

Run:

```powershell
.\scripts\test.ps1
```

Expected: the previous 444 tests plus all new tests pass; Ruff reports `All checks passed!`.

- [ ] **Step 4: Commit**

```powershell
git add tests/test_task_management_e2e.py tests/test_logging.py tests/test_paths.py
git commit -m "test: verify task management end to end"
```

### Task 7: Prepare v0.7.0 packaging and documentation

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/telegram_downloader/__init__.py`
- Modify: `installer/TelegramDownloader.iss`
- Modify: `TelegramDownloader.spec`
- Modify: `README.md`
- Create: `docs/releases/v0.7.0.md`
- Modify: `tests/test_packaging_contract.py`
- Modify: `tests/test_self_test.py`

- [ ] **Step 1: Write failing version and packaging assertions**

```python
def test_v070_version_and_content_runtime_contract_are_consistent() -> None:
    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    package_init = (root / "src/telegram_downloader/__init__.py").read_text(
        encoding="utf-8"
    )
    installer = (root / "installer/TelegramDownloader.iss").read_text(
        encoding="utf-8"
    )
    spec = (root / "TelegramDownloader.spec").read_text(encoding="utf-8")
    assert project["project"]["version"] == "0.7.0"
    assert '__version__ = "0.7.0"' in package_init
    assert '#define AppVersion "0.7.0"' in installer
    assert '"telegram_downloader.ui.models"' in spec
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_packaging_contract.py tests/test_self_test.py -q
```

Expected: version assertions fail at 0.6.0.

- [ ] **Step 3: Bump metadata and document exact behavior**

Set every version field to `0.7.0` and add `"telegram_downloader.ui.models"` to the explicit PyInstaller hidden imports. Update README with filters, multi-select actions, archive semantics, item details, one-query refresh, and the guarantee that archive retains files and dedup records.

Create release notes:

```markdown
# TelegramDownloader v0.7.0

## 下载任务管理中心

- 按任务名称和状态筛选，并显示分类数量。
- 多选批量暂停、继续和重试失败任务。
- 查看单任务媒体明细，双击打开已完成文件。
- 已完成任务支持可恢复归档；下载文件和去重记录始终保留。

## 性能与数据安全

- 任务摘要由单次 SQLite 聚合查询刷新，任务增多时不再逐任务查库。
- 文件操作经过应用目录路径守卫；所有业务数据继续位于应用目录。
```

- [ ] **Step 4: Run packaging contract and complete tests**

Run:

```powershell
.\scripts\test.ps1
```

Expected: all tests and Ruff pass.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml src/telegram_downloader/__init__.py installer/TelegramDownloader.iss TelegramDownloader.spec README.md docs/releases/v0.7.0.md tests/test_packaging_contract.py tests/test_self_test.py
git commit -m "docs: prepare task management release"
```

### Task 8: Realistic QA, Windows artifacts, and local integration

**Files:**
- Create: `docs/verification/v0.7.0-task-management.md`
- Do not commit: `.build-temp/task-management-real-qa-*`

- [ ] **Step 1: Run three automated verification rounds**

Round 1:

```powershell
.\scripts\test.ps1
```

Round 2:

```powershell
.\scripts\build-installer.ps1
```

Expected: full pytest/Ruff pass, `PACKAGED_SMOKE_OK`, and `INSTALLER_SMOKE_OK`.

Round 3, after all QA corrections:

```powershell
.\scripts\test.ps1
```

Expected: same test count, zero failures, Ruff clean.

- [ ] **Step 2: Run isolated saved-session and synthetic-task QA**

Copy only the encrypted settings/session into a project-local ignored QA root. Redirect `TEMP`, `TMP`, `APPDATA`, and `LOCALAPPDATA` to that root. Insert synthetic tasks and files only in the isolated database/download tree, then verify these booleans without printing private content:

```text
saved_session_connected=true
qr_requested=false
filter_counts_correct=true
keyword_filter_correct=true
multi_selection_correct=true
bulk_actions_correct=true
details_visible=true
missing_file_safe=true
outside_path_rejected=true
archive_persisted=true
restore_completed=true
dedup_preserved=true
all_paths_local=true
cleanup_complete=true
```

Capture Qt screenshots at 1280×780 and 1180×720. Inspect task filters, tables, splitter, details, buttons, wrapping, elision, and empty states. Never include task names, file names, peer IDs, message IDs, keywords, or screenshots in committed evidence.

- [ ] **Step 3: Audit release privacy and artifact integrity**

Confirm the portable ZIP has zero matches for `data/`, `downloads/`, databases, logs, `secrets.dat`, and self-test reports. Run the packaged EXE with `Start-Process -Wait --self-test`; verify version 0.7.0, schema compatibility, all components, and every writable path under its runtime root. Record byte size and SHA-256 for ZIP, setup EXE, and direct EXE.

- [ ] **Step 4: Write and commit verification evidence**

Document the three rounds, requirement matrix, aggregate observations only from real QA, artifact hashes, ZIP privacy count, path boundary, known scope, and the fact that no online stable pointer was changed.

```powershell
git add docs/verification/v0.7.0-task-management.md
git commit -m "docs: record task management verification"
```

- [ ] **Step 5: Merge locally and verify the merged result**

From the main repository, fast-forward `main`, run `scripts/test.ps1`, create `dist/release/v0.7.0-portable` by preserving the previous direct-run `data/` and overlaying only v0.7.0 managed runtime files, then run the direct EXE self-test with explicit process waiting. Copy the clean ZIP and setup EXE into the main project `dist` paths.

Do not push GitHub/魔搭, create a tag, publish a release, or modify stable manifests during this local integration.
