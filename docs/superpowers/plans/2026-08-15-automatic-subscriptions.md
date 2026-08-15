# Automatic Subscriptions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add project-local, application-runtime Telegram subscription rules that establish a safe baseline, incrementally scan new messages, and automatically enqueue matching media through the existing resumable download pipeline.

**Architecture:** Add isolated subscription domain models and catalog schema v2 persistence, two narrow incremental gateway methods, a service that converts deterministic message ranges into deduplicated download tasks, and a single-worker async scheduler with backoff and foreground priority. A dedicated PySide6 page talks only to `AppController`; all state remains under existing `ApplicationPaths` and no Windows service or scheduler is introduced.

**Tech Stack:** Python 3.12, asyncio/qasync, PySide6, Telethon, SQLite, pytest/pytest-qt, Ruff, PyInstaller, Inno Setup.

---

### Task 1: Subscription domain and catalog schema v2

**Files:**
- Create: `src/telegram_downloader/subscriptions.py`
- Modify: `src/telegram_downloader/catalog.py`
- Create: `tests/test_subscriptions.py`
- Modify: `tests/test_catalog.py`

- [ ] **Step 1: Write failing domain validation tests**

```python
def test_subscription_rule_requires_keyword_media_and_supported_interval(now):
    with pytest.raises(ValueError, match="关键词"):
        rule(now, keyword="")
    with pytest.raises(ValueError, match="媒体类型"):
        rule(now, media_kinds=frozenset())
    with pytest.raises(ValueError, match="检查间隔"):
        rule(now, interval_minutes=7)


def test_subscription_rule_normalizes_keyword(now):
    assert rule(now, keyword="  美 女  ").normalized_keyword == "美 女"
```

- [ ] **Step 2: Run domain tests and verify failure**

Run: `python -m pytest tests/test_subscriptions.py -q`

Expected: FAIL because `telegram_downloader.subscriptions` does not exist.

- [ ] **Step 3: Implement focused immutable models**

```python
class SubscriptionState(StrEnum):
    BASELINING = "baselining"
    WAITING = "waiting"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_NETWORK = "waiting_network"
    AUTH_REQUIRED = "auth_required"
    FAILED = "failed"


class SubscriptionRunStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class SubscriptionRule:
    id: str
    account_id: str
    peer_ref: str
    dialog_title: str
    keyword: str
    media_kinds: frozenset[MediaKind]
    interval_minutes: int
    enabled: bool
    state: SubscriptionState
    last_message_id: int | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_error: str | None
    failure_count: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.keyword.strip():
            raise ValueError("订阅关键词不能为空")
        if not self.media_kinds:
            raise ValueError("请至少选择一种媒体类型")
        if self.interval_minutes not in {5, 15, 30, 60, 180}:
            raise ValueError("不支持的检查间隔")
        if self.last_message_id is not None and self.last_message_id < 0:
            raise ValueError("消息游标不能为负数")

    @property
    def normalized_keyword(self) -> str:
        return " ".join(self.keyword.casefold().split())


@dataclass(frozen=True, slots=True)
class SubscriptionProgress:
    rule_id: str
    inspected: int
    matched: int
    queued: int
    duplicate: int
    phase: str


@dataclass(frozen=True, slots=True)
class SubscriptionDraft:
    peer_ref: str
    keyword: str
    media_kinds: frozenset[MediaKind]
    interval_minutes: int = 30


@dataclass(frozen=True, slots=True)
class SubscriptionRun:
    id: str
    rule_id: str
    account_id: str
    started_at: datetime
    finished_at: datetime
    status: SubscriptionRunStatus
    inspected: int
    matched: int
    queued: int
    duplicate: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SubscriptionRunReport:
    run: SubscriptionRun
    task_ids: tuple[str, ...]
    last_processed_id: int
    has_more: bool
```

- [ ] **Step 4: Write failing schema migration and repository tests**

