# Health Diagnostics Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make GUI diagnostics and command self-test accurate for live WAL state, semantic corruption, configured credentials/storage, bounded cancellation, and privacy-safe remediation.

**Architecture:** Keep `DiagnosticsService` and the existing report pipeline. Harden command boundaries and SQLite probes in place, pass missing runtime context from app composition, and isolate presentation-only remediation/metric formatting in a new UI helper.

**Tech Stack:** Python 3.12, asyncio, SQLite/WAL, PySide6/qasync, pytest/pytest-asyncio/pytest-qt, Ruff, PyInstaller, PowerShell smoke scripts.

---

## File map

- Modify `src/telegram_downloader/__main__.py`: guard command health modes with the existing single-instance mutex.
- Modify `src/telegram_downloader/app.py`: remove command self-test recovery and pass correct credential/storage/network context.
- Modify `src/telegram_downloader/diagnostic_probes.py`: WAL-aware snapshots, semantic checks, storage checks, and network deadlines.
- Modify `src/telegram_downloader/diagnostic_store.py`: strict allowlists for new fixed results and metrics.
- Modify `src/telegram_downloader/controller.py` and `src/telegram_downloader/ui/diagnostics.py`: cancellation feedback and details card.
- Create `src/telegram_downloader/ui/diagnostic_details.py`: fixed remediation and safe metric formatting.
- Modify focused tests plus `README.md`; no business schema migration and no production-only test hooks.

## Task 1: Make command self-test business-data read-only

**Files:**
- Modify: `src/telegram_downloader/app.py:265-316`
- Modify: `src/telegram_downloader/__main__.py:50-76`
- Test: `tests/test_self_test.py`
- Test: `tests/test_bootstrap.py`

- [ ] **Step 1: Write a failing real-state preservation test**

Seed a downloading task/media row and a running subscription through repository fixtures, project all status/timestamp fields, run `run_self_test(tmp_path)`, and assert the projection is unchanged:

```python
def test_self_test_does_not_recover_live_business_state(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    seed_downloading_task(paths.database)
    seed_running_subscription(paths.catalog_database)
    before = business_state_projection(paths)

    report = run_self_test(tmp_path)

    assert report["ok"] is True
    assert business_state_projection(paths) == before
```

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_self_test.py::test_self_test_does_not_recover_live_business_state -q`

Expected: FAIL because task changes to `queued` and subscription changes to `waiting`.

- [ ] **Step 3: Remove only business recovery from `run_self_test()`**

Delete:

```python
repository.recover_interrupted()
catalog.recover_interrupted_subscriptions(datetime.now(UTC))
```

Keep path creation, `initialize()`/schema compatibility, component imports, thumbnail initialization, and atomic `self-test.json` output.

- [ ] **Step 4: Verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_self_test.py -q`

Expected: all self-test tests PASS and the state projection is unchanged.

- [ ] **Step 5: Write failing single-instance command tests**

Add a helper-level test that injects a guard and a forbidden self-test callback:

```python
@dataclass
class FakeGuard:
    acquired: bool
    released: bool = False

    def acquire(self) -> bool:
        return self.acquired

    def release(self) -> None:
        self.released = True


def test_health_command_refuses_database_access_when_instance_runs(tmp_path, capsys):
    called = False
    def forbidden(_root):
        nonlocal called
        called = True
        return {"ok": True}
    code = main_module._run_health_command(
        tmp_path, confirmation=None, self_test=forbidden, guard=FakeGuard(False)
    )
    assert code == 2
    assert called is False
    assert json.loads(capsys.readouterr().out) == {
        "ok": False, "code": "instance-running"
    }
```

Add a success companion asserting confirmation is written only for `ok=true` and `guard.release()` always runs.

