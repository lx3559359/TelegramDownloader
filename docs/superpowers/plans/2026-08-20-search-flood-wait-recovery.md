# Search Flood-Wait Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make foreground Telegram search visibly wait, retry, and resume safely when Telegram returns short flood waits, so a paginated 500-result search can complete without restarting from the beginning.

**Architecture:** `ContentBrowserService` owns a per-page retry state machine because it owns both the persisted search cursor and the final catalog writes. It injects async sleep for deterministic countdown tests, keeps Telegram calls before page persistence, and returns an incomplete resumable session when the wait policy cannot continue. The controller presents the returned state, while `ContentBrowserPage` exposes incomplete sessions through the existing load-more action relabelled as `继续搜索`.

**Tech Stack:** Python 3.12, asyncio, Telethon gateway abstractions, PySide6/qasync, SQLite catalog repository, pytest/pytest-asyncio/pytest-qt, Ruff.

---

## File Map

- Modify `src/telegram_downloader/content_browser.py`: retry policy, countdown state machine, safe terminal flood-wait result, and privacy-safe logging.
- Modify `src/telegram_downloader/controller.py`: display the returned session error for both initial search and continuation.
- Modify `src/telegram_downloader/ui/content_browser.py`: expose and label continuation for incomplete non-exhausted sessions.
- Modify `tests/test_content_browser.py`: service retry, countdown, cancellation, logging, terminal-state, continuation, and 500-result pagination coverage.
- Modify `tests/test_controller.py`: first-page incomplete state remains active and visible.
- Modify `tests/ui/test_content_browser.py`: incomplete sessions expose `继续搜索`.

### Task 1: Add cancellable short-wait retries around one search page

**Files:**
- Modify: `tests/test_content_browser.py`
- Modify: `src/telegram_downloader/content_browser.py`

- [ ] **Step 1: Extend the test gateway and service factory for deterministic progress and sleep**

In `tests/test_content_browser.py`, import the flood-wait type and the new policy API expected from production:

```python
import logging

from telegram_downloader.content_browser import (
    ContentBrowserService,
    NothingToQueueError,
    SearchRetryPolicy,
)
from telegram_downloader.gateway import (
    FloodWaitError,
    RemoteMedia,
    RemoteSearchHit,
    RemoteSearchPage,
    TransientNetworkError,
)
```

Extend `FakeGateway` so a failing request can still report the last scan counters:

```python
class FakeGateway:
    def __init__(self, profile: AccountProfile) -> None:
        self.profile = profile
        self.dialogs: list[ContentDialog] = []
        self.pages: list[RemoteSearchPage | BaseException] = []
        self.page_progress: list[SearchProgress | None] = []
        self.albums: dict[int, tuple[RemoteSearchHit, ...]] = {}
        self.thumbnail_values: dict[int, bytes | BaseException | None] = {}
        self.profile_calls = 0
        self.search_cursors: list[SearchCursor | None] = []
        self.album_calls: list[int] = []
        self.thumbnail_calls: list[int] = []

    async def search_media_page(self, peer_ref, query, cursor, *, on_progress=None):
        self.search_cursors.append(cursor)
        progress = self.page_progress.pop(0) if self.page_progress else None
        if progress is not None and on_progress is not None:
            on_progress(progress)
        value = self.pages.pop(0)
        if isinstance(value, BaseException):
            raise value
        if progress is None and on_progress is not None:
            on_progress(SearchProgress(len(value.items), len(value.items), "正在整理结果"))
        return value
```

Add `sleep` and `retry_policy` passthroughs to `prepared_online_service`:

```python
async def prepared_online_service(
    tmp_path: Path,
    now: datetime,
    gateway: FakeGateway,
    *,
    album_concurrency: int = 4,
    thumbnail_concurrency: int = 4,
    sleep=asyncio.sleep,
    retry_policy: SearchRetryPolicy | None = None,
) -> ContentBrowserService:
    catalog = initialized_catalog(tmp_path)
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        gateway=gateway,
        planner=PlannerStub(),
        clock=lambda: now,
        album_concurrency=album_concurrency,
        thumbnail_concurrency=thumbnail_concurrency,
        sleep=sleep,
        retry_policy=retry_policy,
    )
    await service.activate_account()
    catalog.replace_dialogs(
        "a1",
        [make_dialog("a1", "-1001", "资料群", now)],
        now,
    )
    return service
```

