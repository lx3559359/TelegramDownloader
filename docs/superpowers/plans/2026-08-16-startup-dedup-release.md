# TelegramDownloader v0.8.0 Startup, Dedup, and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver v0.8.0 with immediate startup feedback, atomic global deduplication for ordinary links, constant-size item-state lookups, and a verified GitHub/ModelScope stable release.

**Architecture:** A small PySide-only startup indicator is created before importing the full application and is passed into `app.run()` through a protocol-style interface. `TaskPlanner` filters existing media before preview and uses the repository's transactional deduplicating insert at commit time, while `DownloadScheduler` reads one item instead of a whole task during status changes. Existing signed dual-source release tooling remains the only promotion path.

**Tech Stack:** Python 3.12, PySide6, qasync, Telethon, SQLite WAL, pytest/pytest-qt, Ruff, PyInstaller, Inno Setup, Ed25519, GitHub CLI, ModelScope Hub.

---

## File map

- Create `src/telegram_downloader/startup.py`: minimal, project-data-free Qt startup indicator.
- Modify `src/telegram_downloader/__main__.py`: create startup feedback only for normal GUI launches before importing the full app.
- Modify `src/telegram_downloader/app.py`: update startup phases, show the main window before controller initialization, and always close the indicator.
- Create `tests/test_startup.py`: indicator rendering, status, finish, and idempotent close tests.
- Modify `tests/test_app.py`: startup ordering, duplicate-instance cleanup, and failure cleanup.
- Modify `src/telegram_downloader/planner.py`: preview-time filtering and atomic commit-time deduplication.
- Modify `src/telegram_downloader/controller.py`: consume `SelectedCommit`, report accepted/duplicate counts, and avoid starting empty tasks.
- Modify `tests/test_planner.py`, `tests/test_controller.py`, `tests/test_task_management_e2e.py`: ordinary-link duplicate and race coverage.
- Modify `src/telegram_downloader/scheduler.py`, `tests/test_scheduler.py`: use one-item repository lookups for state transitions.
- Modify `pyproject.toml`, `src/telegram_downloader/__init__.py`, `installer/TelegramDownloader.iss`, `TelegramDownloader.spec`, `README.md`, and packaging tests: v0.8.0 metadata and behavior contract.
- Create `docs/releases/v0.8.0.md`: cumulative public release notes.
- Create `docs/verification/v0.8.0-startup-dedup-release.md`: local, real-account, artifact, update, and remote evidence without private content.

### Task 1: Show startup feedback before loading the full application

**Files:**
- Create: `src/telegram_downloader/startup.py`
- Create: `tests/test_startup.py`
- Modify: `src/telegram_downloader/__main__.py`
- Modify: `src/telegram_downloader/app.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write failing startup indicator tests**

Add tests that create the indicator under pytest-qt, update its message, finish against a host window, and close it twice:

```python
def test_startup_indicator_is_visible_updates_and_closes(qtbot) -> None:
    indicator = create_startup_indicator()
    qtbot.addWidget(indicator.widget)
    assert indicator.widget.isVisible()

    indicator.set_status("正在准备本地数据…")
    assert indicator.status == "正在准备本地数据…"

    host = QWidget()
    qtbot.addWidget(host)
    host.show()
    indicator.finish(host)
    assert indicator.widget.isVisible() is False
    indicator.close()
    indicator.close()
```

Add source-path tests proving self-test and health-check branches are evaluated before `_run_gui(root)` and `_run_gui` accepts injected factory/runner objects without importing the full application.

- [ ] **Step 2: Run the startup tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_startup.py tests/test_app.py -q
```

Expected: import/attribute failures for `telegram_downloader.startup`, `_run_gui`, and the startup-indicator parameter to `app.run()`.

- [ ] **Step 3: Implement the minimal startup component**

Create a wrapper around a generated `QPixmap` and `QSplashScreen`:

```python
class StartupIndicator:
    def __init__(self, application: QApplication, widget: QSplashScreen) -> None:
        self.application = application
        self.widget = widget
        self.status = ""
        self._closed = False

    def set_status(self, text: str) -> None:
        if self._closed:
            return
        self.status = text
        self.widget.showMessage(
            text,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
            QColor("#9fb4ca"),
        )
        self.application.processEvents()

    def finish(self, window: QWidget) -> None:
        if not self._closed:
            self.widget.finish(window)
            self._closed = True

    def close(self) -> None:
        if not self._closed:
            self.widget.close()
            self._closed = True
```

`create_startup_indicator()` must reuse `QApplication.instance()`, draw a 520×240 dark cyan-accented pixmap, show it centered, set the first status, and process events. It must not open files, create directories, or read application settings.

- [ ] **Step 4: Add lazy GUI bootstrap and startup phases**

Keep all Qt imports out of the self-test path:

```python
def _default_startup_factory():
    from telegram_downloader.startup import create_startup_indicator

    return create_startup_indicator()


def _run_gui(root: Path, *, startup_factory=None, runner=None) -> int:
    indicator = (startup_factory or _default_startup_factory)()
    try:
        indicator.set_status("正在加载运行组件…")
        if runner is None:
            from telegram_downloader.app import run as runner
        return runner(root, startup_indicator=indicator)
    finally:
        indicator.close()
```

Change `app.run()` to accept `startup_indicator: object | None = None`. Before `create_application()`, set “正在准备本地数据…”. In the loop task, set “正在恢复任务与账号…”, show the window, finish the indicator, and only then await controller startup:

```python
async def start_application() -> None:
    _startup_status(startup_indicator, "正在恢复任务与账号…")
    controller.window.show()
    _startup_finish(startup_indicator, controller.window)
    await controller.start()
```

Use small helper functions guarded by `getattr` so test doubles and a failed indicator never prevent application startup. Close the indicator on duplicate-instance return and in `finally`.

- [ ] **Step 5: Verify startup ordering and cleanup**

Extend `tests/test_app.py`:

```python
def test_main_window_is_shown_before_controller_start() -> None:
    source = getsource(app.run)
    assert source.index("controller.window.show()") < source.index(
        "await controller.start()"
    )


def test_duplicate_instance_closes_startup_indicator(tmp_path, monkeypatch) -> None:
    indicator = RecordingStartupIndicator()
    assert app.run(tmp_path, Guard(False), startup_indicator=indicator) == 2
    assert indicator.closed is True
```

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_startup.py tests/test_app.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/startup.py src/telegram_downloader/__main__.py src/telegram_downloader/app.py tests/test_startup.py tests/test_app.py
```

Expected: all selected tests and Ruff pass.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/startup.py src/telegram_downloader/__main__.py src/telegram_downloader/app.py tests/test_startup.py tests/test_app.py
git commit -m "feat: show startup progress immediately"
```

### Task 2: Filter ordinary-link duplicates before confirmation

**Files:**
- Modify: `src/telegram_downloader/planner.py`
- Modify: `tests/test_planner.py`

- [ ] **Step 1: Write failing preview-time duplicate tests**

Add two tests with a `FakeRepository.existing` media key:

```python
@pytest.mark.asyncio
async def test_scan_filters_media_already_present_in_any_task(tmp_path: Path) -> None:
    first, duplicate = remote(9, "m9"), remote(8, "m8")
    repo = FakeRepository()
    repo.existing = {("peer", 8, "m8")}
    planner = TaskPlanner(FakeGateway([first, duplicate]), repo, tmp_path)

    preview = await planner.scan(source(), filters())

    assert [(item.message_id, item.media_id) for item in preview.items] == [(9, "m9")]


@pytest.mark.asyncio
async def test_scan_explains_when_every_media_item_is_already_queued(tmp_path: Path) -> None:
    duplicate = remote(8, "m8")
    repo = FakeRepository()
    repo.existing = {("peer", 8, "m8")}
    planner = TaskPlanner(FakeGateway([duplicate]), repo, tmp_path)

    with pytest.raises(EmptyScanError, match="已全部存在于下载队列"):
        await planner.scan(source(), filters())
```

