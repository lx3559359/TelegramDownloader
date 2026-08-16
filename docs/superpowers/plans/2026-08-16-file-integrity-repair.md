# TelegramDownloader v0.9.0 File Integrity Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persistent SHA-256 verification, explicit completed-file health checks, and selected-media safe redownload without allowing project data to leave the application directory.

**Architecture:** Extend the task repository with integrity metadata and atomic state transitions, teach the downloader to commit hashes with completed files, and isolate later disk verification/quarantine in a new `FileIntegrityService`. The scheduler gets a selected-item execution path; the existing controller and task detail UI orchestrate cancellable progress without blocking Qt.

**Tech Stack:** Python 3.12, asyncio, hashlib, SQLite, PySide6/qasync, pytest/pytest-asyncio/pytest-qt, Ruff, PyInstaller, Inno Setup, Ed25519 release manifests.

---

## File map

- Create `src/telegram_downloader/file_integrity.py`: disk hashing, verification summaries, cancellation, and safe quarantine preparation.
- Create `tests/test_file_integrity.py`: service and quarantine behavior.
- Modify `src/telegram_downloader/domain.py`: `IntegrityStatus` and media integrity fields.
- Modify `src/telegram_downloader/repository.py`: idempotent migration and atomic integrity/repair transitions.
- Modify `src/telegram_downloader/downloader.py`: SHA-256 for new, resumed, and pre-existing completed files.
- Modify `src/telegram_downloader/scheduler.py`: selected-item execution and task-state recomputation.
- Modify `src/telegram_downloader/ui/models.py`: integrity value in task item summaries and table.
- Modify `src/telegram_downloader/ui/main.py`: detail multi-select, verify/repair actions, progress, and cancel.
- Modify `src/telegram_downloader/controller.py`: cancellable integrity orchestration and safe status messages.
- Modify `src/telegram_downloader/app.py`: construct and inject the integrity service.
- Modify focused tests under `tests/` and `tests/ui/` alongside each production change.
- Modify version, packaging contract, README, release notes, and verification evidence for v0.9.0 only after behavior is green.

### Task 1: Integrity domain and schema migration

**Files:**
- Modify: `src/telegram_downloader/domain.py`
- Modify: `src/telegram_downloader/repository.py`
- Test: `tests/test_repository.py`

- [ ] **Step 1: Write failing migration and validation tests**

Add tests that initialize a v0.8-shaped database, preserve its task/media rows, and assert the new row loads as `UNVERIFIED`. Add repository tests for valid verified metadata and rejection of malformed SHA-256.

```python
def test_initialize_migrates_v080_media_to_unverified(tmp_path: Path) -> None:
    database = tmp_path / "tasks.sqlite3"
    create_v080_database(database)
    repository = TaskRepository(database)
    repository.initialize()

    item = repository.get_item("media-1")

    assert item.integrity_status is IntegrityStatus.UNVERIFIED
    assert item.content_sha256 is None
    assert item.verified_at is None


def test_record_integrity_success_validates_digest(tmp_path: Path) -> None:
    repository, item = repository_with_completed_item(tmp_path)

    with pytest.raises(ValueError, match="SHA-256"):
        repository.record_integrity_success(item.id, "not-a-digest", datetime.now(UTC))
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_repository.py -k "integrity or v080" -q`

Expected: collection/import failure because `IntegrityStatus` and repository integrity methods do not exist.

- [ ] **Step 3: Add the domain fields and idempotent migration**

Define the enum and extend `MediaItem` with backward-compatible defaults:

```python
class IntegrityStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    MISSING = "missing"
    SIZE_MISMATCH = "size_mismatch"
    HASH_MISMATCH = "hash_mismatch"


@dataclass(frozen=True, slots=True)
class MediaItem:
    # existing fields remain in their current order
    integrity_status: IntegrityStatus = IntegrityStatus.UNVERIFIED
    content_sha256: str | None = None
    verified_at: datetime | None = None
```

Extend `_SCHEMA`, `_ITEM_COLUMNS`, row/value conversion, and `initialize()` so each missing column is added independently. Add these repository methods with one transaction per call:

```python
def record_integrity_success(
    self, item_id: str, sha256: str, verified_at: datetime
) -> None: ...

def record_integrity_failure(
    self, item_id: str, status: IntegrityStatus, safe_error: str
) -> None: ...

def prepare_integrity_repair(self, item_id: str) -> MediaItem: ...

def recompute_task_status(self, task_id: str) -> TaskStatus: ...
```

`record_integrity_failure` accepts only missing/size/hash mismatch, changes the item to failed, and changes its task to partial failure. `prepare_integrity_repair` accepts one integrity-failed row, resets it to queued/0 retries/0 bytes/unverified, clears hash/time/error, and returns the pre-reset record. The service deliberately invokes this atomic repository operation per successfully quarantined item so an unrelated file failure cannot partially reset a batch.

- [ ] **Step 4: Run repository tests and confirm GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_repository.py -q`

Expected: all repository tests pass, including repeated `initialize()`.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/domain.py src/telegram_downloader/repository.py tests/test_repository.py
git commit -m "feat: persist media integrity state"
```

### Task 2: Hash every successful download

**Files:**
- Modify: `src/telegram_downloader/downloader.py`
- Modify: `tests/test_downloader.py`

- [ ] **Step 1: Write failing digest tests**

Extend the fake progress writer with `complete_item`. Assert exact SHA-256 for fresh download, resumed `.part`, and an already-present final file. Assert pause and size mismatch never call `complete_item`.

```python
class Repo:
    def complete_item(self, item_id, downloaded_bytes, sha256, verified_at):
        self.completed.append((item_id, downloaded_bytes, sha256, verified_at))


@pytest.mark.asyncio
async def test_resumed_download_records_full_file_sha256(tmp_path: Path) -> None:
    target = tmp_path / "downloads" / "video.mp4"
    target.parent.mkdir(parents=True)
    target.with_suffix(".mp4.part").write_bytes(b"ab")
    downloader, repo = downloader_for_chunks(tmp_path, [b"cdef"])

    await downloader.download(media_item(target, expected_size=6))

    assert repo.completed[0][2] == hashlib.sha256(b"abcdef").hexdigest()
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_downloader.py -k "sha256 or digest" -q`

Expected: failure because successful downloads still call only `update_item_progress`.

- [ ] **Step 3: Implement non-blocking full-file hashing and atomic completion**

Add a 1 MiB synchronous helper used through `asyncio.to_thread` for existing files and resumed prefixes:

```python
def _hash_file(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest
```

For a new stream initialize `hashlib.sha256()`. For resumed content load the prefix digest with `await asyncio.to_thread(_hash_file, part)` before requesting network bytes, then call `digest.update(chunk)` for every downloaded chunk. After final size check and rename call:

```python
self.repository.complete_item(
    item.id,
    downloaded,
    digest.hexdigest(),
    datetime.now(UTC),
)
```

The existing-file fast path also hashes through `to_thread` before `complete_item`.

- [ ] **Step 4: Run downloader and scheduler compatibility tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_downloader.py tests/test_scheduler.py -q`

Expected: all pass; update test fakes to implement `complete_item` without weakening state assertions.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/downloader.py tests/test_downloader.py tests/test_scheduler.py
git commit -m "feat: hash completed media files"
```

### Task 3: Cancellable file integrity service

**Files:**
- Create: `src/telegram_downloader/file_integrity.py`
- Create: `tests/test_file_integrity.py`

- [ ] **Step 1: Write failing service tests**

Cover baseline creation, successful recheck, missing, known-size mismatch, same-size hash mismatch, path escape, read failure, deduplicated IDs, progress order, and cancellation. Use a real `TaskRepository` in temporary project roots.

```python
@pytest.mark.asyncio
async def test_same_size_change_is_hash_mismatch(tmp_path: Path) -> None:
    service, repository, item, target = integrity_fixture(tmp_path, b"good")
    await service.verify([item.id])
    target.write_bytes(b"evil")

    summary = await service.verify([item.id])

    assert summary.hash_mismatch == 1
    assert repository.get_item(item.id).integrity_status is IntegrityStatus.HASH_MISMATCH
```

