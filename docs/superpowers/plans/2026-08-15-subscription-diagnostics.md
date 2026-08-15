# Subscription Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explainable automatic-subscription diagnostics workspace with persisted keyword-hit statistics, recent run history, and a cancellable read-only probe of the latest 100 Telegram messages.

**Architecture:** Extend catalog schema v2 to v3 with one backward-compatible run counter, add a bounded recent-message gateway method, and keep probing inside `SubscriptionService` so it shares matching and album semantics with real runs. Put explanation logic in a pure module and new Qt detail/history models in a separate UI module, while `AppController` owns one foreground probe task and cancellation lifecycle.

**Tech Stack:** Python 3.12, asyncio/qasync, PySide6, Telethon, SQLite, pytest/pytest-qt, Ruff, PyInstaller, Inno Setup.

---

### Task 1: Diagnostic domain models, explanations, and catalog schema v3

**Files:**
- Modify: `src/telegram_downloader/subscriptions.py`
- Create: `src/telegram_downloader/subscription_diagnostics.py`
- Modify: `src/telegram_downloader/catalog.py`
- Modify: `tests/test_subscriptions.py`
- Create: `tests/test_subscription_diagnostics.py`
- Modify: `tests/test_catalog.py`

- [ ] **Step 1: Write failing domain and explanation tests**

Add concrete tests that construct the new immutable values and cover every explanation branch:

```python
def test_probe_report_rejects_invalid_counts_and_limits_samples() -> None:
    with pytest.raises(ValueError, match="关键词命中"):
        SubscriptionProbeReport("r1", 2, 3, 0, 0, (), NOW)


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (subscription_run(status=SubscriptionRunStatus.FAILED, error="TimeoutError"), "检查失败"),
        (subscription_run(inspected=0), "没有新消息"),
        (subscription_run(inspected=4, keyword_hits=0), "未命中关键词"),
        (subscription_run(inspected=4, keyword_hits=2, matched=0), "没有所选媒体类型"),
        (subscription_run(inspected=4, keyword_hits=2, matched=2, duplicate=2), "均已在队列"),
        (subscription_run(inspected=4, keyword_hits=2, matched=2, queued=1, duplicate=1), "新增 1 项"),
    ],
)
def test_explain_run_covers_actionable_outcomes(run, expected: str) -> None:
    assert expected in explain_run(run)
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest tests/test_subscriptions.py tests/test_subscription_diagnostics.py -q`

Expected: FAIL because probe models, `keyword_hits`, and `explain_run` do not exist.

- [ ] **Step 3: Add exact domain fields and pure explanation functions**

Add `keyword_hits` between `inspected` and `matched` in `SubscriptionProgress` and `SubscriptionRun`. Add these types:

```python
@dataclass(frozen=True, slots=True)
class SubscriptionProbeProgress:
    rule_id: str
    inspected: int
    keyword_hits: int
    matched: int
    phase: str


@dataclass(frozen=True, slots=True)
class SubscriptionProbeSample:
    message_id: int
    message_date_utc: datetime
    media_kind: MediaKind
    original_name: str
    expected_size: int | None
    already_queued: bool
    excerpt: str


@dataclass(frozen=True, slots=True)
class SubscriptionProbeReport:
    rule_id: str
    inspected: int
    keyword_hits: int
    matched: int
    duplicate: int
    samples: tuple[SubscriptionProbeSample, ...]
    finished_at: datetime
```

Validate non-negative monotonic counts (`keyword_hits <= inspected`, `duplicate <= matched`), positive message IDs, non-negative optional sizes, and at most 20 samples. Implement `explain_run(run)` and `explain_probe(report)` in `subscription_diagnostics.py` with the six priorities from the design; never include rule keywords, dialog names, excerpts, or raw exception messages in returned failure text.

- [ ] **Step 4: Write failing v2-to-v3 migration and round-trip tests**

Add tests that create a real schema-v2 database, migrate it, and save/read a run with `keyword_hits=3`:

```python
def test_catalog_migrates_v2_subscription_runs_to_v3(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    create_v2_catalog_with_run(database)
    repository = CatalogRepository(database)
    repository.initialize()
    assert repository.schema_version() == 3
    run = repository.list_subscription_runs("a1", "r1")[0]
    assert run.keyword_hits == 0


def test_subscription_run_round_trip_includes_keyword_hits(catalog) -> None:
    catalog.save_subscription_run(subscription_run(keyword_hits=3, matched=2))
    assert catalog.list_subscription_runs("a1", "r1")[0].keyword_hits == 3
```

- [ ] **Step 5: Implement schema v3 and typed persistence**

Add and execute after v2:

```sql
ALTER TABLE subscription_runs
    ADD COLUMN keyword_hits INTEGER NOT NULL DEFAULT 0 CHECK(keyword_hits >= 0);
PRAGMA user_version=3;
```

Update `initialize()` to advance a local `version` variable after each migration and reject anything except 3. Include `keyword_hits` in `save_subscription_run` and `_subscription_run_from_row`. Keep existing 100-run retention and account scoping unchanged.

- [ ] **Step 6: Run domain/catalog tests and commit**

Run: `python -m pytest tests/test_subscriptions.py tests/test_subscription_diagnostics.py tests/test_catalog.py -q`

Expected: PASS.

```powershell
git add src/telegram_downloader/subscriptions.py src/telegram_downloader/subscription_diagnostics.py src/telegram_downloader/catalog.py tests/test_subscriptions.py tests/test_subscription_diagnostics.py tests/test_catalog.py
git commit -m "feat: persist explainable subscription runs"
```

### Task 2: Bounded recent-message Telegram gateway

**Files:**
- Modify: `src/telegram_downloader/gateway.py`
- Modify: `tests/test_gateway.py`

- [ ] **Step 1: Write failing recent-message tests**

```python
@pytest.mark.asyncio
async def test_recent_messages_returns_oldest_first_with_limit() -> None:
    gateway, client = gateway_with_messages(message(13), message(12), message(11))
    values = await gateway.recent_messages("peer:1", limit=2)
    assert [item.message_id for item in values] == [12, 13]
    assert client.iter_messages_calls[-1].limit == 2


@pytest.mark.asyncio
async def test_recent_messages_rejects_limit_before_network_access() -> None:
    gateway, client = gateway_with_messages()
    with pytest.raises(ValueError, match="1 到 100"):
        await gateway.recent_messages("peer:1", limit=101)
    assert client.iter_messages_calls == []
```

Also cover an empty dialog, UTC conversion, media-less messages, and existing error mapping.

- [ ] **Step 2: Run gateway tests to verify RED**

Run: `python -m pytest tests/test_gateway.py -q`

Expected: FAIL because `recent_messages` is absent from the protocol and implementation.

- [ ] **Step 3: Implement the narrow protocol method**

Add `recent_messages(entity_ref: str, *, limit: int) -> tuple[RemoteMessage, ...]` to `TelegramGateway` and `TelethonGateway`. Validate `1 <= limit <= 100`, resolve the entity once, iterate newest-first with `iter_messages(entity, limit=limit)`, convert through the same private `RemoteMessage` builder used by incremental reads, then sort by `(message_id, message_date_utc)` oldest-first before returning. Wrap Telethon failures through `_raise_mapped`.

- [ ] **Step 4: Remove conversion duplication and run tests**

Extract `_remote_message(peer_ref, title, message) -> RemoteMessage | None` and call it from both `recent_messages` and `incremental_messages`; skip invalid IDs or dates identically.

Run: `python -m pytest tests/test_gateway.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/gateway.py tests/test_gateway.py
git commit -m "feat: read recent messages for subscription probes"
```

### Task 3: Shared matching semantics and read-only probe service

**Files:**
- Modify: `src/telegram_downloader/subscription_service.py`
- Modify: `tests/test_subscription_service.py`