- [ ] **Step 6: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_bootstrap.py -q`

Expected: FAIL because `_run_health_command` does not exist.

- [ ] **Step 7: Implement guarded health command routing**

Add `_run_health_command(root, *, confirmation, self_test=None, guard=None) -> int`. Acquire `WindowsInstanceGuard`, print `{"ok":false,"code":"instance-running"}` and return 2 on contention, otherwise run self-test inside `try/finally`, write confirmation through an extracted `_write_health_confirmation()`, print UTF-8 JSON, and release the guard.

Route both `--self-test` and `--update-health-check` through the helper.

- [ ] **Step 8: Verify and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_bootstrap.py tests\test_self_test.py tests\test_instance_guard.py -q
git add src/telegram_downloader/app.py src/telegram_downloader/__main__.py tests/test_self_test.py tests/test_bootstrap.py
git commit -m "fix: keep command self-test business-state read-only"
```

## Task 2: Make task database diagnostics WAL-aware and semantic

**Files:**
- Modify: `src/telegram_downloader/diagnostic_probes.py:261-343,627-704`
- Modify: `src/telegram_downloader/diagnostic_store.py`
- Test: `tests/test_diagnostic_probes.py`
- Test: `tests/test_diagnostic_store.py`

- [ ] **Step 1: Write failing WAL/foreign-key/domain tests**

Add independent tests that keep a writer open with one committed uncheckpointed row, insert an orphan media row with foreign keys disabled, and parameterize unknown values across:

```python
[
    ("tasks", "source_kind"), ("tasks", "status"), ("tasks", "pause_reason"),
    ("media_items", "media_kind"), ("media_items", "status"),
    ("media_items", "integrity_status"),
]
```

Assertions:

```python
assert wal_result.metrics["taskCount"] == 1
assert orphan_result.code == "database-semantics-invalid"
assert orphan_result.metrics["foreignKeysValid"] is False
assert unknown_result.metrics["stateValuesValid"] is False
assert "private-invalid-value" not in unknown_result.summary + repr(dict(unknown_result.metrics))
```

Add a store round-trip test for both fixed task/content semantic-failure summaries and the two boolean metrics. It must fail on the current strict allowlist before probe implementation starts.

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_diagnostic_probes.py -k "wal or foreign_key or unknown_domain" -q`

Expected: WAL count is stale and corruption reports `task-database-ok`.

- [ ] **Step 3: Open a WAL-aware read transaction**

Replace `mode=ro&immutable=1` with:

```python
uri = f"{database.resolve().as_uri()}?mode=ro"
connection = sqlite3.connect(uri, uri=True, timeout=2)
connection.execute("PRAGMA query_only=ON")
connection.execute("PRAGMA busy_timeout=2000")
connection.execute("BEGIN")
```

Do not checkpoint or fall back to immutable mode.

- [ ] **Step 4: Add shared semantic validators**

```python
def _foreign_keys_valid(connection: sqlite3.Connection) -> bool:
    return connection.execute("PRAGMA foreign_key_check").fetchone() is None

def _column_values_valid(connection, table, column, allowed, *, nullable=False):
    placeholders = ",".join("?" for _ in allowed)
    predicate = (
        f"{column} IS NOT NULL AND {column} NOT IN ({placeholders})"
        if nullable else f"{column} IS NULL OR {column} NOT IN ({placeholders})"
    )
    return connection.execute(
        f"SELECT 1 FROM {table} WHERE {predicate} LIMIT 1", allowed
    ).fetchone() is None
```

Allowed values come from existing `StrEnum` members; only trusted constant table/column names call the helper.

- [ ] **Step 5: Fail the task probe on semantic damage**

Add `foreignKeysValid` and `stateValuesValid` booleans to success metrics. If either is false, return `FAILED`, code `database-semantics-invalid`, summary `下载任务数据库包含无效关系或状态`. Any aggregate `*Other` metric is also a semantic failure.

In the same RED/GREEN cycle, add the two booleans to task/content metric allowlists and the two exact database semantic variants to `_RESULT_VARIANTS`. Keep report schema 1.

- [ ] **Step 6: Verify and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_diagnostic_probes.py tests\test_diagnostic_store.py -q
git add src/telegram_downloader/diagnostic_probes.py src/telegram_downloader/diagnostic_store.py tests/test_diagnostic_probes.py tests/test_diagnostic_store.py
git commit -m "fix: diagnose live task database semantics"
```

