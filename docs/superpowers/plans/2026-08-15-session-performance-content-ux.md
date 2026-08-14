# Session Stability and Content UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Telegram login durable across transient disconnects, add honest synchronization/search feedback, enlarge result previews with double-click viewing, and reduce repeated Telegram work without increasing flood-wait risk.

**Architecture:** Keep encrypted StringSession and cached content under the runtime root. Add a Windows named-mutex guard before application construction, extend the existing shared connection recovery with an idle monitor, pass typed progress events through gateway → service → controller → Qt page, cache resolved entities per gateway, and use semaphores for bounded album/thumbnail work. The UI remains a table and opens a focused preview dialog on demand.

**Tech Stack:** Python 3.12, PySide6 6.11, qasync, Telethon 1.44, SQLite, pytest/pytest-asyncio/pytest-qt, Ruff, PyInstaller, Inno Setup.

---

## File map

- Create `src/telegram_downloader/instance_guard.py`: Windows current-user named mutex and duplicate-instance notice.
- Create `src/telegram_downloader/content_progress.py`: immutable dialog/search progress events and throttled search reporter.
- Create `src/telegram_downloader/ui/media_preview.py`: in-app image/cover/metadata preview dialog.
- Create `tests/test_instance_guard.py`: mutex ownership and duplicate-instance behavior.
- Create `tests/test_content_progress.py`: progress throttling and final-event behavior.
- Create `tests/ui/test_media_preview.py`: image scaling and metadata fallback behavior.
- Modify `src/telegram_downloader/app.py`: acquire/release the instance guard and wire new UI signals.
- Modify `src/telegram_downloader/gateway.py`: connection visibility, entity cache, and search progress callbacks.
- Modify `src/telegram_downloader/connectivity.py`: shared retry state needed by the idle monitor.
- Modify `src/telegram_downloader/content_browser.py`: sync/search progress, bounded album work, bounded thumbnail work, and result lookup.
- Modify `src/telegram_downloader/controller.py`: cached-first startup, background connection monitor, retry action, progress forwarding, cancellation, and preview loading.
- Modify `src/telegram_downloader/ui/content_browser.py`: connection retry, refresh/search progress widgets, larger rows, and double-click preview signal.
- Modify `src/telegram_downloader/ui/content_models.py`: stable large preview decoration and cached-path lookup.
- Modify focused existing tests in `tests/test_app.py`, `tests/test_connectivity.py`, `tests/test_gateway.py`, `tests/test_content_browser.py`, `tests/test_controller.py`, `tests/ui/test_content_models.py`, and `tests/ui/test_content_browser.py`.

## Task 1: Prevent concurrent app instances from invalidating one Session

**Files:**
- Create: `src/telegram_downloader/instance_guard.py`
- Create: `tests/test_instance_guard.py`
- Modify: `src/telegram_downloader/app.py:421-429`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write the failing mutex behavior tests**

```python
# tests/test_instance_guard.py
from telegram_downloader.instance_guard import WindowsInstanceGuard


class KernelStub:
    def __init__(self, *, last_error: int = 0) -> None:
        self.last_error = last_error
        self.closed: list[int] = []

    def create_mutex(self, name: str) -> int:
        assert name == r"Local\TelegramDownloader.SingleInstance"
        return 41

    def get_last_error(self) -> int:
        return self.last_error

    def close_handle(self, handle: int) -> None:
        self.closed.append(handle)


def test_first_instance_owns_mutex_until_release() -> None:
    kernel = KernelStub()
    guard = WindowsInstanceGuard(kernel=kernel)
    assert guard.acquire() is True
    guard.release()
    assert kernel.closed == [41]


def test_duplicate_instance_closes_unowned_handle() -> None:
    kernel = KernelStub(last_error=183)
    guard = WindowsInstanceGuard(kernel=kernel)
    assert guard.acquire() is False
    assert kernel.closed == [41]
```

Add this application-level test:

```python
def test_duplicate_instance_exits_before_application_construction(tmp_path, monkeypatch) -> None:
    class Guard:
        def acquire(self) -> bool:
            return False

        def notify_already_running(self) -> None:
            self.notified = True

        def release(self) -> None:
            raise AssertionError("unowned guard must not be released")

    guard = Guard()
    monkeypatch.setattr(app, "create_application", lambda _root: (_ for _ in ()).throw(AssertionError()))
    assert app.run(tmp_path, instance_guard=guard) == 2
    assert guard.notified is True
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_instance_guard.py tests\test_app.py::test_duplicate_instance_exits_before_application_construction -q
```

