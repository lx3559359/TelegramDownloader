# Telegram Content Reconnect and Link Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Telegram 短时断网后账号内容页无法同步或搜索的问题，恢复每个群组最近搜索，支持从内容搜索框路由 `t.me` 链接，并交付不在 C 盘写入应用数据的 v0.3.2 便携包与安装包候选版。

**Architecture:** 新增一个与 Qt 无关的异步连接恢复器，负责三次有限重试、并发合并和取消；`AppController` 统一调用它并把状态投射到现有界面。内容服务继续使用项目内 SQLite，只增加内存同步时间和按群组读取最近搜索的接口；界面通过明确信号完成群组选择、页面激活和链接路由，不引入后台轮询或自动下载。

**Tech Stack:** Python 3.12、asyncio、PySide6、qasync、Telethon、SQLite、pytest、pytest-asyncio、pytest-qt、PyInstaller、Inno Setup、PowerShell。

---

## File map

- Create `src/telegram_downloader/connectivity.py`: 与界面无关的连接重试、并发合并和取消逻辑。
- Create `tests/test_connectivity.py`: 连接恢复器的确定性异步单元测试。
- Modify `src/telegram_downloader/content_browser.py`: 群组缓存新鲜度和按群组查找最近搜索。
- Modify `tests/test_content_browser.py`: 缓存年龄、空列表同步时间和群组历史隔离测试。
- Modify `src/telegram_downloader/ui/content_browser.py`: 离线可编辑、连接状态、群组选择信号、搜索表单恢复和链接入口。
- Modify `tests/ui/test_content_browser.py`: 账号内容页交互测试。
- Modify `src/telegram_downloader/ui/login.py`: 已保存 API 和代理设置回填。
- Modify `tests/ui/test_login_dialog.py`: 敏感字段遮罩与回填测试。
- Modify `src/telegram_downloader/ui/main.py`: 账号内容页激活信号和任务中心链接预览入口。
- Modify `tests/ui/test_main_window.py`: 页面激活与链接路由界面测试。
- Modify `src/telegram_downloader/links.py`: 只判断是否应交给严格解析器的 `t.me` 候选链接函数。
- Modify `tests/test_links.py`: 候选识别和 `?single` 规范化回归测试。
- Modify `src/telegram_downloader/controller.py`: 统一重连、过期同步、群组历史恢复、链接路由和登录回填编排。
- Modify `tests/test_controller.py`: 控制器端到端状态与错误恢复测试。
- Modify `src/telegram_downloader/app.py`: 新增 Qt/qasync 信号连接。
- Modify `tests/test_app.py`: 应用装配和项目内路径回归测试。
- Modify `src/telegram_downloader/__init__.py`, `pyproject.toml`, `installer/TelegramDownloader.iss`: 候选版本统一为 0.3.2。
- Modify `tests/ui/test_main_window.py`, `tests/test_installer_contract.py`, `tests/test_packaging_contract.py`: 版本与打包契约回归。
- Create `docs/releases/v0.3.2.md`: 本次修复说明。
- Create `docs/verification/2026-08-14-v0.3.2-checklist.md`: 自动化、界面、便携包、安装包和路径验证实录。

## Execution preflight

- [ ] 使用 `superpowers:using-git-worktrees` 在项目内 `.worktrees/content-reconnect` 创建 `codex/fix-content-reconnect` 隔离分支。
- [ ] 在工作树执行 `powershell -ExecutionPolicy Bypass -File scripts/test.ps1`。
- [ ] 预期：当前基线 `265 passed`；若数量因仓库基线变化，以“全部通过且无失败”为准，未通过时先停止并报告，不把基线故障混入本计划。

### Task 1: Bounded connection recovery

**Files:**
- Create: `src/telegram_downloader/connectivity.py`
- Create: `tests/test_connectivity.py`

- [ ] **Step 1: Write failing retry and concurrency tests**