- [ ] **Step 1: Write failing no-side-effect probe test**

```python
@pytest.mark.asyncio
async def test_probe_rule_reports_matches_without_side_effects(service, gateway, catalog, tasks) -> None:
    before_rule = service.get_rule("r1")
    before_runs = catalog.list_subscription_runs("a1", "r1")
    gateway.recent = (
        remote_message(40, text="普通", media=photo(40)),
        remote_message(41, text="美女", media=photo(41)),
        remote_message(42, text="美女", media=video(42)),
    )
    report = await service.probe_rule("r1")
    assert (report.inspected, report.keyword_hits, report.matched) == (3, 2, 1)
    assert service.get_rule("r1") == before_rule
    assert catalog.list_subscription_runs("a1", "r1") == before_runs
    assert tasks.list_tasks() == []
```

- [ ] **Step 2: Add failing album, duplicate, sample, progress, and cancellation tests**

Prove that one keyword-hit album includes every allowed album item, media keys are deduplicated, planner-existing keys set `already_queued`, samples stop at 20, excerpts stop at 80 characters, progress is monotonic, and cancellation leaves cursor/run/task counts unchanged. Add a parity test where the same messages passed to probe and formal run produce identical `keyword_hits`, `matched`, and duplicate counts.

- [ ] **Step 3: Run service tests to verify RED**

Run: `python -m pytest tests/test_subscription_service.py -q`

Expected: FAIL because `probe_rule` and recent gateway behavior are missing.

- [ ] **Step 4: Extract one shared matcher**

Create an internal immutable candidate and result in `subscription_service.py`:

```python
@dataclass(frozen=True, slots=True)
class _MatchedCandidate:
    remote: RemoteMedia
    message_id: int
    message_date_utc: datetime
    excerpt: str


@dataclass(frozen=True, slots=True)
class _MatchResult:
    inspected: int
    keyword_hits: int
    candidates: tuple[_MatchedCandidate, ...]


@dataclass(frozen=True, slots=True)
class _MatchStep:
    inspected: int
    keyword_hits: int
    matched: int
    phase: str
```

Implement `_match_messages(rule, messages, on_step: Callable[[_MatchStep], None] | None)` so formal runs and probes share normalization, album expansion, media-kind filtering, grouped-ID suppression and media-key deduplication. Count each keyword-hit message once; an expanded album contributes allowed unique media candidates but not additional keyword hits. Formal runs translate `_MatchStep` to `SubscriptionProgress`; probes translate it to `SubscriptionProbeProgress`, avoiding a callback type that sometimes emits two unrelated public objects.

- [ ] **Step 5: Implement `probe_rule` without persistence**

Add:

```python
async def probe_rule(
    self,
    rule_id: str,
    *,
    on_progress: Callable[[SubscriptionProbeProgress], None] | None = None,
) -> SubscriptionProbeReport:
    rule = self.get_rule(rule_id)
    gateway, planner = self._require_online()
    messages = await gateway.recent_messages(rule.peer_ref, limit=100)
    def emit(step: _MatchStep) -> None:
        if on_progress is not None:
            on_progress(
                SubscriptionProbeProgress(
                    rule.id,
                    step.inspected,
                    step.keyword_hits,
                    step.matched,
                    step.phase,
                )
            )
    matched = await self._match_messages(rule, messages, emit)
    keys = {self._media_key(item.remote) for item in matched.candidates}
    existing = planner.existing_media_keys(keys)
    samples = tuple(
        SubscriptionProbeSample(
            item.message_id,
            item.message_date_utc,
            item.remote.kind,
            item.remote.original_name,
            item.remote.expected_size,
            self._media_key(item.remote) in existing,
            item.excerpt[:80],
        )
        for item in matched.candidates[:20]
    )
    return SubscriptionProbeReport(
        rule.id,
        matched.inspected,
        matched.keyword_hits,
        len(matched.candidates),
        len(existing),
        samples,
        self.clock(),
    )
```