- [ ] **Step 2: Write failing tests for policy validation, countdown retry, cancellation, and privacy-safe logging**

Append these tests to `tests/test_content_browser.py`:

```python
def test_search_retry_policy_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError, match="等待上限"):
        SearchRetryPolicy(maximum_wait_seconds=0)
    with pytest.raises(ValueError, match="重试次数"):
        SearchRetryPolicy(maximum_retries=-1)


@pytest.mark.asyncio
async def test_short_flood_wait_counts_down_and_retries_same_cursor(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.pages = [
        FloodWaitError(3),
        RemoteSearchPage((make_hit(9, now),), None, True),
    ]
    gateway.page_progress = [SearchProgress(7, 2, "正在扫描"), None]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    service = await prepared_online_service(
        tmp_path,
        now,
        gateway,
        sleep=fake_sleep,
    )
    progress: list[SearchProgress] = []

    with caplog.at_level(
        logging.WARNING,
        logger="telegram_downloader.content_browser",
    ):
        session, results = await service.start_search(
            "-1001",
            make_query(now, "隐私关键词"),
            on_progress=progress.append,
        )

    countdown = [item for item in progress if "自动重试" in item.phase]
    assert [item.phase for item in countdown] == [
        "Telegram 限流，3 秒后自动重试（1/2）",
        "Telegram 限流，2 秒后自动重试（1/2）",
        "Telegram 限流，1 秒后自动重试（1/2）",
    ]
    assert all((item.inspected, item.matched) == (7, 2) for item in countdown)
    assert sleeps == [1, 1, 1]
    assert gateway.search_cursors == [None, None]
    assert session.status is SearchStatus.COMPLETED
    assert [item.message_id for item in results] == [9]
    assert "seconds=3" in caplog.text
    assert "attempt=1" in caplog.text
    assert "cursor=0" in caplog.text
    assert "隐私关键词" not in caplog.text
    assert "资料群" not in caplog.text


@pytest.mark.asyncio
async def test_cancelling_flood_wait_stops_before_retry(tmp_path: Path) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.pages = [FloodWaitError(20)]

    async def cancel_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    service = await prepared_online_service(
        tmp_path,
        now,
        gateway,
        sleep=cancel_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        await service.start_search("-1001", make_query(now))

    interrupted = service.latest_session("-1001")
    assert interrupted is not None
    assert interrupted.status is SearchStatus.INCOMPLETE
    assert interrupted.last_error == "搜索已取消"
    assert gateway.search_cursors == [None]
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_content_browser.py::test_search_retry_policy_rejects_invalid_limits tests/test_content_browser.py::test_short_flood_wait_counts_down_and_retries_same_cursor tests/test_content_browser.py::test_cancelling_flood_wait_stops_before_retry -q --basetemp=.build-temp/search-flood-red-1
```

Expected: collection or execution fails because `SearchRetryPolicy`, `retry_policy`, and `sleep` do not exist yet, and the service does not retry flood waits.

- [ ] **Step 4: Implement the retry policy and short-wait retry loop**

In `src/telegram_downloader/content_browser.py`, extend imports and add the logger and policy:

```python
import asyncio
import logging
from collections.abc import Awaitable, Callable

_LOGGER = logging.getLogger("telegram_downloader.content_browser")


@dataclass(frozen=True, slots=True)
class SearchRetryPolicy:
    maximum_wait_seconds: int = 120
    maximum_retries: int = 2

    def __post_init__(self) -> None:
        if self.maximum_wait_seconds <= 0:
            raise ValueError("搜索限流等待上限必须大于零")
        if self.maximum_retries < 0:
            raise ValueError("搜索限流重试次数不能为负数")
```

Extend `ContentBrowserService.__init__`:

```python
def __init__(
    self,
    catalog: CatalogRepository,
    thumbnails: ThumbnailCache,
    *,
    gateway: TelegramGateway | None = None,
    planner: TaskPlanner | None = None,
    uuid_factory: Callable[[], str] | None = None,
    clock: Callable[[], datetime] | None = None,
    album_concurrency: int = 4,
    thumbnail_concurrency: int = 4,
    retry_policy: SearchRetryPolicy | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    if album_concurrency <= 0 or thumbnail_concurrency <= 0:
        raise ValueError("内容查询并发数必须大于零")
    self.gateway = gateway
    self.catalog = catalog
    self.planner = planner
    self.thumbnails = thumbnails
    self.uuid_factory = uuid_factory or (lambda: str(uuid4()))
    self.clock = clock or (lambda: datetime.now(UTC))
    self.retry_policy = retry_policy or SearchRetryPolicy()
    self.sleep = sleep
```

Extract the existing page operation into `_fetch_page_once` with this complete body:

```python
async def _fetch_page_once(
    self,
    session: SearchSession,
    *,
    on_progress: Callable[[SearchProgress], None] | None = None,
) -> tuple[SearchSession, list[SearchResult]]:
    account = self._require_account()
    gateway, planner = self._require_online()
    page = await gateway.search_media_page(
        session.peer_ref,
        session.query,
        session.cursor,
        on_progress=on_progress,
    )
    expanded = list(page.items)
    group_triggers: dict[int, int] = {}
    for hit in page.items:
        grouped_id = hit.remote.grouped_id
        if grouped_id is None:
            continue
        group_triggers.setdefault(grouped_id, hit.remote.message_id)

    async def expand(
        trigger: tuple[int, int],
    ) -> tuple[RemoteSearchHit, ...]:
        grouped_id, message_id = trigger
        async with self._album_semaphore:
            return await gateway.expand_album(
                session.peer_ref,
                message_id,
                grouped_id,
            )

    album_values = await asyncio.gather(
        *(expand(item) for item in group_triggers.items())
    )
    for values in album_values:
        expanded.extend(values)

    unique = self._deduplicate_hits(expanded)
    existing_results = self.catalog.list_results(
        account.account_id,
        session.id,
    )
    existing_keys = {
        (item.peer_ref, item.message_id, item.media_id)
        for item in existing_results
    }
    units = self._album_units(unique)
    remaining_total = max(
        0,
        session.query.filters.item_limit - len(existing_results),
    )
    remaining_page = min(100, remaining_total)
    accepted: list[RemoteSearchHit] = []
    deferred_cursor: SearchCursor | None = None
    skipped_album = False
    for unit in units:
        new_items = [
            hit
            for hit in unit
            if self._media_key(hit.remote) not in existing_keys
        ]
        if not new_items:
            continue
        is_album = new_items[0].remote.grouped_id is not None
        if len(new_items) > remaining_total:
            skipped_album = skipped_album or is_album
            continue
        if len(new_items) > remaining_page:
            grouped_id = new_items[0].remote.grouped_id
            trigger = (
                group_triggers.get(grouped_id, new_items[0].remote.message_id)
                if grouped_id is not None
                else new_items[0].remote.message_id
            )
            deferred_cursor = SearchCursor(trigger + 1)
            break
        accepted.extend(new_items)
        remaining_total -= len(new_items)
        remaining_page -= len(new_items)
        existing_keys.update(self._media_key(hit.remote) for hit in new_items)

    queued_keys = planner.existing_media_keys(
        {self._media_key(hit.remote) for hit in accepted}
    )
    saved = [
        self._result_from_hit(
            account.account_id,
            session,
            hit,
            queued=self._media_key(hit.remote) in queued_keys,
        )
        for hit in accepted
    ]
    self.catalog.save_search_page(
        account.account_id,
        session.id,
        session.generation,
        saved,
    )
    result_count = len(
        self.catalog.list_results(account.account_id, session.id)
    )
    reached_limit = result_count >= session.query.filters.item_limit
    complete = (
        page.exhausted
        or reached_limit
        or skipped_album
        or (deferred_cursor is None and page.next_cursor is None)
    )
    cursor = (
        None
        if complete
        else deferred_cursor
        if deferred_cursor is not None
        else page.next_cursor
    )
    self.catalog.finish_search(
        account.account_id,
        session.id,
        session.generation,
        cursor,
        complete,
        self.clock(),
        status=(
            SearchStatus.COMPLETED if complete else SearchStatus.RUNNING
        ),
        error="达到数量上限" if skipped_album else None,
    )
    current = self.catalog.get_session(account.account_id, session.id)
    results = self.catalog.list_results(account.account_id, session.id)
    return current, results
```