```python
import asyncio

import pytest

from telegram_downloader.connectivity import ConnectionRecovery
from telegram_downloader.gateway import SessionExpiredError, TransientNetworkError


@pytest.mark.asyncio
async def test_transient_failures_use_bounded_delays_then_recover() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        async def connect(self) -> None:
            self.calls += 1
            if self.calls < 3:
                raise TransientNetworkError("Telegram 网络连接失败")

    sleeps: list[float] = []
    attempts: list[tuple[int, int]] = []
    recovery = ConnectionRecovery(
        delays=(0.0, 1.0, 3.0),
        sleeper=lambda value: _record_sleep(sleeps, value),
    )

    await recovery.ensure_connected(Gateway(), attempts.append)

    assert sleeps == [1.0, 3.0]
    assert attempts == [(1, 3), (2, 3), (3, 3)]


async def _record_sleep(values: list[float], value: float) -> None:
    values.append(value)


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_connect_attempt() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class Gateway:
        calls = 0

        async def connect(self) -> None:
            self.calls += 1
            entered.set()
            await release.wait()

    gateway = Gateway()
    recovery = ConnectionRecovery(delays=(0.0,))
    first = asyncio.create_task(recovery.ensure_connected(gateway))
    second = asyncio.create_task(recovery.ensure_connected(gateway))
    await entered.wait()
    release.set()
    await asyncio.gather(first, second)

    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_session_expiry_is_not_retried() -> None:
    class Gateway:
        calls = 0

        async def connect(self) -> None:
            self.calls += 1
            raise SessionExpiredError("登录已失效")

    gateway = Gateway()
    recovery = ConnectionRecovery(delays=(0.0, 1.0, 3.0))

    with pytest.raises(SessionExpiredError):
        await recovery.ensure_connected(gateway)

    assert gateway.calls == 1
```

- [ ] **Step 2: Run the new tests and verify RED**

Run: `python -m pytest tests/test_connectivity.py -v`

Expected: collection fails with `ModuleNotFoundError: No module named 'telegram_downloader.connectivity'`.

- [ ] **Step 3: Implement the minimal recovery class**

```python
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from typing import Protocol

from telegram_downloader.gateway import TransientNetworkError


class Connectable(Protocol):
    async def connect(self) -> None: ...


AttemptCallback = Callable[[tuple[int, int]], None]
Sleeper = Callable[[float], Awaitable[None]]


class ConnectionRecovery:
    def __init__(
        self,
        *,
        delays: Sequence[float] = (0.0, 1.0, 3.0),
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        if not delays or delays[0] != 0 or any(value < 0 for value in delays):
            raise ValueError("重连延迟必须以零开始且不能为负数")
        self.delays = tuple(float(value) for value in delays)
        self.sleeper = sleeper
        self._active: asyncio.Task[None] | None = None

    async def ensure_connected(
        self,
        gateway: Connectable,
        on_attempt: AttemptCallback | None = None,
    ) -> None:
        task = self._active
        if task is None or task.done():
            task = asyncio.create_task(self._run(gateway, on_attempt))
            self._active = task
        try:
            await asyncio.shield(task)
        finally:
            if self._active is task and task.done():
                self._active = None

    async def cancel(self) -> None:
        task = self._active
        self._active = None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(
        self,
        gateway: Connectable,
        on_attempt: AttemptCallback | None,
    ) -> None:
        total = len(self.delays)
        for index, delay in enumerate(self.delays, start=1):
            if on_attempt is not None:
                on_attempt((index, total))
            if delay:
                await self.sleeper(delay)
            try:
                await gateway.connect()
                return
            except TransientNetworkError:
                if index == total:
                    raise
```

- [ ] **Step 4: Add and pass cancellation/validation tests**

Add tests that call `await recovery.cancel()` while `connect()` is blocked and assert the shared operation is cancelled, plus invalid-delay cases `()` and `(-1.0,)`.

Run: `python -m pytest tests/test_connectivity.py -v`

Expected: all connectivity tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/telegram_downloader/connectivity.py tests/test_connectivity.py
git commit -m "feat: add bounded Telegram connection recovery"
```

### Task 2: Dialog cache freshness and per-dialog history

**Files:**
- Modify: `src/telegram_downloader/content_browser.py:65-158`
- Modify: `tests/test_content_browser.py`

- [ ] **Step 1: Write failing service tests**

```python
from datetime import timedelta


@pytest.mark.asyncio
async def test_dialog_cache_age_and_empty_sync_are_tracked(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 8, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    gateway = FakeGateway(AccountProfile("a1", "账号一"))
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        gateway=gateway,
        planner=PlannerStub(),
        clock=lambda: now,
    )
    await service.activate_account()

    assert service.dialog_cache_stale(timedelta(seconds=60)) is True
    await service.sync_dialogs()
    assert service.dialog_cache_stale(timedelta(seconds=60)) is False