- [ ] **Step 2: Run the new test file and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_file_integrity.py -q`

Expected: import failure for `telegram_downloader.file_integrity`.

- [ ] **Step 3: Implement service types and verification**

Create immutable progress/result types and a cancel token:

```python
@dataclass(frozen=True, slots=True)
class IntegrityProgress:
    completed: int
    total: int
    item_id: str
    file_name: str
    status: IntegrityStatus


@dataclass(frozen=True, slots=True)
class IntegritySummary:
    verified: int = 0
    baselined: int = 0
    missing: int = 0
    size_mismatch: int = 0
    hash_mismatch: int = 0
    skipped: int = 0
    cancelled: int = 0
```

`FileIntegrityService.verify(item_ids, progress=None, cancelled=None)` guards paths, checks existence/size, and calls a `to_thread` hash helper that checks a `threading.Event` between 1 MiB blocks. Each item catches `OSError` and records the fixed safe error `无法读取本地文件` without exposing the path.

- [ ] **Step 4: Implement safe quarantine preparation**

Add `prepare_repairs(item_ids)` that deduplicates IDs and handles each selected row independently: validate the record and every path, move existing final/part files with `_next_corrupt_path`, then call `repository.prepare_integrity_repair(item.id)`. If either move fails, restore any move already completed for that row and do not reset it. If the repository reset fails, restore both quarantined files before propagating the failure. Continue after expected per-row filesystem failures and return accepted IDs plus skipped count; never leave a row queued while its quarantine step is incomplete.

- [ ] **Step 5: Run service tests and confirm GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_file_integrity.py -q`

Expected: all integrity service tests pass and no test path escapes its temporary root.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/file_integrity.py tests/test_file_integrity.py
git commit -m "feat: verify and quarantine local media"
```

### Task 4: Selected-media scheduler execution

**Files:**
- Modify: `src/telegram_downloader/scheduler.py`
- Modify: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing selected-item tests**

Create three failed media rows, select only the second integrity-repair item, and assert only its downloader call occurs. Add mixed final-state cases so the task becomes completed only when every row is completed, partial failure while any row is failed, and paused while any selected execution pauses.

```python
@pytest.mark.asyncio
async def test_run_items_downloads_only_requested_media() -> None:
    repo = Repo(count=3)
    for item in repo.items:
        item.status = ItemStatus.FAILED
    scheduler = DownloadScheduler(repo, RecordingDownloader())

    await scheduler.run_items("t", ["i1"])

    assert scheduler.downloader.ids == ["i1"]
```

- [ ] **Step 2: Run focused scheduler tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_scheduler.py -k "run_items or recompute" -q`

Expected: `DownloadScheduler` has no `run_items`.

- [ ] **Step 3: Implement the selected path**

Add `run_items(task_id, item_ids)` that rejects empty input, deduplicates IDs, confirms every row belongs to the task and is queued, executes only those records through `_guarded_item`, then calls `repository.recompute_task_status(task_id)`. Protect concurrent task and selected-item runs with the same `_active` task entry so they cannot race.

- [ ] **Step 4: Run scheduler tests and confirm GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_scheduler.py -q`

Expected: all scheduler tests pass, including original task retry behavior.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/scheduler.py tests/test_scheduler.py
git commit -m "feat: run selected media repairs"
```

### Task 5: Task detail integrity UI

**Files:**
- Modify: `src/telegram_downloader/ui/models.py`
- Modify: `src/telegram_downloader/ui/main.py`
- Modify: `tests/ui/test_main_window.py`
- Modify: `tests/ui/test_content_models.py` only if shared helpers require it

- [ ] **Step 1: Write failing model and interaction tests**

Assert the new “完整性” column text and verified-time tooltip. Assert detail table extended selection, stable selected media IDs, verify/repair signal payloads, confirmation for repair, button enable rules, busy progress text, cancellation signal, and unchanged double-click open behavior.