Replace `_fetch_page` with the retry wrapper. The existing cancellation and generic gateway error persistence stays outside the loop:

```python
async def _fetch_page(
    self,
    session: SearchSession,
    *,
    on_progress: Callable[[SearchProgress], None] | None = None,
) -> tuple[SearchSession, list[SearchResult]]:
    latest_progress = SearchProgress(0, 0, "正在连接 Telegram")
    retries = 0

    def report(progress: SearchProgress) -> None:
        nonlocal latest_progress
        latest_progress = progress
        if on_progress is not None:
            on_progress(progress)

    try:
        while True:
            try:
                return await self._fetch_page_once(
                    session,
                    on_progress=report if on_progress is not None else None,
                )
            except FloodWaitError as error:
                if (
                    error.seconds > self.retry_policy.maximum_wait_seconds
                    or retries >= self.retry_policy.maximum_retries
                ):
                    raise
                retries += 1
                _LOGGER.warning(
                    "search flood wait seconds=%d attempt=%d cursor=%d",
                    error.seconds,
                    retries,
                    session.cursor.offset_id if session.cursor is not None else 0,
                )
                for remaining in range(error.seconds, 0, -1):
                    if on_progress is not None:
                        on_progress(
                            SearchProgress(
                                latest_progress.inspected,
                                latest_progress.matched,
                                "Telegram 限流，"
                                f"{remaining} 秒后自动重试（{retries}/"
                                f"{self.retry_policy.maximum_retries}）",
                            )
                        )
                    await self.sleep(1)
    except asyncio.CancelledError:
        self._finish_incomplete(session, "搜索已取消")
        raise
    except GatewayError as error:
        self._finish_incomplete(session, self._safe_gateway_error(error))
        raise
```

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_content_browser.py::test_search_retry_policy_rejects_invalid_limits tests/test_content_browser.py::test_short_flood_wait_counts_down_and_retries_same_cursor tests/test_content_browser.py::test_cancelling_flood_wait_stops_before_retry -q --basetemp=.build-temp/search-flood-green-1
```

Expected: `3 passed` with no warnings or leaked sensitive text.

- [ ] **Step 6: Add and run the intermittent-wait 500-result test**

Add this test before further production changes:

```python
@pytest.mark.asyncio
async def test_five_page_search_reaches_500_across_short_flood_waits(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    pages = [
        RemoteSearchPage(
            tuple(make_hit(message_id, now) for message_id in range(upper, upper - 100, -1)),
            SearchCursor(upper - 100),
            False,
        )
        for upper in (500, 400, 300, 200, 100)
    ]
    gateway.pages = [
        pages[0],
        FloodWaitError(1),
        pages[1],
        pages[2],
        FloodWaitError(1),
        pages[3],
        pages[4],
    ]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    service = await prepared_online_service(
        tmp_path,
        now,
        gateway,
        sleep=fake_sleep,
    )

    session, results = await service.start_search(
        "-1001",
        make_query(now, limit=500),
    )
    while not session.exhausted:
        session, results = await service.load_more(session.id)

    assert session.status is SearchStatus.COMPLETED
    assert len(results) == 500
    assert len({item.id for item in results}) == 500
    assert sleeps == [1, 1]
    assert gateway.search_cursors == [
        None,
        SearchCursor(400),
        SearchCursor(400),
        SearchCursor(300),
        SearchCursor(200),
        SearchCursor(200),
        SearchCursor(100),
    ]
```

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_content_browser.py::test_five_page_search_reaches_500_across_short_flood_waits -q --basetemp=.build-temp/search-flood-500
```

Expected: `1 passed`, proving two separate page operations recover and the fifth page stops exactly at 500.

- [ ] **Step 7: Commit Task 1**

```powershell
git add -- src/telegram_downloader/content_browser.py tests/test_content_browser.py
git commit -m "feat: retry short Telegram search flood waits"
```

### Task 2: Preserve terminal flood waits as resumable search sessions

**Files:**
- Modify: `tests/test_content_browser.py`
- Modify: `src/telegram_downloader/content_browser.py`

- [ ] **Step 1: Write failing tests for long waits and retry exhaustion**

Append:

```python
@pytest.mark.asyncio
async def test_long_flood_wait_returns_incomplete_session_that_can_continue(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.pages = [
        RemoteSearchPage((make_hit(10, now),), SearchCursor(10), False),
        FloodWaitError(121),
    ]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    service = await prepared_online_service(
        tmp_path,
        now,
        gateway,
        sleep=fake_sleep,
    )

    session, results = await service.start_search("-1001", make_query(now))
    session, results = await service.load_more(session.id)

    assert session.status is SearchStatus.INCOMPLETE
    assert session.cursor == SearchCursor(10)
    assert session.last_error == "Telegram 请求需等待 121 秒"
    assert [item.message_id for item in results] == [10]
    assert sleeps == []

    gateway.pages = [RemoteSearchPage((make_hit(5, now),), None, True)]
    resumed, results = await service.load_more(session.id)

    assert resumed.status is SearchStatus.COMPLETED
    assert resumed.last_error is None
    assert [item.message_id for item in results] == [10, 5]
    assert gateway.search_cursors == [
        None,
        SearchCursor(10),
        SearchCursor(10),
    ]


@pytest.mark.asyncio
async def test_third_short_flood_wait_returns_incomplete_without_more_sleep(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    gateway.pages = [FloodWaitError(1), FloodWaitError(1), FloodWaitError(1)]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    service = await prepared_online_service(
        tmp_path,
        now,
        gateway,
        sleep=fake_sleep,
    )

    session, results = await service.start_search("-1001", make_query(now))

    assert session.status is SearchStatus.INCOMPLETE
    assert session.last_error == "Telegram 请求需等待 1 秒"
    assert results == []
    assert sleeps == [1, 1]
    assert gateway.search_cursors == [None, None, None]
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_content_browser.py::test_long_flood_wait_returns_incomplete_session_that_can_continue tests/test_content_browser.py::test_third_short_flood_wait_returns_incomplete_without_more_sleep -q --basetemp=.build-temp/search-flood-red-2
```

Expected: both tests fail because terminal `FloodWaitError` still propagates instead of returning the persisted session.

- [ ] **Step 3: Handle terminal flood waits before generic gateway failures**

In `_fetch_page`, insert this handler before `except GatewayError`:

```python
    except FloodWaitError as error:
        message = self._safe_gateway_error(error)
        _LOGGER.warning(
            "search flood wait terminal seconds=%d retries=%d cursor=%d",
            error.seconds,
            retries,
            session.cursor.offset_id if session.cursor is not None else 0,
        )
        self._finish_incomplete(session, message)
        account = self._require_account()
        current = self.catalog.get_session(account.account_id, session.id)
        results = self.catalog.list_results(account.account_id, session.id)
        return current, results
```

Keep the existing `except GatewayError` directly after it so non-flood gateway failures still propagate.

- [ ] **Step 4: Run service tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_content_browser.py -q --basetemp=.build-temp/search-flood-green-2
```

Expected: all content-browser service tests pass, including existing transient-network and cancellation behavior.

- [ ] **Step 5: Commit Task 2**

```powershell
git add -- src/telegram_downloader/content_browser.py tests/test_content_browser.py
git commit -m "feat: preserve resumable searches after long flood waits"
```

### Task 3: Present incomplete searches and expose continuation in the UI

**Files:**
- Modify: `tests/test_controller.py`
- Modify: `tests/ui/test_content_browser.py`
- Modify: `src/telegram_downloader/controller.py`
- Modify: `src/telegram_downloader/ui/content_browser.py`

- [ ] **Step 1: Write the failing controller test**

Add to `tests/test_controller.py`:

```python
@pytest.mark.asyncio
async def test_terminal_search_wait_activates_session_and_displays_error() -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 500),
    )
    incomplete = SearchSession(
        "search-1",
        "a1",
        "-1001",
        "资料群",
        query,
        SearchStatus.INCOMPLETE,
        1,
        None,
        False,
        0,
        now,
        now,
        "Telegram 请求需等待 121 秒",
    )

    class Browser:
        async def start_search(self, _peer_ref, _query, *, on_progress=None):
            return incomplete, []

        def list_sessions(self):
            return [incomplete]

        def list_results(self, _search_id):
            return []

    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=ConnectedGateway(),
        content_browser=Browser(),
        window=window,
    )

    await controller.search_content("-1001", query)

    assert window.content_page.active_search_id == "search-1"
    assert window.content_page.sessions == [incomplete]
    assert window.content_page.errors[-1] == "Telegram 请求需等待 121 秒"
