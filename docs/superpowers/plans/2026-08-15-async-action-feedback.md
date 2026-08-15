# Async Action Reliability and Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复零参数 Qt 信号触发异步方法时无响应的问题，并为刷新、重连、登录切换和加入下载队列提供及时、可恢复的局部反馈。

**Architecture:** 新增独立的 `AsyncActionBridge`，把零参数 Qt 信号转换成有生命周期管理的 asyncio 任务，以动作键去重并统一处理成功、失败、取消和关闭。业务逻辑仍留在 `AppController`；`ContentBrowserPage` 与 `LoginDialog` 只增加小范围忙碌态 API，不引入事件总线或修改 Telegram、下载、目录、更新协议。

**Tech Stack:** Python 3.12、PySide6、asyncio、qasync 0.28.0、pytest/pytest-asyncio、pytest-qt、Ruff、PyInstaller、Inno Setup。

---

## 文件结构

- 新建 `src/telegram_downloader/ui/async_actions.py`：零参数异步动作的调度、同键去重、回调、异常消费和关闭取消。
- 新建 `tests/ui/test_async_actions.py`：桥接组件的成功、重复触发、异常和取消回归。
- 修改 `src/telegram_downloader/app.py`：移除 7 个零参数 `@qasync.asyncSlot()`，接入桥接并在退出时关闭桥接。
- 修改 `tests/test_app.py`：通过真实 `create_application` 信号连接触发 7 个动作，证明不再依赖有缺陷的零参数 qasync 包装器。
- 修改 `src/telegram_downloader/ui/content_browser.py`：增加重连与入队的局部忙碌态。
- 修改 `tests/ui/test_content_browser.py`：验证即时按钮文本、禁用重复触发和结束恢复。
- 修改 `src/telegram_downloader/ui/login.py`：增加二维码刷新、手机号切换、凭据编辑的局部忙碌态。
- 修改 `tests/ui/test_login_dialog.py`：验证登录动作进行中与恢复状态。
- 修改 `src/telegram_downloader/controller.py`：保证入队操作无论确认、成功、异常或取消都恢复忙碌态。
- 修改 `tests/test_controller.py`：验证入队状态序列和确认取消反馈。
- 修改 `src/telegram_downloader/__init__.py`、`pyproject.toml`、`installer/TelegramDownloader.iss`：同步候选版本 `0.4.2`。
- 修改 `tests/test_packaging_contract.py`、`tests/ui/test_main_window.py`：同步版本契约断言。
- 新建 `docs/releases/v0.4.2.md`：记录可靠性与交互修复，不修改线上 stable 指针。

### Task 1: 实现零参数异步动作桥接

**Files:**
- Create: `src/telegram_downloader/ui/async_actions.py`
- Create: `tests/ui/test_async_actions.py`

- [ ] **Step 1: 写出同键去重与成功回调的失败测试**

在 `tests/ui/test_async_actions.py` 写入：

```python
import asyncio

import pytest

from telegram_downloader.ui.async_actions import ActionHooks, AsyncActionBridge


@pytest.mark.asyncio
async def test_same_action_key_runs_once_and_restores_state() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def action() -> None:
        calls.append("action")
        started.set()
        await release.wait()

    bridge = AsyncActionBridge()
    hooks = ActionHooks(
        started=lambda: calls.append("started"),
        succeeded=lambda: calls.append("succeeded"),
        finished=lambda: calls.append("finished"),
    )

    assert bridge.start("dialogs.refresh", action, hooks=hooks) is True
    await started.wait()
    assert bridge.start("dialogs.refresh", action, hooks=hooks) is False
    release.set()
    await bridge.wait_idle()

    assert calls == ["started", "action", "succeeded", "finished"]
    assert bridge.active_keys == frozenset()
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_async_actions.py::test_same_action_key_runs_once_and_restores_state -q`

Expected: FAIL，错误包含 `ModuleNotFoundError: No module named 'telegram_downloader.ui.async_actions'`。

- [ ] **Step 3: 写出异常消费和关闭取消的失败测试**

继续在 `tests/ui/test_async_actions.py` 写入：