```python
def test_integrity_actions_use_selected_media_ids(qtbot, monkeypatch) -> None:
    window = completed_task_window(qtbot)
    window.set_task_items("task", [verified_item("a"), missing_item("b")])
    select_detail_rows(window, 0, 1)

    with qtbot.waitSignal(window.verify_media_requested) as verify:
        qtbot.mouseClick(window.verify_media_button, Qt.MouseButton.LeftButton)
    assert verify.args == [["a", "b"]]

    monkeypatch.setattr(QMessageBox, "question", answer_yes)
    with qtbot.waitSignal(window.repair_media_requested) as repair:
        qtbot.mouseClick(window.repair_media_button, Qt.MouseButton.LeftButton)
    assert repair.args == [["b"]]
```

- [ ] **Step 2: Run UI tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_main_window.py -k "integrity or selected_media" -q`

Expected: missing summary fields, signals, buttons, and progress widgets.

- [ ] **Step 3: Extend summaries and table model**

Add `integrity_status` and `verified_at` defaults to `TaskItemSummary`; add the column after status. Map enum values to fixed Chinese labels and expose the UTC timestamp only through `Qt.ToolTipRole`.

- [ ] **Step 4: Add multi-select actions and progress UI**

Use `ExtendedSelection`. Add signals:

```python
verify_media_requested = Signal(list)
repair_media_requested = Signal(list)
verify_tasks_requested = Signal(list)
integrity_cancel_requested = Signal()
```

Add `校验所选`, `重新下载所选`, and task-level `校验文件`; add a hidden progress row with label, determinate progress bar, and `取消校验`. Implement `set_integrity_progress(progress_or_none)` and `set_integrity_busy(busy)` without disabling navigation or close.

- [ ] **Step 5: Run UI tests and minimum-size offscreen layout test**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_main_window.py tests/ui/test_content_models.py -q`

Expected: all pass at existing 1180×720 and 1280×780 coverage.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/ui/models.py src/telegram_downloader/ui/main.py tests/ui/test_main_window.py tests/ui/test_content_models.py
git commit -m "feat: add media integrity controls"
```

### Task 6: Controller orchestration and app wiring

**Files:**
- Modify: `src/telegram_downloader/controller.py`
- Modify: `src/telegram_downloader/app.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write failing controller tests**

Add a fake integrity service and assert media/task selection expansion, progress forwarding, duplicate-action suppression, cancellation, summary wording, refresh after completion, safe repair confirmation input, and selected scheduler IDs. Add shutdown cancellation coverage.

```python
@pytest.mark.asyncio
async def test_repair_selected_media_runs_only_prepared_ids(controller_fixture) -> None:
    controller, integrity, scheduler, window = controller_fixture
    integrity.prepared_ids = ["broken"]

    await controller.repair_media(["broken", "healthy"])

    assert scheduler.selected_runs == [("task", ["broken"])]
    assert "已重新下载 1" in window.statusBar().last_message
```

- [ ] **Step 2: Run focused controller tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_controller.py tests/test_app.py -k "integrity or repair_media" -q`

Expected: controller constructor and UI wiring do not expose integrity operations.

- [ ] **Step 3: Inject service and implement cancellable actions**

Add `integrity_service` to `AppController`, `_integrity_task`, and a cancellation event. Implement `verify_media`, `verify_tasks`, `repair_media`, and `cancel_integrity`; use one active operation guard. Expand task IDs with repository queries before starting, forward progress to `window.set_integrity_progress`, and refresh task/detail state in `finally`.

Construct `FileIntegrityService(repository, paths)` in `create_application`. Connect new sync signals through `AsyncActionBridge` with stable keys so repeated clicks reuse/cancel safely.

- [ ] **Step 4: Run controller/app tests and confirm GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_controller.py tests/test_app.py -q`

