# Progressive Media Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show direct Telegram search hits before album expansion and thumbnails finish, then reconcile a transactionally committed, stable result set.

**Architecture:** Add typed result batches beside existing progress events. `ContentBrowserService` emits generation-scoped provisional batches, runs bounded album work, and commits stable pages through one catalog transaction. Qt models apply keyed row differences, while thumbnail requests are shared by cache key and failed keys receive a short cooldown.

**Tech Stack:** Python 3.12, asyncio, SQLite, Telethon gateway contracts, PySide6 models, qasync, pytest, pytest-asyncio, pytest-qt, Ruff.

---

## File map

- Modify `src/telegram_downloader/content_progress.py`: immutable `SearchResultBatch` event.
- Modify `src/telegram_downloader/content_browser.py`: provisional direct-hit emission, stable IDs, bounded album reconciliation, and thumbnail request sharing.
- Modify `src/telegram_downloader/catalog.py`: one-transaction page commit returning the stable session and result set.
- Modify `src/telegram_downloader/controller.py`: generation/task guard and provisional/final UI forwarding.
- Modify `src/telegram_downloader/ui/content_models.py`: keyed insert/update/remove without model reset.
- Modify `src/telegram_downloader/ui/content_browser.py`: streamed-result entry point and queue safety.
- Modify `tests/test_content_progress.py`, `tests/test_content_browser.py`, `tests/test_catalog.py`, `tests/test_controller.py`, `tests/ui/test_content_models.py`, and `tests/ui/test_content_browser.py`.

### Task 1: Define generation-scoped result batches

**Files:**
- Modify: `src/telegram_downloader/content_progress.py`
- Modify: `tests/test_content_progress.py`

- [ ] **Step 1: Write the failing batch-contract test**

```python
# append to tests/test_content_progress.py
from datetime import UTC, datetime

from telegram_downloader.content import SearchResult
from telegram_downloader.content_progress import SearchResultBatch
from telegram_downloader.domain import MediaKind


def test_search_result_batch_is_immutable_and_generation_scoped() -> None:
    result = SearchResult(
        "r1", "s1", "a1", "peer", 7, None, "media", MediaKind.VIDEO,
        "clip.mp4", 12, datetime(2026, 8, 21, tzinfo=UTC), "caption", "thumb",
        True, False, False,
    )
    batch = SearchResultBatch("s1", 3, (result,), stable=False)
    assert batch.search_id == "s1"
    assert batch.generation == 3
    assert batch.results == (result,)
    assert batch.stable is False
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_progress.py::test_search_result_batch_is_immutable_and_generation_scoped -q
```

Expected: import fails because `SearchResultBatch` does not exist.

- [ ] **Step 3: Add the event type**

```python
# src/telegram_downloader/content_progress.py
from telegram_downloader.content import SearchResult


@dataclass(frozen=True, slots=True)
class SearchResultBatch:
    search_id: str
    generation: int
    results: tuple[SearchResult, ...]
    stable: bool

    def __post_init__(self) -> None:
        if not self.search_id or self.generation <= 0:
            raise ValueError("搜索结果批次缺少有效搜索代次")
        if any(result.search_id != self.search_id for result in self.results):
            raise ValueError("搜索结果批次包含其他搜索的数据")
```

- [ ] **Step 4: Run content-progress tests and Ruff**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_progress.py -q
.\.venv\Scripts\python.exe -m ruff check src\telegram_downloader\content_progress.py tests\test_content_progress.py
```

Expected: all selected checks pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/telegram_downloader/content_progress.py tests/test_content_progress.py
git commit -m "feat: define streamed search batches"
```

### Task 2: Emit direct hits before album expansion

**Files:**
- Modify: `src/telegram_downloader/content_browser.py:196-438, 719-751`
- Modify: `tests/test_content_browser.py`

- [ ] **Step 1: Write the failing ordering and stable-ID test**