```

- [ ] **Step 2: Write the failing UI continuation test**

Add to `tests/ui/test_content_browser.py`:

```python
def test_incomplete_search_exposes_continue_action(qtbot) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.show()
    page.set_logged_in(True)
    incomplete = replace(
        session(now),
        status=SearchStatus.INCOMPLETE,
        last_error="Telegram 请求需等待 121 秒",
    )

    page.set_active_search(incomplete)

    assert page.load_more_button.isVisible()
    assert page.load_more_button.text() == "继续搜索"

    page.set_active_search(replace(incomplete, status=SearchStatus.RUNNING))
    assert page.load_more_button.text() == "加载更多"
```

- [ ] **Step 3: Run both tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_controller.py::test_terminal_search_wait_activates_session_and_displays_error tests/ui/test_content_browser.py::test_incomplete_search_exposes_continue_action -q --basetemp=.build-temp/search-flood-red-3
```

Expected: controller test fails because returned `last_error` is not displayed; UI test fails because incomplete status hides the load-more action.

- [ ] **Step 4: Display session errors after initial search and continuation**

In both successful-result blocks of `AppController.search_content` and `AppController.load_more_content`, add error presentation after sessions are refreshed:

```python
page.set_active_search(session)
page.set_results(results)
page.set_sessions(self.content_browser.list_sessions())
page.show_error(session.last_error or "")
```