@pytest.mark.asyncio
async def test_latest_session_is_scoped_to_selected_dialog(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 8, tzinfo=UTC)
    catalog = initialized_catalog(tmp_path)
    catalog.upsert_account(AccountProfile("a1", "账号一"), now)
    catalog.replace_dialogs(
        "a1",
        [
            make_dialog("a1", "-1001", "群一", now),
            make_dialog("a1", "-1002", "群二", now),
        ],
        now,
    )
    service = ContentBrowserService(
        catalog,
        ThumbnailCache(tmp_path / "thumbs"),
        clock=lambda: now,
    )
    await service.activate_cached_account()
    first = catalog.begin_search("s1", "a1", "-1001", "群一", make_query(now, "甲"), now)
    catalog.begin_search("s2", "a1", "-1002", "群二", make_query(now, "乙"), now)

    assert service.latest_session("-1001") == first
    assert service.latest_session("-9999") is None
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_content_browser.py -k "dialog_cache_age or latest_session" -v`

Expected: FAIL because `dialog_cache_stale` and `latest_session` do not exist.

- [ ] **Step 3: Implement freshness and history lookup**

Add `self._last_dialog_sync_at: datetime | None = None` in `ContentBrowserService.__init__`, initialize it from the newest cached dialog whenever an account is activated, and set it to `now` after every successful `sync_dialogs()`, including an empty result.

```python
def dialog_cache_stale(self, max_age: timedelta) -> bool:
    if max_age.total_seconds() < 0:
        raise ValueError("缓存有效期不能为负数")
    synced_at = self._last_dialog_sync_at
    return synced_at is None or self.clock() - synced_at > max_age

def latest_session(self, peer_ref: str) -> SearchSession | None:
    return next(
        (item for item in self.list_sessions() if item.peer_ref == peer_ref),
        None,
    )

def _restore_dialog_sync_time(self) -> None:
    dialogs = self.list_dialogs(include_unavailable=True)
    self._last_dialog_sync_at = max(
        (item.last_synced_at for item in dialogs),
        default=None,
    )
```

- [ ] **Step 4: Run focused and complete service tests**

Run: `python -m pytest tests/test_content_browser.py -v`

Expected: all content browser service tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/telegram_downloader/content_browser.py tests/test_content_browser.py
git commit -m "feat: track dialog cache freshness and history"
```

### Task 3: Offline-editable content page and search restoration

**Files:**
- Modify: `src/telegram_downloader/links.py`
- Modify: `src/telegram_downloader/ui/content_browser.py:49-520`
- Modify: `tests/test_links.py`
- Modify: `tests/ui/test_content_browser.py`

- [ ] **Step 1: Write failing candidate-link and UI tests**

```python
from telegram_downloader.links import is_telegram_link_candidate


def test_only_http_tme_urls_are_link_candidates() -> None:
    assert is_telegram_link_candidate("https://t.me/Zhangzhoulao66/56156?single")
    assert is_telegram_link_candidate("http://www.t.me/example")
    assert not is_telegram_link_candidate("美丽")
    assert not is_telegram_link_candidate("https://example.com/t.me/demo")
```

```python
def test_offline_page_keeps_query_editable_and_routes_tme_link(qtbot) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_logged_in(False)
    page.set_dialogs([dialog(now)])
    page.dialog_list.setCurrentIndex(page.dialog_model.index(0, 0))

    assert page.keyword_input.isEnabled() is True
    assert page.search_button.isEnabled() is True
    page.keyword_input.setText("https://t.me/Zhangzhoulao66/56156?single")
    with qtbot.waitSignal(page.link_requested, timeout=500) as caught:
        qtbot.mouseClick(page.search_button, Qt.MouseButton.LeftButton)
    assert caught.args == ["https://t.me/Zhangzhoulao66/56156?single"]


def test_dialog_selection_emits_peer_and_restores_form(qtbot) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_dialogs([dialog(now)])
    with qtbot.waitSignal(page.dialog_selected, timeout=500) as caught:
        page.dialog_list.setCurrentIndex(page.dialog_model.index(0, 0))
    assert caught.args == ["-1001"]

    restored = session(now)
    page.set_active_search(restored)
    assert page.keyword_input.text() == "安装"
    assert page.limit_input.value() == 500
    assert all(page.media_checks[kind].isChecked() for kind in MediaKind)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_links.py tests/ui/test_content_browser.py -v`