## Task 3: Validate content database semantics

**Files:**
- Modify: `src/telegram_downloader/diagnostic_probes.py:346-397`
- Test: `tests/test_diagnostic_probes.py`

- [ ] **Step 1: Write failing content corruption tests**

Parameterize unknown values across:

```python
[
    ("dialogs", "kind"), ("search_sessions", "status"),
    ("search_sessions", "scope"), ("search_results", "media_kind"),
    ("subscription_rules", "state"), ("subscription_runs", "status"),
]
```

Add an orphan content row test. Every case must return `database-semantics-invalid`, fixed summaries, booleans only, and no invalid raw value.

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_diagnostic_probes.py -k content_probe_rejects -q`

Expected: FAIL because only schema/quick-check/counts are validated.

- [ ] **Step 3: Apply shared validators**

Run `_foreign_keys_valid()` and `_column_values_valid()` after schema compatibility. Successful metrics include `schemaVersion`, `schemaCompatible`, `foreignKeysValid`, `stateValuesValid`, and existing counts. Failure summary is `账号内容数据库包含无效关系或状态`.

- [ ] **Step 4: Verify and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_diagnostic_probes.py -q
git add src/telegram_downloader/diagnostic_probes.py tests/test_diagnostic_probes.py
git commit -m "fix: validate content database semantics"
```

## Task 4: Diagnose actual credential completeness

**Files:**
- Modify: `src/telegram_downloader/diagnostic_probes.py:400-445`
- Modify: `src/telegram_downloader/app.py:701-717`
- Modify: `src/telegram_downloader/diagnostic_store.py`
- Test: `tests/test_diagnostic_probes.py`
- Test: `tests/test_app.py`
- Test: `tests/test_diagnostic_store.py`

- [ ] **Step 1: Write failing incomplete/complete tests**

```python
incomplete = probe_credentials(
    settings_readable=True, secrets_present=True, secrets_decrypted=True,
    credentials_configured=False,
)
assert (incomplete.status, incomplete.code) == (
    DiagnosticStatus.WARNING, "credentials-not-configured"
)
assert incomplete.metrics["credentialsConfigured"] is False
```

At app composition level, use `api_id=0` plus `{}` and positive API ID plus nonblank `api_hash`; assert only the latter passes.

Add a strict store round-trip assertion for `credentialsConfigured`; it must fail before the allowlist is changed.

- [ ] **Step 2: Verify RED**

Run both credential selections in `test_diagnostic_probes.py` and `test_app.py`; expect signature/behavior failure.

- [ ] **Step 3: Implement completeness**

Add required keyword `credentials_configured: bool` and metric `credentialsConfigured`. In `credential_health()`, retain loaded settings/secrets locally and compute:

```python
configured = current_settings.api_id > 0 and bool(
    current_secrets.get("api_hash", "").strip()
)
```

Evaluate this only after settings readable, file present, and decryptable checks. Never pass values into the result.

Add `credentialsConfigured` to the credentials metric allowlist and boolean metric set; no dynamic credential value is permitted.