```python
# append to tests/test_content_browser.py
@pytest.mark.asyncio
async def test_direct_hits_emit_before_blocked_album_expansion(tmp_path: Path) -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    service = await prepared_online_service(tmp_path, now, gateway)
    album_started = asyncio.Event()
    release_album = asyncio.Event()
    batches = []

    direct = make_hit(20, now)
    grouped = make_hit(19, now, grouped_id=900)
    gateway.pages = [RemoteSearchPage((direct, grouped), None, True)]

    async def expand_album(*_args):
        album_started.set()
        await release_album.wait()
        return (grouped, make_hit(18, now, grouped_id=900))

    gateway.expand_album = expand_album
    operation = asyncio.create_task(
        service.start_search("-1001", make_query(now), on_results=batches.append)
    )
    await album_started.wait()

    assert batches
    assert batches[0].stable is False
    assert {item.media_id for item in batches[0].results} == {"m20", "m19"}
    provisional_ids = {item.media_id: item.id for item in batches[0].results}

    release_album.set()
    session, results = await operation
    assert {item.media_id for item in results} == {"m20", "m19", "m18"}
    assert next(item.id for item in results if item.media_id == "m20") == provisional_ids["m20"]
    assert batches[-1].stable is True
    assert batches[-1].search_id == session.id
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_browser.py::test_direct_hits_emit_before_blocked_album_expansion -q
```

Expected: `start_search` rejects `on_results`.

- [ ] **Step 3: Thread the callback through search entry points**

Add this optional parameter to `start_search`, `load_more`, and `_fetch_page`:

```python
on_results: Callable[[SearchResultBatch], None] | None = None
```

After each remote page arrives, allocate one ID per media key and emit direct hits before awaiting album tasks:

```python
result_ids: dict[tuple[str, int, str], str] = {}

def result_for(hit: RemoteSearchHit) -> SearchResult:
    key = self._media_key(hit.remote)
    result_id = result_ids.setdefault(key, self.uuid_factory())
    return self._result_from_hit(
        account.account_id,
        session,
        hit,
        queued=False,
        result_id=result_id,
    )

if on_results is not None and page.items:
    on_results(
        SearchResultBatch(
            session.id,
            session.generation,
            tuple(result_for(hit) for hit in self._deduplicate_hits(page.items)),
            stable=False,
        )
    )
```

Change `_result_from_hit` to accept keyword-only `result_id: str | None = None` and use `result_id or self.uuid_factory()`. Reuse `result_for` for accepted direct and expanded hits so rows do not receive a second ID. Start all album tasks immediately, await them in deterministic trigger order, and retain the existing semaphore limit of four.

After stable results are read from the catalog, emit:

```python
if on_results is not None:
    on_results(SearchResultBatch(current.id, current.generation, tuple(results), stable=True))
```

- [ ] **Step 4: Run focused service tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_browser.py -q
```

Expected: all service tests pass, including album atomicity, cancellation, duplicate, and pagination cases.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/telegram_downloader/content_browser.py tests/test_content_browser.py
git commit -m "perf: stream direct search hits"
```

### Task 3: Commit each search page in one SQLite transaction

**Files:**
- Modify: `src/telegram_downloader/catalog.py:361-535`
- Modify: `src/telegram_downloader/content_browser.py:316-438`
- Modify: `tests/test_catalog.py`
- Modify: `tests/test_content_browser.py`

- [ ] **Step 1: Write a failing one-connection commit test**

```python
# append to tests/test_catalog.py
def test_commit_search_page_uses_one_connection_and_returns_stable_state(
    tmp_path, monkeypatch
) -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    catalog = CatalogRepository(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    account = AccountProfile("a1", "账号一")
    catalog.upsert_account(account, now)
    catalog.replace_dialogs("a1", [dialog("a1", "-1001", "群", now)], now)
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 20),
    )
    session = catalog.begin_search("s1", "a1", "-1001", "群", query, now)
    saved = result(session.id, account.account_id, now)
    opened = 0
    real_connection = catalog._connection

    @contextmanager
    def counted_connection():
        nonlocal opened
        opened += 1
        with real_connection() as connection:
            yield connection

    monkeypatch.setattr(catalog, "_connection", counted_connection)
    commit = catalog.commit_search_page(
        account.account_id,
        session.id,
        session.generation,
        [saved],
        cursor=None,
        complete=True,
        finished_at=session.updated_at,
        status=SearchStatus.COMPLETED,
        error=None,
    )
    assert opened == 1
    assert commit.session.status is SearchStatus.COMPLETED
    assert commit.results == (saved,)
    assert commit.result_count == 1
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_catalog.py::test_commit_search_page_uses_one_connection_and_returns_stable_state -q
```