Keep the existing empty-gateway test asserting “没有找到” so the two causes remain distinct.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_planner.py -q
```

Expected: the duplicate remains in the preview and the fully duplicate case does not raise the dedicated error.

- [ ] **Step 3: Implement distinct empty and duplicate filtering**

In `scan()`, reject an actually empty Telegram result before calling `_build_preview`, then call it with `skip_existing=True` and a duplicate-specific empty message:

```python
remote = [item async for item in self.gateway.scan(source, filters)]
if not remote:
    raise EmptyScanError("筛选范围内没有找到可下载媒体")
return self._build_preview(
    # existing values
    empty_message="扫描媒体已全部存在于下载队列",
    skip_existing=True,
)
```

Do not use file existence as the duplicate fact; only the repository media key is authoritative.

- [ ] **Step 4: Run focused tests and Ruff**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_planner.py tests/test_repository.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/planner.py tests/test_planner.py
```

Expected: pass.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/planner.py tests/test_planner.py
git commit -m "fix: filter duplicate link media before preview"
```

### Task 3: Make ordinary-link commit atomically deduplicating

**Files:**
- Modify: `src/telegram_downloader/planner.py`
- Modify: `src/telegram_downloader/controller.py`
- Modify: `tests/test_planner.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_task_management_e2e.py`

- [ ] **Step 1: Write failing commit-race tests**

Using a real `TaskRepository`, build a two-item preview, insert one item through another task after preview, and assert `commit()` returns one accepted key and one skipped item. Add a fully lost race case that raises `EmptyScanError` and leaves no draft/empty task:

```python
committed = planner.commit(preview)
assert committed.accepted_keys == frozenset({("peer", 9, "m9")})
assert committed.skipped_count == 1
assert [item.message_id for item in repo.list_items(preview.task.id)] == [9]
```

Add an end-to-end assertion that archived media also makes a later ordinary-link scan fully duplicate.

- [ ] **Step 2: Run planner and E2E tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_planner.py tests/test_task_management_e2e.py -q
```

Expected: `commit()` returns a `TaskRecord`, calls non-deduplicating insertion, or creates duplicate rows.

- [ ] **Step 3: Unify both commit entry points**

Make `commit()` return `SelectedCommit` and delegate to the same implementation as `commit_selected()`:

```python
def commit(self, preview: ScanPreview) -> SelectedCommit:
    return self._commit_deduplicating(preview)


def commit_selected(self, preview: ScanPreview) -> SelectedCommit:
    return self._commit_deduplicating(preview)
```

`_commit_deduplicating()` must call `create_task_deduplicating()`, translate `AllMediaAlreadyExists` to `EmptyScanError("扫描媒体已全部存在于下载队列")`, and compute accepted keys/skipped count. No path may call `create_task()` for a user-created media task.

- [ ] **Step 4: Update controller behavior and tests**

Change ordinary scan commit handling:

```python
committed = self.planner.commit(preview)
self.refresh_tasks()
self._start_task(committed.task.id)
self._show_status(
    f"任务已加入下载队列：加入 {len(committed.accepted_keys)} 项，"
    f"跳过重复 {committed.skipped_count} 项"
)
```

Update planner doubles in controller tests to return `SelectedCommit`. Add assertions that a fully duplicate commit does not call `_start_task`, and a partial race starts exactly the accepted task once with a visible count.

- [ ] **Step 5: Run all affected tests and Ruff**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_planner.py tests/test_controller.py tests/test_task_management_e2e.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/planner.py src/telegram_downloader/controller.py tests/test_planner.py tests/test_controller.py tests/test_task_management_e2e.py
```

Expected: pass.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/planner.py src/telegram_downloader/controller.py tests/test_planner.py tests/test_controller.py tests/test_task_management_e2e.py
git commit -m "fix: commit link tasks with atomic deduplication"
```