```python
@pytest.mark.asyncio
async def test_failure_is_reported_without_leaking_from_task() -> None:
    events: list[object] = []

    async def action() -> None:
        raise RuntimeError("private response body")

    bridge = AsyncActionBridge()
    bridge.start(
        "content.activate",
        action,
        hooks=ActionHooks(
            failed=lambda error: events.append(type(error).__name__),
            finished=lambda: events.append("finished"),
        ),
    )
    await bridge.wait_idle()

    assert events == ["RuntimeError", "finished"]
    assert bridge.active_keys == frozenset()


@pytest.mark.asyncio
async def test_shutdown_cancels_running_actions_and_calls_cleanup() -> None:
    entered = asyncio.Event()
    events: list[str] = []

    async def action() -> None:
        entered.set()
        await asyncio.Event().wait()

    bridge = AsyncActionBridge()
    bridge.start(
        "login.qr.refresh",
        action,
        hooks=ActionHooks(
            cancelled=lambda: events.append("cancelled"),
            finished=lambda: events.append("finished"),
        ),
    )
    await entered.wait()
    await bridge.shutdown()

    assert events == ["cancelled", "finished"]
    assert bridge.active_keys == frozenset()
```

- [ ] **Step 4: 实现最小桥接组件**

在 `src/telegram_downloader/ui/async_actions.py` 实现以下公开接口和行为：

```python
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger("telegram_downloader.ui.async_actions")

ActionFactory = Callable[[], Awaitable[Any]]
Callback = Callable[[], None]
FailureCallback = Callable[[Exception], None]


@dataclass(frozen=True, slots=True)
class ActionHooks:
    started: Callback | None = None
    succeeded: Callback | None = None
    failed: FailureCallback | None = None
    cancelled: Callback | None = None
    finished: Callback | None = None


class AsyncActionBridge:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._slots: list[Callable[[], None]] = []

    @property
    def active_keys(self) -> frozenset[str]:
        return frozenset(self._tasks)

    def connect(
        self,
        signal: Any,
        key: str,
        action: ActionFactory,
        *,
        hooks: ActionHooks = ActionHooks(),
    ) -> Callable[[], None]:
        def trigger() -> None:
            self.start(key, action, hooks=hooks)

        signal.connect(trigger)
        self._slots.append(trigger)
        return trigger

    def start(
        self,
        key: str,
        action: ActionFactory,
        *,
        hooks: ActionHooks = ActionHooks(),
    ) -> bool:
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            return False
        self._invoke(hooks.started, key)
        task = asyncio.create_task(self._run(key, action, hooks), name=f"ui:{key}")
        self._tasks[key] = task
        return True

    async def _run(
        self,
        key: str,
        action: ActionFactory,
        hooks: ActionHooks,
    ) -> None:
        try:
            await action()
        except asyncio.CancelledError:
            self._invoke(hooks.cancelled, key)
            raise
        except Exception as error:
            _LOGGER.warning("async UI action %s failed (%s)", key, type(error).__name__)
            self._invoke(hooks.failed, key, error)
        else:
            self._invoke(hooks.succeeded, key)
        finally:
            self._invoke(hooks.finished, key)
            if self._tasks.get(key) is asyncio.current_task():
                self._tasks.pop(key, None)

    async def wait_idle(self) -> None:
        pending = tuple(self._tasks.values())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def shutdown(self) -> None:
        pending = tuple(task for task in self._tasks.values() if not task.done())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    @staticmethod
    def _invoke(callback: Callable[..., None] | None, key: str, *args: object) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as error:
            _LOGGER.warning(
                "async UI action callback %s failed (%s)",
                key,
                type(error).__name__,
            )
```

日志只包含动作键和异常类型，不记录异常正文，避免 Telegram 响应或凭据进入日志。