Read exactly `recent_messages(rule.peer_ref, limit=100)`, call `_match_messages`, query `planner.existing_media_keys`, and build at most 20 samples. Emit progress before reading, after each inspected message, and on completion. Do not call catalog save/update/advance methods or planner commit methods.

Add `list_runs(rule_id: str, *, limit: int = 20) -> list[SubscriptionRun]`: validate `1 <= limit <= 100`, resolve the current account and rule, then return `catalog.list_subscription_runs(account_id, rule_id)[:limit]`. This keeps account scoping and history limits out of the Qt page.

- [ ] **Step 6: Refactor `run_rule` through the matcher and persist `keyword_hits`**

Replace its inline loop with `_match_messages`, pass `keyword_hits` into every completed/failed/cancelled `SubscriptionRun`, and add it to `SubscriptionProgress`. Preserve fixed snapshot, 500-message continuation, safe cursor advancement, commit-race handling and task callback behavior exactly.

- [ ] **Step 7: Run focused tests and commit**

Run: `python -m pytest tests/test_subscription_service.py tests/test_subscription_scheduler.py tests/test_subscription_e2e.py -q`

Expected: PASS with no task or cursor side effects from probes.

```powershell
git add src/telegram_downloader/subscription_service.py tests/test_subscription_service.py tests/test_subscription_scheduler.py tests/test_subscription_e2e.py
git commit -m "feat: probe subscription rules without side effects"
```

### Task 4: Diagnostic history and sample table models

**Files:**
- Create: `src/telegram_downloader/ui/subscription_diagnostics.py`
- Create: `tests/ui/test_subscription_diagnostics.py`

- [ ] **Step 1: Write failing model tests**

```python
def test_run_history_model_formats_explanations_and_counts() -> None:
    model = SubscriptionRunHistoryModel()
    model.set_runs([subscription_run(keyword_hits=2, matched=1, queued=1)])
    assert model.headerData(1, Qt.Horizontal) == "结果"
    assert "新增 1 项" in model.data(model.index(0, 1))
    assert model.data(model.index(0, 3)) == "2"


def test_probe_sample_model_marks_existing_items() -> None:
    model = SubscriptionProbeSampleModel()
    model.set_samples([probe_sample(already_queued=True)])
    assert model.data(model.index(0, 5)) == "已在队列"
```

Assert newest-first deterministic history order, local-time formatting, byte-size formatting, tooltip safety, and that excerpts are display-only.

- [ ] **Step 2: Run UI model tests to verify RED**

Run: `python -m pytest tests/ui/test_subscription_diagnostics.py -q`

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement two focused Qt table models**

`SubscriptionRunHistoryModel` columns: `时间 / 结果 / 扫描 / 关键词 / 媒体 / 新增 / 重复`. It stores at most 20 runs, calls `explain_run`, and exposes no raw account/rule IDs through display roles.

`SubscriptionProbeSampleModel` columns: `日期 / 类型 / 文件 / 大小 / 摘要 / 状态`. It stores at most 20 samples, formats media labels and bytes, and returns the sample from `sample_at(row)` for future preview extension.

- [ ] **Step 4: Run tests and commit**

Run: `python -m pytest tests/ui/test_subscription_diagnostics.py -q`

Expected: PASS.

```powershell
git add src/telegram_downloader/ui/subscription_diagnostics.py tests/ui/test_subscription_diagnostics.py
git commit -m "feat: model subscription diagnostics"
```

### Task 5: Subscription diagnostics pane and responsive page integration

**Files:**
- Modify: `src/telegram_downloader/ui/subscriptions.py`
- Modify: `tests/ui/test_subscriptions.py`

- [ ] **Step 1: Write failing page interaction tests**

Add pytest-qt tests named:

```python
def test_selecting_rule_emits_history_request(qtbot, page):
    page.set_rules([rule()])
    with qtbot.waitSignal(page.rule_selected) as emitted:
        page.rule_table.selectRow(0)
    assert emitted.args == ["rule-1"]


def test_probe_button_emits_selected_rule_and_locks_conflicting_actions(qtbot, page):
    ready_page(page)
    with qtbot.waitSignal(page.probe_requested) as emitted:
        qtbot.mouseClick(page.probe_button, Qt.LeftButton)
    assert emitted.args == ["rule-1"]
    assert not page.probe_button.isEnabled()
    assert not page.edit_button.isEnabled()


def test_probe_progress_shows_counts_and_cancel(qtbot, page):
    ready_page(page)
    page.set_probe_busy("rule-1", True)
    page.set_probe_progress(SubscriptionProbeProgress("rule-1", 12, 3, 2, "正在筛选"))
    assert "已扫描 12" in page.probe_progress_label.text()
    assert page.probe_cancel_button.isVisible()
    with qtbot.waitSignal(page.probe_cancel_requested):
        qtbot.mouseClick(page.probe_cancel_button, Qt.LeftButton)


def test_probe_report_explains_result_and_populates_samples(qtbot, page):
    ready_page(page)
    page.set_probe_result(probe_report(keyword_hits=2, matched=1))
    assert page.probe_sample_model.rowCount() == 1
    assert "匹配" in page.probe_result_label.text()


def test_offline_history_remains_visible_but_probe_is_disabled(qtbot, page):
    ready_page(page)
    page.set_selected_rule_details(rule(), [subscription_run()])
    page.set_logged_in(False)
    assert page.run_history_model.rowCount() == 1
    assert not page.probe_button.isEnabled()


def test_rule_table_uses_elision_and_diagnostics_remain_readable_at_1024x720(qtbot, page):
    ready_page(page)
    page.resize(1024, 720)
    page.show()
    qtbot.waitExposed(page)
    assert page.detail_splitter.sizes()[1] >= 180
    assert page.probe_button.isVisible()
    assert page.run_history_table.viewport().width() > 0
```

Drive actual buttons and selection models. Assert that repeated probe clicks emit once, cancel emits once, and `set_probe_result` restores edit/run/pause/delete actions.

- [ ] **Step 2: Run page tests to verify RED**

Run: `python -m pytest tests/ui/test_subscriptions.py -q`

Expected: FAIL because diagnostics signals and widgets are absent.

- [ ] **Step 3: Add signals and detail/history layout**

Add `rule_selected = Signal(str)`, `probe_requested = Signal(str)`, and `probe_cancel_requested = Signal()`. Below the rule table add a compact detail card with read-only labels, a `SubscriptionRunHistoryModel` table, the `测试最近 100 条` button, hidden cancel button, determinate/indeterminate progress, explanation label, and `SubscriptionProbeSampleModel` table.

Keep the page in one navigation entry. Use a vertical `QSplitter` so 1024×720 remains usable, set stable minimum heights, and persist no splitter state. Elide long rule keywords/file names through Qt table delegates/tooltips rather than wrapping rows.

- [ ] **Step 4: Add complete setters and shared action refresh**

Implement:

```python
def set_selected_rule_details(self, rule, runs):
    self._detail_rule = rule
    self.run_history_model.set_runs(runs[:20])
    self.detail_summary.setText(self._format_rule_summary(rule) if rule else "请选择订阅规则")
    self._refresh_actions()


def set_probe_busy(self, rule_id: str | None, busy: bool):
    self._probe_rule_id = rule_id if busy else None
    self._probe_busy = busy
    self.probe_cancel_button.setVisible(busy)
    self._refresh_actions()


def set_probe_progress(self, progress: SubscriptionProbeProgress | None):
    self.probe_progress_label.setVisible(progress is not None)
    self.probe_progress_label.setText("" if progress is None else self._format_probe_progress(progress))


def set_probe_result(self, report: SubscriptionProbeReport | None):
    self._probe_report = report
    self.probe_result_label.setText("" if report is None else explain_probe(report))
    self.probe_sample_model.set_samples(() if report is None else report.samples)
    self.set_probe_busy(None, False)


def show_probe_cancelled(self):
    self.probe_result_label.setText("测试已取消；规则、游标和下载队列均未改变")
    self.set_probe_busy(None, False)
```

