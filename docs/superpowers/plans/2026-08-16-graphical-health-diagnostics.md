# Graphical Health Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build TelegramDownloader v0.10.0 with a cancellable graphical health center and a strictly allowlisted, project-local diagnostic ZIP that can identify path, disk, component, database, credential, Telegram, and signed update-source problems without exposing user data.

**Architecture:** Add immutable diagnostic domain types and a small async orchestrator, keep local/SQLite probes in a separate module, and keep report persistence/export in a privacy-focused store. A dedicated Qt page emits intent-only signals; the existing controller and `AsyncActionBridge` compose live Telegram/update probes without allowing diagnostics to mutate tasks, searches, subscriptions, or downloads.

**Tech Stack:** Python 3.12, asyncio, sqlite3, pathlib, zipfile, PySide6, qasync, pytest/pytest-asyncio/pytest-qt, Ruff, PyInstaller, Inno Setup, Ed25519 signed GitHub/ModelScope release tooling.

---

## File map

- Create `src/telegram_downloader/diagnostics.py`: statuses, immutable results/reports, probe protocols, total-state reduction, cancellation-aware orchestration.
- Create `src/telegram_downloader/diagnostic_probes.py`: runtime/path, write, disk, component, SQLite, credential, Telegram, and update-source probe adapters.
- Create `src/telegram_downloader/diagnostic_store.py`: schema-1 JSON, recursive privacy validation, atomic latest report, conflict-safe two-file ZIP export.
- Create `src/telegram_downloader/ui/diagnostics.py`: result model and diagnostics page only.
- Modify `src/telegram_downloader/paths.py`: guarded diagnostics and diagnostics-temp directories.
- Modify `src/telegram_downloader/update.py`: public read-only `check_sources()` reused by update reconciliation.
- Modify `src/telegram_downloader/controller.py`: run/cancel/export/open orchestration and safe status reporting.
- Modify `src/telegram_downloader/app.py`: dependency construction, signals, async action key, shared self-test component/path helpers.
- Modify `src/telegram_downloader/ui/main.py`: diagnostics navigation and stacked page integration.
- Create `tests/test_diagnostics.py`, `tests/test_diagnostic_probes.py`, `tests/test_diagnostic_store.py`, `tests/test_diagnostics_e2e.py`, and `tests/ui/test_diagnostics.py`.
- Modify controller/app/update/path/self-test/packaging tests for integration and regression coverage.
- Update version sources, README, release notes, plan, and verification evidence for v0.10.0.

### Task 1: Define diagnostic domain and status reduction

**Files:**
- Create: `src/telegram_downloader/diagnostics.py`
- Create: `tests/test_diagnostics.py`

- [ ] **Step 1: Write failing model and reduction tests**

Add tests that require exact status values, immutable result/report objects, unique probe IDs, bounded non-negative durations, JSON-safe scalar metrics, and deterministic total-state precedence.

```python
def test_report_status_prioritizes_cancel_failure_and_warning() -> None:
    passed = result("paths", DiagnosticStatus.PASSED)
    warning = result("disk", DiagnosticStatus.WARNING)
    failed = result("tasks-db", DiagnosticStatus.FAILED)
    assert reduce_status((passed,)) is DiagnosticStatus.PASSED
    assert reduce_status((passed, warning)) is DiagnosticStatus.WARNING
    assert reduce_status((warning, failed)) is DiagnosticStatus.FAILED
    assert reduce_status((failed,), cancelled=True) is DiagnosticStatus.CANCELLED


def test_report_rejects_duplicate_ids_and_sensitive_metric_shapes() -> None:
    with pytest.raises(ValueError, match="重复"):
        DiagnosticReport.build("0.10.0", NOW, NOW, (result("disk"), result("disk")))
    with pytest.raises(ValueError, match="指标"):
        DiagnosticResult("disk", "磁盘", DiagnosticStatus.PASSED, "ok", "正常", 1, {"x": []})
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_diagnostics.py -q`

Expected: collection fails because `telegram_downloader.diagnostics` does not exist.

