# TelegramDownloader v0.4.2 Three-Pass Real QA Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute three independent, progressively harder real-use QA loops, repair every reproducible defect with TDD, rebuild both Windows distributions, and merge the verified result locally without publishing it.

**Architecture:** Reuse the project-local packaged runtime and its encrypted saved Telegram session. Keep diagnostic scripts and raw screenshots under ignored `.build-temp/manual-qa`, store only aggregate privacy-safe evidence in `docs/verification`, and gate every production edit behind a focused failing test.

**Tech Stack:** Python 3.12, asyncio/qasync, PySide6/QTest, Telethon, pytest, Ruff, PyInstaller, PowerShell, Inno Setup.

---

### Task 1: Freeze the baseline and add a safe online-update probe

**Files:**
- Create (ignored): `.build-temp/manual-qa/update_probe.py`
- Read: `src/telegram_downloader/update.py`
- Read: `src/telegram_downloader/update_contract.py`

- [ ] **Step 1: Record the clean source and runtime baseline**

Run:

```powershell
git status --short --branch
git rev-parse HEAD
Get-Process -Name TelegramDownloader -ErrorAction SilentlyContinue
```

Expected: branch is `codex/three-pass-qa-v042`, no tracked changes, and no packaged application process is running.

- [ ] **Step 2: Fingerprint project-local runtime data without reading contents**

Run a PowerShell block that enumerates files below `dist\TelegramDownloader\data` and `dist\TelegramDownloader\downloads`, hashes each file, and writes only relative path, size, and SHA-256 into `.build-temp\manual-qa\baseline-data.json`. The output path and every enumerated file must resolve below the worktree root.

Expected: JSON contains no credential values or message text and every source path starts with the worktree root.

- [ ] **Step 3: Create the ignored real-source update probe**

Create `.build-temp/manual-qa/update_probe.py` with this behavior:

```python
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from telegram_downloader import __version__
from telegram_downloader.paths import PortablePaths
from telegram_downloader.update import HttpBytesClient, UpdateCoordinator
from telegram_downloader.update_contract import load_trusted_keys
from telegram_downloader.update_download import ResumableUpdateDownloader
from telegram_downloader.update_sources import UpdateSourceId


async def probe(root: Path) -> dict[str, object]:
    coordinator = UpdateCoordinator(
        PortablePaths(root),
        __version__,
        load_trusted_keys(
            Path(__file__).resolve().parents[2]
            / "src"
            / "telegram_downloader"
            / "trusted_update_keys.json"
        ),
        HttpBytesClient(),
        ResumableUpdateDownloader(),
    )
    checks = await asyncio.gather(
        coordinator._check_source(UpdateSourceId.GITHUB),
        coordinator._check_source(UpdateSourceId.MODELSCOPE),
    )
    update = await coordinator.check_for_update()
    return {
        "current_version": __version__,
        "blocked": update.blocked,
        "offered_version": update.version,
        "available_sources": sorted(source.value for source in update.available_sources),
        "sources": {
            check.source.value: {
                "status": check.status.value,
                "version": (
                    check.verified.manifest.version
                    if check.verified is not None
                    else None
                ),
            }
            for check in checks
        },
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe(Path(sys.argv[1]).resolve())), sort_keys=True))
```