Expected: `CatalogRepository` has no `commit_search_page`.

- [ ] **Step 3: Add the transaction return type and method**

```python
@dataclass(frozen=True, slots=True)
class SearchPageCommit:
    session: SearchSession
    results: tuple[SearchResult, ...]
    result_count: int


def commit_search_page(
    self,
    account_id: str,
    search_id: str,
    generation: int,
    results: list[SearchResult],
    *,
    cursor: SearchCursor | None,
    complete: bool,
    finished_at: datetime,
    status: SearchStatus,
    error: str | None,
) -> SearchPageCommit:
    with self._connection() as connection:
        session = connection.execute(
            "SELECT * FROM search_sessions WHERE account_id=? AND id=? AND generation=?",
            (account_id, search_id, generation),
        ).fetchone()
        if session is None:
            raise StaleSearchError("搜索结果已被更新的搜索代次取代")
        self._insert_search_results(connection, account_id, search_id, generation, results)
        count = int(connection.execute(
            "SELECT COUNT(*) FROM search_results WHERE account_id=? AND search_id=? AND generation=?",
            (account_id, search_id, generation),
        ).fetchone()[0])
        self._update_search_state(
            connection, account_id, search_id, generation, cursor,
            complete, finished_at, status, error, count,
        )
        session_row = connection.execute(
            "SELECT * FROM search_sessions WHERE account_id=? AND id=?",
            (account_id, search_id),
        ).fetchone()
        result_rows = self._select_result_rows(connection, account_id, search_id)
    return SearchPageCommit(
        self._session_from_row(session_row),
        tuple(self._result_from_row(row) for row in result_rows),
        count,
    )
```

Extract `_insert_search_results`, `_update_search_state`, and `_select_result_rows` from the exact SQL currently used by `save_search_page`, `finish_search`, and `list_results`; keep those public methods as compatibility wrappers around the helpers. `_select_result_rows` must preserve the existing generation/date/message ordering and source join behavior.

- [ ] **Step 4: Use `commit_search_page` in `_fetch_page`**

Replace the separate save, list, finish, get-session, and final list calls for each page with one `commit_search_page`. Use `commit.result_count` for limit checks, `commit.session` for cursor/complete state, and `list(commit.results)` for the returned UI rows.

- [ ] **Step 5: Run catalog and content tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_catalog.py tests\test_content_browser.py tests\test_account_wide_search_e2e.py -q
```

Expected: all selected tests pass and the new test observes one connection.

- [ ] **Step 6: Commit Task 3**

```powershell
git add src/telegram_downloader/catalog.py src/telegram_downloader/content_browser.py tests/test_catalog.py tests/test_content_browser.py
git commit -m "perf: commit search pages transactionally"
```

### Task 4: Apply result changes without resetting the Qt model

**Files:**
- Modify: `src/telegram_downloader/ui/content_models.py`
- Modify: `src/telegram_downloader/ui/content_browser.py`
- Modify: `tests/ui/test_content_models.py`
- Modify: `tests/ui/test_content_browser.py`

- [ ] **Step 1: Write failing incremental-model tests**

```python
# append to tests/ui/test_content_models.py
def test_apply_results_inserts_and_updates_without_model_reset(qtbot) -> None:
    model = SearchResultTableModel()
    first = search_results(datetime(2026, 8, 15, tzinfo=UTC))[0]
    second = replace(first, id="r2", message_id=2, media_id="m2")
    resets = QSignalSpy(model.modelReset)
    inserted = QSignalSpy(model.rowsInserted)

    model.apply_results([first])
    model.apply_results([replace(first, selected=True), second])

    assert len(resets) == 0
    assert len(inserted) >= 1
    assert model.rowCount() == 2
    assert model.result_at(0).selected is True