- [ ] **Step 3: Implement the immutable domain**

Define the exact public types and validation used by later tasks:

```python
class DiagnosticStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


MetricValue = bool | int | float | str


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    id: str
    title: str
    status: DiagnosticStatus
    code: str
    summary: str
    duration_ms: int
    metrics: Mapping[str, MetricValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    schema_version: int
    app_version: str
    started_at: datetime
    finished_at: datetime
    status: DiagnosticStatus
    results: tuple[DiagnosticResult, ...]
```

Implement `reduce_status(results, cancelled=False)` with precedence `cancelled > failed > warning > passed`; skipped results do not lower a report containing a passed result, and an all-skipped report is warning. Validate stable IDs with `^[a-z][a-z0-9-]*$`, UTC timestamps, unique IDs, durations `>= 0`, and scalar metric values.

- [ ] **Step 4: Run domain tests and confirm GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_diagnostics.py -q`

Expected: all Task 1 tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/diagnostics.py tests/test_diagnostics.py
git commit -m "feat: define diagnostic report domain"
```

### Task 2: Add project-local paths and local runtime probes

**Files:**
- Modify: `src/telegram_downloader/paths.py`
- Create: `src/telegram_downloader/diagnostic_probes.py`
- Create: `tests/test_diagnostic_probes.py`
- Modify: `tests/test_paths.py`

- [ ] **Step 1: Write failing path, write, disk, and component tests**

Require `paths.diagnostics == root/data/diagnostics` and `paths.diagnostic_temp == root/data/temp/diagnostics`. Test that the write probe performs write/fsync/read/delete below the root and leaves no file, including a failed-write injected filesystem. Test the disk thresholds exactly: `>= 1 GiB` passes, `256 MiB <= free < 1 GiB` warns, and `< 256 MiB` fails. Test the component helper returns only the six fixed booleans.

```python
def test_disk_probe_uses_fixed_thresholds(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    assert probe_disk(paths, lambda _: usage(10, 2 * GIB)).status is DiagnosticStatus.PASSED
    assert probe_disk(paths, lambda _: usage(10, 512 * MIB)).status is DiagnosticStatus.WARNING
    assert probe_disk(paths, lambda _: usage(10, 128 * MIB)).status is DiagnosticStatus.FAILED


def test_write_probe_cleans_project_local_marker(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    result = probe_project_write(paths, marker=b"diagnostic")
    assert result.status is DiagnosticStatus.PASSED
    assert list(paths.diagnostic_temp.iterdir()) == []
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_paths.py tests/test_diagnostic_probes.py -q`

Expected: missing diagnostics path properties and probe imports fail.

- [ ] **Step 3: Implement guarded paths and pure local probes**

Add properties and layout creation:

```python
@property
def diagnostics(self) -> Path:
    return self.data / "diagnostics"

@property
def diagnostic_temp(self) -> Path:
    return self.temp / "diagnostics"
```

Implement `component_availability(importer=importlib.import_module)`, `probe_environment(paths, *, frozen, windows_x64, system_drive)`, `probe_project_write(paths, marker=None)`, and `probe_disk(paths, usage_provider=shutil.disk_usage)`. Use only fixed summaries/codes; catch exceptions by category without placing `str(exc)` in results. Runtime metrics are only booleans, while disk metrics are integer `totalBytes` and `freeBytes`.

- [ ] **Step 4: Run local probe tests and confirm GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_paths.py tests/test_diagnostic_probes.py -q`

Expected: all focused tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/paths.py src/telegram_downloader/diagnostic_probes.py tests/test_paths.py tests/test_diagnostic_probes.py
git commit -m "feat: probe project-local runtime health"
```

### Task 3: Add read-only database and credential probes

**Files:**
- Modify: `src/telegram_downloader/diagnostic_probes.py`
- Modify: `tests/test_diagnostic_probes.py`

- [ ] **Step 1: Write failing database and credential tests**

