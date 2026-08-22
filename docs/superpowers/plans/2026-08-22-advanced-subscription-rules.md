# Advanced Subscription Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add multi-keyword include/exclude matching with AND/OR semantics and restart-safe 0/1/3/7/30-day historical catch-up while preserving every existing single-keyword subscription.

**Architecture:** Put all rule normalization, validation, matching, summaries, and fingerprints in a pure `subscription_matching.py` module. Persist structured criteria plus a fixed backfill start/snapshot in catalog schema 5; let `SubscriptionService` own baseline and catch-up lifecycle, while the Telegram gateway only resolves the message ID immediately before a UTC boundary. The UI and diagnostics consume the same immutable criteria object so probing and scheduled execution cannot diverge.

**Tech Stack:** Python 3.12, frozen dataclasses, asyncio, SQLite, Telethon, PySide6, pytest/pytest-asyncio/pytest-qt, Ruff, PyInstaller, Inno Setup.

---

## File map

- Create `src/telegram_downloader/subscription_matching.py`: pure structured criteria and matching.
- Modify `src/telegram_downloader/subscriptions.py`: criteria-bearing drafts/rules and backfill state.
- Modify `src/telegram_downloader/catalog.py`: schema 5 migration and structured rule persistence.
- Modify `src/telegram_downloader/gateway.py`: UTC history-boundary lookup.
- Modify `src/telegram_downloader/subscription_service.py`: baseline, fixed-snapshot catch-up, and shared matcher.
- Modify `src/telegram_downloader/subscription_diagnostics.py`: “规则命中” explanations.
- Modify `src/telegram_downloader/planner.py`: accept a stable criteria summary for subscription task naming.
- Modify `src/telegram_downloader/ui/subscriptions.py`: advanced rule editor and details.
- Modify `src/telegram_downloader/ui/subscription_models.py`: summary display and stable sorting.
- Modify `src/telegram_downloader/ui/subscription_diagnostics.py`: “规则” column label.
- Modify subscription/catalog/gateway/UI/E2E tests and their rule builders for the new model.
- Modify version metadata, README, release notes, packaging contract, and verification record for v0.14.0.

### Task 1: Build the pure structured matcher

**Files:**
- Create: `src/telegram_downloader/subscription_matching.py`
- Create: `tests/test_subscription_matching.py`

- [ ] **Step 1: Write failing truth-table and validation tests**

```python
import pytest

from telegram_downloader.subscription_matching import (
    SubscriptionCriteria,
    SubscriptionMatchMode,
)


@pytest.mark.parametrize(
    ("criteria", "text", "expected"),
    [
        (SubscriptionCriteria(("AI", "模型")), "新的 ai 工具", True),
        (SubscriptionCriteria(("AI", "模型")), "只有新闻", False),
        (
            SubscriptionCriteria(("AI", "大 模型"), mode=SubscriptionMatchMode.ALL),
            "AI   大\n模型 发布",
            True,
        ),
        (
            SubscriptionCriteria(("AI",), ("广告",)),
            "AI 广告",
            False,
        ),
    ],
)
def test_structured_subscription_matching(criteria, text, expected):
    assert criteria.matches(text) is expected


def test_criteria_deduplicates_terms_and_rejects_conflicts():
    value = SubscriptionCriteria((" AI ", "ai", "模型"), (" 广告 ", "广告"))
    assert value.include_keywords == ("AI", "模型")
    assert value.exclude_keywords == ("广告",)
    with pytest.raises(ValueError, match="不能同时"):
        SubscriptionCriteria(("AI",), (" ai ",))


def test_fingerprint_is_order_independent_but_semantics_sensitive():
    first = SubscriptionCriteria(("AI", "模型"), ("广告",))
    reordered = SubscriptionCriteria(("模型", "AI"), ("广告",))
    all_terms = SubscriptionCriteria(
        ("AI", "模型"), ("广告",), SubscriptionMatchMode.ALL
    )
    assert first.fingerprint == reordered.fingerprint
    assert first.fingerprint != all_terms.fingerprint


def test_matching_uses_unicode_casefold_and_collapsed_whitespace():
    criteria = SubscriptionCriteria(("STRASSE  模型",))
    assert criteria.matches("Straße\n模型") is True


@pytest.mark.parametrize(
    "criteria",
    [
        lambda: SubscriptionCriteria(tuple(f"词-{number}" for number in range(21))),
        lambda: SubscriptionCriteria(("词" * 101,)),
        lambda: SubscriptionCriteria(
            tuple(f"{number:02d}" + "词" * 98 for number in range(20)),
            ("排" * 100,),
        ),
    ],
)
def test_criteria_rejects_quantity_and_length_limits(criteria):
    with pytest.raises(ValueError):
        criteria()
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_subscription_matching.py -q`

Expected: collection fails because `telegram_downloader.subscription_matching` does not exist.

- [ ] **Step 3: Implement the immutable matcher**