Expected: all existing login, search, subscription, download, and update tests remain green.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/controller.py src/telegram_downloader/app.py tests/test_controller.py tests/test_app.py
git commit -m "feat: orchestrate integrity verification and repair"
```

### Task 7: End-to-end repair and privacy regression

**Files:**
- Create: `tests/test_file_integrity_e2e.py`
- Modify: `tests/test_logging.py`
- Modify: `tests/test_packaging_contract.py`

- [ ] **Step 1: Write the end-to-end test**

Create a real repository, task, two completed files, and controller/service/scheduler with a controlled gateway. Establish baselines, overwrite one file with same-size bytes, verify mismatch, prepare only that item, redownload it, and assert the other item and its retry count were untouched. Assert `.corrupt` contains the changed bytes and all paths are under the test root.

- [ ] **Step 2: Add privacy and packaging assertions**

Assert integrity errors/log messages never include the absolute test path or a registered secret. Require README language for hash storage and safe repair, and require the PyInstaller source graph to include `telegram_downloader.file_integrity` through the normal app import.

- [ ] **Step 3: Run E2E plus full suite**

Run: `.venv\Scripts\python.exe -m pytest tests/test_file_integrity_e2e.py tests/test_logging.py tests/test_packaging_contract.py -q`

Then: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1`

Expected: all tests and Ruff pass.

- [ ] **Step 4: Commit**

```powershell
git add tests/test_file_integrity_e2e.py tests/test_logging.py tests/test_packaging_contract.py
git commit -m "test: cover integrity repair end to end"
```

### Task 8: Prepare and verify v0.9.0

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/telegram_downloader/__init__.py`
- Modify: `installer/TelegramDownloader.iss`
- Modify: `README.md`
- Create: `docs/releases/v0.9.0.md`
- Create: `docs/verification/v0.9.0-file-integrity-repair.md`
- Modify: `tests/test_packaging_contract.py`

- [ ] **Step 1: Write failing version/package contract assertions**

Require 0.9.0 consistently, the release note file, README terms `SHA-256 完整性`, `校验所选`, `重新下载所选`, and the integrity module import in `app.py`.

- [ ] **Step 2: Run contract test and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_packaging_contract.py -q`

Expected: version remains 0.8.0 and documentation is absent.

- [ ] **Step 3: Update version and user documentation**

Set 0.9.0 in all three version sources. Document migration, manual verification cost, `.corrupt*` retention, selected repair, no startup full-disk hash, and project-local data boundaries. Do not claim formal hashes until the release pipeline produces them.

- [ ] **Step 4: Run three complete verification rounds**

Run `scripts/test.ps1` three times from the same clean feature commit. Record test counts and durations. Run real-data QA only on a copied project-local database/download tree: baseline both files, alter a copied byte without changing size, detect hash mismatch, repair with controlled media bytes, and verify original recovery data hashes are unchanged.

- [ ] **Step 5: Build and inspect both Windows artifacts**

Run: `scripts/build.ps1`

Run: `scripts/build-installer.ps1 -SkipAppBuild`

Require `PACKAGED_SMOKE_OK` and `INSTALLER_SMOKE_OK`; inspect ZIP entries for zero `data/`, `downloads/`, databases, hashes, `.corrupt*`, credentials, logs, and release secrets. Re-run frozen `--self-test` in an isolated non-C project directory.

- [ ] **Step 6: Commit release evidence and integrate to main**

Commit code/release notes, obtain review, fast-forward local main, and run one fresh full suite on main. Preserve the v0.8.0 direct-run data while creating `dist/release/v0.9.0-portable` from the clean runtime and copied `data`/`downloads`; verify settings/secrets/download hashes before and after.

- [ ] **Step 7: Publish the signed dual-source release**

From clean main run:

```powershell
$env:MODELSCOPE_API_TOKEN = (Get-Content .release-secrets/modelscope-api-token.txt -Raw).Trim()
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/release/release.ps1 -Version 0.9.0
```

Require `RELEASE_PUBLISHED v0.9.0`. Independently verify GitHub main, ModelScope source, both tag objects, seven GitHub assets, both latest pointers, Ed25519 manifest signature, runtime/installer SHA-256, and the program's own `UpdateCoordinator` discovering 0.9.0 from both sources when current version is 0.8.0.

- [ ] **Step 8: Record public hashes without moving the release tag**

Append formal URLs, sizes, hashes, tag commit, direct-run QA, and D/F volume status to the verification document. Commit the evidence after the tag and push only GitHub `main` and ModelScope `source`; prove the release tag still points to the release code commit.