- [ ] **Step 4: Verify and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_diagnostic_probes.py tests\test_app.py tests\test_diagnostic_store.py -k "diagnostic or credential" -q
git add src/telegram_downloader/diagnostic_probes.py src/telegram_downloader/app.py src/telegram_downloader/diagnostic_store.py tests/test_diagnostic_probes.py tests/test_app.py tests/test_diagnostic_store.py
git commit -m "fix: diagnose configured Telegram credentials"
```

## Task 5: Diagnose the active download root and both volumes

**Files:**
- Modify: `src/telegram_downloader/diagnostic_probes.py:135-213`
- Modify: `src/telegram_downloader/app.py:724-750`
- Modify: `src/telegram_downloader/diagnostic_store.py`
- Test: `tests/test_diagnostic_probes.py`
- Test: `tests/test_diagnostic_store.py`
- Test: `tests/test_diagnostics_e2e.py`

- [ ] **Step 1: Write failing active-root write tests**

Inject a recording `DownloadPathPolicy` probe and assert `probe_project_write(..., download_paths=policy)` calls `require_current_writable()`, returns `downloadWritable=True`, and maps a private-path failure to fixed code `download-write-failed` without the path.

- [ ] **Step 2: Write failing dual-volume disk tests**

Use a recording usage provider for syntactic `D:/App` and `E:/Media` roots. Assert same-volume calls once; different-volume calls twice; add app/download critical (<256 MiB), low (<1 GiB), and unavailable cases. Require metrics:

```python
{
    "downloadSameVolume": False,
    "downloadTotalBytes": 8 * GIB,
    "downloadFreeBytes": 128 * MIB,
}
```

Add strict store tests for the new storage codes, fixed summaries, and scalar metrics before changing the allowlists.

- [ ] **Step 3: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_diagnostic_probes.py -k "project_write or disk_probe" -q`

Expected: probes do not accept active download context.

- [ ] **Step 4: Implement active-root checks**

Extend `probe_project_write(paths, *, download_paths=None)` and call `download_paths.require_current_writable()` after the diagnostic-temp marker. Map `DownloadPathError`, `OSError`, and `ValueError` to fixed failure.

Keep positional `usage_provider` compatibility and add keyword-only `download_root`. Compare normalized anchors, preserve `totalBytes/freeBytes`, add download metrics, and use fixed app/download low/critical/unavailable codes.

Extend disk/project-write allowlists, boolean metrics, and exact result variants in the same cycle.

- [ ] **Step 5: Wire app composition**

```python
lambda: probe_project_write(paths, download_paths=download_paths)
lambda: probe_disk(paths, download_root=download_paths.current_root)
```

- [ ] **Step 6: Verify and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_diagnostic_probes.py tests\test_diagnostic_store.py tests\test_diagnostics_e2e.py -q
git add src/telegram_downloader/diagnostic_probes.py src/telegram_downloader/diagnostic_store.py src/telegram_downloader/app.py tests/test_diagnostic_probes.py tests/test_diagnostic_store.py tests/test_diagnostics_e2e.py
git commit -m "fix: diagnose active download storage"
```

## Task 6: Bound online checks and expose cancellation convergence

**Files:**
- Modify: `src/telegram_downloader/diagnostic_probes.py:448-587`
- Modify: `src/telegram_downloader/app.py:153-166`
- Modify: `src/telegram_downloader/diagnostic_store.py`
- Modify: `src/telegram_downloader/controller.py:2241-2244`
- Modify: `src/telegram_downloader/ui/diagnostics.py:273-299`
- Test: `tests/test_diagnostic_probes.py`, `tests/test_app.py`, `tests/test_controller.py`, `tests/ui/test_diagnostics.py`

- [ ] **Step 1: Write failing timeout tests**

Use blocking coroutines plus `timeout_seconds=0.001`. Telegram must return warning `telegram-network-timeout`; update sources must return warning `update-sources-timeout`; both blocking coroutines must observe cancellation.

Add store tests for both fixed timeout variants before adding the variants.

- [ ] **Step 2: Write failing shared-recovery isolation test**

Compose `_telegram_health()` with a gateway and a `connection_recovery` fake that raises if called. Assert the gateway is tested directly and recovery has zero calls.

- [ ] **Step 3: Verify RED**

Run selected timeout/telegram-health tests; expect missing timeout API and a recovery call.

- [ ] **Step 4: Implement bounded direct checks**

Wrap only the external await in:

```python
async with asyncio.timeout(timeout_seconds):
    await gateway.test_connection()