```python
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum


class SubscriptionMatchMode(StrEnum):
    ANY = "any"
    ALL = "all"


def normalize_subscription_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _clean_terms(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    found: list[str] = []
    normalized: set[str] = set()
    for raw in values:
        value = raw.strip()
        if not value:
            continue
        if len(value) > 100:
            raise ValueError(f"{label}单项不能超过 100 个字符")
        key = normalize_subscription_text(value)
        if key not in normalized:
            normalized.add(key)
            found.append(value)
    if len(found) > 20:
        raise ValueError(f"{label}最多 20 个")
    return tuple(found)


@dataclass(frozen=True, slots=True)
class SubscriptionCriteria:
    include_keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...] = ()
    mode: SubscriptionMatchMode = SubscriptionMatchMode.ANY

    def __post_init__(self) -> None:
        includes = _clean_terms(self.include_keywords, "包含词")
        excludes = _clean_terms(self.exclude_keywords, "排除词")
        if not includes:
            raise ValueError("请至少输入一个包含词")
        if sum(map(len, includes + excludes)) > 2000:
            raise ValueError("全部订阅词组合计不能超过 2000 个字符")
        overlap = set(map(normalize_subscription_text, includes)) & set(
            map(normalize_subscription_text, excludes)
        )
        if overlap:
            raise ValueError("同一个词不能同时出现在包含词和排除词中")
        object.__setattr__(self, "include_keywords", includes)
        object.__setattr__(self, "exclude_keywords", excludes)

    @property
    def fingerprint(self) -> str:
        payload = {
            "exclude": sorted(map(normalize_subscription_text, self.exclude_keywords)),
            "include": sorted(map(normalize_subscription_text, self.include_keywords)),
            "mode": self.mode.value,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def summary(self) -> str:
        label = "全部" if self.mode is SubscriptionMatchMode.ALL else "任意"
        value = f"{label}：{'、'.join(self.include_keywords)}"
        if self.exclude_keywords:
            value += f"；排除：{'、'.join(self.exclude_keywords)}"
        return value

    def matches(self, text: str) -> bool:
        normalized = normalize_subscription_text(text)
        include_hits = [
            normalize_subscription_text(term) in normalized
            for term in self.include_keywords
        ]
        included = all(include_hits) if self.mode is SubscriptionMatchMode.ALL else any(include_hits)
        excluded = any(
            normalize_subscription_text(term) in normalized
            for term in self.exclude_keywords
        )
        return included and not excluded
```

- [ ] **Step 4: Run matcher tests and Ruff**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_subscription_matching.py -q`

Run: `.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/subscription_matching.py tests/test_subscription_matching.py`

Expected: all matcher tests pass and Ruff prints `All checks passed!`.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/subscription_matching.py tests/test_subscription_matching.py
git commit -m "feat: define structured subscription matching"
```

### Task 2: Move subscription drafts and rules to structured criteria

**Files:**
- Modify: `src/telegram_downloader/subscriptions.py`
- Modify: `tests/test_subscriptions.py`
- Modify: subscription rule builders in `tests/test_catalog.py`, `tests/test_subscription_service.py`, `tests/test_subscription_scheduler.py`, `tests/test_subscription_controller.py`, `tests/test_subscription_e2e.py`, `tests/test_subscription_diagnostics_e2e.py`, `tests/ui/test_subscriptions.py`

- [ ] **Step 1: Replace the old keyword tests with failing structured-state tests**

```python
from telegram_downloader.subscription_matching import SubscriptionCriteria


def test_subscription_draft_validates_history_and_exposes_summary():
    draft = SubscriptionDraft(
        " peer:1 ",
        SubscriptionCriteria(("AI", "模型"), ("广告",)),
        frozenset({MediaKind.PHOTO}),
        15,
        7,
    )
    assert draft.peer_ref == "peer:1"
    assert draft.keyword == "任意：AI、模型；排除：广告"
    assert draft.matcher_fingerprint == draft.criteria.fingerprint

    with pytest.raises(ValueError, match="历史补抓"):
        replace(draft, history_days=2)


def test_backfill_snapshot_requires_a_persisted_start_time():
    with pytest.raises(ValueError, match="补抓起点"):
        rule(backfill_through_id=99, backfill_from_utc=None)
```

- [ ] **Step 2: Run and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_subscriptions.py -q`

Expected: constructor/type failures because drafts and rules still use `keyword`.

- [ ] **Step 3: Update the domain model**

Implement these authoritative fields and compatibility properties:

```python
SUPPORTED_HISTORY_DAYS = frozenset({0, 1, 3, 7, 30})


@dataclass(frozen=True, slots=True)
class SubscriptionDraft:
    peer_ref: str
    criteria: SubscriptionCriteria
    media_kinds: frozenset[MediaKind]
    interval_minutes: int = 30
    history_days: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "peer_ref", self.peer_ref.strip())
        _validate_rule_fields(
            self.peer_ref, self.media_kinds, self.interval_minutes, self.history_days
        )

    @property
    def keyword(self) -> str:
        return self.criteria.summary

    @property
    def matcher_fingerprint(self) -> str:
        return self.criteria.fingerprint


@dataclass(frozen=True, slots=True)
class SubscriptionRule:
    id: str
    account_id: str
    peer_ref: str
    dialog_title: str
    criteria: SubscriptionCriteria
    media_kinds: frozenset[MediaKind]
    interval_minutes: int
    history_days: int
    enabled: bool
    state: SubscriptionState
    last_message_id: int | None
    backfill_from_utc: datetime | None
    backfill_through_id: int | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_error: str | None
    failure_count: int
    created_at: datetime
    updated_at: datetime

    @property
    def keyword(self) -> str:
        return self.criteria.summary

    @property
    def normalized_keyword(self) -> str:
        return self.criteria.fingerprint