Expected: collection fails because `telegram_downloader.instance_guard` and the injectable `instance_guard` argument do not exist.

- [ ] **Step 3: Implement the named mutex and guard the normal GUI path**

Implement this interface in `instance_guard.py`:

```python
from __future__ import annotations

import ctypes
from typing import Protocol


class KernelApi(Protocol):
    def create_mutex(self, name: str) -> int: ...
    def get_last_error(self) -> int: ...
    def close_handle(self, handle: int) -> None: ...


class WindowsKernelApi:
    def __init__(self) -> None:
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def create_mutex(self, name: str) -> int:
        return int(self.kernel32.CreateMutexW(None, False, name) or 0)

    def get_last_error(self) -> int:
        return ctypes.get_last_error()

    def close_handle(self, handle: int) -> None:
        self.kernel32.CloseHandle(handle)


class WindowsInstanceGuard:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self, kernel: KernelApi | None = None) -> None:
        self.kernel = kernel or WindowsKernelApi()
        self.handle = 0

    def acquire(self) -> bool:
        handle = self.kernel.create_mutex(r"Local\TelegramDownloader.SingleInstance")
        if not handle:
            raise OSError("无法创建程序单实例保护")
        if self.kernel.get_last_error() == self.ERROR_ALREADY_EXISTS:
            self.kernel.close_handle(handle)
            return False
        self.handle = handle
        return True

    def notify_already_running(self) -> None:
        ctypes.windll.user32.MessageBoxW(
            None,
            "Telegram 下载器已经在运行。",
            "Telegram 下载器",
            0x40,
        )

    def release(self) -> None:
        if self.handle:
            self.kernel.close_handle(self.handle)
            self.handle = 0
```

Change `app.run()` to acquire before `create_application()`, return `2` for a duplicate, and release in `finally`. Keep `--self-test` and update health checks outside this guard because they do not open Telegram.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2. Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/telegram_downloader/instance_guard.py src/telegram_downloader/app.py tests/test_instance_guard.py tests/test_app.py
git commit -m "fix: prevent duplicate Telegram sessions"
```

## Task 2: Preserve saved login and supervise disconnected gateways

**Files:**
- Modify: `src/telegram_downloader/gateway.py:97-164, 180-274`
- Modify: `src/telegram_downloader/connectivity.py`
- Modify: `src/telegram_downloader/controller.py:179-338, 907-923, 1023-1097`
- Modify: `tests/test_gateway.py`
- Modify: `tests/test_connectivity.py`
- Modify: `tests/test_controller.py`

- [ ] **Step 1: Write failing connection-state and Session-retention tests**

Use this gateway test to prove `is_connected()` reflects the Telethon client:

```python
def test_gateway_connection_state_comes_from_client() -> None:
    class Client:
        def is_connected(self) -> bool:
            return True

    gateway = TelethonGateway.from_client_for_test(Client(), connected=False)
    assert gateway.is_connected() is True
```

Add controller tests with a memory vault:

```python
@pytest.mark.asyncio
async def test_transient_offline_state_keeps_session_and_never_opens_login() -> None:
    class Gateway:
        def is_connected(self) -> bool:
            return False

        async def connect(self) -> None:
            raise TransientNetworkError("offline")

    vault = _MemoryVault({"session": "saved", "api_hash": "hash"})
    controller = AppController.for_test(gateway=Gateway(), vault=vault, secrets=vault.load())
    shown: list[str] = []
    controller.show_login = lambda: shown.append("login")

    assert await controller.ensure_telegram_online() is False
    assert controller.secrets["session"] == "saved"
    assert vault.load()["session"] == "saved"
    assert shown == []
```

Use an injected sleeper to test the 30-second interval and shutdown cancellation:

```python
@pytest.mark.asyncio
async def test_connection_monitor_waits_30_seconds_and_shutdown_cancels_it() -> None:
    sleeping = asyncio.Event()
    blocker = asyncio.Event()
    intervals: list[float] = []

    async def sleep(value: float) -> None:
        intervals.append(value)
        sleeping.set()
        await blocker.wait()

    class Gateway:
        def is_connected(self) -> bool:
            return False

        async def disconnect(self) -> None:
            pass

    controller = AppController.for_test(
        gateway=Gateway(),
        connection_monitor_interval=30.0,
        connection_sleeper=sleep,
    )
    task = asyncio.create_task(controller._monitor_connection())
    controller._connection_monitor_task = task
    await sleeping.wait()

    assert intervals == [30.0]
    await controller.shutdown()
    assert task.cancelled() is True