Initialize real temporary `TaskRepository` and `CatalogRepository` databases, add representative rows, and require only schema/count metrics. Corrupt a copied database and assert a fixed `database-unreadable` or `database-corrupt` code with no path or SQLite message. Inject credential state containing secret values and assert the result exposes only booleans.

```python
def test_task_database_probe_reports_schema_and_counts_only(tmp_path: Path) -> None:
    repository = TaskRepository(tmp_path / "tasks.sqlite3")
    repository.initialize()
    repository.create_task(task_with_two_items())
    result = probe_task_database(repository.database)
    assert result.status is DiagnosticStatus.PASSED
    assert result.metrics == {
        "taskCount": 1,
        "mediaCount": 2,
        "schemaCompatible": True,
    }


def test_credentials_probe_never_exposes_values() -> None:
    result = probe_credentials(settings_readable=True, secrets_present=True, secrets_decrypted=True)
    assert result.metrics == {
        "settingsReadable": True,
        "secretsPresent": True,
        "secretsDecryptable": True,
    }
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_diagnostic_probes.py -q`

Expected: database and credential probe names are missing.

- [ ] **Step 3: Implement read-only SQLite probes**

Open SQLite using an absolute URI with `mode=ro`, set `busy_timeout`, run `PRAGMA quick_check`, inspect required tables/columns, and issue aggregate count queries only. Task metrics include task/media counts and counts by task, item, and integrity status using stable metric keys. Catalog metrics include account/dialog/search/subscription counts only. Never include row values, database paths, SQL text, or exception text.

Implement `probe_credentials(*, settings_readable, secrets_present, secrets_decrypted)` with boolean metrics and statuses: unreadable settings or undecryptable existing secrets fail; absent secrets warn as “尚未配置账号”; all available values pass.

- [ ] **Step 4: Run probe tests and confirm GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_diagnostic_probes.py -q`

Expected: all local, database, corruption, lock, and privacy tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/diagnostic_probes.py tests/test_diagnostic_probes.py
git commit -m "feat: diagnose local databases and credentials"
```

### Task 4: Add live network probes and cancellable orchestration

**Files:**
- Modify: `src/telegram_downloader/update.py`
- Modify: `src/telegram_downloader/diagnostics.py`
- Modify: `src/telegram_downloader/diagnostic_probes.py`
- Modify: `tests/update/test_update_coordinator.py`
- Modify: `tests/test_diagnostics.py`
- Modify: `tests/test_diagnostic_probes.py`

- [ ] **Step 1: Write failing update-source API and orchestration tests**

Require `UpdateCoordinator.check_sources()` to return both `SourceCheck` values and `check_for_update()` to call it once. Add Telegram outcomes for absent gateway, authorized, session expired, transient network, unexpected safe failure, and cancellation. Add an orchestrator test with deterministic clock/progress proving repeated `run()` calls share one active task and `cancel()` marks remaining checks cancelled.

```python
@pytest.mark.asyncio
async def test_check_for_update_reuses_public_source_checks(tmp_path: Path) -> None:
    coordinator = coordinator_with_documents(tmp_path)
    checks = await coordinator.check_sources()
    assert {item.source for item in checks} == {UpdateSourceId.GITHUB, UpdateSourceId.MODELSCOPE}
    assert all(item.status is SourceStatus.VALID for item in checks)


@pytest.mark.asyncio
async def test_diagnostics_cancel_keeps_completed_results() -> None:
    service = DiagnosticsService((instant_probe("paths"), blocking_probe("telegram")))
    running = asyncio.create_task(service.run())
    await service.wait_until("telegram")
    await service.cancel()
    report = await running
    assert report.results[0].status is DiagnosticStatus.PASSED
    assert report.results[1].status is DiagnosticStatus.CANCELLED
    assert report.status is DiagnosticStatus.CANCELLED
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/update/test_update_coordinator.py tests/test_diagnostics.py tests/test_diagnostic_probes.py -q`

Expected: public source API, service, and network probes are absent.

- [ ] **Step 3: Implement public source checks and async diagnostics service**

Add:

```python
async def check_sources(self) -> tuple[SourceCheck, SourceCheck]:
    github, modelscope = await asyncio.gather(
        self._check_source(UpdateSourceId.GITHUB),
        self._check_source(UpdateSourceId.MODELSCOPE),
    )
    return github, modelscope
```

Make `check_for_update()` call `check_sources()` then existing reconciliation unchanged.

Define `DiagnosticProbe` protocol (`id`, `title`, `async run(cancel_event)`) and `DiagnosticProgress(completed, total, current_id, current_title, status)`. `DiagnosticsService.run(on_progress=None)` uses one protected active task, executes the fixed probe sequence, wraps each duration with an injected monotonic clock, continues after non-cancellation errors with fixed `probe-failed`, and returns a report. `cancel()` sets an event, cancels the active network child, awaits convergence, and is idempotent.

Implement Telegram adapter using `gateway.test_connection()` and fixed mappings for `SessionExpiredError`, `TransientNetworkError`, and safe unknown failures. Implement update adapter using both source checks: invalid is failed, one unavailable is warning, both unavailable is warning, valid sources include only status, discovered version, and integer latency.

- [ ] **Step 4: Run focused network/orchestration tests and confirm GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/update/test_update_coordinator.py tests/test_diagnostics.py tests/test_diagnostic_probes.py -q`

Expected: all focused tests pass and existing update reconciliation remains unchanged.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/update.py src/telegram_downloader/diagnostics.py src/telegram_downloader/diagnostic_probes.py tests/update/test_update_coordinator.py tests/test_diagnostics.py tests/test_diagnostic_probes.py
git commit -m "feat: orchestrate live health diagnostics"
```

### Task 5: Persist and export privacy-safe reports

**Files:**
- Create: `src/telegram_downloader/diagnostic_store.py`
- Create: `tests/test_diagnostic_store.py`

- [ ] **Step 1: Write failing serialization, privacy, and ZIP tests**

Require canonical schema-1 JSON, atomic `latest.json`, tolerant loading of absent/invalid history, and a ZIP containing exactly two entries. Inject secrets, phone numbers, QR URLs, Telegram links, absolute Windows paths, root text, file names, mappings with unknown keys, write failure, and name collisions.

```python
def test_export_contains_exact_allowlisted_entries(tmp_path: Path) -> None:
    store = DiagnosticReportStore(PortablePaths(tmp_path), secrets={"api-secret"})
    target = store.export(report())
    with ZipFile(target) as archive:
        assert sorted(archive.namelist()) == [
            "diagnostic-report.json",
            "diagnostic-summary.txt",
        ]
        payload = b"\n".join(archive.read(name) for name in archive.namelist())
    assert b"api-secret" not in payload
    assert str(tmp_path).encode() not in payload


@pytest.mark.parametrize("unsafe", [
    "+8613812345678",
    "tg://login?token=secret",
    "https://t.me/example/42",
    r"D:\\private\\file.mp4",
])
def test_privacy_validator_rejects_unsafe_strings(tmp_path: Path, unsafe: str) -> None:
    with pytest.raises(DiagnosticPrivacyError):
        DiagnosticReportStore(PortablePaths(tmp_path), secrets=set()).validate_value(unsafe)
```

- [ ] **Step 2: Run store tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_diagnostic_store.py -q`

Expected: store module is missing.

- [ ] **Step 3: Implement allowlisted store and atomic ZIP export**

Serialize only the defined report/result keys and scalar metrics; reject unknown status/code shapes and recursively validate every string against registered secrets, phone, QR URL, Telegram URL, drive/UNC path, root text, and environment username. The Chinese summary is generated from the same validated fields.

Write `latest.json.tmp`, flush/fsync, then `os.replace`. Export through a temporary ZIP below `data/temp/diagnostics`, validate its exact entry names and bytes, then move to `data/diagnostics/TelegramDownloader-diagnostics-YYYYMMDDTHHMMSSZ.zip`; use `.2`, `.3` on timestamp collision. On failure, remove only guarded temporary files and preserve old reports/packages.

- [ ] **Step 4: Run store tests and confirm GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_diagnostic_store.py -q`