```

Validate `history_days`, non-negative cursors, and reject `backfill_through_id` without `backfill_from_utc`. Update all test builders to pass `SubscriptionCriteria(("美女",))`, `history_days=0`, and two `None` backfill fields.

- [ ] **Step 4: Run domain and direct-consumer tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_subscriptions.py tests/test_subscription_scheduler.py tests/test_subscription_controller.py -q`

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/subscriptions.py tests/test_subscriptions.py tests/test_subscription_scheduler.py tests/test_subscription_controller.py tests/test_subscription_e2e.py tests/test_subscription_diagnostics_e2e.py tests/ui/test_subscriptions.py tests/test_catalog.py tests/test_subscription_service.py
git commit -m "refactor: model structured subscription rules"
```

### Task 3: Migrate catalog schema 4 to structured schema 5

**Files:**
- Modify: `src/telegram_downloader/catalog.py`
- Modify: `tests/test_catalog.py`
- Modify: `tests/test_self_test.py`
- Modify: `tests/test_diagnostic_probes.py`
- Modify: `tests/test_packaging_contract.py`

- [ ] **Step 1: Add failing migration and round-trip tests**

```python
def create_v4_catalog_with_subscription(
    database: Path,
    now: datetime,
    *,
    keyword: str = "资料",
) -> Path:
    create_v2_catalog_with_run(database, now)
    with sqlite3.connect(database) as connection:
        connection.executescript(catalog_module._SCHEMA_V3_MIGRATION)
        connection.executescript(catalog_module._SCHEMA_V4_MIGRATION)
        connection.execute(
            "UPDATE subscription_rules SET keyword=?, normalized_keyword=? WHERE id=?",
            (keyword, " ".join(keyword.casefold().split()), "r1"),
        )
    return database


def test_catalog_migrates_single_keyword_rules_to_schema_five(tmp_path):
    now = datetime(2026, 8, 14, tzinfo=UTC)
    database = tmp_path / "catalog.sqlite3"
    create_v4_catalog_with_subscription(database, now, keyword=" 资料 ")

    repository = CatalogRepository(database)
    repository.initialize()

    saved = repository.get_subscription("a1", "r1")
    assert repository.schema_version() == 5
    assert saved.criteria == SubscriptionCriteria(("资料",))
    assert saved.history_days == 0
    assert saved.last_message_id == 10
    assert saved.state is SubscriptionState.WAITING
    assert repository.list_subscription_runs("a1", "r1")[0].id == "run-old"


def test_structured_subscription_round_trip_preserves_backfill_state(tmp_path):
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repository = CatalogRepository(tmp_path / "catalog.sqlite3")
    repository.initialize()
    repository.upsert_account(AccountProfile("a1", "账号"), now)
    repository.replace_dialogs(
        "a1", [dialog("a1", "-1001", "群-a1", now)], now
    )
    saved = replace(
        subscription("a1", "-1001", now),
        criteria=SubscriptionCriteria(("AI", "模型"), ("广告",)),
        history_days=7,
        backfill_from_utc=now - timedelta(days=7),
        backfill_through_id=900,
    )
    repository.save_subscription(saved)
    assert repository.get_subscription("a1", saved.id) == saved


def test_schema_five_migration_rolls_back_all_rows_on_invalid_legacy_rule(
    tmp_path, monkeypatch
):
    now = datetime(2026, 8, 14, tzinfo=UTC)
    database = create_v4_catalog_with_subscription(
        tmp_path / "catalog.sqlite3", now
    )
    monkeypatch.setattr(
        catalog_module,
        "SubscriptionCriteria",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("invalid")),
    )
    with pytest.raises(ValueError, match="invalid"):
        CatalogRepository(database).initialize()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(subscription_rules)")
        }
    assert "include_keywords_json" not in columns
```

- [ ] **Step 2: Run and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_catalog.py -q`

Expected: schema remains 4 and structured columns are missing.

- [ ] **Step 3: Add an atomic Python migration and serialization helpers**

Set `CATALOG_SCHEMA_VERSION = 5`. Add columns with individual `connection.execute(...)` calls inside the existing transaction, then migrate each row with `json.dumps(..., ensure_ascii=False)` and `SubscriptionCriteria((old_keyword,))`. Set both `matcher_fingerprint` and legacy `normalized_keyword` to `criteria.fingerprint`; set legacy `keyword` to `criteria.summary`; finally execute `PRAGMA user_version=5`.

Use explicit JSON helpers:

```python
def _criteria_json(values: tuple[str, ...]) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _criteria_from_row(row: sqlite3.Row) -> SubscriptionCriteria:
    return SubscriptionCriteria(
        tuple(json.loads(str(row["include_keywords_json"]))),
        tuple(json.loads(str(row["exclude_keywords_json"]))),
        SubscriptionMatchMode(str(row["match_mode"])),
    )
```

Extend `save_subscription` to persist all seven schema-5 fields and verify `rule.criteria.fingerprint == rule.normalized_keyword`. Extend `_subscription_from_row` to restore criteria, history days, and UTC backfill values. Keep the old unique constraint operational through the canonical fingerprint.