```

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_gateway.py::test_gateway_connection_state_comes_from_client tests\test_connectivity.py tests\test_controller.py -k "transient_offline_state or connection_monitor or gateway_connection_state" -q
```

Expected: failures because `is_connected`, monitor dependencies, and the new offline behavior are missing.

- [ ] **Step 3: Add connection visibility and a single monitor loop**

Extend the gateway protocol and implementation:

```python
class TelegramGateway(Protocol):
    def is_connected(self) -> bool: ...


def is_connected(self) -> bool:
    method = getattr(self._client, "is_connected", None)
    return bool(method()) if callable(method) else self._connected
```

Expose `ConnectionRecovery.active` as a read-only property so the controller can avoid duplicate work:

```python
@property
def active(self) -> bool:
    return self._active is not None and not self._active.done()
```

Add `connection_monitor_interval=30.0` and `connection_sleeper=asyncio.sleep` dependencies to `AppController`. Start `_monitor_connection()` after cached account activation whenever a saved Gateway exists:

```python
async def _monitor_connection(self) -> None:
    while not self._shutting_down:
        await self._connection_sleeper(self._connection_monitor_interval)
        gateway = self.gateway
        if gateway is None or gateway.is_connected():
            continue
        await self.ensure_telegram_online()
```

Make `start()` return after showing cached state and scheduling `_restore_saved_session()` in the background. In `shutdown()`, cancel and await the monitor before disconnecting the gateway.

Keep `_handle_session_expired()` as the only path that removes `self.secrets["session"]`. Ensure all transient branches set offline UI/status and return without calling `show_login()`.

- [ ] **Step 4: Verify GREEN and run existing login/controller coverage**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_connectivity.py tests\test_gateway.py tests\test_controller.py -q
```

Expected: all tests pass, including existing QR login and explicit Session-expiry tests.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/telegram_downloader/gateway.py src/telegram_downloader/connectivity.py src/telegram_downloader/controller.py tests/test_gateway.py tests/test_connectivity.py tests/test_controller.py
git commit -m "feat: supervise saved Telegram sessions"
```

## Task 3: Define honest, throttled content progress and cache entities

**Files:**
- Create: `src/telegram_downloader/content_progress.py`
- Create: `tests/test_content_progress.py`
- Modify: `src/telegram_downloader/gateway.py:138-162, 525-618, 654-682`
- Modify: `tests/test_gateway.py`

- [ ] **Step 1: Write failing progress and entity-cache tests**

```python
# tests/test_content_progress.py
from telegram_downloader.content_progress import SearchProgressReporter


def test_reporter_throttles_intermediate_updates_and_forces_final_event() -> None:
    now = [0.0]
    events = []
    reporter = SearchProgressReporter(events.append, clock=lambda: now[0])
    for index in range(9):
        reporter.record(matched=index < 2)
    assert events == []
    now[0] = 0.2
    reporter.record(matched=True)
    assert [(item.inspected, item.matched) for item in events] == [(10, 3)]
    reporter.finish("正在整理结果")
    assert events[-1].phase == "正在整理结果"
```

Use these gateway tests for final counts and entity reuse:

```python
@pytest.mark.asyncio
async def test_search_progress_finishes_with_real_scanned_and_matched_counts() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    messages = [media_message(value, now) for value in range(25, 0, -1)]

    class Client:
        async def get_entity(self, _entity):
            return SimpleNamespace(title="资料群")

        def iter_messages(self, _entity, **_kwargs):
            async def generate():
                for message in messages:
                    yield message
            return generate()

    events = []
    client = Client()
    gateway = TelethonGateway.from_client_for_test(client)
    await gateway.search_media_page(
        "-1001",
        make_search_query(now),
        None,
        on_progress=events.append,
    )
    assert events[-1].inspected == 25
    assert events[-1].matched == 25
    assert events[-1].phase == "正在整理结果"


@pytest.mark.asyncio
async def test_resolved_entities_are_cached_per_gateway() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        async def get_entity(self, entity):
            self.calls += 1
            return SimpleNamespace(id=entity, title="资料群")

    client = Client()
    gateway = TelethonGateway.from_client_for_test(client)
    first = await gateway._resolve_entity("-1001")
    second = await gateway._resolve_entity("-1001")
    assert first is second
    assert client.calls == 1
```