Expected: all serialization, privacy, collision, and failure-rollback tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/diagnostic_store.py tests/test_diagnostic_store.py
git commit -m "feat: export private diagnostic reports"
```

### Task 6: Build the diagnostics page

**Files:**
- Create: `src/telegram_downloader/ui/diagnostics.py`
- Create: `tests/ui/test_diagnostics.py`
- Modify: `src/telegram_downloader/ui/main.py`
- Modify: `tests/ui/test_main_window.py`

- [ ] **Step 1: Write failing model, interaction, and layout tests**

Require status labels for all seven states, four table columns, historical-result labeling, progress bounds, and exact button rules. Require navigation to the new page and verify geometry at 1180×720 and 1280×780 with no clipped controls.

```python
def test_page_button_state_tracks_run_and_report(qtbot) -> None:
    page = DiagnosticsPage()
    qtbot.addWidget(page)
    assert page.start_button.isEnabled()
    assert not page.cancel_button.isEnabled()
    assert not page.export_button.isEnabled()
    page.set_running(True)
    assert not page.start_button.isEnabled()
    assert page.cancel_button.isEnabled()
    page.set_report(report(), historical=False)
    page.set_running(False)
    assert page.export_button.isEnabled()


def test_page_emits_intent_only_signals(qtbot) -> None:
    page = DiagnosticsPage()
    with qtbot.waitSignal(page.run_requested):
        qtbot.mouseClick(page.start_button, Qt.MouseButton.LeftButton)
```

- [ ] **Step 2: Run UI tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_diagnostics.py tests/ui/test_main_window.py -q`

Expected: diagnostics page and navigation are missing.

- [ ] **Step 3: Implement the result model and page**

Create `DiagnosticResultModel(QAbstractTableModel)` with headers `检查项`, `状态`, `耗时`, `说明`, returning the stable ID in `UserRole`. Create `DiagnosticsPage` signals `run_requested`, `cancel_requested`, `export_requested`, `open_directory_requested`; methods `set_report`, `set_progress`, `set_running`, and `show_error`; and fixed status translations.

Add `diagnostics_nav_button`, `diagnostics_activated`, and the page to `MainWindow.page_stack`. Reuse existing theme and navigation patterns. The table stretches the explanation column, the status banner uses text plus color, and bottom buttons remain visible at minimum size.

- [ ] **Step 4: Run UI tests and confirm GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_diagnostics.py tests/ui/test_main_window.py -q`

Expected: model, interactions, navigation, and both layout sizes pass offscreen.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/ui/diagnostics.py src/telegram_downloader/ui/main.py tests/ui/test_diagnostics.py tests/ui/test_main_window.py
git commit -m "feat: add graphical diagnostics page"
```

### Task 7: Wire controller, application, shutdown, and shared self-test probes

**Files:**
- Modify: `src/telegram_downloader/controller.py`
- Modify: `src/telegram_downloader/app.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_self_test.py`

- [ ] **Step 1: Write failing controller and application integration tests**

Require activation to load history without network, run to persist a valid report, progress forwarding, duplicate-click suppression through key `diagnostics.run`, cancel convergence, export/open path guards, safe failures, and shutdown cancellation. Require `run_self_test()` to use shared component/path helpers while preserving its public JSON fields.

```python
@pytest.mark.asyncio
async def test_controller_runs_persists_and_refreshes_diagnostics(tmp_path: Path) -> None:
    report = diagnostic_report()
    service = FakeDiagnosticsService(report)
    store = FakeDiagnosticStore()
    controller = AppController.for_test(diagnostics=service, diagnostic_store=store)
    await controller.run_diagnostics()
    assert store.saved == [report]
    assert controller.window.diagnostics_page.report == report


def test_diagnostic_signals_use_one_async_action_key(tmp_path: Path) -> None:
    created = create_application(tmp_path)
    assert created.async_action_keys["run_diagnostics"] == "diagnostics.run"
```