- [ ] **Step 4: Add an atomic cursor/backfill completion operation**

Extend `advance_subscription` with `complete_backfill: bool = False`. Its single `UPDATE` must advance `last_message_id` and, when complete, set both `backfill_from_utc` and `backfill_through_id` to `NULL`. Keep the existing monotonic-cursor check.

```python
repo.advance_subscription(
    "a1", saved.id, 900, NOW, complete_backfill=True
)
completed = repo.get_subscription("a1", saved.id)
assert completed.last_message_id == 900
assert completed.backfill_from_utc is None
assert completed.backfill_through_id is None
```

- [ ] **Step 5: Run catalog and packaging contract tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_catalog.py tests/test_self_test.py tests/test_diagnostic_probes.py tests/test_packaging_contract.py -q`

Expected: all pass with schema 5 assertions.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/catalog.py tests/test_catalog.py tests/test_self_test.py tests/test_diagnostic_probes.py tests/test_packaging_contract.py
git commit -m "feat: persist advanced subscription rules"
```

### Task 4: Resolve an exact UTC history boundary in the Telegram gateway

**Files:**
- Modify: `src/telegram_downloader/gateway.py`
- Modify: `tests/test_gateway.py`
- Modify gateway fakes in subscription tests

- [ ] **Step 1: Add failing boundary tests**

```python
@pytest.mark.asyncio
async def test_message_id_before_uses_utc_date_cursor_and_returns_boundary():
    cutoff = datetime(2026, 8, 10, 12, tzinfo=UTC)

    class Client:
        def __init__(self):
            self.calls = []

        async def get_entity(self, value):
            return SimpleNamespace(id=value)

        def iter_messages(self, entity, **kwargs):
            self.calls.append(kwargs)

            async def generate():
                yield SimpleNamespace(id=73)

            return generate()

    client = Client()
    gateway = TelethonGateway.from_client_for_test(client)
    assert await gateway.message_id_before("-1001", cutoff) == 73
    assert client.calls == [{"offset_date": cutoff, "limit": 1}]


@pytest.mark.asyncio
async def test_message_id_before_returns_zero_for_no_older_message():
    class Client:
        async def get_entity(self, value):
            return SimpleNamespace(id=value)

        def iter_messages(self, _entity, **_kwargs):
            async def generate():
                if False:
                    yield None

            return generate()

    cutoff = datetime(2026, 8, 10, 12, tzinfo=UTC)
    gateway = TelethonGateway.from_client_for_test(Client())
    assert await gateway.message_id_before("-1001", cutoff) == 0
```

- [ ] **Step 2: Run and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_gateway.py -k message_id_before -q`

Expected: `TelethonGateway` has no `message_id_before` method.

- [ ] **Step 3: Extend the protocol and Telethon adapter**

```python
async def message_id_before(
    self,
    entity_ref: str,
    before_utc: datetime,
) -> int:
    if before_utc.tzinfo is None:
        raise ValueError("历史边界必须包含时区")
    before_utc = before_utc.astimezone(UTC)
    try:
        entity = await self._resolve_entity(entity_ref)
        async for message in self._client.iter_messages(
            entity, offset_date=before_utc, limit=1
        ):
            value = getattr(message, "id", None)
            return int(value) if isinstance(value, int) else 0
        return 0
    except Exception as exc:
        self._raise_mapped(exc)
```

Add the signature to `TelegramGateway`. Add `boundary_id`, `boundary_calls`, `latest_calls`, and matching async methods/counters to all gateway fakes used by subscription tests.

- [ ] **Step 4: Run gateway tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_gateway.py -q`

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/gateway.py tests/test_gateway.py tests/test_subscription_service.py tests/test_subscription_e2e.py tests/test_subscription_diagnostics_e2e.py
git commit -m "feat: locate subscription history boundaries"
```

### Task 5: Establish restart-safe no-history and historical baselines

**Files:**
- Modify: `src/telegram_downloader/subscription_service.py`
- Modify: `tests/test_subscription_service.py`

- [ ] **Step 1: Add failing creation and retry tests**

```python
@pytest.mark.asyncio
async def test_create_historical_rule_locks_cutoff_and_snapshot(tmp_path):
    service, gateway, catalog, tasks = build_service(tmp_path)
    gateway.latest_id = 900
    gateway.boundary_id = 300

    saved = await service.create_rule(
        SubscriptionDraft(
            "-1001",
            SubscriptionCriteria(("AI",)),
            frozenset({MediaKind.PHOTO}),
            history_days=7,
        )
    )

    assert gateway.boundary_calls == [("-1001", NOW - timedelta(days=7))]
    assert saved.last_message_id == 300
    assert saved.backfill_from_utc == NOW - timedelta(days=7)
    assert saved.backfill_through_id == 900
    assert saved.next_run_at == NOW
    assert tasks.list_tasks() == []
    assert catalog.get_subscription("a1", saved.id) == saved


@pytest.mark.asyncio
async def test_failed_history_baseline_retries_original_cutoff(tmp_path):
    service, gateway, _, _ = build_service(tmp_path)
    gateway.latest_error = TransientNetworkError("offline")
    draft = SubscriptionDraft(
        "-1001",
        SubscriptionCriteria(("AI",)),
        frozenset({MediaKind.PHOTO}),
        history_days=3,
    )
    with pytest.raises(TransientNetworkError):
        await service.create_rule(draft)
    failed = service.list_rules()[0]
    assert failed.backfill_from_utc == NOW - timedelta(days=3)

    gateway.latest_error = None
    gateway.latest_id = 500
    gateway.boundary_id = 100
    await service.run_rule(failed.id)
    assert gateway.boundary_calls[-1] == ("-1001", NOW - timedelta(days=3))