In the first test, add this local helper:

```python
def make_search_query(now: datetime) -> ContentSearchQuery:
    return ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 500),
    )
```

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_progress.py tests\test_gateway.py -k "progress or entity_cache" -q
```

Expected: missing progress module/callback and repeated entity-resolution assertions fail.

- [ ] **Step 3: Implement typed progress and gateway-local entity caching**

Create these immutable events:

```python
@dataclass(frozen=True, slots=True)
class DialogSyncProgress:
    discovered: int


@dataclass(frozen=True, slots=True)
class SearchProgress:
    inspected: int
    matched: int
    phase: str
```

Implement `SearchProgressReporter` with defaults `every=10`, `min_interval=0.2`, `clock=time.monotonic`; `record(matched=...)` emits only when both thresholds permit, while `finish(phase)` always emits.

Add an optional keyword callback to `TelegramGateway.search_media_page()`:

```python
async def search_media_page(
    self,
    peer_ref: str,
    query: ContentSearchQuery,
    cursor: SearchCursor | None,
    *,
    on_progress: Callable[[SearchProgress], None] | None = None,
) -> RemoteSearchPage:
```

Record every inspected message, mark matching media accurately, and force a final `正在整理结果` event before returning.

Initialize `self._entity_cache: dict[str, object] = {}` in both constructors. `_resolve_entity()` checks this cache first and stores successfully resolved entities, including private peer fallback results. Never cache exceptions.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2. Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 3**

```powershell
git add src/telegram_downloader/content_progress.py src/telegram_downloader/gateway.py tests/test_content_progress.py tests/test_gateway.py
git commit -m "feat: report Telegram search progress"
```

## Task 4: Stream sync feedback and bound album/thumbnail concurrency

**Files:**
- Modify: `src/telegram_downloader/content_browser.py:65-85, 145-337, 459-479`
- Modify: `tests/test_content_browser.py`

- [ ] **Step 1: Write failing service progress and concurrency tests**

Use the desired sync API directly and verify incremental counts:

```python
events: list[DialogSyncProgress] = []
dialogs = await service.sync_dialogs(on_progress=events.append)
assert [item.discovered for item in events] == [1, 2]

search_events: list[SearchProgress] = []
await service.start_search("-1001", query, on_progress=search_events.append)
```

Use this failure case to prove the old cache is preserved:

```python
@pytest.mark.asyncio
async def test_failed_dialog_sync_preserves_cached_dialogs(tmp_path: Path) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    old = make_dialog("a1", "-1001", "旧缓存群", now)
    catalog.replace_dialogs("a1", [old], now)

    class Gateway(FakeGateway):
        def iter_content_dialogs(self, account_id: str):
            async def generate():
                yield make_dialog(account_id, "-1002", "未完成的新群", now)
                raise TransientNetworkError("offline")
            return generate()

    gateway = Gateway(AccountProfile("a1", "账号一"))
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        gateway=gateway,
        planner=PlannerStub(),
        clock=lambda: now,
    )
    await service.activate_account()
    with pytest.raises(TransientNetworkError):
        await service.sync_dialogs()
    assert catalog.list_dialogs("a1") == [old]
```

Use a four-request barrier to prove album expansion is bounded and stable:

```python
@pytest.mark.asyncio
async def test_album_expansion_uses_at_most_four_concurrent_requests(tmp_path: Path) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    reached_four = asyncio.Event()
    release = asyncio.Event()

    class Gateway(FakeGateway):
        def __init__(self) -> None:
            super().__init__(AccountProfile("a1", "账号一"))
            self.active = 0
            self.max_active = 0

        async def expand_album(self, _peer_ref, _message_id, _grouped_id):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == 4:
                reached_four.set()
            await release.wait()
            self.active -= 1
            return ()

    gateway = Gateway()
    gateway.pages = [RemoteSearchPage(tuple(
        make_hit(100 - value, now, grouped_id=value) for value in range(1, 6)
    ), None, True)]
    service = await prepared_online_service(
        tmp_path,
        now,
        gateway,
        album_concurrency=4,
    )
    operation = asyncio.create_task(service.start_search("-1001", make_query(now)))
    await reached_four.wait()
    assert gateway.max_active == 4
    release.set()
    _session, results = await operation
    assert [item.message_id for item in results] == [99, 98, 97, 96, 95]