- [ ] **Step 2: Run integration tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_controller.py tests/test_app.py tests/test_self_test.py -q`

Expected: controller dependencies, actions, and signal wiring are missing.

- [ ] **Step 3: Implement controller and app composition**

Add optional diagnostics/store dependencies and controller methods:

```python
def activate_diagnostics(self) -> None: ...
async def run_diagnostics(self) -> None: ...
async def cancel_diagnostics(self) -> None: ...
def export_diagnostics(self) -> None: ...
def open_diagnostics_directory(self) -> None: ...
```

`run_diagnostics()` forwards progress, awaits the shared service task, atomically saves only privacy-valid reports, and refreshes the page in `finally`. `export_diagnostics()` requires a completed in-memory/latest report and shows only the project-relative export name. Opening uses the existing guarded file opener on `paths.diagnostics`.

In `create_application`, construct the fixed probe list from already initialized paths/repositories/settings/secrets, a Telegram adapter that reuses the controller connection-recovery lock without opening login UI, and the existing update coordinator. Connect page signals via `AsyncActionBridge`; include diagnostics cancellation in controller shutdown. Extract component/path pure helpers from `run_self_test` into the probe module and keep the CLI report keys/version and 13 original writable paths compatible while adding the diagnostics directory only to the internal guard check.

- [ ] **Step 4: Run integration tests and confirm GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_controller.py tests/test_app.py tests/test_self_test.py -q`

Expected: all diagnostics integration and existing login/search/subscription/download/update tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/controller.py src/telegram_downloader/app.py tests/test_controller.py tests/test_app.py tests/test_self_test.py
git commit -m "feat: integrate graphical health diagnostics"
```

### Task 8: Add end-to-end privacy regression and prepare v0.10.0

**Files:**
- Create: `tests/test_diagnostics_e2e.py`
- Modify: `tests/test_logging.py`
- Modify: `tests/test_packaging_contract.py`
- Modify: `pyproject.toml`
- Modify: `src/telegram_downloader/__init__.py`
- Modify: `installer/TelegramDownloader.iss`
- Modify: `README.md`
- Create: `docs/releases/v0.10.0.md`
- Create: `docs/verification/v0.10.0-graphical-health-diagnostics.md`

- [ ] **Step 1: Write the failing end-to-end and release contract tests**

Create real task/catalog databases with fake account/group/search/media values and registered fake credentials. Run all local probes plus controlled network probes, persist and export, then scan extracted bytes and names for every injected value and absolute path. Assert original database/data hashes are unchanged. Add package contract assertions for version 0.10.0, diagnostics imports in `app.py`, README terms `健康诊断`, `开始自检`, `导出诊断包`, and data exclusion.

```python
def test_diagnostic_bundle_is_structured_private_and_read_only(tmp_path: Path) -> None:
    before = inventory(data_root)
    report = asyncio.run(run_controlled_diagnostics(root, injected_private_values))
    package = store.export(report)
    payload = extracted_payload(package)
    assert inventory(data_root, exclude={"diagnostics"}) == before
    for value in injected_private_values | {str(root)}:
        assert value.encode("utf-8") not in payload


def test_v010_version_and_diagnostics_contract_are_consistent() -> None:
    assert project_version() == init_version() == installer_version() == "0.10.0"
    assert all(term in readme() for term in ("健康诊断", "开始自检", "导出诊断包"))
```

- [ ] **Step 2: Run the new tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_diagnostics_e2e.py tests/test_logging.py tests/test_packaging_contract.py -q`

Expected: version/docs remain 0.9.0 and E2E integration is incomplete.

- [ ] **Step 3: Complete privacy hardening, version, and user docs**

Fix only defects exposed by the E2E tests. Set version 0.10.0 in all three version sources. Document what each diagnostic state means, the fact that checks are user-triggered and non-destructive, the exact two-file ZIP contents, the absence of logs/content/credentials, and the project-local path. Write release notes and an initially evidence-free verification document; do not claim public hashes before release.

- [ ] **Step 4: Run E2E and the full suite**

Run: `.venv\Scripts\python.exe -m pytest tests/test_diagnostics_e2e.py tests/test_logging.py tests/test_packaging_contract.py -q`