```

Use fixed default 20 seconds for Telegram/update sources and catch `TimeoutError` before generic exceptions. Simplify `_telegram_health()` to preserve saved authorization failure and skipped cases, otherwise call `probe_telegram(gateway)` directly.

Add the two exact timeout variants to the strict report contract in the same RED/GREEN cycle.

- [ ] **Step 5: Write failing cancellation UI/controller tests**

Assert `set_cancelling(True)` shows `正在取消，当前本地检查完成后停止`, disables repeated cancel, and controller sets/clears it around a blocking `diagnostics.cancel()`.

- [ ] **Step 6: Implement cancellation feedback**

Add `_cancelling`, `set_cancelling()`, and button rules. Controller sets cancelling before await and clears in `finally`; `set_running(True)` resets stale cancellation state.

- [ ] **Step 7: Verify and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_diagnostics.py tests\test_diagnostic_probes.py tests\test_app.py tests\test_controller.py tests\ui\test_diagnostics.py -k "diagnostic or timeout" -q
git add src/telegram_downloader/diagnostic_probes.py src/telegram_downloader/diagnostic_store.py src/telegram_downloader/app.py src/telegram_downloader/controller.py src/telegram_downloader/ui/diagnostics.py tests
git commit -m "fix: bound and cancel diagnostic probes"
```

## Task 7: Add actionable privacy-safe details

**Files:**
- Create: `src/telegram_downloader/ui/diagnostic_details.py`
- Create: `tests/ui/test_diagnostic_details.py`
- Modify: `src/telegram_downloader/ui/diagnostics.py`
- Modify: `tests/ui/test_diagnostics.py`

- [ ] **Step 1: Write failing pure presentation tests**

```python
result = DiagnosticResult(
    "disk",
    "磁盘空间",
    DiagnosticStatus.WARNING,
    "download-disk-space-low",
    "下载所在磁盘可用空间低于 1 GiB",
    1,
    {"downloadFreeBytes": 512 * MIB, "privateUnknown": "D:/private"},
)
detail = present_diagnostic_details(result)
assert "清理下载所在磁盘" in detail.remediation
assert "512 MiB" in detail.metrics_text
assert "D:/private" not in detail.metrics_text
```

Unknown codes use `重新运行检查；持续失败时使用诊断包反馈。`; unknown metrics produce `无可显示的安全指标`.

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\ui\test_diagnostic_details.py -q`

Expected: module import failure.

- [ ] **Step 3: Implement the presentation helper**

Create frozen `DiagnosticDetails(remediation, metrics_text)`, a fixed code/remediation map, and an explicit metric formatter whitelist. Support booleans, byte sizes, milliseconds, versions, source statuses, and fixed authorization reasons. Never stringify unknown keys/values.

- [ ] **Step 4: Write failing UI selection tests**

Set a two-row report, select rows, and assert the details card changes. Assert injected unknown/private metrics never appear in labels.

- [ ] **Step 5: Implement the details card**

Add fixed “处理建议” and “安全指标” labels below the results table. Connect `currentRowChanged`; select the first result when a report loads; clear the card when no report exists.

- [ ] **Step 6: Verify and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ui\test_diagnostic_details.py tests\ui\test_diagnostics.py -q
git add src/telegram_downloader/ui/diagnostic_details.py src/telegram_downloader/ui/diagnostics.py tests/ui/test_diagnostic_details.py tests/ui/test_diagnostics.py
git commit -m "feat: show actionable diagnostic details"
```

## Task 8: Reconcile end-to-end privacy contracts and documentation

**Files:**
- Modify: `src/telegram_downloader/diagnostic_store.py`
- Modify: `tests/test_diagnostic_store.py`, `tests/test_diagnostics_e2e.py`, `tests/test_packaging_contract.py`
- Modify: `README.md:174-180`