```python
def test_catalog_migrates_v1_to_v2_without_losing_searches(tmp_path, now):
    database = tmp_path / "catalog.sqlite3"
    create_v1_catalog_with_search(database, now)
    repo = CatalogRepository(database)
    repo.initialize()
    assert sqlite_user_version(database) == 2
    assert repo.list_sessions("a1")[0].keyword == "美女"


def test_subscription_crud_is_account_isolated(tmp_path, now):
    repo = initialized_catalog(tmp_path)
    repo.save_subscription(rule(now, account_id="a1"))
    repo.save_subscription(rule(now, id="r2", account_id="a2"))
    assert [item.id for item in repo.list_subscriptions("a1")] == ["r1"]
    with pytest.raises(KeyError):
        repo.get_subscription("a1", "r2")


def test_advancing_subscription_cursor_is_monotonic(tmp_path, now):
    repo = initialized_catalog(tmp_path)
    repo.save_subscription(rule(now, last_message_id=10))
    repo.advance_subscription("a1", "r1", 15, now)
    with pytest.raises(ValueError, match="倒退"):
        repo.advance_subscription("a1", "r1", 14, now)
```

- [ ] **Step 5: Add schema v2 and typed repository methods**

Add `_SCHEMA_V2_MIGRATION` with `subscription_rules` and `subscription_runs`, foreign keys to `accounts` and `dialogs`, indexes on `(account_id, enabled, next_run_at)` and `(rule_id, started_at DESC)`, and `PRAGMA user_version=2`.

Implement the exact public methods `save_subscription(rule)`, `get_subscription(account_id, rule_id)`, `list_subscriptions(account_id)`, `list_due_subscriptions(account_id, now)`, `delete_subscription(account_id, rule_id)`, `advance_subscription(account_id, rule_id, message_id, now)`, `update_subscription_runtime(account_id, rule_id, state, next_run_at, last_run_at, last_error, failure_count, now)`, and `save_subscription_run(run, retain=100)` with the return types defined by the domain models.

Use comma-separated sorted `MediaKind.value` values consistently with search persistence. Every update includes `account_id` and checks `rowcount`; deletion cascades only to `subscription_runs`.

- [ ] **Step 6: Run domain/catalog tests**

Run: `python -m pytest tests/test_subscriptions.py tests/test_catalog.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add src/telegram_downloader/subscriptions.py src/telegram_downloader/catalog.py tests/test_subscriptions.py tests/test_catalog.py
git commit -m "feat: persist automatic subscription rules"
```

### Task 2: Telegram incremental message gateway

**Files:**
- Modify: `src/telegram_downloader/gateway.py`
- Modify: `tests/test_gateway.py`

- [ ] **Step 1: Write failing latest-ID and bounded-order tests**

```python
@pytest.mark.asyncio
async def test_latest_message_id_returns_zero_for_empty_dialog():
    gateway, client = test_gateway(messages=[])
    assert await gateway.latest_message_id("peer:1") == 0


@pytest.mark.asyncio
async def test_incremental_messages_are_oldest_first_and_bounded():
    gateway, client = test_gateway(messages=[message(13), message(11), message(12)])
    values = await gateway.incremental_messages("peer:1", after_id=10, through_id=12, limit=500)
    assert [item.message_id for item in values] == [11, 12]
```

- [ ] **Step 2: Run gateway tests and verify failure**

Run: `python -m pytest tests/test_gateway.py -q`

Expected: FAIL because the protocol and gateway methods are missing.

- [ ] **Step 3: Add the transport-neutral message type and protocol methods**

```python
@dataclass(frozen=True, slots=True)
class RemoteMessage:
    message_id: int
    grouped_id: int | None
    message_date_utc: datetime
    text: str
    media: RemoteMedia | None


```

Add `latest_message_id(entity_ref: str) -> int` and `incremental_messages(entity_ref: str, *, after_id: int, through_id: int, limit: int) -> tuple[RemoteMessage, ...]` to `TelegramGateway` and `TelethonGateway`.

- [ ] **Step 4: Implement Telethon bounded reads with existing error mapping**

Resolve the entity through `_resolve_entity`. `latest_message_id` reads one message and returns zero when absent. `incremental_messages` calls `iter_messages(entity, min_id=after_id, max_id=through_id + 1, reverse=True, limit=limit)`, converts dates to UTC, uses `_message_excerpt` for text, and calls `remote_media_from_message` only when media exists. Reject negative bounds and limits outside `1..500` before network access. Wrap Telethon exceptions through `_raise_mapped`.

- [ ] **Step 5: Run gateway regression tests**

Run: `python -m pytest tests/test_gateway.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/gateway.py tests/test_gateway.py
git commit -m "feat: read bounded Telegram message ranges"
```