```

Define the preparation helper beside `initialized_catalog()`:

```python
async def prepared_online_service(
    tmp_path: Path,
    now: datetime,
    gateway: FakeGateway,
    *,
    album_concurrency: int = 4,
    thumbnail_concurrency: int = 4,
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
    )
    await service.activate_account()
    catalog.replace_dialogs(
        "a1",
        [make_dialog("a1", "-1001", "资料群", now)],
        now,
    )
    return service
```

Use this thumbnail concurrency test:

```python
@pytest.mark.asyncio
async def test_thumbnail_loading_uses_at_most_four_concurrent_requests(tmp_path: Path) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    reached_four = asyncio.Event()
    release = asyncio.Event()

    class Gateway(FakeGateway):
        def __init__(self) -> None:
            super().__init__(AccountProfile("a1", "账号一"))
            self.active = 0
            self.max_active = 0

        async def load_thumbnail(self, _peer_ref, _message_id, _media_id):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == 4:
                reached_four.set()
            await release.wait()
            self.active -= 1
            return b"thumbnail"

    gateway = Gateway()
    service = await prepared_online_service(
        tmp_path,
        now,
        gateway,
        thumbnail_concurrency=4,
    )
    session = service.catalog.begin_search(
        "s1", "a1", "-1001", "资料群", make_query(now), now
    )
    hits = [make_hit(100 - value, now) for value in range(6)]
    saved = [
        service._result_from_hit("a1", session, hit, queued=False)
        for hit in hits
    ]
    service.catalog.save_search_page("a1", "s1", session.generation, saved)
    tasks = [asyncio.create_task(service.load_thumbnail(item.id)) for item in saved]
    await reached_four.wait()
    assert gateway.max_active == 4
    release.set()
    assert all(path is not None for path in await asyncio.gather(*tasks))
```

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_browser.py -k "progress or concurrency or preserves_cached" -q
```

Expected: callback keyword arguments and concurrency limits are absent.

- [ ] **Step 3: Implement service callbacks and semaphores**

Extend the constructor with validated `album_concurrency=4` and `thumbnail_concurrency=4`, then create semaphores.

In `sync_dialogs()`, append one yielded dialog at a time and emit `DialogSyncProgress(len(dialogs))`; do not call `catalog.replace_dialogs()` until enumeration finishes successfully.

Forward `on_progress` from `start_search()`/`load_more()` to `_fetch_page()` and then to `gateway.search_media_page()`.

Replace sequential album expansion with stable `asyncio.gather()` over one coroutine per unique group:

```python
async def expand(trigger: tuple[int, int]) -> tuple[RemoteSearchHit, ...]:
    grouped_id, message_id = trigger
    async with self._album_semaphore:
        return await gateway.expand_album(session.peer_ref, message_id, grouped_id)

album_values = await asyncio.gather(*(expand(item) for item in group_triggers.items()))
for values in album_values:
    expanded.extend(values)
```

Wrap cache-miss gateway thumbnail loads in `async with self._thumbnail_semaphore`. Add `get_result(result_id)` to return the account-scoped catalog result for preview metadata.

- [ ] **Step 4: Verify GREEN and existing catalog behavior**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_browser.py tests\test_catalog.py -q
```

Expected: all tests pass; cancellation and history preservation remain green.

- [ ] **Step 5: Commit Task 4**

```powershell
git add src/telegram_downloader/content_browser.py tests/test_content_browser.py
git commit -m "perf: bound content lookup concurrency"
```

## Task 5: Forward connection, sync, and search state through the controller

**Files:**
- Modify: `src/telegram_downloader/controller.py:40-140, 270-338, 624-823, 1098-1149`
- Modify: `tests/test_controller.py`

- [ ] **Step 1: Write failing controller orchestration tests**

Use these controller assertions for state forwarding:

```python
@pytest.mark.asyncio
async def test_manual_refresh_reports_counts_and_keeps_one_task() -> None:
    # Content service invokes on_progress twice before returning two dialogs.
    await controller.refresh_content_dialogs()
    assert page.sync_states == [
        ("正在刷新群组…", True, 0),
        ("正在刷新，已发现 1 个群组/频道", True, 1),
        ("正在刷新，已发现 2 个群组/频道", True, 2),
        ("刚刚同步，共 2 个", False, 2),
    ]