Expected: FAIL because the helper and new signals do not exist and logged-out inputs are disabled.

- [ ] **Step 3: Add strict candidate classification and UI signals**

```python
def is_telegram_link_candidate(value: str) -> bool:
    parsed = urlparse(value.strip())
    return (
        parsed.scheme.lower() in {"http", "https"}
        and (parsed.hostname or "").lower() in {"t.me", "www.t.me"}
    )
```

Add to `ContentBrowserPage`:

```python
dialog_selected = Signal(str)
link_requested = Signal(str)

def set_connection_state(self, text: str) -> None:
    self.empty_hint.setText(text)

def _dialog_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
    if current.isValid():
        dialog = self.dialog_model.dialog_at(current.row())
        self.current_dialog_label.setText(dialog.title)
        self.dialog_selected.emit(dialog.peer_ref)
    else:
        self.current_dialog_label.setText("请选择群组或频道")
    self._refresh_actions()
```

In `_emit_search`, check `is_telegram_link_candidate(keyword)` immediately after reading the input and before requiring a selected dialog; emit `link_requested` and return. In `_refresh_actions`, keep `keyword_input`, `search_button`, and `refresh_button` enabled whenever their own operation is not busy; keep queue, selection, and load-more actions dependent on online state.

- [ ] **Step 4: Restore all form values from the active session**

Update `set_active_search` so a non-null session restores keyword, inclusive local dates, limit and media checks; a null session resets keyword, the seven-day date range, limit 500 and all media types.

```python
def _set_form_from_session(self, session: SearchSession | None) -> None:
    if session is None:
        self.keyword_input.clear()
        self.date_from.setDate(QDate.currentDate().addDays(-7))
        self.date_to.setDate(QDate.currentDate())
        self.limit_input.setValue(500)
        selected = frozenset(MediaKind)
    else:
        filters = session.query.filters
        start = filters.date_from_utc.astimezone().date()
        end = filters.date_to_utc.astimezone().date()
        self.keyword_input.setText(session.query.keyword)
        self.date_from.setDate(QDate(start.year, start.month, start.day))
        self.date_to.setDate(QDate(end.year, end.month, end.day))
        self.limit_input.setValue(filters.item_limit)
        selected = filters.media_kinds
    for kind, check in self.media_checks.items():
        check.setChecked(kind in selected)
```

- [ ] **Step 5: Run UI and parser tests**

Run: `python -m pytest tests/test_links.py tests/ui/test_content_browser.py -v`

Expected: all selected tests pass, including existing history, thumbnails and selection tests.

- [ ] **Step 6: Commit Task 3**

```powershell
git add src/telegram_downloader/links.py src/telegram_downloader/ui/content_browser.py tests/test_links.py tests/ui/test_content_browser.py
git commit -m "feat: keep content search editable while offline"
```

### Task 4: Saved login settings prefill

**Files:**
- Modify: `src/telegram_downloader/ui/login.py:35-370`
- Modify: `src/telegram_downloader/controller.py:82-140, 330-366, 859-873`
- Modify: `tests/ui/test_login_dialog.py`
- Modify: `tests/test_controller.py`

- [ ] **Step 1: Write failing prefill tests**

```python
from telegram_downloader.settings import ProxySettings


def test_saved_credentials_and_proxy_are_prefilled_but_masked(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)
    proxy = ProxySettings("socks5", "127.0.0.1", 1080, "alice")

    dialog.set_saved_credentials(12345, "saved-hash", proxy, "saved-password")

    assert dialog.api_id.value() == 12345
    assert dialog.api_hash.text() == "saved-hash"
    assert dialog.api_hash.echoMode() is QLineEdit.EchoMode.Password
    assert dialog.proxy_kind.currentData() == "socks5"
    assert dialog.proxy_host.text() == "127.0.0.1"
    assert dialog.proxy_port.value() == 1080
    assert dialog.proxy_username.text() == "alice"
    assert dialog.proxy_password.text() == "saved-password"
    assert dialog.proxy_password.echoMode() is QLineEdit.EchoMode.Password
```