### Task 3: Planner support and incremental subscription service

**Files:**
- Modify: `src/telegram_downloader/planner.py`
- Create: `src/telegram_downloader/subscription_service.py`
- Modify: `tests/test_planner.py`
- Create: `tests/test_subscription_service.py`

- [ ] **Step 1: Write failing planner test for an automatic task**

```python
def test_plan_subscription_uses_archive_layout_and_rule_title(planner, remote, now):
    preview = planner.plan_subscription("peer:1", "群组", "美女", [remote])
    assert preview.task.display_title == "群组（自动订阅：美女）"
    assert preview.task.source_url == "telegram://peer/peer:1"
    assert preview.items[0].target_path.is_relative_to(planner.downloads)
```

- [ ] **Step 2: Add `TaskPlanner.plan_subscription`**

Build a `ScanFilters` from the minimum and maximum media UTC dates, the selected media kinds, and `max(1, len(remote))`, then call `_build_preview` with `SourceKind.CHANNEL_OR_GROUP`, `skip_existing=True`, and display title `f"{source_title}（自动订阅：{keyword}）"`.

- [ ] **Step 3: Write failing service tests**

```python
@pytest.mark.asyncio
async def test_create_rule_establishes_baseline_without_queueing(service, gateway, now):
    gateway.latest_id = 42
    saved = await service.create_rule(draft(now))
    assert saved.last_message_id == 42
    assert planner.commits == []


@pytest.mark.asyncio
async def test_run_queues_only_matching_new_media_and_advances(service, gateway, now):
    gateway.latest_id = 45
    gateway.messages = (
        remote_message(43, text="普通内容", media=photo(43)),
        remote_message(44, text="美女写真", media=photo(44)),
        remote_message(45, text="美女视频", media=video(45)),
    )
    report = await service.run_rule("r1")
    assert report.inspected == 3
    assert report.matched == 2
    assert report.queued == 2
    assert catalog.get_subscription("a1", "r1").last_message_id == 45


@pytest.mark.asyncio
async def test_network_failure_does_not_advance_cursor(service, gateway):
    gateway.incremental_error = TransientNetworkError("offline")
    with pytest.raises(TransientNetworkError):
        await service.run_rule("r1")
    assert catalog.get_subscription("a1", "r1").last_message_id == 42
```

- [ ] **Step 4: Implement `SubscriptionService`**

The service owns catalog/planner/gateway references and an account binding. Implement:

Implement `SubscriptionService` with the exact public operations `bind_online`, `go_offline`, `set_account`, `list_rules`, `create_rule`, `update_rule`, `set_enabled`, `delete_rule`, and `run_rule`. `create_rule`/`update_rule` return `SubscriptionRule`; `run_rule` returns `SubscriptionRunReport`; all account access is derived from the bound `AccountProfile` rather than accepted as an untrusted UI argument.

Normalize matching with `normalized_keyword in " ".join(message.text.casefold().split())`. Expand a matched grouped item through existing `expand_album`, filter album media by rule kinds, deduplicate remote keys, ask the planner for existing keys, plan and commit only new media, then advance the cursor after all messages through the returned page have deterministic outcomes. If the page reaches its 500-message cap before the fixed snapshot, set `has_more=True` so the scheduler can continue after yielding. Editing `peer_ref` or the normalized keyword establishes a fresh latest-message baseline before saving; editing only kinds or interval retains the existing cursor.

- [ ] **Step 5: Cover albums, overlaps, empty pages and commit races**

Add tests proving one matching album queues all allowed album items, duplicate media count is reported, no-match scans still advance, an `AllMediaAlreadyExists` race is treated as duplicates, and `SessionExpiredError`, `FloodWaitError`, `TransientNetworkError`, cancellation and unknown errors leave the prior cursor intact.

- [ ] **Step 6: Run service/planner tests**

Run: `python -m pytest tests/test_planner.py tests/test_subscription_service.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add src/telegram_downloader/planner.py src/telegram_downloader/subscription_service.py tests/test_planner.py tests/test_subscription_service.py
git commit -m "feat: enqueue incremental subscription matches"
```

### Task 4: Single-worker scheduler, backoff and foreground priority

**Files:**
- Create: `src/telegram_downloader/subscription_scheduler.py`
- Create: `tests/test_subscription_scheduler.py`