- [ ] **Step 1: Write a failing public-documentation contract test**

Extend `test_packaging_contract.py` to require README statements that command self-test never recovers business state, active download storage is checked, and online diagnostics are bounded. Run it before README changes and observe failure.

Also extend the E2E report fixture with every already-allowlisted new code/metric and assert strict serialize/deserialize equality, two fixed ZIP entries, and absence of injected secrets/paths.

- [ ] **Step 2: Verify RED**

Run packaging-contract plus diagnostic store/E2E tests. Expected: packaging contract fails only because README lacks the approved statements; store and E2E privacy checks pass using the incrementally completed allowlists.

- [ ] **Step 3: Audit completed allowlists**

Confirm the incremental tasks added only approved metrics and exact variants for `database-semantics-invalid`, `download-write-failed`, app/download disk codes, `telegram-network-timeout`, and `update-sources-timeout`. Keep schema 1, exact key checks, fixed summaries, and existing secret/path validators; remove any unused or overly broad entry found by the audit.

- [ ] **Step 4: Update README**

Document that command self-test can initialize schema/write its report but never recovers business state; GUI diagnostics check active download storage, bounded online sources, and fixed safe details.

- [ ] **Step 5: Verify and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_diagnostic_store.py tests\test_diagnostics_e2e.py tests\test_packaging_contract.py -q
git add src/telegram_downloader/diagnostic_store.py tests/test_diagnostic_store.py tests/test_diagnostics_e2e.py tests/test_packaging_contract.py README.md
git commit -m "docs: define hardened diagnostic contracts"
```

## Task 9: Complete verification, packaging, and independent review

**Files:**
- Verify the entire repository and frozen artifacts; production changes require a new failing regression test.

- [ ] **Step 1: Run diagnostic-focused tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_diagnostics.py tests\test_diagnostic_probes.py tests\test_diagnostic_store.py tests\test_diagnostics_e2e.py tests\test_self_test.py tests\test_bootstrap.py tests\test_app.py tests\test_controller.py tests\ui\test_diagnostic_details.py tests\ui\test_diagnostics.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 2: Run Ruff and the complete suite**

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: Ruff clean and every test PASS; record exact count/duration.

- [ ] **Step 3: Run source/frozen self-test checks**

Repeat the real business-state invariance test, then run the complete repository build transaction:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_self_test.py::test_self_test_does_not_recover_live_business_state tests\test_bootstrap.py -q
& .\scripts\build-installer.ps1
```

`build-installer.ps1` invokes `build.ps1`, which runs the full test script, builds the frozen runtime, executes `smoke.ps1`, creates and privacy-scans the portable ZIP; it then builds the installer and invokes `smoke-installer.ps1` with the exact generated setup path.

Expected: the source invariance test passes and the build emits `PACKAGED_SMOKE_OK` plus `INSTALLER_SMOKE_OK`; frozen health command exits 0 alone and the command-level guard test proves exit 2 before database access under an occupied instance guard.

- [ ] **Step 4: Inspect privacy and workspace state**

```powershell
git diff --check
git status --short --branch
git log --oneline --decorate -15
```

Inspect the privacy-scan output produced by `scripts\build.ps1` during Step 3 and confirm the ZIP validation completes without a forbidden-entry exception. Expect no credentials, sessions, user databases, logs, diagnostics, partial downloads, update backups, or release secrets.

- [ ] **Step 5: Request independent review**

Use `requesting-code-review` with the approved spec, this plan, commit range, and exact verification. Ask reviewers to focus on WAL correctness, SQLite read-only guarantees, command concurrency, task ownership/cancellation, and diagnostic privacy.

- [ ] **Step 6: Address findings through RED/GREEN and re-run Steps 1-4**

Every accepted finding receives a failing regression test before production changes and a separate commit.

- [ ] **Step 7: Final branch check**

Expected: clean isolated feature worktree, all tasks committed, no merge or release without a separate user request.