@pytest.mark.asyncio
async def test_search_progress_is_forwarded_and_always_stops() -> None:
    await controller.search_content("-1001", query)
    assert page.search_busy_values == [True, False]
    assert page.search_progress[-1].inspected == 20
```

Use this replacement-search structure to prove cancellation order:

```python
@pytest.mark.asyncio
async def test_new_search_cancels_the_running_search_before_replacement() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    first_started = asyncio.Event()
    calls: list[str] = []

    def make_query(keyword: str) -> ContentSearchQuery:
        return ContentSearchQuery(
            keyword,
            ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 500),
        )

    def make_session(query: ContentSearchQuery) -> SearchSession:
        return SearchSession(
            f"session-{query.keyword}",
            "a1",
            "-1001",
            "资料群",
            query,
            SearchStatus.COMPLETED,
            1,
            None,
            True,
            0,
            now,
            now,
        )

    class Gateway:
        def is_connected(self) -> bool:
            return True

    class Browser:
        async def start_search(self, _peer_ref, query, *, on_progress=None):
            calls.append(query.keyword)
            if query.keyword == "first":
                first_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    calls.append("first-cancelled")
                    raise
            return make_session(query), []

        def list_sessions(self):
            return []

    controller = AppController.for_test(
        gateway=Gateway(),
        content_browser=Browser(),
        window=ContentWindowFake(),
    )
    first = asyncio.create_task(
        controller.search_content("-1001", make_query("first"))
    )
    await first_started.wait()
    await controller.search_content("-1001", make_query("second"))
    with pytest.raises(asyncio.CancelledError):
        await first
    assert calls == ["first", "first-cancelled", "second"]
```

Use this manual retry test to prove shared recovery:

```python
@pytest.mark.asyncio
async def test_manual_connection_retry_shares_the_existing_recovery() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        def is_connected(self) -> bool:
            return False

        async def connect(self) -> None:
            self.calls += 1
            entered.set()
            await release.wait()

    gateway = Gateway()
    recovery = ConnectionRecovery(delays=(0.0,))
    controller = AppController.for_test(
        gateway=gateway,
        connection_recovery=recovery,
        window=ContentWindowFake(),
    )
    first = asyncio.create_task(controller.retry_telegram_connection())
    second = asyncio.create_task(controller.retry_telegram_connection())
    await entered.wait()
    release.set()
    assert await asyncio.gather(first, second) == [True, True]
    assert gateway.calls == 1
    assert controller.connection_recovery is recovery
```

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_controller.py -k "reports_counts or search_progress or replacement_search or manual_connection_retry" -q
```

Expected: null/page contracts and callback forwarding do not exist.

- [ ] **Step 3: Implement controller state forwarding and safe replacement**

Extend `_NullContentPage` with:

```python
def set_search_progress(self, _progress: SearchProgress | None) -> None: ...
def set_sync_state(self, _text: str, *, busy: bool = False, count: int = 0) -> None: ...
def set_connection_state(self, _text: str, *, retryable: bool = False) -> None: ...
def show_preview(self, _result: SearchResult, _path: Path | None) -> None: ...
```

In `refresh_content_dialogs()`, set busy state before awaiting connectivity, pass a callback to `sync_dialogs()`, and show the final count. Keep the old page list on failure.

In `search_content()` and `load_more_content()`, pass `page.set_search_progress` into the service. Set an initial `SearchProgress(0, 0, "正在连接 Telegram")`, and clear it in `finally` after `set_search_busy(False)`.

Before starting a replacement search, cancel and await an existing `_content_search_task` unless it is the current task. Add `retry_telegram_connection()` as a public async method that calls `ensure_telegram_online()`.

Add `open_content_preview(result_id)` that gets metadata via `content_browser.get_result()`, awaits the bounded `load_thumbnail()`, and calls `page.show_preview(result, path)`; use local errors only.

- [ ] **Step 4: Run all controller tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_controller.py -q
```

Expected: all controller tests pass, including login, queueing, cancellation, and shutdown coverage.

- [ ] **Step 5: Commit Task 5**

```powershell
git add src/telegram_downloader/controller.py tests/test_controller.py
git commit -m "feat: expose content operation feedback"
```

## Task 6: Add progress widgets, retry feedback, and larger table thumbnails

**Files:**
- Modify: `src/telegram_downloader/ui/content_browser.py:1-24, 50-349, 525-564`
- Modify: `src/telegram_downloader/ui/content_models.py:188-329`
- Modify: `tests/ui/test_content_browser.py`
- Modify: `tests/ui/test_content_models.py`

- [ ] **Step 1: Write failing Qt behavior tests**

Use these Qt assertions:

```python
assert page.result_table.iconSize() == QSize(112, 84)
assert page.result_table.verticalHeader().defaultSectionSize() == 96