```

- [ ] **Step 2: Run and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_subscription_service.py -k "historical_rule or history_baseline" -q`

Expected: structured historical baseline fields are not populated.

- [ ] **Step 3: Centralize baseline establishment**

Add `_pending_cutoff(draft, now)` and async `_establish_baseline(rule)`. Save the initial rule before network access. For `history_days == 0`, set the cursor to the latest ID, clear backfill fields, and schedule at the normal interval. For historical rules, reuse the persisted `backfill_from_utc`, get the boundary ID, lock `backfill_through_id` to the latest snapshot, and schedule immediately. Preserve paused rules by keeping `enabled=False`, `state=PAUSED`, and `next_run_at=None` after a successful edit baseline.

On error, keep the original cutoff, set `FAILED`, increment failure count, and leave the rule retryable. When `run_rule` sees `last_message_id is None`, call `_establish_baseline`; return an empty completed run only when no catch-up remains.

- [ ] **Step 4: Add edit-reset tests and implement semantic comparison**

```python
original_draft = SubscriptionDraft(
    "-1001",
    SubscriptionCriteria(("AI",)),
    frozenset({MediaKind.PHOTO}),
    30,
    0,
)
interval_only = await service.update_rule(
    saved.id, replace(original_draft, interval_minutes=60)
)
assert interval_only.last_message_id == saved.last_message_id
assert gateway.latest_calls == 1  # creation only

changed = await service.update_rule(
    saved.id,
    replace(original_draft, criteria=SubscriptionCriteria(("模型",)), history_days=7),
)
assert changed.backfill_through_id == gateway.latest_id
assert changed.next_run_at == NOW
```

Implement one `_requires_rebaseline(current, draft)` predicate comparing peer, criteria fingerprint, media kinds, and history days. Do not include interval or enabled state.

- [ ] **Step 5: Run service tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_subscription_service.py -q`

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/subscription_service.py tests/test_subscription_service.py
git commit -m "feat: establish historical subscription baselines"
```

### Task 6: Execute fixed-snapshot catch-up across pages and restarts

**Files:**
- Modify: `src/telegram_downloader/subscription_service.py`
- Modify: `src/telegram_downloader/catalog.py`
- Modify: `tests/test_subscription_service.py`
- Modify: `tests/test_subscription_e2e.py`

- [ ] **Step 1: Add a failing three-page catch-up test**

```python
@pytest.mark.asyncio
async def test_history_catchup_uses_fixed_snapshot_and_clears_state_on_last_page(tmp_path):
    service, gateway, catalog, _ = build_service(tmp_path)
    gateway.latest_id = 1200
    gateway.boundary_id = 0
    saved = await service.create_rule(
        SubscriptionDraft(
            "-1001",
            SubscriptionCriteria(("AI",)),
            frozenset({MediaKind.PHOTO}),
            history_days=7,
        )
    )
    gateway.latest_id = 1300  # arrived after the fixed snapshot
    gateway.messages = tuple(
        message(number, "AI", remote(number)) for number in range(1, 1301)
    )

    first = await service.run_rule(saved.id)
    second = await service.run_rule(saved.id)
    third = await service.run_rule(saved.id)

    assert gateway.incremental_calls == [
        (0, 1200, 500),
        (500, 1200, 500),
        (1000, 1200, 500),
    ]
    assert first.has_more is second.has_more is True
    assert third.has_more is False
    completed = catalog.get_subscription("a1", saved.id)
    assert completed.last_message_id == 1200
    assert completed.backfill_from_utc is None
    assert completed.backfill_through_id is None

    await service.run_rule(saved.id)
    assert gateway.incremental_calls[-1] == (1200, 1300, 500)
```

- [ ] **Step 2: Run and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_subscription_service.py -k fixed_snapshot -q`

Expected: the service reads a new latest snapshot for each page or fails to clear backfill state.

- [ ] **Step 3: Implement fixed-through paging**

Select `through_id = rule.backfill_through_id` whenever present; only call `latest_message_id` for normal incremental runs. Continue to use `PAGE_LIMIT = 500`. Set `last_processed_id` to the last returned ID while another page remains, otherwise to `through_id`. Call `advance_subscription(..., complete_backfill=True)` only on the final catch-up page. Keep the existing five-second reschedule for `has_more=True`.

- [ ] **Step 4: Add cancellation, failure, duplicate, and restart tests**

Tests must prove:

```python
before = catalog.get_subscription("a1", saved.id)
gateway.incremental_error = TransientNetworkError("offline")
with pytest.raises(TransientNetworkError):
    await service.run_rule(saved.id)
after = catalog.get_subscription("a1", saved.id)
assert after.last_message_id == before.last_message_id
assert after.backfill_through_id == before.backfill_through_id