- [ ] **Step 1: Write failing scheduler behavior tests**

Create six concrete async tests named `test_scheduler_runs_one_due_rule_at_a_time`, `test_manual_wake_deduplicates_same_rule`, `test_foreground_busy_defers_without_marking_failure`, `test_transient_failures_back_off_1_2_5_15_minutes`, `test_flood_wait_uses_server_delay`, and `test_shutdown_cancels_wait_and_awaits_active_run`. Each test uses an event-controlled fake service, starts the real runner, waits with a one-second timeout, and asserts persisted runtime calls plus zero remaining runner tasks after shutdown.

- [ ] **Step 2: Run scheduler tests and verify failure**

Run: `python -m pytest tests/test_subscription_scheduler.py -q`

Expected: FAIL because the scheduler is missing.

- [ ] **Step 3: Implement scheduler lifecycle**

Implement `SubscriptionScheduler` with constructor dependencies `service`, `clock`, `sleeper`, `foreground_busy`, `on_rules_changed`, and `on_task_created`, plus public methods `start()`, `set_account(account_id)`, `wake(rule_id=None)`, and `shutdown()`.

Use one runner task and one `asyncio.Event`; never run more than one rule concurrently. Explicit wakes are kept in an ordered set. Due rules come from catalog through the service. When `foreground_busy()` is true, retain the wake and wait briefly. On success schedule `finished_at + interval`; when `report.has_more` schedule a five-second continuation. Persist transient backoff using `(1, 2, 5, 15)[min(failure_count, 3)]`, FloodWait using the server duration, auth failures as `AUTH_REQUIRED` with no due time, and access loss as paused/failed with no automatic spin.

- [ ] **Step 4: Run scheduler tests**

Run: `python -m pytest tests/test_subscription_scheduler.py -q`

Expected: PASS with no un-awaited task warnings.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/subscription_scheduler.py tests/test_subscription_scheduler.py
git commit -m "feat: schedule automatic subscription checks"
```

### Task 5: Subscription page and editor

**Files:**
- Create: `src/telegram_downloader/ui/subscriptions.py`
- Create: `src/telegram_downloader/ui/subscription_models.py`
- Modify: `src/telegram_downloader/ui/main.py`
- Create: `tests/ui/test_subscriptions.py`
- Modify: `tests/ui/test_main_window.py`

- [ ] **Step 1: Write failing page interaction tests**

Create concrete pytest-qt tests named `test_rule_editor_requires_dialog_keyword_and_media`, `test_page_emits_create_edit_run_pause_and_delete`, `test_busy_rule_disables_duplicate_actions_but_not_navigation`, `test_progress_and_next_run_are_visible`, and `test_main_window_navigates_to_subscriptions`. Drive each button with `qtbot.mouseClick`, capture signals with `qtbot.waitSignal`, and assert button text/enabled state before and after `set_rule_busy`.

- [ ] **Step 2: Implement `SubscriptionTableModel`**

Columns are “订阅规则 / 群组或频道 / 状态 / 上次结果 / 下次检查”. Store the rule ID in `Qt.UserRole`, provide accessible Chinese status text, and keep sorting deterministic by enabled state, next run, title and ID.

- [ ] **Step 3: Implement the page and modal editor**

Define `SubscriptionPage` signals `activated`, `create_requested(object)`, `update_requested(str, object)`, `run_requested(str)`, `enabled_requested(str, bool)`, and `delete_requested(str)`. Implement complete setters `set_logged_in`, `set_dialogs`, `set_rules`, `set_rule_busy`, `set_progress`, and `show_error`; every setter must update the matching label/model and call one shared `_refresh_actions` method.

The editor exposes one current dialog, a trimmed keyword, six media checkboxes and the supported interval combo. Delete confirmation explicitly states that queued tasks and files are retained. The page has immediate busy text for create/edit/run/pause/delete.

- [ ] **Step 4: Add main navigation**

Create `subscriptions_nav_button`, add `SubscriptionPage` to `page_stack`, emit `subscriptions_activated` when shown, hide the task statistics rail on both content and subscription pages, and include the new button in active styling.

- [ ] **Step 5: Run UI tests**

Run: `python -m pytest tests/ui/test_subscriptions.py tests/ui/test_main_window.py -q`

Expected: PASS under the existing offscreen Qt fixture.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/ui/subscriptions.py src/telegram_downloader/ui/subscription_models.py src/telegram_downloader/ui/main.py tests/ui/test_subscriptions.py tests/ui/test_main_window.py
git commit -m "feat: add automatic subscriptions workspace"
```