### Task 4: Remove whole-task reads from item status transitions

**Files:**
- Modify: `src/telegram_downloader/scheduler.py`
- Modify: `tests/test_scheduler.py`

- [ ] **Step 1: Write a failing query-shape test**

Extend the scheduler fake with `get_item_calls` and `list_item_calls`. Run one transient retry and assert item-state updates use one-item lookups while task-level setup/finalization remains allowed:

```python
assert repo.get_item_calls == ["i0", "i0", "i0"]
assert all(call.statuses is not None for call in repo.list_item_calls)
```

The exact `get_item_calls` count should match the state transitions produced by the focused test; the invariant is that `_set_item_state()` never calls unfiltered `list_items(task_id)` to find one item.

- [ ] **Step 2: Run scheduler tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scheduler.py -q
```

Expected: the fake has no `get_item()` call and records whole-task reads during each transition.

- [ ] **Step 3: Use the existing repository single-item API**

Extend `SchedulerRepository`:

```python
def get_item(self, item_id: str) -> MediaItem: ...
```

Replace the linear search:

```python
current = self.repository.get_item(item_id)
self.repository.update_item_progress(
    item_id,
    current.downloaded_bytes,
    status,
    error=error,
    retry_count=retry_count,
)
```

Do not catch `KeyError`; a missing media row is a repository invariant violation and must follow the existing guarded-item error path.

- [ ] **Step 4: Run scheduler/repository regression and Ruff**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scheduler.py tests/test_repository.py tests/test_downloader.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/scheduler.py tests/test_scheduler.py
```

Expected: pass.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/scheduler.py tests/test_scheduler.py
git commit -m "perf: query one media item during state changes"
```

### Task 5: Prepare cumulative v0.8.0 release metadata

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/telegram_downloader/__init__.py`
- Modify: `installer/TelegramDownloader.iss`
- Modify: `TelegramDownloader.spec`
- Modify: `README.md`
- Modify: `tests/test_packaging_contract.py`
- Modify: `tests/test_self_test.py`
- Create: `docs/releases/v0.8.0.md`

- [ ] **Step 1: Write failing v0.8.0 packaging assertions**

Rename the version contract test and require `0.8.0` in project, package, installer, startup module inclusion, and README behavior phrases:

```python
assert project["project"]["version"] == "0.8.0"
assert '__version__ = "0.8.0"' in package_init
assert '#define AppVersion "0.8.0"' in installer
assert '"telegram_downloader.startup"' in spec
assert "普通链接、搜索和订阅统一去重" in readme
```

- [ ] **Step 2: Run contract tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_packaging_contract.py tests/test_self_test.py -q
```

Expected: fail on v0.7.0 and missing startup/release documentation.

- [ ] **Step 3: Update metadata and release notes**

Set every version declaration to 0.8.0 and explicitly add `telegram_downloader.startup` to PyInstaller hidden imports. Update README startup/dedup descriptions.

Create cumulative public notes containing these sections:

```markdown
# TelegramDownloader v0.8.0

## 启动与下载可靠性
- 启动加载阶段立即显示状态，主窗口不等待账号恢复。
- 普通链接、相册、搜索和订阅统一按 Telegram 媒体键去重。
- 下载状态更新只读取目标媒体项。

## 首次公开的累计功能
- 自动订阅与只读诊断。
- 账号内容搜索、预览和选择性入队。
- 任务筛选、多选操作、明细和可恢复归档。

## 数据安全
- 所有业务数据继续位于应用目录。
- 更新继续使用 GitHub/魔搭双源、Ed25519 和 SHA-256。
```

- [ ] **Step 4: Run the complete source suite**

```powershell
.\scripts\test.ps1
```

Expected: all tests plus the new tests pass; Ruff prints `All checks passed!`.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml src/telegram_downloader/__init__.py installer/TelegramDownloader.iss TelegramDownloader.spec README.md docs/releases/v0.8.0.md tests/test_packaging_contract.py tests/test_self_test.py
git commit -m "release: prepare TelegramDownloader 0.8.0"
```