- [ ] **Step 5: 运行桥接测试和 Ruff**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_async_actions.py -q`

Expected: 3 tests passed。

Run: `.venv\Scripts\ruff.exe check src/telegram_downloader/ui/async_actions.py tests/ui/test_async_actions.py`

Expected: `All checks passed!`

- [ ] **Step 6: 提交桥接组件**

```powershell
git add src/telegram_downloader/ui/async_actions.py tests/ui/test_async_actions.py
git commit -m "fix: add reliable async UI action bridge"
```

### Task 2: 增加局部交互忙碌态

**Files:**
- Modify: `src/telegram_downloader/ui/content_browser.py:69-85,343-370,538-540,603-627`
- Modify: `tests/ui/test_content_browser.py:92-131,232-260`
- Modify: `src/telegram_downloader/ui/login.py:50-60,171-180,265-274`
- Modify: `tests/ui/test_login_dialog.py:83-104`

- [ ] **Step 1: 写出账号内容页忙碌态的失败测试**

在 `tests/ui/test_content_browser.py` 增加：

```python
def test_connection_and_queue_busy_states_disable_duplicate_actions(qtbot) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_logged_in(True)
    page.set_dialogs([dialog(now)])
    page.dialog_list.setCurrentIndex(page.dialog_model.index(0, 0))
    page.set_active_search(session(now))
    page.set_results([replace(result(now, "r1", 1), selected=True)])
    page.set_connection_state("离线，点击重试", retryable=True)

    page.set_connection_action_busy(True)
    assert page.connection_retry_button.text() == "重连中…"
    assert page.connection_retry_button.isEnabled() is False
    page.set_connection_action_busy(False)
    assert page.connection_retry_button.text() == "重新连接"
    assert page.connection_retry_button.isEnabled() is True

    page.set_queue_busy(True)
    assert page.queue_button.text() == "正在准备已选 1 项…"
    assert page.queue_button.isEnabled() is False
    page.set_queue_busy(False)
    assert page.queue_button.text() == "加入下载队列"
    assert page.queue_button.isEnabled() is True
```

并在现有 `test_selection_summary_and_queue_signal_skip_unavailable_and_queued` 中，在点击后追加：

```python
assert page.queue_button.text() == "正在准备已选 2 项…"
assert page.queue_button.isEnabled() is False
```

- [ ] **Step 2: 写出登录动作忙碌态的失败测试**

在 `tests/ui/test_login_dialog.py` 增加：

```python
def test_qr_actions_show_scoped_busy_state_and_recover(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)

    dialog.set_action_busy("qr.refresh", True)
    assert dialog.qr_refresh.text() == "正在刷新…"
    assert dialog.qr_refresh.isEnabled() is False
    assert dialog.phone_fallback.isEnabled() is False
    assert dialog.credentials_edit.isEnabled() is False

    dialog.set_action_busy("qr.refresh", False)
    assert dialog.qr_refresh.text() == "刷新二维码"
    assert dialog.qr_refresh.isEnabled() is True
    assert dialog.phone_fallback.isEnabled() is True
    assert dialog.credentials_edit.isEnabled() is True
```

- [ ] **Step 3: 运行 UI 测试并确认缺少状态 API**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_content_browser.py tests/ui/test_login_dialog.py -q`

Expected: FAIL，错误分别指出 `set_connection_action_busy`、`set_queue_busy` 或 `set_action_busy` 不存在。

- [ ] **Step 4: 实现账号内容页局部状态**

在 `ContentBrowserPage.__init__` 增加 `_connection_action_busy = False` 和 `_queue_busy = False`。增加：

```python
def set_connection_action_busy(self, busy: bool) -> None:
    self._connection_action_busy = busy
    self.connection_retry_button.setText("重连中…" if busy else "重新连接")
    self._refresh_actions()

def set_queue_busy(self, busy: bool) -> None:
    self._queue_busy = busy
    selected = sum(
        item.selected and item.available and not item.queued for item in self.results
    )
    self.queue_button.setText(
        f"正在准备已选 {selected} 项…" if busy else "加入下载队列"
    )
    self._refresh_actions()
```

在 `_emit_queue` 中先执行 `self.set_queue_busy(True)` 再发射 `queue_requested`。在 `_refresh_actions` 中让重连按钮受 `not self._connection_action_busy` 控制，并给入队按钮原条件追加 `and not self._queue_busy`。刷新按钮仍只受 `_sync_busy` 控制，缓存浏览和历史表不被锁定。

- [ ] **Step 5: 实现登录动作局部状态**

在 `LoginDialog.__init__` 初始化 `self._busy_action: str | None = None`，并增加：

```python
def set_action_busy(self, action: str, busy: bool) -> None:
    if busy:
        self._busy_action = action
    elif self._busy_action == action:
        self._busy_action = None
    labels = {
        "qr.refresh": (self.qr_refresh, "正在刷新…", "刷新二维码"),
        "login.phone": (self.phone_fallback, "正在切换…", "改用手机号登录"),
        "login.credentials": (
            self.credentials_edit,
            "正在打开…",
            "修改 API/代理设置",
        ),
    }
    for key, (button, running_text, idle_text) in labels.items():
        button.setText(running_text if self._busy_action == key else idle_text)
        button.setEnabled(self._busy_action is None)
```

