# v0.4.2 Real QA Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the verified shutdown connection leak and make search/task actions reflect what the user can actually do.

**Architecture:** Keep the scheduler, result model, and main window boundaries unchanged. Add one scheduler configuration value for graceful shutdown, derive search bulk-action state from result eligibility, and derive task actions from the selected `TaskSummary` status.

**Tech Stack:** Python 3.12, asyncio/qasync, PySide6, Telethon, pytest, pytest-qt, Ruff.

---

### Task 1: Allow active Telegram downloads to settle during shutdown

**Files:**
- Modify: `tests/test_scheduler.py`
- Modify: `src/telegram_downloader/scheduler.py`

- [x] **Step 1: Write the failing scheduler tests**

Add a test that constructs `DownloadScheduler(..., shutdown_grace_seconds=0.1)`, starts a pause-aware downloader that needs more than one event-loop turn to observe the pause flag, calls `shutdown()`, and asserts the downloader was not cancelled and the repository ends paused. Add validation cases for zero and negative values, and assert the production default is 30 seconds.

- [x] **Step 2: Run the focused tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_scheduler.py -q`

Expected: FAIL because `shutdown_grace_seconds` is not accepted or exposed.

- [x] **Step 3: Implement the minimum scheduler change**

Store a positive `shutdown_grace_seconds: float = 30.0` constructor value and replace the hard-coded `timeout=5` in `shutdown()` with it. Preserve the existing timeout cancellation fallback.

- [x] **Step 4: Run the focused tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_scheduler.py -q`

Expected: all scheduler tests pass.

- [x] **Step 5: Commit the scheduler fix**

Run: `git add tests/test_scheduler.py src/telegram_downloader/scheduler.py && git commit -m "fix: let Telegram downloads settle on shutdown"`

### Task 2: Explain non-selectable search results

**Files:**
- Modify: `tests/ui/test_content_browser.py`
- Modify: `src/telegram_downloader/ui/content_browser.py`

- [x] **Step 1: Write the failing Qt test**

Create a page with two queued results and one unavailable result, then assert “全选”和“反选” are disabled and `selection_summary` contains `2 项已入队` and `1 项不可用`. Extend the mixed-results test to assert bulk actions remain enabled when at least one eligible result exists.

- [x] **Step 2: Run the focused test and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_browser.py -q`

Expected: FAIL because bulk buttons are enabled whenever the result list is non-empty and the summary omits exclusion counts.

- [x] **Step 3: Implement derived eligibility and summary text**

In `_refresh_actions()`, compute whether any result is available and not queued, and use that value for both bulk buttons. In `_update_selection_summary()`, append queued and unavailable counts without changing the selected-size calculation.

- [x] **Step 4: Run focused UI tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_browser.py tests/ui/test_content_models.py -q`

Expected: all selected UI tests pass.

- [x] **Step 5: Commit the search feedback fix**

Run: `git add tests/ui/test_content_browser.py src/telegram_downloader/ui/content_browser.py && git commit -m "fix: explain search results that cannot be queued"`

### Task 3: Enable only valid task actions

**Files:**
- Modify: `tests/ui/test_main_window.py`
- Modify: `src/telegram_downloader/ui/main.py`

- [x] **Step 1: Write the failing task-state test**

Parameterize task summaries for `queued`, `downloading`, `waiting_retry`, `paused`, `completed`, and `partial_failure`. After selecting the row, assert the exact `(pause, resume, retry, open)` enabled tuple is respectively `(True, False, False, True)`, `(True, False, False, True)`, `(True, False, False, True)`, `(False, True, False, True)`, `(False, False, False, True)`, and `(False, False, True, True)`.

- [x] **Step 2: Run the focused test and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ui/test_main_window.py -q`

Expected: FAIL because all four actions are currently enabled for every selected task.

- [x] **Step 3: Implement selected-summary lookup and status mapping**

Resolve the selected task ID against `TaskTableModel.task_at()`. Enable each button using the status mapping from the design, and disable all buttons if the selection is missing or stale.

- [x] **Step 4: Run focused UI tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ui/test_main_window.py -q`

Expected: all main-window tests pass.

- [x] **Step 5: Commit the task-action fix**

Run: `git add tests/ui/test_main_window.py src/telegram_downloader/ui/main.py && git commit -m "fix: match task actions to task status"`

### Task 4: Treat signed older releases as no update

**Files:**
- Modify: `tests/update/test_update_coordinator.py`
- Modify: `src/telegram_downloader/update.py`

- [x] **Step 1: Write the failing coordinator tests**

Create signed dual-source documents for version `0.3.1` with current version `0.4.2`. Assert `check_for_update()` returns no manifest, `blocked` is false, and both sources remain available. Add a newer `0.5.0` manifest requiring updater `0.4.3` and assert startup remains blocked.

- [x] **Step 2: Run the focused tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/update/test_update_coordinator.py -q`

Expected: the older-release case raises source reconciliation failure because both valid signed sources are prematurely classified as invalid.

- [x] **Step 3: Separate source verification from install policy**

Verify each source without `installed_version`. After `reconcile_sources()`, return no update for older/equal releases as it already specifies; for a selected newer manifest, raise `UpdatePolicyError` if the current version is lower than `minimum_updater_version`.

- [x] **Step 4: Run update tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/update tests/ui/test_update_dialog.py -q`

Expected: all update and update-dialog tests pass.

- [x] **Step 5: Commit the update discovery fix**

Run: `git add tests/update/test_update_coordinator.py src/telegram_downloader/update.py && git commit -m "fix: treat older signed releases as no update"`

### Task 5: Verify the integrated candidate and real restart path

**Files:**
- Modify: `docs/verification/2026-08-15-session-performance-content-ux-checklist.md`

- [x] **Step 1: Run static and full automated verification**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Run: `.\.venv\Scripts\python.exe -m ruff check src tests scripts`

Expected: all tests pass and Ruff reports no errors.

- [x] **Step 2: Repeat the crash/recovery probe**

Use the ignored `.build-temp/manual-qa/crash_probe.py` and `.build-temp/manual-qa/recovery_probe.py` against `dist/TelegramDownloader`. Expected: session restores without QR, recovered task becomes queued then resumes, bytes are preserved, pause settles, gateway disconnects, and `pending_after_shutdown` is empty.

- [x] **Step 3: Repeat the search audit**

Run `.build-temp/manual-qa/qt_human_audit.py` against the real local runtime. Expected: search returns results, preview opens, and when every result is already queued both bulk buttons are disabled with an explicit queued count.

- [x] **Step 4: Update verification evidence and commit**

Record aggregate outcomes without account names, message text, secrets, or external publication. Commit only tracked source, tests, and verification documents.

- [x] **Step 5: Rebuild portable and installer artifacts**

Run: `powershell -ExecutionPolicy Bypass -File scripts/build.ps1`

Run: `powershell -ExecutionPolicy Bypass -File scripts/build-installer.ps1`

Expected: both smoke checks pass, runtime user-data hashes remain unchanged, and the portable ZIP contains no `data/` or `downloads/` entries.