### Task 6: Run realistic QA and build Windows artifacts

**Files:**
- Create: `docs/verification/v0.8.0-startup-dedup-release.md`
- Do not commit: `.build-temp/v080-*`, `.local/temp/v080-*`

- [ ] **Step 1: Run automated verification round 1**

```powershell
.\scripts\test.ps1
```

Expected: zero failures and Ruff clean. Record the exact test count and duration.

- [ ] **Step 2: Run isolated saved-session and real-link QA**

Copy only encrypted settings/session to a project-local ignored QA root and redirect `TEMP`, `TMP`, `APPDATA`, and `LOCALAPPDATA` under it. Without printing private values, verify:

```text
saved_session_connected=true
qr_requested=false
dialog_sync_positive=true
real_link_preview_count=2
real_download_completed=true
download_sizes_match=true
all_paths_local=true
second_scan_fully_duplicate=true
task_count_unchanged_after_duplicate=true
file_count_unchanged_after_duplicate=true
```

If Telegram returns FloodWait for content search, record the safe rate-limit handling as an external condition; do not retry before the supplied wait expires.

- [ ] **Step 3: Capture startup and main-window visual evidence**

Use Qt-driven screenshots in ignored project directories. Verify the startup indicator is visible before backend initialization and inspect 1280×780 plus 1180×720 main windows for clipping, overlap, disabled-action state, task filters, and media details. Commit only aggregate observations.

- [ ] **Step 4: Build and smoke test packages (verification round 2)**

```powershell
.\scripts\build-installer.ps1
```

Expected: full tests/Ruff pass, `PACKAGED_SMOKE_OK`, `INSTALLER_SMOKE_OK`, and artifacts:

```text
dist/TelegramDownloader-0.8.0-win-x64-portable.zip
dist/release/TelegramDownloader-0.8.0-win-x64-setup.exe
dist/TelegramDownloader/TelegramDownloader.exe
```

- [ ] **Step 5: Audit artifacts and run verification round 3**

Audit ZIP entry names for zero `data/`, `downloads/`, settings, secrets, sessions, databases, logs, self-test reports, release secrets, and QA evidence. Verify PyInstaller contains startup/planner/scheduler/controller modules. Run packaged `--self-test` with `Start-Process -Wait` and require version 0.8.0, catalog schema 3, all components true, and all 13 writable paths under runtime root.

Then run:

```powershell
.\scripts\test.ps1
```

Expected: same test count as round 1 and Ruff clean.

- [ ] **Step 6: Write and commit local verification evidence**

Document the three rounds, root causes, requirement matrix, privacy-safe real QA booleans, startup timing/visual observations, exact artifact bytes/SHA-256, ZIP privacy count, installer behavior, and all known limits.

```powershell
git add docs/verification/v0.8.0-startup-dedup-release.md
git commit -m "docs: verify TelegramDownloader 0.8.0"
```

### Task 7: Verify cross-version update and integrate locally

**Files:**
- Modify after verification: `docs/verification/v0.8.0-startup-dedup-release.md`
- Do not commit: `.build-temp/v080-update-e2e/**`

- [ ] **Step 1: Create signed local candidate documents**

Build `dist/release/v0.8.0` from exact local artifacts, create a source archive with `git archive`, and run `scripts.release.generate_manifest` with the project-local Ed25519 key. Verify the generated manifest immediately with `trusted_update_keys.json` and record no key material.

- [ ] **Step 2: Exercise the v0.4.0 → v0.8.0 updater path**

Download the public v0.4.0 portable ZIP into `.build-temp/v080-update-e2e`, extract it, add synthetic `data/sentinel.keep`, database, cache, and download files, then apply the local signed v0.8.0 runtime package through `UpdateHelper.exe`/the production update transaction.

Require:

```text
updated_version=0.8.0
health_check_exit=0
sentinel_preserved=true
database_preserved=true
download_preserved=true
runtime_manifest_matches=true
rollback_artifacts_clean=true
all_paths_local=true
```

The test root must resolve under the project before any cleanup or replacement.

- [ ] **Step 3: Fast-forward local main and verify merged source**

Use the finishing-development-branch workflow selected by the user's standing instruction: fast-forward `main`, run `scripts/test.ps1`, and keep remote state unchanged until all release gates pass.

- [ ] **Step 4: Upgrade the direct-run directory without user-data loss**

Create `dist/release/v0.8.0-portable` from v0.7.0 user data plus v0.8.0 managed runtime files, never overwrite the old v0.7.0 directory. Compare settings/secrets hashes, verify the tasks archive column, run explicit-wait self-test, and require all writable paths under the new direct root.

- [ ] **Step 5: Commit final local integration evidence**

Append cross-version and direct-run results to the verification document and commit on main.

```powershell
git add docs/verification/v0.8.0-startup-dedup-release.md
git commit -m "docs: record v0.8.0 update integration"
```

### Task 8: Publish GitHub and ModelScope stable v0.8.0

**Files:**
- Generated, ignored: `dist/release/v0.8.0/**`
- Modify after publication: `docs/verification/v0.8.0-startup-dedup-release.md`

- [ ] **Step 1: Run the formal release preflight**

Require clean `main`, source version 0.8.0, project-local Ed25519 private key present, GitHub authenticated, ModelScope token loaded into the release process from `.release-secrets/modelscope-api-token.txt` without printing it, and both remotes configured. Confirm `v0.8.0` does not already exist remotely.

- [ ] **Step 2: Execute the transactional release**

```powershell
$env:MODELSCOPE_API_TOKEN = (Get-Content -Raw -LiteralPath '.release-secrets\modelscope-api-token.txt').Trim()
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\release\release.ps1 -Version 0.8.0
```

Expected terminal output: `RELEASE_PUBLISHED v0.8.0`. The script must rerun tests/build, create and push the tag, stage both platforms, redownload every candidate byte, compare it locally, promote ModelScope, publish GitHub latest, and verify both pointers.

- [ ] **Step 3: Independently verify remote state and update discovery**

Require:

```text
github_main == modelscope_source == local_main
github_tag_v0.8.0 == local_main
modelscope_tag_v0.8.0 == local_main
github_release_public=true
github_release_latest=true
github_assets_exact=true
modelscope_pointer_version=0.8.0
remote_asset_hashes_match=true
v0.7.0_offered_version=0.8.0
update_sources=github,modelscope
update_blocked=false
```

Use `scripts.release.verify_remote_release` plus a fresh `UpdateCoordinator` check. Cite the public GitHub release and ModelScope model page in final evidence.

- [ ] **Step 4: Record publication and commit/push the evidence**

Append the exact release commit, tag, remote pointer results, asset hashes, URLs, and post-publish update check to the verification document. Commit, push `main` to GitHub and `HEAD:source` to ModelScope, and verify those final evidence commits remotely. Do not retag v0.8.0 after publication; the tag remains the exact release source commit, while the evidence-only commit may follow it.

- [ ] **Step 5: Cleanup owned QA/worktree data**

After all remote and local evidence is copied, validate the exact owned worktree/QA paths under `.worktrees`, `.build-temp`, or `.local/temp`; remove only those paths, prune the owned worktree, and delete the merged feature branch. Preserve v0.7.0 and v0.8.0 direct-run directories and formal release artifacts.

## Stop conditions

- Any failing test, Ruff error, package smoke failure, C-drive acceptance, privacy match, path escape, hash mismatch, signature failure, remote-byte mismatch, or pointer inconsistency stops promotion.
- Do not publish a draft, candidate, tag, or stable pointer from an unclean or unverified source tree.
- Do not retry Telegram FloodWait before its required delay.
- Do not print API Hash, Session, private signing key, ModelScope token, account/group identifiers, message content, file names, or target paths in committed evidence.