Add a controller test whose fake dialog records `set_saved_credentials(...)`, call `show_login()`, and assert saved API/proxy values are passed before `show()`.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/ui/test_login_dialog.py tests/test_controller.py -k "prefill or show_login_uses_saved" -v`

Expected: FAIL because `set_saved_credentials` is missing or not called.

- [ ] **Step 3: Implement dialog prefill**

```python
def set_saved_credentials(
    self,
    api_id: int,
    api_hash: str,
    proxy: ProxySettings,
    proxy_password: str,
) -> None:
    self.api_id.setValue(max(0, api_id))
    self.api_hash.setText(api_hash)
    index = self.proxy_kind.findData(proxy.kind)
    self.proxy_kind.setCurrentIndex(max(0, index))
    self.proxy_host.setText(proxy.host)
    self.proxy_port.setValue(proxy.port)
    self.proxy_username.setText(proxy.username)
    self.proxy_password.setText(proxy_password)
    self._update_proxy_fields()
```

Add `_prefill_login()` to `AppController`, call it at the start of `show_login()` and `edit_credentials()`, and add a no-op method to `_NullLoginDialog`.

- [ ] **Step 4: Preserve prefilled data on QR network failure**

Catch `TransientNetworkError` separately in `begin_qr_login()` and `refresh_qr_login()`: call `_prefill_login()`, show `LoginPage.CREDENTIALS`, then show the safe network error. Do not clear `self.secrets` or overwrite settings.

- [ ] **Step 5: Run login and controller tests**

Run: `python -m pytest tests/ui/test_login_dialog.py tests/test_controller.py -v`

Expected: all tests pass; existing successful-login tests still verify sensitive values are cleared after login completes.

- [ ] **Step 6: Commit Task 4**

```powershell
git add src/telegram_downloader/ui/login.py src/telegram_downloader/controller.py tests/ui/test_login_dialog.py tests/test_controller.py
git commit -m "fix: prefill saved Telegram login settings"
```

### Task 5: Controller reconnect orchestration and stale refresh

**Files:**
- Modify: `src/telegram_downloader/controller.py:1-1105`
- Modify: `tests/test_controller.py`

- [ ] **Step 1: Extend controller fakes and write failing behavior tests**

Extend `ContentPageFake` with `connection_states` and `set_connection_state(text)`. Add tests for:

```python
@pytest.mark.asyncio
async def test_offline_search_reconnects_then_continues_without_losing_query() -> None:
    calls = []

    class Gateway:
        async def connect(self):
            calls.append("connect")

    class ContentService:
        async def start_search(self, peer_ref, query):
            calls.append(("search", peer_ref, query))
            return SimpleNamespace(id="s1"), []

        def list_sessions(self):
            return [SimpleNamespace(id="s1")]

        def list_results(self, _search_id):
            return []

    query = object()
    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=Gateway(), content_browser=ContentService(), window=window
    )

    await controller.search_content("-1001", query)

    assert calls == ["connect", ("search", "-1001", query)]
    assert window.content_page.busy == [True, False]
```

Also add deterministic tests asserting:

- three transient failures leave cached dialogs/results intact and show `重连失败，请检查网络或代理后重试`;
- two concurrent callers reach one `gateway.connect()`;
- `activate_content_page()` refreshes only when `dialog_cache_stale(timedelta(seconds=60))` is true;
- manual `refresh_content_dialogs()` always forces a sync after reconnect;
- `select_content_dialog(peer_ref)` restores that peer's latest session and results before awaiting network;
- `SessionExpiredError` still clears only `session` and opens the prefilled login flow;
- `shutdown()` cancels the connection recovery task before disconnecting services.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python -m pytest tests/test_controller.py -k "reconnect or activate_content_page or selected_dialog or stale" -v`

Expected: FAIL because the controller does not own a recovery object and the new entry points do not exist.

- [ ] **Step 3: Inject and expose the recovery service**

Import `ConnectionRecovery`, add optional `connection_recovery` to `AppController.__init__` and `for_test`, default it to `ConnectionRecovery()`, and add a no-op `set_connection_state` to `_NullContentPage`.