### Task 6: Controller and application wiring

**Files:**
- Modify: `src/telegram_downloader/controller.py`
- Modify: `src/telegram_downloader/app.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write failing controller lifecycle tests**

Add event-controlled controller tests named `test_account_activation_binds_subscription_account_and_wakes_scheduler`, `test_create_rule_refreshes_page_and_scheduler`, `test_rule_task_is_started_by_download_scheduler`, `test_logout_keeps_rules_but_stops_account_scheduler`, and `test_shutdown_stops_subscription_scheduler_before_gateway`. Assert exact call order, page snapshots, busy-state restoration, `_start_task` IDs, and that rule deletion is never called during logout.

- [ ] **Step 2: Inject null-safe subscription dependencies**

Add `_NullSubscriptionPage`, `_NullSubscriptionService` and `_NullSubscriptionScheduler` test defaults. Extend `AppController.__init__` with `subscriptions` and `subscription_scheduler`, and expose `foreground_telegram_busy()` returning true while dialog sync, content search, QR/login transition or shutdown is active.

- [ ] **Step 3: Implement controller actions**

Implement controller actions `activate_subscriptions_page`, `create_subscription`, `update_subscription`, `set_subscription_enabled`, `run_subscription_now`, and `delete_subscription` with the parameter and return types from `SubscriptionService` and `SubscriptionPage`.

Each action sets local page busy state before work and clears it in `finally`. Account activation binds both content and subscription services, refreshes cached rules immediately, and wakes due work only after an online account is known. Scheduler task callbacks call `refresh_tasks()` and `_start_task(task_id)`.

- [ ] **Step 4: Wire app services and Qt signals**

Construct `SubscriptionService(catalog)` and `SubscriptionScheduler` from existing objects. `build_services` calls `subscriptions.bind_online(gateway, planner)` without changing its established return tuple. Connect page signals through qasync slots/`AsyncActionBridge`, retain slot references in `_ui_slots`, and ensure controller shutdown awaits the subscription scheduler before the download scheduler and Telegram disconnect.

- [ ] **Step 5: Run controller/app tests**

Run: `python -m pytest tests/test_controller.py tests/test_app.py -q`

Expected: PASS with actions restoring busy state on success, failure and cancellation.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/controller.py src/telegram_downloader/app.py tests/test_controller.py tests/test_app.py
git commit -m "feat: integrate automatic subscriptions"
```

### Task 7: Recovery, privacy and project-local path regression

**Files:**
- Modify: `src/telegram_downloader/bootstrap.py`
- Modify: `tests/test_bootstrap.py`
- Modify: `tests/test_paths.py`
- Modify: `tests/test_logging.py`
- Modify: `tests/test_self_test.py`

- [ ] **Step 1: Add recovery and privacy tests**

Test that interrupted `RUNNING` rules become due `WAITING` rules at startup without moving their cursor, rule errors are passed through existing safe error rendering/log redaction, and a self-test inventory contains catalog v2 but no account ID, keyword, message text or session material.

- [ ] **Step 2: Add startup recovery**

Implement `CatalogRepository.recover_interrupted_subscriptions(now)` to convert only `RUNNING`/`BASELINING` records to a recoverable state, preserve paused/auth-required rules, retain the last cursor and clear impossible past in-memory claims. Call it after catalog initialization.

- [ ] **Step 3: Assert all paths remain local**

Extend path and packaging tests to assert there are no references to `APPDATA`, `LOCALAPPDATA`, `QSettings`, `tempfile`, Windows Task Scheduler APIs or service APIs in subscription modules. The only persisted subscription data must be `paths.catalog_database` and existing download paths.

- [ ] **Step 4: Run boundary tests**

Run: `python -m pytest tests/test_bootstrap.py tests/test_paths.py tests/test_logging.py tests/test_self_test.py -q`