Every setter calls `_refresh_actions`. Selection clears stale samples, emits one history request, and updates rule labels immediately. General rule mutations and probing share one busy contract so buttons cannot submit conflicting work.

- [ ] **Step 5: Run page tests and commit**

Run: `python -m pytest tests/ui/test_subscriptions.py tests/ui/test_subscription_diagnostics.py tests/ui/test_main_window.py -q`

Expected: PASS.

```powershell
git add src/telegram_downloader/ui/subscriptions.py tests/ui/test_subscriptions.py
git commit -m "feat: add subscription diagnostics workspace"
```

### Task 6: Controller probe lifecycle, foreground priority, and app wiring

**Files:**
- Modify: `src/telegram_downloader/controller.py`
- Modify: `src/telegram_downloader/app.py`
- Modify: `tests/test_subscription_controller.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write failing controller lifecycle tests**

Create event-controlled tests proving:

```python
@pytest.mark.asyncio
async def test_probe_is_foreground_and_repeated_request_is_deduplicated(controller, service):
    first = asyncio.create_task(controller.probe_subscription("rule-1"))
    await service.probe_started.wait()
    assert controller.foreground_telegram_busy()
    await controller.probe_subscription("rule-1")
    assert service.probe_calls == ["rule-1"]
    service.probe_release.set()
    await first
    assert not controller.foreground_telegram_busy()

@pytest.mark.asyncio
async def test_probe_cancel_restores_page_without_rule_or_task_changes(controller, service, page):
    before = service.snapshot()
    running = asyncio.create_task(controller.probe_subscription("rule-1"))
    await service.probe_started.wait()
    controller.cancel_subscription_probe()
    await running
    assert service.snapshot() == before
    assert page.cancelled_messages == 1
    assert page.probe_busy is False

@pytest.mark.asyncio
async def test_account_switch_cancels_probe_before_rebinding_services(controller, service):
    running = asyncio.create_task(controller.probe_subscription("rule-1"))
    await service.probe_started.wait()
    await controller._cancel_subscription_probe()
    service.set_account(AccountProfile("a2", "第二账号"))
    await running
    assert service.events[:2] == ["probe-cancelled", "account:a2"]

@pytest.mark.asyncio
async def test_shutdown_awaits_probe_before_gateway_disconnect(controller, service, gateway):
    running = asyncio.create_task(controller.probe_subscription("rule-1"))
    await service.probe_started.wait()
    await controller.shutdown()
    await running
    assert controller.lifecycle_events.index("probe-stopped") < controller.lifecycle_events.index("gateway-disconnected")
    assert gateway.connected is False

def test_rule_selection_loads_only_latest_twenty_runs(controller, service, page):
    service.runs = [subscription_run(id=f"run-{index}") for index in range(25)]
    controller.show_subscription_details("rule-1")
    assert service.list_run_limits == [20]
    assert len(page.detail_runs) == 20