page.set_search_busy(True)
page.set_search_progress(SearchProgress(20, 3, "正在扫描"))
assert page.search_progress.isVisible()
assert "已扫描 20 条" in page.search_state_label.text()
assert page.cancel_button.isVisible()

page.set_sync_state("正在刷新，已发现 3 个群组/频道", busy=True, count=3)
assert page.refresh_button.text() == "刷新中…"
assert page.sync_progress.isVisible()
assert page.refresh_button.isEnabled() is False

page.set_connection_state("离线，点击重试", retryable=True)
assert page.connection_retry_button.isVisible()
```

Use this model test for exact cached-path lookup:

```python
def test_thumbnail_path_returns_the_cached_project_file(tmp_path) -> None:
    model = SearchResultTableModel()
    item = result(datetime(2026, 8, 15, tzinfo=UTC), "r1", 1)
    path = tmp_path / "data" / "cache" / "thumbnails" / "r1.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"image")
    model.set_results([item])
    model.set_thumbnail("r1", path)
    assert model.thumbnail_path("r1") == path
```

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ui\test_content_browser.py tests\ui\test_content_models.py -q
```

Expected: missing progress widgets, retry button, large icon sizing, and path accessor failures.

- [ ] **Step 3: Implement the approved enhanced-table UI**

Add `QProgressBar` and `QSize`. Under the connection hint, place a hidden `connection_retry_button`. Under the sync label, place a hidden `sync_progress` with range `(0, 0)`. Under the search form, place a hidden `search_progress` with range `(0, 0)` and `search_state_label`.

Configure the result table exactly:

```python
self.result_table.setIconSize(QSize(112, 84))
self.result_table.verticalHeader().setDefaultSectionSize(96)
self.result_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
self.result_table.setColumnWidth(1, 124)
```

Add `connection_retry_requested = Signal()` and emit it from the retry button. `set_sync_state()` changes the button text and progress visibility. `set_search_progress()` renders `已扫描 N 条 · 找到 M 项 · <phase>` without inventing a percentage. `set_search_busy(False)` hides the progress area.

In `SearchResultTableModel`, keep `QIcon` decoration and add:

```python
def thumbnail_path(self, result_id: str) -> Path | None:
    return self._thumbnails.get(result_id)
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2. Expected: all UI/model tests pass.

- [ ] **Step 5: Commit Task 6**

```powershell
git add src/telegram_downloader/ui/content_browser.py src/telegram_downloader/ui/content_models.py tests/ui/test_content_browser.py tests/ui/test_content_models.py
git commit -m "feat: show content operation progress"
```

## Task 7: Open an in-app preview on result double-click

**Files:**
- Create: `src/telegram_downloader/ui/media_preview.py`
- Create: `tests/ui/test_media_preview.py`
- Modify: `src/telegram_downloader/ui/content_browser.py`
- Modify: `src/telegram_downloader/app.py:289-389`
- Modify: `tests/ui/test_content_browser.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write failing preview-dialog and signal tests**

```python
# tests/ui/test_media_preview.py
def test_image_preview_scales_without_losing_aspect_ratio(qtbot, tmp_path) -> None:
    path = tmp_path / "preview.png"
    image = QImage(400, 200, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.cyan)
    assert image.save(str(path))
    dialog = MediaPreviewDialog(result(now, "r1", 1), path)
    qtbot.addWidget(dialog)
    assert dialog.preview_label.pixmap().size().width() >= 400
    assert "1.mp4" in dialog.metadata_label.text()


def test_non_image_preview_shows_metadata_without_crashing(qtbot) -> None:
    dialog = MediaPreviewDialog(result(now, "r1", 1), None)
    qtbot.addWidget(dialog)
    assert "视频" in dialog.metadata_label.text()
```

Use this page signal test:

```python
with qtbot.waitSignal(page.preview_requested, timeout=500) as caught:
    page.result_table.doubleClicked.emit(page.result_model.index(0, 1))
assert caught.args == ["r1"]
```

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ui\test_media_preview.py tests\ui\test_content_browser.py -k "preview" -q
```

Expected: missing preview module and signal.

- [ ] **Step 3: Implement preview dialog and wire async loading**

Create `MediaPreviewDialog(QDialog)` with a `QScrollArea`, centered `QLabel`, metadata label, “适应窗口/原始尺寸” toggle, and close button. Load `QPixmap(path)` when valid, scale with `Qt.AspectRatioMode.KeepAspectRatio`, and use a media-type placeholder when missing or invalid. Do not create temporary files.

Add `preview_requested = Signal(str)` to `ContentBrowserPage`, connect `result_table.doubleClicked` to a handler that emits the row result ID, and retain non-blocking dialogs in `self._preview_dialogs` until their `finished` signal fires.

Implement `show_preview(result, path)` by constructing `MediaPreviewDialog(result, path, self)`, calling `open()`, and removing the dialog from the retained set on finish.

In `app.py`, add a qasync slot for `preview_requested` that awaits `controller.open_content_preview(result_id)`. Wire the retry button to `controller.retry_telegram_connection()` in the same pass.

- [ ] **Step 4: Verify preview and application wiring GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ui\test_media_preview.py tests\ui\test_content_browser.py tests\test_app.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 7**

```powershell
git add src/telegram_downloader/ui/media_preview.py src/telegram_downloader/ui/content_browser.py src/telegram_downloader/app.py tests/ui/test_media_preview.py tests/ui/test_content_browser.py tests/test_app.py
git commit -m "feat: preview content results in app"
```

## Task 8: Integration regression, packaging, and manual Windows smoke check

**Files:**
- Create: `docs/verification/2026-08-15-session-performance-content-ux-checklist.md`
- No production-file changes are planned in this task; any discovered defect returns to the task that owns the affected component and repeats its RED/GREEN cycle.

- [ ] **Step 1: Run focused integration coverage**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_instance_guard.py tests\test_connectivity.py tests\test_gateway.py tests\test_content_progress.py tests\test_content_browser.py tests\test_controller.py tests\ui\test_content_models.py tests\ui\test_content_browser.py tests\ui\test_media_preview.py tests\test_app.py -q
```

Expected: all selected tests pass with no warnings or pending-task errors.

- [ ] **Step 2: Run the complete test and lint suites**

```powershell
.\scripts\test.ps1
```

Expected: all pytest tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 3: Build both Windows distributions and run packaging smoke checks**

Ensure no packaged `TelegramDownloader.exe` from this project is running, then run:

```powershell
.\scripts\build-installer.ps1
```

Expected: exit code `0`, `PACKAGED_SMOKE_OK`, `INSTALLER_SMOKE_OK`, portable ZIP and setup EXE created under `dist`, and installation/upgrade/uninstall data-preservation checks pass on a non-C project path.

- [ ] **Step 4: Perform the live GUI acceptance checks**

Launch the freshly built executable from `dist\TelegramDownloader\TelegramDownloader.exe` and verify all of the following with project-local test data:

1. Launching a second copy displays the already-running notice and does not open a second window.
2. A saved Telegram session reaches the account page without QR login.
3. Disconnecting the network shows reconnect/offline state without clearing the Session; restoring the network reconnects without QR.
4. Clicking group refresh immediately shows “刷新中…”, moving progress, discovered count, and a final count.
5. Searching immediately shows moving progress and real scanned/matched counts; cancel stops the indicator.
6. Cached groups and search history remain visible while network work runs.
7. Result thumbnails are visibly larger; only visible rows request thumbnails.
8. Double-click opens an in-app image/cover/metadata preview and a failed preview does not affect the result list.
9. `data`, logs, cache, temp files, downloads, and updates remain below the executable/runtime root.

- [ ] **Step 5: Record evidence and run final repository checks**

Write the observed commands, outputs, package paths, SHA-256 values, and nine GUI outcomes to `docs/verification/2026-08-15-session-performance-content-ux-checklist.md`. Then run:

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors and only the verification document plus intentional source/test changes are present.

- [ ] **Step 6: Commit verification evidence**

```powershell
git add docs/verification/2026-08-15-session-performance-content-ux-checklist.md
git commit -m "docs: verify session and content UX improvements"
```

## Completion gate

Do not merge, tag, publish, or claim completion until Task 8 has fresh successful evidence. A release version bump, signed update manifest, GitHub Release, and ModelScope promotion are separate release actions and require an explicit publish request.