- [ ] **Step 4: Verify the update probe against both real sources**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
.\.venv\Scripts\python.exe .build-temp\manual-qa\update_probe.py dist\TelegramDownloader
```

Expected: both `github` and `modelscope` are `valid`; for local `0.4.2` and remote `0.4.0`, `blocked` is false and `offered_version` is null.

### Task 2: Run pass one — normal user path

**Files:**
- Use (ignored): `.build-temp/manual-qa/qt_human_audit.py`
- Use (ignored): `.build-temp/manual-qa/download_probe.py`
- Use (ignored): `.build-temp/manual-qa/update_probe.py`
- Test: `tests/`

- [ ] **Step 1: Run the complete automated baseline**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

Expected: all tests pass and Ruff reports no errors.

- [ ] **Step 2: Run the first real GUI journey**

Run with `PYTHONPATH=src`, `QT_QPA_PLATFORM=offscreen`, runtime root `dist\TelegramDownloader`, and screenshot folder `.build-temp\manual-qa\round-1`:

```powershell
.\.venv\Scripts\python.exe .build-temp\manual-qa\qt_human_audit.py dist\TelegramDownloader .build-temp\manual-qa\round-1
```

Expected: saved session restores without login dialog; public-link scan, content activation, group refresh, search, preview and queue-state recovery finish; no step times out.

- [ ] **Step 3: Run the first real download/pause cycle**

Run:

```powershell
.\.venv\Scripts\python.exe .build-temp\manual-qa\download_probe.py dist\TelegramDownloader
```

Expected: session restores, resume starts, downloaded byte count increases or the task reaches a terminal state, and the task is paused before shutdown.

- [ ] **Step 4: Run the first independent crash/recovery cycle**

Run `crash_probe.py` and accept exit code 23 only after its safe JSON confirms session restore, resume start and byte growth. Immediately run `recovery_probe.py` and require queued recovery, auto-resume, preserved bytes, paused settlement, disconnected gateway, zero scheduler-active tasks and an empty pending-task list.

- [ ] **Step 5: Recheck online update and installer behavior**

Run the update probe, then:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\smoke-installer.ps1 -SetupPath dist\release\TelegramDownloader-0.4.2-win-x64-setup.exe
```

Expected: both update sources are valid and installer output ends with `INSTALLER_SMOKE_OK`.

- [ ] **Step 6: Record pass-one aggregate evidence**

Write only boolean outcomes, counts, durations, byte deltas and error class names to the verification record. Do not record Telegram names, usernames, keywords, links, captions or credentials.

### Task 3: Run pass two — repeated interaction and crash recovery

**Files:**
- Use (ignored): `.build-temp/manual-qa/qt_human_audit.py`
- Use (ignored): `.build-temp/manual-qa/crash_probe.py`
- Use (ignored): `.build-temp/manual-qa/recovery_probe.py`
- Test: `tests/ui/test_async_actions.py`
- Test: `tests/ui/test_content_browser.py`
- Test: `tests/update/`

- [ ] **Step 1: Re-run the real GUI journey in a new evidence directory**

Use `.build-temp\manual-qa\round-2` and assert that refresh, search and queue buttons expose busy feedback and return to their normal state. Run the focused async-action and content-browser tests after the journey.

Expected: the second session restore also avoids QR login; no action remains in the controller's active-key set.

- [ ] **Step 2: Crash only the dedicated probe process during real download**

Run `crash_probe.py` with the project-local runtime and capture its last JSON line. Accept exit code 23 only when `session_restored`, `resume_started` and `progressed` are true and `bytes_after` is greater than `bytes_before`.

Expected: the main application is not running; only the probe process exits abnormally.

- [ ] **Step 3: Restart and verify recovery plus graceful shutdown**

Run:

```powershell
.\.venv\Scripts\python.exe .build-temp\manual-qa\recovery_probe.py dist\TelegramDownloader
```

Expected: interrupted work is recovered to queue, automatically resumes, preserves bytes, settles to paused during shutdown, leaves zero borrowed senders, zero scheduler-active tasks, a disconnected gateway and an empty `pending_after_shutdown` list.

- [ ] **Step 4: Exercise update failure policies and installer preservation again**

Run all tests under `tests/update`, the real update probe, and `smoke-installer.ps1` against the existing setup.

Expected: valid older releases remain no-update, one unavailable source can degrade safely, invalid or contradictory signed data blocks, minimum-updater policy blocks incompatible newer versions, and installer smoke ends with `INSTALLER_SMOKE_OK`.

- [ ] **Step 5: Record pass-two aggregate evidence**

Record process exit code, byte delta, recovered statuses, pending-task count, shutdown duration, source statuses and installer result without external identifiers.

### Task 4: Repair every reproducible defect with TDD

**Files:**
- Modify: the smallest production module that owns each confirmed root cause
- Test: the focused test module nearest that production module
- Modify: `docs/superpowers/plans/2026-08-15-three-pass-real-qa.md`

- [ ] **Step 1: Stop at the first failing pass assertion and preserve evidence**

Capture the exact command, exit code, final safe JSON report, error class, log timestamp and affected state transition. Re-run only that step once to prove it is reproducible.

- [ ] **Step 2: Trace the failing value across component boundaries**

Identify whether the first incorrect state originates in the Qt page, controller action bridge, content service, repository, scheduler, Telegram gateway, update coordinator or packaging script. Compare it with the nearest working path before editing code.