This also clears a previous terminal wait message after a successful continuation.

- [ ] **Step 5: Relabel and expose incomplete continuation**

In `ContentBrowserPage._refresh_actions`, replace the current `can_load` status check with:

```python
        incomplete = (
            self.active_session is not None
            and self.active_session.status is SearchStatus.INCOMPLETE
        )
        self.load_more_button.setText("继续搜索" if incomplete else "加载更多")
        can_load = (
            self.active_session is not None
            and self.active_session.status
            in (SearchStatus.RUNNING, SearchStatus.INCOMPLETE)
            and not self.active_session.exhausted
            and not self._search_busy
            and self._logged_in
        )
        self.load_more_button.setVisible(can_load)
```

- [ ] **Step 6: Run controller and UI tests and verify GREEN**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/test_controller.py tests/ui/test_content_browser.py -q --basetemp=.build-temp/search-flood-green-3
```

Expected: all selected controller and content-browser UI tests pass.

- [ ] **Step 7: Commit Task 3**

```powershell
git add -- src/telegram_downloader/controller.py src/telegram_downloader/ui/content_browser.py tests/test_controller.py tests/ui/test_content_browser.py
git commit -m "feat: continue incomplete Telegram searches"
```

### Task 4: Verify the complete change

**Files:**
- Verify only; no source or documentation edits.

- [ ] **Step 1: Run focused search verification**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/test_content.py tests/test_content_progress.py tests/test_content_browser.py tests/test_catalog.py tests/test_controller.py tests/test_gateway.py tests/ui/test_content_browser.py tests/ui/test_content_models.py -q --basetemp=.build-temp/search-flood-focused
```

Expected: all selected search, gateway, controller, catalog, and UI tests pass.

- [ ] **Step 2: Run the full regression and Ruff checks**

Run:

```powershell
.\scripts\test.ps1
```

Expected: the complete pytest suite passes, followed by `All checks passed!` from Ruff.

- [ ] **Step 3: Run packaged smoke verification if a package is present**

Run:

```powershell
if (Test-Path -LiteralPath '.\dist\TelegramDownloader\TelegramDownloader.exe') {
    .\scripts\smoke.ps1
}
```

Expected when the package exists: `PACKAGED_SMOKE_OK`. This checks package health only; the package will not contain the new source change until a later build/release step.

- [ ] **Step 4: Verify the final diff and branch state**

Run:

```powershell
git diff --check
git status --short
git log -4 --oneline
```

Expected: `git diff --check` is silent, the worktree is clean, and the design, plan, retry implementation, and continuation UI commits are visible. Record the exact focused-test count, full-suite count, Ruff result, and packaged-smoke result in the final handoff. Do not merge or push unless the user explicitly requests it.