reopened_catalog = CatalogRepository(catalog.database)
reopened_catalog.initialize()
reopened = SubscriptionService(
    reopened_catalog,
    uuid_factory=ids("reopened-subscription"),
    clock=lambda: NOW,
)
reopened_planner = TaskPlanner(
    gateway,
    tasks,
    tmp_path / "downloads",
    uuid_factory=ids("reopened-task"),
    clock=lambda: NOW,
)
reopened.bind_online(gateway, reopened_planner)
reopened.set_account(AccountProfile("a1", "账号"))
assert reopened.get_rule(saved.id).last_message_id == 500
```

Also assert planner dedupe prevents a rebaselined rule from creating a second task for the same media key.

- [ ] **Step 5: Run service and E2E tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_subscription_service.py tests/test_subscription_e2e.py -q`

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/subscription_service.py src/telegram_downloader/catalog.py tests/test_subscription_service.py tests/test_subscription_e2e.py
git commit -m "feat: resume paged subscription catch-up"
```

### Task 7: Share matching semantics across runs, probes, diagnostics, and tasks

**Files:**
- Modify: `src/telegram_downloader/subscription_service.py`
- Modify: `src/telegram_downloader/subscription_diagnostics.py`
- Modify: `src/telegram_downloader/planner.py`
- Modify: `src/telegram_downloader/ui/subscription_diagnostics.py`
- Modify: `tests/test_subscription_service.py`
- Modify: `tests/test_subscription_diagnostics.py`
- Modify: `tests/ui/test_subscription_diagnostics.py`
- Modify: `tests/test_planner.py`

- [ ] **Step 1: Add a failing run/probe parity test**

```python
@pytest.mark.asyncio
async def test_probe_and_scheduled_run_share_advanced_matcher(tmp_path):
    service, gateway, _, _ = build_service(tmp_path)
    saved = await service.create_rule(
        SubscriptionDraft(
            "-1001",
            SubscriptionCriteria(
                ("AI", "模型"),
                ("广告",),
                SubscriptionMatchMode.ALL,
            ),
            frozenset({MediaKind.PHOTO}),
        )
    )
    messages = (
        message(43, "AI 模型", remote(43)),
        message(44, "AI 模型 广告", remote(44)),
        message(45, "只有 AI", remote(45)),
    )
    gateway.recent = messages
    gateway.messages = messages
    gateway.latest_id = 45

    probe = await service.probe_rule(saved.id)
    run = await service.run_rule(saved.id)
    assert (probe.inspected, probe.keyword_hits, probe.matched) == (3, 1, 1)
    assert (run.run.inspected, run.run.keyword_hits, run.run.matched) == (3, 1, 1)
```

- [ ] **Step 2: Run and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_subscription_service.py -k advanced_matcher -q`

Expected: `_match_messages` still performs single-keyword substring matching.

- [ ] **Step 3: Route all matching through `criteria.matches`**

Replace the inline normalized-keyword comparison with `rule.criteria.matches(item.text)`. Increment `keyword_hits` only after includes pass and exclusions do not. Keep media and album behavior unchanged. Pass `rule.criteria.summary` to `planner.plan_subscription`.

- [ ] **Step 4: Change user-visible diagnostics to “规则命中”**

Update safe explanations:

```python
if run.keyword_hits == 0:
    return f"扫描 {run.inspected} 条，新消息未命中规则"
if run.matched == 0:
    return f"规则命中 {run.keyword_hits} 条消息，但没有所选媒体类型"
```

Change `SubscriptionRunHistoryModel.HEADERS` column four from `关键词` to `规则`. Keep database/property name `keyword_hits` for migration compatibility.

- [ ] **Step 5: Run matching, diagnostics, planner, and privacy tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_subscription_matching.py tests/test_subscription_service.py tests/test_subscription_diagnostics.py tests/test_planner.py tests/ui/test_subscription_diagnostics.py tests/test_notifications.py -q`

Expected: all pass; no notification includes criteria text.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/subscription_service.py src/telegram_downloader/subscription_diagnostics.py src/telegram_downloader/planner.py src/telegram_downloader/ui/subscription_diagnostics.py tests/test_subscription_service.py tests/test_subscription_diagnostics.py tests/test_planner.py tests/ui/test_subscription_diagnostics.py
git commit -m "feat: apply advanced subscription matching"
```

### Task 8: Build the advanced subscription editor and summaries

**Files:**
- Modify: `src/telegram_downloader/ui/subscriptions.py`
- Modify: `src/telegram_downloader/ui/subscription_models.py`
- Modify: `tests/ui/test_subscriptions.py`

- [ ] **Step 1: Add failing editor round-trip and validation tests**