```

Assert exact call order, one service invocation, monotonic page progress, safe error rendering, and `_subscription_probe_task is None` after every terminal path.

- [ ] **Step 2: Run controller tests to verify RED**

Run: `python -m pytest tests/test_subscription_controller.py tests/test_app.py -q`

Expected: FAIL because probe lifecycle and UI wiring are missing.

- [ ] **Step 3: Implement controller operations**

Add `show_subscription_details(rule_id)`, `probe_subscription(rule_id)`, `cancel_subscription_probe()`, and `_cancel_subscription_probe()`. `probe_subscription` owns exactly one task, marks the page busy before the first await, forwards progress, and in `finally` clears task/progress/busy state and reloads details. `asyncio.CancelledError` shows the non-error cancelled state; other exceptions use `_safe_error`.

Extend `foreground_telegram_busy()` with an active probe check. Call `_cancel_subscription_probe()` before account rebind, logout, credential replacement, session-expired handling and shutdown. Never cancel the background subscription runner from the page probe button.

- [ ] **Step 4: Wire Qt signals through `AsyncActionBridge`**

Connect rule selection synchronously to history loading, probe requests under key `subscriptions.probe`, and cancel directly to `cancel_subscription_probe`. Retain dynamic slots in `_ui_slots`; expose progress callbacks via the existing event-loop thread only.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest tests/test_subscription_controller.py tests/test_app.py tests/test_controller.py -q`

Expected: PASS with no un-awaited task warnings.

```powershell
git add src/telegram_downloader/controller.py src/telegram_downloader/app.py tests/test_subscription_controller.py tests/test_app.py tests/test_controller.py
git commit -m "feat: integrate cancellable subscription probes"
```

### Task 7: Recovery, privacy, version, and packaging contracts

**Files:**
- Modify: `src/telegram_downloader/app.py`
- Modify: `tests/test_self_test.py`
- Modify: `tests/test_logging.py`
- Modify: `tests/test_paths.py`
- Modify: `tests/test_packaging_contract.py`
- Modify: `tests/test_installer_contract.py`
- Modify: `pyproject.toml`
- Modify: `src/telegram_downloader/__init__.py`
- Modify: `installer/TelegramDownloader.iss`
- Modify: `README.md`
- Create: `docs/releases/v0.6.0.md`

- [ ] **Step 1: Write failing privacy and recovery contracts**

Assert self-test reports catalog schema 3, diagnostic modules do not reference `APPDATA`, `LOCALAPPDATA`, `tempfile`, QSettings, task scheduler or service APIs, and captured logs never contain a probe keyword, dialog title, excerpt or file name. Assert frozen-module discovery includes `subscription_diagnostics` and `ui.subscription_diagnostics`.

- [ ] **Step 2: Bump the local candidate to v0.6.0**

Update all three version declarations and packaging tests. Do not alter trusted update keys, online stable pointers, Git tags, GitHub releases or ModelScope assets.

- [ ] **Step 3: Document diagnostics guarantees**

README and `docs/releases/v0.6.0.md` must state: recent 20 runs, read-only latest-100 probe, 20-sample limit, cancellation, no cursor/task/history changes from probing, explainable outcomes, in-memory samples, and application-directory-only data.

- [ ] **Step 4: Run boundary and packaging tests**

Run: `python -m pytest tests/test_self_test.py tests/test_logging.py tests/test_paths.py tests/test_packaging_contract.py tests/test_installer_contract.py -q`

Expected: PASS with schema 3 and no sensitive captured values.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/app.py tests/test_self_test.py tests/test_logging.py tests/test_paths.py tests/test_packaging_contract.py tests/test_installer_contract.py pyproject.toml src/telegram_downloader/__init__.py installer/TelegramDownloader.iss README.md docs/releases/v0.6.0.md
git commit -m "docs: prepare subscription diagnostics release"
```

### Task 8: End-to-end diagnostics probe and restart verification

**Files:**
- Modify: `tests/test_subscription_e2e.py`
- Create: `tests/test_subscription_diagnostics_e2e.py`

- [ ] **Step 1: Add an isolated synthetic end-to-end test**

Use real `PortablePaths`, catalog, task repository, planner, subscription service and both schedulers with a fake gateway. Prove:

```python
report = await service.probe_rule(rule.id)
assert report.keyword_hits == 2
assert report.matched == 2
assert report.duplicate == 1
assert repository.list_tasks() == before_tasks
assert service.get_rule(rule.id).last_message_id == before_cursor
assert catalog.list_subscription_runs("a1", rule.id) == before_runs
```

Then run the formal rule, complete its download, restart repositories/services, verify `keyword_hits` history and cursor survive, and prove a second probe still creates no task.

- [ ] **Step 2: Add real Qt event-level journey coverage**

Build the actual `SubscriptionPage`, controller fakes and event loop. Select a rule, start probe, issue a duplicate click, cancel, restart probe, deliver a report, inspect model rows, switch account and close. Assert all buttons, active action keys and tasks return to idle.

- [ ] **Step 3: Run complete focused diagnostics suite**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_subscription_diagnostics.py tests/test_subscription_service.py tests/test_subscription_controller.py tests/test_subscription_diagnostics_e2e.py tests/ui/test_subscription_diagnostics.py tests/ui/test_subscriptions.py -q
& .\.venv\Scripts\python.exe -m ruff check src tests
```