```python
async def ensure_telegram_online(self) -> bool:
    page = self._content_page()
    if self.gateway is None:
        page.set_logged_in(False)
        page.set_connection_state("请先登录 Telegram；已保存的搜索历史仍可查看")
        self.show_login()
        return False

    def attempt(value: tuple[int, int]) -> None:
        number, total = value
        text = (
            "正在连接 Telegram…"
            if number == 1
            else f"正在重连（{number}/{total}）…"
        )
        page.set_connection_state(text)

    try:
        await self.connection_recovery.ensure_connected(self.gateway, attempt)
    except SessionExpiredError as error:
        await self._handle_session_expired(error)
        return False
    except TransientNetworkError:
        page.set_logged_in(False)
        page.set_connection_state("重连失败，请检查网络或代理后重试")
        return False

    page.set_logged_in(True)
    page.set_connection_state("连接已恢复")
    return True
```

- [ ] **Step 4: Route all remote content actions through the recovery entry**

Implement `activate_content_page()` and `select_content_dialog(peer_ref)`. The selection method restores local history first, then reconnects and schedules a dialog sync only when the 60-second cache is stale. Change startup, stale refresh, manual refresh, `search_content`, `load_more_content`, and task-center `scan_link` to call `ensure_telegram_online()` before the remote operation. Set search busy before reconnect so duplicate submits are blocked, and always clear it in `finally`.

For stale refresh, use exactly `timedelta(seconds=60)` and do not create another sync task while `_dialog_sync_task` is running. Manual refresh bypasses the stale check. A failed reconnect must return before `ContentBrowserService.start_search`, so no empty search history is created.

- [ ] **Step 5: Preserve session-expiry and shutdown ordering**

At the start of `_handle_session_expired`, call `await self.connection_recovery.cancel()`. Also cancel it before replacing or disconnecting the gateway in `submit_credentials()` and `edit_credentials()`. In `shutdown`, cancel content operations, then connection recovery, then scheduler/gateway. Keep API Hash and proxy secrets while deleting only `session`.

- [ ] **Step 6: Run controller tests**

Run: `python -m pytest tests/test_controller.py -v`

Expected: all controller tests pass, including existing scan safety, task progress, QR login and session-expiry tests.

- [ ] **Step 7: Commit Task 5**

```powershell
git add src/telegram_downloader/controller.py tests/test_controller.py
git commit -m "fix: reconnect Telegram before content operations"
```

### Task 6: Page activation and t.me link routing