def test_apply_results_removes_missing_rows_without_reset(qtbot) -> None:
    model = SearchResultTableModel()
    values = search_results(datetime(2026, 8, 15, tzinfo=UTC))
    model.apply_results(values)
    removed = QSignalSpy(model.rowsRemoved)
    model.apply_results(values[:1])
    assert len(removed) == 1
    assert model.rowCount() == 1
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ui\test_content_models.py::test_apply_results_inserts_and_updates_without_model_reset tests\ui\test_content_models.py::test_apply_results_removes_missing_rows_without_reset -q
```

Expected: `SearchResultTableModel` has no `apply_results`.

- [ ] **Step 3: Implement keyed row reconciliation**

```python
def apply_results(self, results: list[SearchResult]) -> None:
    target = list(results)
    target_ids = {item.id for item in target}
    current = list(self._results)
    for row in range(len(current) - 1, -1, -1):
        if current[row].id not in target_ids:
            self.beginRemoveRows(QModelIndex(), row, row)
            current.pop(row)
            self.endRemoveRows()
    for row, wanted in enumerate(target):
        existing = next((i for i, item in enumerate(current) if item.id == wanted.id), None)
        if existing is None:
            self.beginInsertRows(QModelIndex(), row, row)
            current.insert(row, wanted)
            self.endInsertRows()
        else:
            if existing != row:
                self.beginRemoveRows(QModelIndex(), existing, existing)
                moved = current.pop(existing)
                self.endRemoveRows()
                self.beginInsertRows(QModelIndex(), row, row)
                current.insert(row, moved)
                self.endInsertRows()
            if current[row] != wanted:
                current[row] = wanted
                self._results = tuple(current)
                self.dataChanged.emit(
                    self.index(row, 0),
                    self.index(row, self.columnCount() - 1),
                )
    self._results = tuple(current)
```

Retain `set_results` for whole-search/account switches. Add `ContentBrowserPage.apply_search_batch(batch)` that calls `result_model.apply_results(list(batch.results))`, records the batch search ID/generation, requests visible thumbnails, and keeps queue actions disabled while `batch.stable` is false.

- [ ] **Step 4: Run all content model/page tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ui\test_content_models.py tests\ui\test_content_browser.py -q
```

Expected: all selected tests pass with no model reset in the new cases.

- [ ] **Step 5: Commit Task 4**

```powershell
git add src/telegram_downloader/ui/content_models.py src/telegram_downloader/ui/content_browser.py tests/ui/test_content_models.py tests/ui/test_content_browser.py
git commit -m "perf: update search results incrementally"
```

### Task 5: Share thumbnail requests and cool down failures

**Files:**
- Modify: `src/telegram_downloader/content_browser.py:70-96, 585-606`
- Modify: `tests/test_content_browser.py`

- [ ] **Step 1: Write failing request-sharing tests**

```python
async def thumbnail_service(tmp_path: Path):
    now = datetime(2026, 8, 21, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.pages = [RemoteSearchPage((make_hit(10, now),), None, True)]
    service = await prepared_online_service(tmp_path, now, gateway)
    _session, results = await service.start_search("-1001", make_query(now))
    return service, gateway, results[0].id


@pytest.mark.asyncio
async def test_thumbnail_requests_share_one_gateway_call(tmp_path: Path) -> None:
    service, gateway, result_id = await thumbnail_service(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def load_thumbnail(*_args):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return b"image"

    gateway.load_thumbnail = load_thumbnail
    first = asyncio.create_task(service.load_thumbnail(result_id))
    await started.wait()
    second = asyncio.create_task(service.load_thumbnail(result_id))
    release.set()
    assert await first == await second
    assert calls == 1


@pytest.mark.asyncio
async def test_thumbnail_failure_cooldown_skips_immediate_retry(tmp_path: Path) -> None:
    service, gateway, result_id = await thumbnail_service(tmp_path)
    calls = 0

    async def missing(*_args):
        nonlocal calls
        calls += 1
        return None

    gateway.load_thumbnail = missing
    assert await service.load_thumbnail(result_id) is None
    assert await service.load_thumbnail(result_id) is None
    assert calls == 1
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_browser.py::test_thumbnail_requests_share_one_gateway_call tests\test_content_browser.py::test_thumbnail_failure_cooldown_skips_immediate_retry -q
```

Expected: the Gateway is called twice and both tests fail.

- [ ] **Step 3: Implement in-flight sharing and a 30-second cooldown**

Add constructor fields `thumbnail_failure_cooldown: float = 30.0` and `monotonic_clock: Callable[[], float] = time.monotonic`, plus:

```python
self._thumbnail_inflight: dict[str, asyncio.Task[Path | None]] = {}
self._thumbnail_failures: dict[str, float] = {}
```

Resolve the result and cache key, check the disk cache first, and return `None` while the key's failure deadline is in the future. Otherwise create one task that acquires `_thumbnail_semaphore`, calls the Gateway, writes non-empty bytes with `await asyncio.to_thread(self.thumbnails.put, key, content)`, and records a cooldown on `None` or a caught non-cancellation exception. All waiters use `await asyncio.shield(task)`. The creator removes the exact completed task from `_thumbnail_inflight` in `finally`.

- [ ] **Step 4: Run thumbnail and controller tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_browser.py -k thumbnail tests\test_controller.py -k thumbnail -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 5**

```powershell
git add src/telegram_downloader/content_browser.py tests/test_content_browser.py
git commit -m "perf: coalesce thumbnail loading"
```

### Task 6: Guard and display streamed batches in the controller

**Files:**
- Modify: `src/telegram_downloader/controller.py:1041-1115`
- Modify: `tests/test_controller.py`

- [ ] **Step 1: Write a failing stale-batch controller test**

```python
@pytest.mark.asyncio
async def test_search_displays_current_batch_and_rejects_replaced_task() -> None:
    window = ContentWindowFake()
    page = window.content_page
    page.batches = []
    page.apply_search_batch = page.batches.append

    class Service:
        def __init__(self) -> None:
            self.calls = 0

        async def start_search(self, _peer, _query, **kwargs):
            self.calls += 1
            search_id = "old" if self.calls == 1 else "new"
            kwargs["on_results"](
                SimpleNamespace(search_id=search_id, generation=self.calls, results=(), stable=False)
            )
            if self.calls == 1:
                await asyncio.Event().wait()
            return SimpleNamespace(id=search_id), []

        def list_sessions(self):
            return [SimpleNamespace(id="new")]

    class Gateway:
        def is_connected(self) -> bool:
            return True

    controller = AppController.for_test(
        gateway=Gateway(),
        content_browser=Service(),
        window=window,
    )
    first = asyncio.create_task(controller.search_content("peer", object()))
    await asyncio.sleep(0)
    second = asyncio.create_task(controller.search_content("peer", object()))
    await asyncio.sleep(0)
    await asyncio.gather(first, second, return_exceptions=True)
    assert page.batches[-1].search_id == "new"
```

- [ ] **Step 2: Run the test and verify RED**

Expected: the fake service receives no `on_results`, or the page has no batch method.

- [ ] **Step 3: Forward batches only from the active search task**

Inside `search_content` and `load_more_content`, define:

```python
def show_batch(batch: SearchResultBatch) -> None:
    if self._content_search_task is current and not current.cancelled():
        page.apply_search_batch(batch)
```

Pass `on_results=show_batch` to the service. On successful return, apply one final stable batch before refreshing sessions. In cancellation/failure `finally`, reload committed rows so temporary rows disappear. Keep queue busy/disabled until the final stable reload.

- [ ] **Step 4: Run complete search regression and Ruff**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_progress.py tests\test_catalog.py tests\test_gateway.py tests\test_content_browser.py tests\test_controller.py tests\test_account_wide_search_e2e.py tests\ui\test_content_models.py tests\ui\test_content_browser.py -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

Expected: all selected tests and Ruff pass.

- [ ] **Step 5: Commit Task 6**

```powershell
git add src/telegram_downloader/controller.py tests/test_controller.py
git commit -m "feat: present progressive search results"
```

### Task 7: Record progressive-search verification

**Files:**
- Create: `docs/verification/2026-08-21-progressive-media-search.md`

- [ ] **Step 1: Run the focused suite with durations**

Run the Task 6 pytest command with `--durations=10`, then run Ruff and `git status --short`.

- [ ] **Step 2: Record exact evidence**

Record pass count, duration, direct-before-album ordering result, one-connection page commit result, no-reset model result, thumbnail coalescing result, commit SHA, and worktree status. Do not include Telegram account, peer, keyword, message, or media identifiers from real data.

- [ ] **Step 3: Commit the record**

```powershell
git add docs/verification/2026-08-21-progressive-media-search.md
git commit -m "docs: verify progressive media search"
```