Expected: PASS and no secret values in captured output.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/bootstrap.py tests/test_bootstrap.py tests/test_paths.py tests/test_logging.py tests/test_self_test.py
git commit -m "test: protect subscription recovery and local data boundaries"
```

### Task 8: Version, user documentation and release contract

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/telegram_downloader/__init__.py`
- Modify: `README.md`
- Create: `docs/releases/v0.5.0.md`
- Modify: `tests/test_packaging_contract.py`
- Modify: `tests/test_installer_contract.py`

- [ ] **Step 1: Bump local release version to 0.5.0**

Update both package version declarations and remove stale README references to v0.3.1/current build names. Do not modify online `stable` pointers, create remote releases or publish assets without a separate explicit publishing request.

- [ ] **Step 2: Document automatic subscriptions**

README must explain application-runtime scheduling, baseline-only first enable, supported intervals, pause/edit/delete semantics, offline/auth behavior, automatic queue reuse, the 500-message chunk and project-local database path. Add `docs/releases/v0.5.0.md` with the same user-facing guarantees and limitations.

- [ ] **Step 3: Extend packaging contracts**

Assert the frozen build imports subscription modules and that neither portable ZIP nor installer source includes `data/database/catalog.sqlite3`, local logs, sessions, rules or downloads.

- [ ] **Step 4: Run documentation/packaging tests**

Run: `python -m pytest tests/test_packaging_contract.py tests/test_installer_contract.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml src/telegram_downloader/__init__.py README.md docs/releases/v0.5.0.md tests/test_packaging_contract.py tests/test_installer_contract.py
git commit -m "docs: prepare automatic subscriptions release"
```

### Task 9: Full verification, build and real-use QA

**Files:**
- Modify only if failures reveal defects.
- Create: `docs/verification/v0.5.0-automatic-subscriptions.md`

- [ ] **Step 1: Run focused tests and Ruff**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_subscriptions.py tests/test_catalog.py tests/test_gateway.py tests/test_subscription_service.py tests/test_subscription_scheduler.py tests/ui/test_subscriptions.py tests/test_controller.py tests/test_app.py -q
& .\.venv\Scripts\python.exe -m ruff check src tests
```

Expected: all focused tests pass and Ruff reports no errors.

- [ ] **Step 2: Run the complete automated suite**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1`

Expected: every test passes with no Qt task destruction, un-awaited coroutine or Telethon disconnect warnings.

- [ ] **Step 3: Perform an isolated synthetic end-to-end probe**

Use a project-local temporary runtime under `.build-temp/subscription-e2e`, a fake gateway and the real catalog, planner, task repository, subscription service and both schedulers. Prove baseline creates no task, two later matching media create one queued task, duplicate rerun creates none, restart preserves cursor, and resulting paths stay beneath the probe root.

- [ ] **Step 4: Perform non-destructive real-account UI QA**

Launch the source application with the existing project-local encrypted session. Open “自动订阅”, create a rule in an already joined dialog using a unique harmless keyword, confirm baseline feedback, immediately check, pause/continue, edit interval, restart, and confirm the rule/cursor remains. Delete only the temporary QA rule. Verify `data/logs/app.log` contains no credentials or full message text and no unexpected QR login occurred.

- [ ] **Step 5: Build and inspect the portable package**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1`

Expected: `dist/TelegramDownloader-0.5.0-win-x64-portable.zip` passes packaged `--self-test`, opens a visible window, and contains no `data/`, `downloads/`, `.release-secrets/` or local database entries.

- [ ] **Step 6: Build and smoke-test the installer**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-installer.ps1`

Expected: `dist/release/TelegramDownloader-0.5.0-win-x64-setup.exe` rejects C-drive installation, installs and self-tests on the configured non-C test root, and normal uninstall preserves test data.

- [ ] **Step 7: Record requirement-by-requirement evidence**

Write `docs/verification/v0.5.0-automatic-subscriptions.md` containing exact commands, exit codes, test counts, artifact hashes, real QA observations, data-boundary inventory and any limitation. Run `git diff --check` and ensure `git status --short` contains only intended evidence changes.

- [ ] **Step 8: Commit verification evidence**

```powershell
git add docs/verification/v0.5.0-automatic-subscriptions.md
git commit -m "docs: verify automatic subscriptions"
```

- [ ] **Step 9: Final scope audit**

Compare every success criterion in `docs/superpowers/specs/2026-08-15-automatic-subscriptions-design.md` against code, tests and runtime evidence. Do not claim completion while any criterion lacks direct evidence.