**Files:**
- Modify: `src/telegram_downloader/ui/main.py:42-465`
- Modify: `src/telegram_downloader/controller.py:467-495`
- Modify: `src/telegram_downloader/app.py:228-397`
- Modify: `tests/ui/test_main_window.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write failing window and controller routing tests**

```python
def test_content_navigation_emits_activation_and_link_preview_routes_to_tasks(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    with qtbot.waitSignal(window.content_activated, timeout=500):
        qtbot.mouseClick(window.content_nav_button, Qt.MouseButton.LeftButton)

    with qtbot.waitSignal(window.scan_requested, timeout=500) as caught:
        window.open_link_preview("https://t.me/Zhangzhoulao66/56156")

    assert window.page_stack.currentWidget() is window.task_page
    assert window.link_input.text() == "https://t.me/Zhangzhoulao66/56156"
    assert caught.args == ["https://t.me/Zhangzhoulao66/56156"]
```

Add controller tests asserting `route_content_link(".../56156?single")` passes the normalized URL without `?single` to `window.open_link_preview`, while malformed `https://t.me/bad#fragment` remains on the content page and shows `InvalidTelegramLink`'s safe Chinese message.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python -m pytest tests/ui/test_main_window.py tests/test_controller.py -k "activation or link_preview or route_content" -v`

Expected: FAIL because the signal, window method and controller route are absent.

- [ ] **Step 3: Implement window route and strict controller normalization**

```python
class MainWindow(QMainWindow):
    content_activated = Signal()

    def show_page(self, name: str) -> None:
        content = name == "content"
        self.page_stack.setCurrentWidget(
            self.content_page if content else self.task_page
        )
        self.statistics_panel.setVisible(not content)
        self._set_nav_active(
            self.content_nav_button if content else self.tasks_nav_button
        )
        if content:
            self.content_activated.emit()

    def open_link_preview(self, link: str) -> None:
        self.link_input.setText(link)
        self.show_page("tasks")
        self.scan_requested.emit(link)
```

```python
def route_content_link(self, link: str) -> None:
    try:
        source = parse_telegram_link(link)
    except (InvalidTelegramLink, ValueError) as error:
        self._content_page().show_error(str(error))
        return
    self.window.open_link_preview(source.normalized_url)
```

- [ ] **Step 4: Wire qasync and direct signals in app assembly**

Add async slots for `content_activated` and `dialog_selected`, calling `controller.activate_content_page()` and `controller.select_content_dialog(peer_ref)`. Connect `content_page.link_requested` directly to `controller.route_content_link`. Retain all slot objects in `controller._ui_slots`.

- [ ] **Step 5: Add application assembly regression**

In `tests/test_app.py`, create the application under `tmp_path`, assert the controller exposes the recovery service, emit `content_page.link_requested` with an invalid link, and assert the error remains local without creating a task record. Also assert every `run_self_test(tmp_path)["writable_paths"]` value resolves under `tmp_path`.

- [ ] **Step 6: Run routing and assembly tests**

Run: `python -m pytest tests/test_links.py tests/ui/test_main_window.py tests/test_controller.py tests/test_app.py -v`

Expected: all selected tests pass; routing only starts scan preview and still requires the existing confirmation before queue insertion.

- [ ] **Step 7: Commit Task 6**

```powershell
git add src/telegram_downloader/ui/main.py src/telegram_downloader/controller.py src/telegram_downloader/app.py tests/ui/test_main_window.py tests/test_controller.py tests/test_app.py
git commit -m "feat: route content links to task preview"
```

### Task 7: Full regression and v0.3.2 candidate metadata

**Files:**
- Modify: `src/telegram_downloader/__init__.py`
- Modify: `pyproject.toml`
- Modify: `installer/TelegramDownloader.iss`
- Modify: `tests/ui/test_main_window.py`
- Modify: `tests/test_installer_contract.py`
- Modify: `tests/test_packaging_contract.py`
- Create: `docs/releases/v0.3.2.md`

- [ ] **Step 1: Run the complete suite before the version bump**

Run: `powershell -ExecutionPolicy Bypass -File scripts/test.ps1`

Expected: all tests pass. This includes download queue, retry, pause/resume, naming, project-local paths, settings, signed dual-source updates and package contracts.

- [ ] **Step 2: Write failing version assertions**

Change existing version expectations in UI and package-contract tests from `0.3.1` to `0.3.2`, then run:

Run: `python -m pytest tests/ui/test_main_window.py tests/test_installer_contract.py tests/test_packaging_contract.py -v`

Expected: FAIL while production metadata still reports `0.3.1`.

- [ ] **Step 3: Update all version sources**

```python
__version__ = "0.3.2"
```

Set `version = "0.3.2"` in `pyproject.toml` and `#define AppVersion "0.3.2"` in `installer/TelegramDownloader.iss`.

- [ ] **Step 4: Add release notes**

Create `docs/releases/v0.3.2.md` with these completed changes: bounded automatic reconnect, stale-aware group sync, per-group search restoration, offline-editable search, `t.me` link routing, saved API/proxy prefill, preserved project-local data paths, and no automatic download after link detection. State that remote publication is pending user approval.

- [ ] **Step 5: Run the complete suite again**

Run: `powershell -ExecutionPolicy Bypass -File scripts/test.ps1`

Expected: all tests pass with the new tests added in Tasks 1-6; no warnings contain credentials or session data.

- [ ] **Step 6: Commit Task 7**

```powershell
git add src/telegram_downloader/__init__.py pyproject.toml installer/TelegramDownloader.iss tests/ui/test_main_window.py tests/test_installer_contract.py tests/test_packaging_contract.py docs/releases/v0.3.2.md
git commit -m "chore: prepare v0.3.2 candidate"
```

### Task 8: Portable and installer candidate verification

**Files:**
- Create: `docs/verification/2026-08-14-v0.3.2-checklist.md`
- Generated: `dist/TelegramDownloader-0.3.2-win-x64-portable.zip`
- Generated: `dist/release/TelegramDownloader-0.3.2-win-x64-setup.exe`

- [ ] **Step 1: Ensure the formal app process is closed before rebuilding**

Run: `Get-Process TelegramDownloader -ErrorAction SilentlyContinue | Select-Object Id, Path`

Expected: no process. If the user has the program open, ask them to close it; do not terminate it without permission.

- [ ] **Step 2: Build and smoke-test both package forms**

Run: `powershell -ExecutionPolicy Bypass -File scripts/build-installer.ps1`

Expected: exit code 0. `scripts/build.ps1` runs the packaged self-test, and `scripts/smoke-installer.ps1` installs under `.build-temp/installed-smoke`, verifies project-local `data`, starts the installed executable self-test, and uninstalls the smoke copy.

- [ ] **Step 3: Verify candidate files and hashes**

Run:

```powershell
Get-Item 'dist\TelegramDownloader-0.3.2-win-x64-portable.zip', 'dist\release\TelegramDownloader-0.3.2-win-x64-setup.exe' | Select-Object FullName, Length
Get-FileHash -Algorithm SHA256 'dist\TelegramDownloader-0.3.2-win-x64-portable.zip', 'dist\release\TelegramDownloader-0.3.2-win-x64-setup.exe'
```

Expected: both files exist, have non-zero lengths, and each has a 64-character SHA-256 hash.

- [ ] **Step 4: Perform Windows GUI self-check without automating authentication**

Start `dist/TelegramDownloader/TelegramDownloader.exe`, observe the existing project-local account data, and verify:

1. startup reconnect status progresses and the UI remains responsive;
2. opening “账号内容” refreshes only stale dialogs;
3. selecting `@Zhangzhoulao66` restores its last search without starting a new search and does not repeat a fresh list sync;
4. the search field remains editable whenever no search/reconnect operation is active; the deterministic automated tests, rather than a system network change, verify the three-attempt offline path;
5. `https://t.me/Zhangzhoulao66/56156?single` routes to task center as normalized `https://t.me/Zhangzhoulao66/56156` and opens preview confirmation;
6. decline the confirmation so no duplicate download is created;
7. saved API ID, masked API Hash and proxy settings appear in the login page;
8. existing task list, pause/resume controls, settings, open-directory action and update-check entry still work;
9. `data`, logs, cache, temp, downloads and update staging remain below `dist/TelegramDownloader`.

Stop immediately if computer-use detects concurrent user input. Never type API credentials, phone codes, passwords or scan a QR code on the user's behalf.

- [ ] **Step 5: Write the verification record using observed facts**

Create `docs/verification/2026-08-14-v0.3.2-checklist.md` containing the exact pytest pass count, package byte sizes and SHA-256 values from Steps 2-3, each GUI check result from Step 4, and an explicit statement that GitHub/魔塔 publication and online manifest promotion were not performed.

- [ ] **Step 6: Commit Task 8 documentation only**

```powershell
git add docs/verification/2026-08-14-v0.3.2-checklist.md
git commit -m "docs: record v0.3.2 candidate verification"
```

Do not commit generated `dist`, `build`, `.build-temp`, caches, logs, secrets, databases or downloads.

### Task 9: Review and final verification

**Files:**
- Review all files changed since the implementation branch point.

- [ ] **Step 1: Use `superpowers:requesting-code-review`**

Request a fresh review against `docs/superpowers/specs/2026-08-14-content-reconnect-link-routing-design.md`. Require the reviewer to check retry cancellation, session-expiry safety, no automatic download, Qt signal lifetime, project-local paths and preservation of user data.

- [ ] **Step 2: Address only evidence-backed findings**

For every accepted finding, write or tighten a failing test, observe it fail, apply the smallest fix, rerun the focused test, and commit with a message describing that fix. If no actionable findings exist, make no review-only code change.

- [ ] **Step 3: Use `superpowers:verification-before-completion` and rerun fresh checks**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/test.ps1
powershell -ExecutionPolicy Bypass -File scripts/build-installer.ps1
git status --short
git log --oneline --decorate -10
```

Expected: all tests and both package smoke checks pass; the worktree is clean; generated packages remain untracked/ignored; no user data is staged.

- [ ] **Step 4: Hand off the candidate without publishing**

Report the local branch, commit range, test count, GUI results, package paths, sizes and hashes. Ask for explicit approval before merging to `main`, pushing GitHub/魔塔 tags, creating a GitHub release or promoting the signed online-update manifest.