```python
def test_editor_builds_advanced_rule_draft(qtbot):
    editor = SubscriptionEditorDialog([dialog()], parent=None)
    qtbot.addWidget(editor)
    editor.include_input.setPlainText("AI\n模型")
    editor.exclude_input.setPlainText("广告\n搬运")
    editor.match_mode_combo.setCurrentIndex(
        editor.match_mode_combo.findData(SubscriptionMatchMode.ALL.value)
    )
    editor.history_combo.setCurrentIndex(editor.history_combo.findData(7))

    draft = editor._form_draft()
    assert draft.criteria == SubscriptionCriteria(
        ("AI", "模型"), ("广告", "搬运"), SubscriptionMatchMode.ALL
    )
    assert draft.history_days == 7
    assert "最近 7 天" in editor.baseline_label.text()


def test_editor_keeps_open_for_include_exclude_conflict(qtbot):
    editor = SubscriptionEditorDialog([dialog()])
    qtbot.addWidget(editor)
    editor.include_input.setPlainText("AI")
    editor.exclude_input.setPlainText(" ai ")
    editor._validate_accept()
    assert editor.result() == 0
    assert "不能同时" in editor.error_label.text()


def test_async_save_failure_keeps_editor_open(qtbot):
    page = SubscriptionPage()
    qtbot.addWidget(page)
    page.set_logged_in(True)
    page.set_dialogs([dialog()])
    page.show()
    qtbot.mouseClick(page.new_button, Qt.MouseButton.LeftButton)
    editor = next(iter(page._editors))
    editor.include_input.setPlainText("AI")
    with qtbot.waitSignal(page.create_requested, timeout=500):
        qtbot.mouseClick(
            editor.buttons.button(QDialogButtonBox.StandardButton.Save),
            Qt.MouseButton.LeftButton,
        )
    assert editor.isVisible()
    assert editor.buttons.isEnabled() is False

    page.finish_editor_save(False, "网络暂不可用")
    assert editor.isVisible()
    assert editor.buttons.isEnabled() is True
    assert "网络暂不可用" in editor.error_label.text()

    page.finish_editor_save(True)
    assert editor.isVisible() is False
```

- [ ] **Step 2: Run and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ui/test_subscriptions.py -k "advanced_rule or include_exclude" -q`

Expected: advanced widgets do not exist.

- [ ] **Step 3: Replace the single keyword input**

Use two `QPlainTextEdit` controls with maximum height suitable for three lines, a mode `QComboBox` carrying `any/all`, and a history combo carrying `0/1/3/7/30`. Split input only on lines:

```python
def _lines(editor: QPlainTextEdit) -> tuple[str, ...]:
    return tuple(line for line in editor.toPlainText().splitlines() if line.strip())

criteria = SubscriptionCriteria(
    _lines(self.include_input),
    _lines(self.exclude_input),
    SubscriptionMatchMode(str(self.match_mode_combo.currentData())),
)
return SubscriptionDraft(
    str(peer_ref or ""),
    criteria,
    selected_kinds,
    int(self.interval_combo.currentData()),
    int(self.history_combo.currentData()),
)
```

Connect history changes to `_refresh_baseline_help()` and refill all fields when editing. Change the editor save button from immediate `accept()` to a `save_requested` signal: validate and cache the draft, disable the button box, but keep the dialog visible. `SubscriptionPage` retains the pending editor and exposes `finish_editor_save(success, error="")`; success calls `accept()`, while failure re-enables the buttons and shows the safe error inside the same editor. Do not connect `accepted` back to create/update, which would emit a duplicate request.

- [ ] **Step 4: Update list/detail summaries and model sorting**

Use `rule.criteria.summary` and append `不补抓历史` or `最近 N 天`. Sort by normalized summary only for display ordering; never parse it for matching. Ensure tooltips show the complete summary and tables elide long values.

- [ ] **Step 5: Run all subscription UI tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ui/test_subscriptions.py tests/ui/test_subscription_diagnostics.py -q`

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add src/telegram_downloader/ui/subscriptions.py src/telegram_downloader/ui/subscription_models.py tests/ui/test_subscriptions.py
git commit -m "feat: edit advanced subscription rules"
```

### Task 9: Complete application and end-to-end integration

**Files:**
- Modify: `src/telegram_downloader/controller.py`
- Modify: `tests/test_subscription_e2e.py`
- Modify: `tests/test_subscription_diagnostics_e2e.py`
- Modify: `tests/test_subscription_controller.py`

- [ ] **Step 1: Add a failing restart E2E contract**

The test must create a 7-day AND rule with an exclusion, process more than one page against a fixed snapshot, close all repositories/services, reopen them from the same application root, finish catch-up, and assert:

```python
assert reopened_rule.criteria == original.criteria
assert reopened_rule.backfill_through_id == 1200
assert resumed_gateway.incremental_calls[0] == (500, 1200, 500)
assert final_rule.last_message_id == 1200
assert final_rule.backfill_through_id is None
expected_unique_keys = {
    ("-1001", number, f"media-{number}") for number in (101, 502, 1100)
}
persisted_keys = {
    (item.peer_ref, item.message_id, item.media_id)
    for task in tasks.list_tasks()
    for item in tasks.list_items(task.id)
}
assert persisted_keys == expected_unique_keys
assert latest_run.keyword_hits == expected_rule_hits
```

It must use a temporary application root and fake Telegram gateway, with no credentials, network, registry, tray, or system notification access.

- [ ] **Step 2: Run and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_subscription_e2e.py tests/test_subscription_diagnostics_e2e.py -q`

Expected: at least the new restart contract fails until all app/service wiring uses structured drafts.

- [ ] **Step 3: Adapt signal/controller boundaries without weakening types**

Keep `create_requested` and `update_requested` carrying `SubscriptionDraft`; remove any remaining code that expects `draft.keyword`. Make controller create/update paths call `page.finish_editor_save(True)` only after service persistence and rule reload succeed. On `ValueError`, `CatalogError`, or existing service errors, call `page.finish_editor_save(False, controller._safe_error(error))`; the dialog remains open and the page busy state is still cleared in `finally`. Do not add test-only production hooks.