`show_page` 不主动清除此状态；桥接的 `finished` 回调是唯一恢复入口，避免协程未结束时按钮提前可点。

- [ ] **Step 6: 运行 UI 测试和 Ruff**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_content_browser.py tests/ui/test_login_dialog.py -q`

Expected: all tests passed。

Run: `.venv\Scripts\ruff.exe check src/telegram_downloader/ui/content_browser.py src/telegram_downloader/ui/login.py tests/ui/test_content_browser.py tests/ui/test_login_dialog.py`

Expected: `All checks passed!`

- [ ] **Step 7: 提交局部反馈**

```powershell
git add src/telegram_downloader/ui/content_browser.py src/telegram_downloader/ui/login.py tests/ui/test_content_browser.py tests/ui/test_login_dialog.py
git commit -m "feat: show scoped async action feedback"
```

### Task 3: 用桥接替换全部零参数 qasync 槽并管理退出

**Files:**
- Modify: `src/telegram_downloader/app.py:97-115,266-280,290-296,335-337,363-427,442-450`
- Modify: `tests/test_app.py:38-105`

- [ ] **Step 1: 写出真实应用信号连接的失败回归**

在 `tests/test_app.py` 增加 `import asyncio`，并增加：

```python
def test_zero_argument_ui_signals_schedule_each_controller_action_once(
    tmp_path,
    monkeypatch,
) -> None:
    application, loop, controller = app.create_application(tmp_path)
    calls: list[str] = []

    async def record(name: str) -> None:
        calls.append(name)

    actions = {
        "activate_content_page": "content.activate",
        "refresh_content_dialogs": "dialogs.refresh",
        "retry_telegram_connection": "telegram.retry",
        "refresh_qr_login": "login.qr.refresh",
        "use_phone_fallback": "login.phone",
        "edit_credentials": "login.credentials",
        "cancel_login": "login.cancel",
    }
    for method_name, action_name in actions.items():
        monkeypatch.setattr(
            controller,
            method_name,
            lambda name=action_name: record(name),
        )

    try:
        controller.window.content_activated.emit()
        controller.window.content_page.refresh_requested.emit()
        controller.window.content_page.connection_retry_requested.emit()
        controller.login_dialog.qr_refresh_requested.emit()
        controller.login_dialog.phone_fallback_requested.emit()
        controller.login_dialog.credentials_edit_requested.emit()
        controller.login_dialog.login_cancelled.emit()
        loop.run_until_complete(controller._async_actions.wait_idle())

        assert calls == list(actions.values())
        assert controller._async_actions.active_keys == frozenset()
    finally:
        loop.run_until_complete(controller._async_actions.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()
```

旧实现会继续出现 `asyncSlot was not callable from Signal`，且 `calls` 为空，因此该测试能直接覆盖真实故障。

- [ ] **Step 2: 运行新回归并确认旧实现失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_app.py::test_zero_argument_ui_signals_schedule_each_controller_action_once -q`

Expected: FAIL，7 个控制器动作没有全部被调用，或控制台出现零参数 `asyncSlot` 签名错误。

- [ ] **Step 3: 在 create_application 中接入桥接**

在 `app.py` 导入 `ActionHooks`、`AsyncActionBridge`，控制器创建后实例化 `async_actions = AsyncActionBridge()` 并保存为 `controller._async_actions`。删除 7 个 `@qasync.asyncSlot()` 零参数函数及其 `_ui_slots` 引用，改用动态 lambda 工厂，确保测试替换控制器方法后仍能验证真实连接：

```python
def content_failure(error: Exception) -> None:
    window.content_page.show_error(controller._safe_error(error))

def login_hooks(action: str) -> ActionHooks:
    return ActionHooks(
        started=lambda: login_dialog.set_action_busy(action, True),
        failed=lambda error: login_dialog.show_error(controller._safe_error(error)),
        finished=lambda: login_dialog.set_action_busy(action, False),
    )

async_actions.connect(
    window.content_activated,
    "content.activate",
    lambda: controller.activate_content_page(),
    hooks=ActionHooks(failed=content_failure),
)
async_actions.connect(
    window.content_page.refresh_requested,
    "dialogs.refresh",
    lambda: controller.refresh_content_dialogs(),
    hooks=ActionHooks(failed=content_failure),
)
async_actions.connect(
    window.content_page.connection_retry_requested,
    "telegram.retry",
    lambda: controller.retry_telegram_connection(),
    hooks=ActionHooks(
        started=lambda: window.content_page.set_connection_action_busy(True),
        failed=content_failure,
        finished=lambda: window.content_page.set_connection_action_busy(False),
    ),
)
async_actions.connect(
    login_dialog.qr_refresh_requested,
    "login.qr.refresh",
    lambda: controller.refresh_qr_login(),
    hooks=login_hooks("qr.refresh"),
)
async_actions.connect(
    login_dialog.phone_fallback_requested,
    "login.phone",
    lambda: controller.use_phone_fallback(),
    hooks=login_hooks("login.phone"),
)
async_actions.connect(
    login_dialog.credentials_edit_requested,
    "login.credentials",
    lambda: controller.edit_credentials(),
    hooks=login_hooks("login.credentials"),
)
async_actions.connect(
    login_dialog.login_cancelled,
    "login.cancel",
    lambda: controller.cancel_login(),
    hooks=ActionHooks(
        failed=lambda error: login_dialog.show_error(controller._safe_error(error))
    ),
)
```

保留扫描、手机号、验证码、密码、任务恢复、失败重试、对话选择、搜索、加载更多、入队和预览的带参数 `@qasync.asyncSlot(...)`。

- [ ] **Step 4: 在应用退出时先关闭桥接**

在 `run` 的事件循环尾部改为：

```python
loop.run_forever()
loop.run_until_complete(controller._async_actions.shutdown())
loop.run_until_complete(controller.shutdown())
```

这样关闭期间不再启动新的 UI 动作，桥接任务先取消并等待，控制器随后按原顺序停止搜索、缩略图、下载调度和 Telegram 连接。

- [ ] **Step 5: 更新现有 create_application 断言**

把 `test_create_application_initializes_project_local_content_services` 中对零参数槽名称的断言改为：

```python
assert controller._async_actions.active_keys == frozenset()
assert len(controller._async_actions._slots) == 7
```

保留 `content_preview_requested` 等带参数槽的名称断言，并在 `finally` 中先执行 `loop.run_until_complete(controller._async_actions.shutdown())`。

- [ ] **Step 6: 运行应用连接测试与 Ruff**

Run: `.venv\Scripts\python.exe -m pytest tests/test_app.py tests/ui/test_async_actions.py -q`

Expected: all tests passed，控制台不出现 `asyncSlot was not callable from Signal`。

Run: `.venv\Scripts\ruff.exe check src/telegram_downloader/app.py tests/test_app.py`

Expected: `All checks passed!`

- [ ] **Step 7: 提交信号修复**

```powershell
git add src/telegram_downloader/app.py tests/test_app.py
git commit -m "fix: route zero argument Qt signals safely"
```

### Task 4: 让加入下载队列始终给出反馈并恢复

**Files:**
- Modify: `src/telegram_downloader/controller.py:86-132,879-909`
- Modify: `tests/test_controller.py:1114-1165,1756-1857`

- [ ] **Step 1: 扩展控制器假页面并写失败断言**

在 `_NullContentPage` 增加空实现：

```python
def set_queue_busy(self, _busy: bool) -> None:
    pass
```

在 `tests/test_controller.py` 的 `ContentPageFake.__init__` 增加 `self.queue_busy = []`，并增加：

```python
def set_queue_busy(self, busy):
    self.queue_busy.append(busy)
```

在 `test_content_search_selection_and_queue_flow` 的最后增加：

```python
assert window.content_page.queue_busy == [True, False]
```

再增加确认取消测试：

```python
@pytest.mark.asyncio
async def test_cancelled_queue_confirmation_restores_action_state() -> None:
    class ContentService:
        def prepare_download(self, search_id):
            return SimpleNamespace(preview="preview")

    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=ConnectedGateway(),
        content_browser=ContentService(),
        planner=object(),
        window=window,
        confirm_preview=lambda _preview: False,
    )

    await controller.queue_content_selection("search-1")

    assert window.content_page.queue_busy == [True, False]
    assert window.message == "已取消创建任务"
```

- [ ] **Step 2: 运行控制器测试并确认忙碌态没有恢复**

Run: `.venv\Scripts\python.exe -m pytest tests/test_controller.py::test_content_search_selection_and_queue_flow tests/test_controller.py::test_cancelled_queue_confirmation_restores_action_state -q`

Expected: FAIL，`queue_busy` 不是 `[True, False]`。

- [ ] **Step 3: 用 try/finally 包住完整入队流程**

把 `queue_content_selection` 调整为先取得页面、清除同类旧错误并进入忙碌态，所有现有业务分支放入 `try`，最后恢复：

```python
async def queue_content_selection(self, search_id: str) -> None:
    page = self._content_page()
    page.show_error("")
    page.set_queue_busy(True)
    try:
        if self.content_browser is None or self.planner is None:
            self._show_error("请先连接 Telegram 账号")
            return
        preparation = self.content_browser.prepare_download(search_id)
        if not self.confirm_preview(preparation.preview):
            self._show_status("已取消创建任务")
            return
        committed = self.planner.commit_selected(preparation.preview)
        joined_count = len(committed.accepted_keys)
        report = self.content_browser.finalize_queue(search_id, joined_count)
        self._reload_content_search(search_id)
        self.refresh_tasks()
        self._start_task(committed.task.id)
        self._show_status(
            f"选择 {report.selected_count} 项，加入 {report.joined_count} 项，"
            f"跳过重复 {report.duplicate_count} 项，"
            f"不可用 {report.unavailable_count} 项"
        )
    except NothingToQueueError as error:
        self._show_status(
            f"选择 {error.selected_count} 项，加入 0 项，"
            f"跳过重复 {error.duplicate_count} 项，"
            f"不可用 {error.unavailable_count} 项"
        )
    except Exception as error:
        page.show_error(self._safe_error(error))
    finally:
        page.set_queue_busy(False)
```

- [ ] **Step 4: 运行入队、搜索和内容页回归**

Run: `.venv\Scripts\python.exe -m pytest tests/test_controller.py tests/test_app.py tests/ui/test_content_browser.py -q`

Expected: all tests passed；入队成功、无可入队项、确认取消和异常都恢复按钮。

- [ ] **Step 5: 提交入队反馈**

```powershell
git add src/telegram_downloader/controller.py tests/test_controller.py
git commit -m "fix: restore queue action state on every outcome"
```

### Task 5: 形成 v0.4.2 本地候选版本

**Files:**
- Modify: `src/telegram_downloader/__init__.py:1`
- Modify: `pyproject.toml:7`
- Modify: `installer/TelegramDownloader.iss:1-3`
- Modify: `tests/test_packaging_contract.py:50-65`
- Modify: `tests/ui/test_main_window.py:21`
- Create: `docs/releases/v0.4.2.md`

- [ ] **Step 1: 写出 0.4.2 版本契约**

把 `tests/test_packaging_contract.py` 的版本测试重命名为 `test_v042_version_and_content_runtime_contract_are_consistent`，并把三处预期改为 `0.4.2`。把 `tests/ui/test_main_window.py` 的版本标签预期改为：

```python
assert window.version_label.text() == "v0.4.2 · stable"
```

- [ ] **Step 2: 运行版本契约并确认源版本仍为 0.4.1**

Run: `.venv\Scripts\python.exe -m pytest tests/test_packaging_contract.py tests/ui/test_main_window.py::test_main_window_contains_task_workspace -q`

Expected: FAIL，实际版本仍为 `0.4.1`。

- [ ] **Step 3: 同步版本号并写发行说明**

将 `src/telegram_downloader/__init__.py`、`pyproject.toml` 和安装器默认 `AppVersion` 同步为 `0.4.2`。新建 `docs/releases/v0.4.2.md`：

```markdown
# TelegramDownloader v0.4.2

## 异步操作可靠性与交互优化

- 修复账号内容页、群组刷新、Telegram 重连和登录二维码操作点击后可能无响应的问题。
- 同一刷新、重连或登录切换运行期间不再重复启动，关闭程序时会安全取消未完成的界面动作。
- 重连、二维码刷新、手机号切换、凭据编辑和加入下载队列现在会立即显示进行中状态，并在成功、失败或取消后恢复。
- 保留搜索进度、缓存群组、搜索历史、双击预览和选择性下载能力；所有业务数据仍保存在应用目录。
```

不要修改 GitHub、魔搭或在线更新 stable 指针。

- [ ] **Step 4: 运行版本与打包契约测试**

Run: `.venv\Scripts\python.exe -m pytest tests/test_packaging_contract.py tests/test_installer_contract.py tests/ui/test_main_window.py -q`

Expected: all tests passed。

- [ ] **Step 5: 提交候选版本元数据**

```powershell
git add src/telegram_downloader/__init__.py pyproject.toml installer/TelegramDownloader.iss tests/test_packaging_contract.py tests/ui/test_main_window.py docs/releases/v0.4.2.md
git commit -m "chore: prepare v0.4.2 candidate"
```

### Task 6: 全量验证、真人路径复验与本地合并

**Files:**
- Verify only: repository tests, `dist/TelegramDownloader`, portable ZIP, installer EXE, project-local runtime data and logs

- [ ] **Step 1: 运行完整测试与静态检查**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1`

Expected: pytest 全部通过且 Ruff 输出 `All checks passed!`；输出目录只位于当前工作树的 `.build-temp`、`.tool-cache` 和 `.venv`。

- [ ] **Step 2: 构建并验证便携包**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1`

Expected: 生成 `dist/TelegramDownloader/TelegramDownloader.exe` 与 `dist/TelegramDownloader-0.4.2-win-x64-portable.zip`，内置 `--self-test` 通过，报告中所有 writable paths 都在该便携目录下。

- [ ] **Step 3: 构建并验证安装包**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-installer.ps1`

Expected: 生成 `dist/release/TelegramDownloader-0.4.2-win-x64-setup.exe`；脚本验证拒绝 C 盘、项目内测试目录安装、自检、覆盖升级、普通卸载保留 `data`/`downloads`。

- [ ] **Step 4: 用项目内现有会话完成真人路径**

从 `dist/TelegramDownloader/TelegramDownloader.exe` 正常启动，不读取或输出 `secrets.dat` 内容，按顺序执行：

1. 确认有效会话自动恢复，不要求重复扫码。
2. 进入“账号内容”，点击刷新并确认按钮立即显示“刷新中…”，缓存群组仍可点击。
3. 选择群组，输入关键词，确认搜索进度、结果缩略图和双击预览。
4. 勾选结果，点击入队，确认立即显示“正在准备已选 N 项…”，取消一次并确认按钮恢复，再确认一次并观察任务创建。
5. 点击重新连接、二维码刷新、手机号切换和修改凭据，确认每个动作只启动一次且结束恢复；不提交或展示敏感值。
6. 观察下载任务暂停、继续、失败重试、打开目录和重启恢复。
7. 检查本次启动新增日志，不应出现 `asyncSlot was not callable from Signal`、未消费 Task 异常或敏感值；新增业务文件必须位于应用目录。

若 Windows 图形捕获仍被系统权限阻止，使用 Qt/qasync 事件循环对同一控件执行真实 `click()`/信号发射、状态断言和逐页截图；不绕过系统权限，也不跳过上述业务路径。

- [ ] **Step 5: 检查提交范围和工作树洁净度**

Run: `git status --short`

Expected: 无输出。

Run: `git diff main...HEAD --check`

Expected: 无输出。

Run: `git log --oneline main..HEAD`

Expected: 只包含本设计、实施计划和本轮修复/候选版本提交，不包含凭据、运行数据或构建产物。

- [ ] **Step 6: 快进合并到本地 main**

在 `D:\Codex Project\Telegram下载器` 运行：

```powershell
git merge --ff-only codex/fix-content-queue
```

Expected: 本地 `main` 快进到已验证候选提交；不执行 push、不创建标签、不修改 GitHub/魔搭版本指针。

- [ ] **Step 7: 合并后做最小可信复核**

由于主目录存在已知不可读历史文档，不在主目录重跑会遍历该文件的完整套件。运行：

```powershell
git rev-parse main
git rev-parse codex/fix-content-queue
git status --short
```

Expected: 两个提交哈希相同且状态无新增修改。保留 `.worktrees/content-ux` 及其中项目本地运行数据，除非用户另行明确授权清理。

## 完成标准

- 7 个零参数异步信号都通过 `AsyncActionBridge` 执行且同键去重。
- 所有桥接任务在异常、取消、应用关闭时被消费和清理。
- 刷新、重连、登录切换和入队都有即时局部反馈，结束后可再次操作。
- 搜索、预览、选择、入队、下载、恢复和在线更新既有能力没有回归。
- v0.4.2 便携包与安装包通过项目内冒烟测试，应用数据路径仍不离开程序目录。
- 修复仅本地快进合并；没有外部发布副作用。