Then: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1`

Expected: every test and Ruff pass.

- [ ] **Step 5: Commit**

```powershell
git add src tests pyproject.toml installer/TelegramDownloader.iss README.md docs/releases/v0.10.0.md docs/verification/v0.10.0-graphical-health-diagnostics.md
git commit -m "chore: prepare v0.10.0 diagnostics release"
```

### Task 9: Verify, integrate, package, and publish v0.10.0

**Files:**
- Modify: `docs/superpowers/plans/2026-08-16-graphical-health-diagnostics.md`
- Modify: `docs/verification/v0.10.0-graphical-health-diagnostics.md`

- [ ] **Step 1: Perform structured code/spec review**

Review the complete feature diff against `docs/superpowers/specs/2026-08-16-graphical-health-diagnostics-design.md`. Resolve every Critical/Important issue using a failing regression test first. Record review base/head and findings; do not enter packaging with unresolved Critical/Important findings.

- [ ] **Step 2: Run three complete verification rounds on one clean commit**

Run `scripts/test.ps1` three consecutive times without code changes. Record exact pass counts, pytest durations, script durations, Ruff results, commit SHA, and clean-worktree proof.

- [ ] **Step 3: Run realistic copied-data diagnostics QA**

Copy only the v0.9.0 direct-run `data`/`downloads` needed for QA into a guarded F-drive worktree `.build-temp` directory; never mutate the original. Run local diagnostics with controlled Telegram/update probes, verify expected task/media/account/dialog counts, export the ZIP, scan it for original names/paths/credentials, and compare every original file hash before/after. Drive the frozen GUI like a user: open diagnostics, start, observe progress, cancel once, rerun, export, open directory, navigate away/back, and close cleanly.

- [ ] **Step 4: Build and inspect Windows artifacts**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-installer.ps1 -SkipAppBuild
```

Require `PACKAGED_SMOKE_OK` and `INSTALLER_SMOKE_OK`. Inspect the portable ZIP for zero `data/`, `downloads/`, databases, reports, diagnostics packages, credentials, logs, sessions, `.part`, `.corrupt*`, or release secrets. Run frozen `--self-test` and graphical diagnostics in isolated non-C project roots.

- [ ] **Step 5: Integrate to local main and preserve direct-run data**

Commit build/review evidence, fast-forward local `main`, and rerun `scripts/test.ps1`. Build `dist/release/v0.10.0-portable` from a clean runtime plus copied v0.9.0 `data` and `downloads`; keep v0.9.0 unchanged. Before and after self-test/diagnostics, verify settings, DPAPI secrets, both databases, downloads, and prior diagnostic packages as applicable; database row counts must remain equal.

- [ ] **Step 6: Publish the signed dual-source release**

From clean `main` run:

```powershell
$env:MODELSCOPE_API_TOKEN = (Get-Content .release-secrets/modelscope-api-token.txt -Raw).Trim()
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/release/release.ps1 -Version 0.10.0
```

Require `RELEASE_PUBLISHED v0.10.0`. Independently verify GitHub `main`, ModelScope `source`, both peeled tags, seven GitHub assets, both stable latest pointers, Ed25519 manifest signature, runtime/installer SHA-256, exact remote byte matches, and `UpdateCoordinator` discovering 0.10.0 from both sources when current version is 0.9.0.

- [ ] **Step 7: Record public evidence without moving the release tag**

Append public URLs, sizes, SHA-256 values, tag commit, direct-run QA, ZIP privacy count, GUI QA, and D/F volume status to the verification document. Commit after the tag and push only GitHub `main` and ModelScope `source`; independently prove both source branches contain the evidence commit while both `v0.10.0` tags still point to the release code commit.

- [ ] **Step 8: Final completion gate**

On the final source commit run `scripts/test.ps1`, frozen direct-run `--self-test`, live dual-source update discovery, protected user-data hash comparison, formal ZIP privacy scan, remote branch/tag inspection, and GitHub release asset count. Require a clean main worktree before marking the goal complete.