Expected: PASS and Ruff clean.

- [ ] **Step 4: Commit**

```powershell
git add tests/test_subscription_e2e.py tests/test_subscription_diagnostics_e2e.py
git commit -m "test: verify subscription diagnostics end to end"
```

### Task 9: Full verification, real-account QA, and Windows delivery

**Files:**
- Modify only if failures reveal defects.
- Create: `docs/verification/v0.6.0-subscription-diagnostics.md`

- [ ] **Step 1: Run three fresh regression rounds**

Round 1: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1`

Round 2 is the complete test/static phase inside `scripts/build-installer.ps1`.

Round 3: rerun `scripts/test.ps1` after all QA-driven fixes and documentation.

Expected each time: every test passes and Ruff reports no errors.

- [ ] **Step 2: Perform non-destructive real-account source UI QA**

Copy only the existing project-local encrypted settings/session into a new ignored `.build-temp/subscription-diagnostics-real-qa` runtime. Use a random unique keyword, create a temporary rule, run and cancel the latest-100 probe, repeat-click while active, complete a probe, immediately run the formal rule, inspect history, restart and verify persistence, then delete only the temporary rule. Assert no QR appears and task count is unchanged.

- [ ] **Step 3: Inspect the actual Qt screenshot**

Save a Qt-owned screenshot under the ignored QA runtime and inspect it at 1280×768 and 1024×720. Verify no clipped headers, overlapping columns, hidden cancel action or unreadable detail pane. If defects appear, add a failing pytest-qt size/layout contract before fixing them.

- [ ] **Step 4: Build and smoke-test Windows artifacts**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-installer.ps1`

Expected: `PACKAGED_SMOKE_OK`, `INSTALLER_SMOKE_OK`, portable v0.6.0 ZIP and setup EXE. Installer must reject C, install under the project test root, preserve data through upgrade/uninstall and report schema 3.

- [ ] **Step 5: Audit archive privacy and frozen modules**

Count ZIP entries and reject matches for `data/`, `downloads/`, databases, logs or `secrets.dat`. Use `pyi-archive_viewer -r -b` to prove both diagnostics modules are frozen. Record exact artifact bytes and SHA-256.

- [ ] **Step 6: Write and commit verification evidence**

Create `docs/verification/v0.6.0-subscription-diagnostics.md` with commands, exit codes, test counts, real-account booleans, UI observations, schema/path inventory, hashes, limitations and remote-publication boundary. Do not include account IDs, dialog names, keywords, excerpts, filenames or credentials.

```powershell
git diff --check
git add docs/verification/v0.6.0-subscription-diagnostics.md
git commit -m "docs: verify subscription diagnostics"
```

- [ ] **Step 7: Final scope audit and local merge**

Compare every success criterion in `docs/superpowers/specs/2026-08-15-subscription-diagnostics-design.md` to code, tests and runtime evidence. Use `verification-before-completion`, merge the clean feature branch into local `main`, rerun `scripts/test.ps1` on the merged result, copy the verified ZIP/setup/direct-run folder to main, and keep remote GitHub/ModelScope pointers unchanged.