- [ ] **Step 4: Run all subscription-focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_subscriptions.py tests/test_subscription_matching.py tests/test_catalog.py tests/test_gateway.py tests/test_subscription_service.py tests/test_subscription_scheduler.py tests/test_subscription_controller.py tests/test_subscription_e2e.py tests/test_subscription_diagnostics.py tests/test_subscription_diagnostics_e2e.py tests/ui/test_subscriptions.py tests/ui/test_subscription_diagnostics.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add src/telegram_downloader/controller.py tests/test_subscription_e2e.py tests/test_subscription_diagnostics_e2e.py tests/test_subscription_controller.py
git commit -m "feat: integrate advanced subscriptions end to end"
```

### Task 10: Document, version, package, and verify v0.14.0

**Files:**
- Modify: `README.md`
- Modify: `pyproject.toml`
- Modify: `src/telegram_downloader/__init__.py`
- Modify: `installer/TelegramDownloader.iss`
- Modify: `tests/test_packaging_contract.py`
- Create: `docs/releases/v0.14.0.md`
- Create: `docs/verification/v0.14.0-advanced-subscription-rules.md`

- [ ] **Step 1: Add failing packaging/documentation assertions**

```python
assert project["project"]["version"] == "0.14.0"
assert '__version__ = "0.14.0"' in package_init
assert '#define AppVersion "0.14.0"' in installer
assert "多个包含词" in readme
assert "排除词" in readme
assert "最近 30 天" in readme
assert "CATALOG_SCHEMA_VERSION = 5" in catalog_source
assert (root / "docs/releases/v0.14.0.md").is_file()
```

- [ ] **Step 2: Run and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_packaging_contract.py -q`

Expected: version/document assertions still describe v0.13.0.

- [ ] **Step 3: Update public metadata and documentation**

Set all authoritative versions to 0.14.0. README must explain one-term-per-line input, AND/OR, exclusion priority, fixed history choices, one-time catch-up, 500-message paging, restart continuation, and unchanged dedupe/privacy behavior. Release notes must call out schema-4 migration compatibility. The verification record must list every automated and manual result truthfully; unavailable real-account scenarios remain “待执行”.

- [ ] **Step 4: Run complete source verification**

Run: `.\scripts\test.ps1`

Expected: all pytest tests pass and Ruff prints `All checks passed!`.

- [ ] **Step 5: Build and smoke-test frozen and installed upgrades**

Run: `.\scripts\build-installer.ps1`

Expected: `PACKAGED_SMOKE_OK`, `INSTALLER_SMOKE_OK`, schema 5 in frozen self-test, and both `TelegramDownloader-0.14.0-win-x64-portable.zip` and `TelegramDownloader-0.14.0-win-x64-setup.exe`.

- [ ] **Step 6: Perform focused Windows acceptance**

With an isolated application root, verify editor layout, validation, rule summaries, old schema-4 upgrade, close/reopen persistence, and no crash without credentials. With the user’s real Telegram account only when available, verify OR, AND, exclusion, no-history, 1-day catch-up, and restart continuation. Do not automate login dialogs or record real rule text, account, chat, message, or file metadata.

- [ ] **Step 7: Self-review the candidate**

Check every section of `docs/superpowers/specs/2026-08-22-advanced-subscription-rules-design.md` against code and tests. Search for stale single-keyword assumptions:

```powershell
rg -n "draft\.keyword|rule\.normalized_keyword|关键词输入|只建立当前位置|schema_version\(\) == 4|CATALOG_SCHEMA_VERSION = 4" src tests README.md docs/releases
git diff --check
```

Every result must be either removed or documented compatibility code. Review migration atomicity, fixed-snapshot boundaries, cursor advancement ordering, notification privacy, and source-mode data boundaries.

- [ ] **Step 8: Commit the candidate**

```powershell
git add README.md pyproject.toml src/telegram_downloader/__init__.py installer/TelegramDownloader.iss tests/test_packaging_contract.py docs/releases/v0.14.0.md docs/verification/v0.14.0-advanced-subscription-rules.md
git commit -m "release: prepare TelegramDownloader 0.14.0"
```

- [ ] **Step 9: Run final clean-tree verification**

Run: `git status --short`

Expected: no output.

Run: `.\scripts\test.ps1`

Expected: all tests pass and Ruff prints `All checks passed!` on the committed candidate.

## Plan self-review checklist

- The matcher has one source of truth for scheduled runs and probes.
- Exclusions are always evaluated after inclusion and always win.
- Fingerprints are insensitive to term order but sensitive to mode and exclusions.
- Old single-keyword rules migrate to one include term, OR, zero-day history without cursor/state loss.
- Historical baselines persist the original UTC cutoff before network access.
- Catch-up locks a finite snapshot, advances only after task commit, and clears state atomically on the last page.
- New messages arriving during catch-up remain for the next normal run.
- Only source/media/criteria/history changes rebaseline; interval and enabled state do not.
- No regex, custom dates, count-based history, multi-dialog rules, batch-link, global-slot, or naming-template scope has leaked into this plan.
- v0.14.0 packaging upgrades a schema-4 data directory without deleting tasks, subscriptions, runs, sessions, or downloads.