- [ ] **Step 3: Add one focused regression test and verify RED**

Add a test named for the externally visible behavior, run only that test, and confirm it fails for the observed reason rather than setup or syntax failure. Append the exact test path and expected failure to this plan before touching production code.

- [ ] **Step 4: Implement the minimum root-cause fix and verify GREEN**

Change only the owning component, rerun the focused test, related module tests and the previously failing real-use step. Do not bundle unrelated refactoring.

- [ ] **Step 5: Commit one defect per commit and restart the interrupted pass**

Commit the regression test and minimal production fix together. Restart the current pass from its first step so earlier successful states are not assumed after a code change.

### Task 5: Run pass three — final candidate and rebuild

**Files:**
- Modify: `docs/verification/2026-08-15-three-pass-real-qa-checklist.md`
- Modify: `docs/releases/v0.4.2.md`
- Build: `dist/TelegramDownloader/TelegramDownloader.exe`
- Build: `dist/TelegramDownloader-0.4.2-win-x64-portable.zip`
- Build: `dist/release/TelegramDownloader-0.4.2-win-x64-setup.exe`

- [ ] **Step 1: Run the third real GUI and download journey**

Use `.build-temp\manual-qa\round-3`, then run `download_probe.py` and `update_probe.py` again. Require a third QR-free session restore, completed refresh/search/preview/queue-state path, real download progress or terminal completion, successful pause, and two valid update sources.

- [ ] **Step 2: Run the third independent crash/recovery cycle**

Run `crash_probe.py` and `recovery_probe.py` once more. Require new byte growth before the controlled exit and the same complete recovery/shutdown invariants as passes one and two.

- [ ] **Step 3: Snapshot runtime data immediately before rebuilding**

Create a second path/size/SHA-256 inventory under `.build-temp\manual-qa`. Ensure no `TelegramDownloader.exe` process is running before invoking build scripts.

- [ ] **Step 4: Rebuild both deliverables**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
powershell -ExecutionPolicy Bypass -File scripts\build-installer.ps1 -SkipAppBuild
```

Expected: full tests and Ruff pass inside the build, `PACKAGED_SMOKE_OK` and `INSTALLER_SMOKE_OK` are printed, and the portable ZIP and setup exist with version `0.4.2`.

- [ ] **Step 5: Verify data preservation, package privacy and direct EXE startup**

Compare the immediate pre-build inventory with restored `data` and `downloads`; verify no existing file disappeared or changed. Inspect the ZIP entry list for zero `data/` and `downloads/` entries. Start the packaged EXE, wait for a visible window titled `Telegram 下载器`, close it normally, require exit code 0 and confirm no new error log lines or remaining process.

- [ ] **Step 6: Run final full verification and compute artifact hashes**

Run full pytest, Ruff and `git diff --check`. Record byte size and SHA-256 for the EXE, portable ZIP and setup. All commands must exit zero.

- [ ] **Step 7: Commit privacy-safe verification evidence**

The final checklist must contain one table row per required subsystem for each of the three passes, each marked with direct evidence. It must also state project-local data paths, artifact hashes, test count, no-publication boundary and any diagnosed/fixed defects.

### Task 6: Review and merge locally

**Files:**
- Review: all changes from `main..codex/three-pass-qa-v042`

- [ ] **Step 1: Perform pre-merge review**

Inspect the complete source/test/document diff for security, data-path escapes, incorrect state transitions, missing regression coverage and accidental secret inclusion. Resolve every critical or important finding before continuing.

- [ ] **Step 2: Re-run completion gates after the final commit**

Run full pytest, Ruff, `git diff --check`, artifact hash checks and package privacy checks from the healthy worktree. Require a clean worktree.

- [ ] **Step 3: Fast-forward local `main`**

From `D:\Codex Project\Telegram下载器`, run:

```powershell
git merge --ff-only codex/three-pass-qa-v042
```

Do not pull, push, tag, publish or update remote manifests.

- [ ] **Step 4: Verify the merged commit without removing runtime data**

Confirm `main` and the linked worktree resolve to the same commit, tests and Ruff still pass from the healthy worktree, both Git statuses are clean, and no packaged process remains. Preserve the linked worktree because it owns the real project-local session and download data.
