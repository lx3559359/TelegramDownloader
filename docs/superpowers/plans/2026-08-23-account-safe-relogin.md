# Account-Safe Relogin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the account navigation read-only for authorized users and move explicit relogin into a rollback-safe candidate session that cannot disturb the current account on cancel or failure.

**Architecture:** Add a focused account-access model and a separate account-status dialog. User-triggered authentication runs against a candidate gateway; online services are built without side effects and are swapped only after the candidate is authorized, confirmed, and healthy. The existing active gateway and encrypted session remain the rollback source until the commit point.

**Tech Stack:** Python 3.12, asyncio, PySide6, qasync, Telethon gateway abstraction, pytest, pytest-qt

---

## File map

- Create `src/telegram_downloader/account_access.py`: account status, online service bundle, candidate session, and commit result types.
- Create `src/telegram_downloader/ui/account_status.py`: read-only account status dialog and user-intent signals.
- Modify `src/telegram_downloader/controller.py`: safe account entry, candidate auth routing, switch guard, commit/rollback, and shutdown cleanup.
- Modify `src/telegram_downloader/app.py`: side-effect-free service builder/binder, confirmations, dialog construction, and async signal wiring.
- Modify `src/telegram_downloader/ui/main.py`: keep the navigation signal but route it through async account access.
- Modify `src/telegram_downloader/ui/login.py`: reset authentication fields between active and candidate attempts without adding account-status responsibilities.
- Create `tests/test_account_access.py`: focused model and candidate cleanup tests.
- Create `tests/ui/test_account_status.py`: dialog rendering and signal tests.
- Modify `tests/test_controller.py`: safe entry, candidate lifecycle, account comparison, active-download guard, and rollback tests.
- Modify `tests/test_app.py`: production wiring and no-QR-on-navigation integration tests.

### Task 1: Account-access types

**Files:**
- Create: `src/telegram_downloader/account_access.py`
- Create: `tests/test_account_access.py`

- [ ] **Step 1: Write failing value-object tests**

```python
from telegram_downloader.account_access import (
    AccountStatusSnapshot,
    AuthorizationState,
    ConnectionState,
    OnlineServices,
)


def test_account_status_snapshot_exposes_safe_actions() -> None:
    status = AccountStatusSnapshot(
        account_id="42",
        display_name="账号",
        authorization=AuthorizationState.AUTHORIZED,
        connection=ConnectionState.ONLINE,
        session_encrypted=True,
        content_available=True,
        subscriptions_available=True,
        active_download_count=0,
    )

    assert status.can_reconnect is False
    assert status.can_reauthenticate is True


def test_online_services_keeps_gateway_and_scheduler_together() -> None:
    services = OnlineServices("gateway", "planner", "scheduler")
    assert services.gateway == "gateway"
    assert services.planner == "planner"
    assert services.scheduler == "scheduler"
```

- [ ] **Step 2: Run the tests and verify the import fails**

Run: `pytest tests/test_account_access.py -q`

Expected: FAIL with `ModuleNotFoundError: telegram_downloader.account_access`.

- [ ] **Step 3: Add the focused types**

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from telegram_downloader.content import AccountProfile
from telegram_downloader.gateway import TelegramGateway


class AuthorizationState(StrEnum):
    MISSING = "missing"
    AUTHORIZED = "authorized"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class ConnectionState(StrEnum):
    OFFLINE = "offline"
    ONLINE = "online"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class AccountStatusSnapshot:
    account_id: str | None
    display_name: str
    authorization: AuthorizationState
    connection: ConnectionState
    session_encrypted: bool
    content_available: bool
    subscriptions_available: bool
    active_download_count: int

    @property
    def can_reconnect(self) -> bool:
        return self.authorization is AuthorizationState.AUTHORIZED and self.connection is not ConnectionState.ONLINE

    @property
    def can_reauthenticate(self) -> bool:
        return self.authorization is not AuthorizationState.MISSING


@dataclass(frozen=True, slots=True)
class OnlineServices:
    gateway: Any
    planner: Any
    scheduler: Any


@dataclass(slots=True)
class CandidateLoginSession:
    gateway: TelegramGateway
    phone: str = ""
    phone_code_hash: str = ""
    profile: AccountProfile | None = None
    qr_wait_task: asyncio.Task[None] | None = None

    async def close(self) -> None:
        task = self.qr_wait_task
        self.qr_wait_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.gateway.disconnect()
```

- [ ] **Step 4: Add cleanup tests and make them pass**

Add a fake gateway whose `disconnect()` records one call, attach a pending task, call `CandidateLoginSession.close()`, and assert the task is cancelled and disconnect is called once.

Run: `pytest tests/test_account_access.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/telegram_downloader/account_access.py tests/test_account_access.py
git commit -m "feat: add account access session types"
```

### Task 2: Read-only account status dialog

**Files:**
- Create: `src/telegram_downloader/ui/account_status.py`
- Create: `tests/ui/test_account_status.py`
- Modify: `src/telegram_downloader/ui/theme.py`

- [ ] **Step 1: Write failing UI tests**

```python
from PySide6.QtCore import Qt

from telegram_downloader.account_access import (
    AccountStatusSnapshot,
    AuthorizationState,
    ConnectionState,
)
from telegram_downloader.ui.account_status import AccountStatusDialog


def snapshot(*, active: int = 0) -> AccountStatusSnapshot:
    return AccountStatusSnapshot(
        "42", "测试账号", AuthorizationState.AUTHORIZED,
        ConnectionState.ONLINE, True, True, True, active,
    )


def test_dialog_shows_account_without_starting_auth(qtbot) -> None:
    dialog = AccountStatusDialog()
    qtbot.addWidget(dialog)
    dialog.set_snapshot(snapshot())

    assert "测试账号" in dialog.account_name.text()
    assert "连接正常" in dialog.connection_label.text()
    assert dialog.reauthenticate_button.isEnabled()


def test_dialog_emits_only_explicit_reauthenticate_intent(qtbot) -> None:
    dialog = AccountStatusDialog()
    qtbot.addWidget(dialog)
    dialog.set_snapshot(snapshot())

    with qtbot.waitSignal(dialog.reauthenticate_requested, timeout=500):
        qtbot.mouseClick(dialog.reauthenticate_button, Qt.MouseButton.LeftButton)
```

- [ ] **Step 2: Run the UI tests and verify the module is missing**

Run: `pytest tests/ui/test_account_status.py -q`

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the dialog**

Create `AccountStatusDialog(QDialog)` with `reconnect_requested` and `reauthenticate_requested` signals, account name, authorization, connection, encrypted-session, content/subscription, active-download labels, an error label, and three buttons. `set_snapshot()` must only render the passed immutable snapshot. Add `show_error(text)` for controller failures. Disable reauthentication when `active_download_count > 0` and show `请先暂停或等待活动下载完成`.

The dialog must not import `TelethonGateway`, create tasks, or expose QR controls.

- [ ] **Step 4: Add degraded and expired state tests**

Assert that degraded authorized state enables reconnect and that expired state hides reconnect but enables explicit reauthentication. Assert that `set_snapshot()` emits no signals.

Run: `pytest tests/ui/test_account_status.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/telegram_downloader/ui/account_status.py src/telegram_downloader/ui/theme.py tests/ui/test_account_status.py
git commit -m "feat: add read-only account status dialog"
```

### Task 3: Safe account navigation

**Files:**
- Modify: `src/telegram_downloader/controller.py`
- Modify: `tests/test_controller.py`

- [ ] **Step 1: Write the no-side-effect controller test**

```python
@pytest.mark.asyncio
async def test_show_account_access_for_authorized_account_never_starts_qr() -> None:
    calls: list[str] = []

    class Gateway:
        def is_connected(self) -> bool:
            return True

        async def account_profile(self):
            return AccountProfile("42", "测试账号")

        async def begin_qr_login(self):
            calls.append("qr")
            raise AssertionError("navigation must not begin QR login")

    class StatusDialog:
        def set_snapshot(self, value):
            self.snapshot = value

        def show(self):
            calls.append("show")

        def raise_(self):
            pass

        def activateWindow(self):
            pass

    controller = AppController.for_test(
        gateway=Gateway(),
        account_status_dialog=StatusDialog(),
        secrets={"api_hash": "saved", "session": "encrypted-session"},
        settings=AppSettings(api_id=1),
    )

    await controller.show_account_access()

    assert calls == ["show"]
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `pytest tests/test_controller.py::test_show_account_access_for_authorized_account_never_starts_qr -q`

Expected: FAIL because `account_status_dialog` and `show_account_access()` do not exist.

- [ ] **Step 3: Add controller dependencies and snapshot construction**

Add `account_status_dialog` to `AppController.__init__()` and `for_test()`. Implement async `show_account_access()` with these branches:

```python
async def show_account_access(self) -> None:
    if self.gateway is None or self.settings.api_id <= 0 or not self.secrets.get("api_hash"):
        self.show_login_credentials()
        return
    try:
        profile = await self.gateway.account_profile()
        snapshot = self._account_status_snapshot(
            profile,
            AuthorizationState.AUTHORIZED,
            ConnectionState.ONLINE if self._gateway_is_connected(self.gateway) else ConnectionState.DEGRADED,
        )
    except SessionExpiredError:
        snapshot = self._account_status_snapshot(
            None, AuthorizationState.EXPIRED, ConnectionState.OFFLINE
        )
    except Exception:
        snapshot = self._account_status_snapshot(
            None, AuthorizationState.UNKNOWN, ConnectionState.DEGRADED
        )
    self.account_status_dialog.set_snapshot(snapshot)
    self.account_status_dialog.show()
    self.account_status_dialog.raise_()
    self.account_status_dialog.activateWindow()
```

Use `self.scheduler.snapshot().active_count` for the active-download count. Use the current window account label as the degraded display fallback without logging it.

- [ ] **Step 4: Separate credentials-only login from QR start**

Replace the overloaded `show_login()` entry with explicit methods:

```python
def show_login_credentials(self) -> None:
    self._prefill_login()
    self.login_dialog.show_page(LoginPage.CREDENTIALS)
    self._show_login_dialog()

def _show_login_dialog(self) -> None:
    self.login_dialog.show()
    self.login_dialog.raise_()
    self.login_dialog.activateWindow()
```

Only authentication recovery and confirmed candidate creation may call `begin_qr_login()`.

- [ ] **Step 5: Add degraded, expired, and no-credentials tests**

Run: `pytest tests/test_controller.py -k "account_access or show_login_without_credentials" -q`

Expected: PASS and zero `begin_qr_login()` calls in every manual-navigation branch.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_downloader/controller.py tests/test_controller.py
git commit -m "fix: make account navigation side effect free"
```

### Task 4: Candidate authentication routing

**Files:**
- Modify: `src/telegram_downloader/controller.py`
- Modify: `src/telegram_downloader/ui/login.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/ui/test_login_dialog.py`

- [ ] **Step 1: Write failing candidate lifecycle tests**

Cover all of these assertions:

```python
assert controller.gateway is active_gateway
assert controller.secrets["session"] == "old-session"
assert candidate_gateway.begin_qr_calls == 1
assert active_gateway.begin_qr_calls == 0
```

After `await controller.cancel_login()`, assert the candidate disconnects and the active gateway remains connected. Add a second test proving repeated reauthentication focuses the current login dialog and creates only one candidate.

- [ ] **Step 2: Run the candidate tests and verify failure**

Run: `pytest tests/test_controller.py -k "candidate_login" -q`

Expected: FAIL because authentication still targets `self.gateway`.

- [ ] **Step 3: Add explicit candidate start and target helpers**

Implement:

```python
async def start_candidate_login(self) -> None:
    if self._candidate_login is not None:
        self._show_login_dialog()
        return
    if self.scheduler.snapshot().active_count:
        self.account_status_dialog.show_error("请先暂停或等待活动下载完成")
        return
    if not await self.confirm_reauthentication():
        return
    candidate = self.gateway_factory(
        self.settings.api_id,
        self.secrets["api_hash"],
        "",
        self.settings.proxy,
        self.secrets.get("proxy_password", ""),
    )
    await candidate.connect()
    self._candidate_login = CandidateLoginSession(candidate)
    self.login_dialog.reset_authentication()
    self._show_login_dialog()
    await self.begin_qr_login()

def _login_gateway(self):
    return self._candidate_login.gateway if self._candidate_login else self.gateway
```

Route QR refresh, phone request, code submission, password submission, and QR wait through `_login_gateway()`. Store phone and code hash on `CandidateLoginSession` while it exists; keep legacy controller fields only for the no-current-session recovery path.

- [ ] **Step 4: Make cancel candidate-only**

`cancel_login()` must cancel the candidate QR wait, disconnect and clear only `_candidate_login`, reset the dialog, and leave `self.gateway`, `self.scheduler`, `self.planner`, `self.secrets`, content services, and subscription services unchanged.

- [ ] **Step 5: Run candidate and login UI tests**

Run: `pytest tests/test_controller.py -k "candidate_login or qr_login or phone" -q`

Run: `pytest tests/ui/test_login_dialog.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_downloader/controller.py src/telegram_downloader/ui/login.py tests/test_controller.py tests/ui/test_login_dialog.py
git commit -m "feat: isolate candidate Telegram authentication"
```

### Task 5: Side-effect-free online service bundles

**Files:**
- Modify: `src/telegram_downloader/app.py`
- Modify: `src/telegram_downloader/controller.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_controller.py`

- [ ] **Step 1: Write a failing service-builder contract test**

In `tests/test_app.py`, construct the application with fake shared content and subscription services. Call the service builder and assert it returns `OnlineServices` without calling either shared service's `bind_online()`. Call the binder and then assert both are bound exactly once.

- [ ] **Step 2: Run the contract test and verify current side effects**

Run: `pytest tests/test_app.py -k "online_service_bundle" -q`

Expected: FAIL because current `build_services()` binds shared services immediately and returns a tuple.

- [ ] **Step 3: Split builder and binder in production assembly**

Refactor `app.py` to this shape:

```python
def build_online_services(gateway: TelethonGateway, resource_settings: AppSettings) -> OnlineServices:
    planner = TaskPlanner(...)
    scheduler = DownloadScheduler(...)
    scheduler.set_admission_open(schedule_state.allowed)
    return OnlineServices(gateway, planner, scheduler)

def bind_online_services(services: OnlineServices) -> None:
    content_browser.bind_online(services.gateway, services.planner)
    subscriptions.bind_online(services.gateway, services.planner)

def unbind_online_services() -> None:
    content_browser.go_offline()
    subscriptions.go_offline()
```

Pass all three callables into `AppController`. At startup, build, bind, and pass the resulting members as the current controller services.

- [ ] **Step 4: Update controller test constructors**

Update `for_test()` defaults so existing tests receive no-op binder/unbinder callables. Replace tuple-length service handling in `submit_credentials()` with `OnlineServices` member access.

- [ ] **Step 5: Run application and controller suites**

Run: `pytest tests/test_app.py tests/test_controller.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_downloader/app.py src/telegram_downloader/controller.py tests/test_app.py tests/test_controller.py
git commit -m "refactor: separate online service build and bind"
```

### Task 6: Atomic candidate commit and rollback

**Files:**
- Modify: `src/telegram_downloader/controller.py`
- Modify: `tests/test_controller.py`

- [ ] **Step 1: Write failing same-account and different-account tests**

Add tests proving:

- Same account ID commits without `confirm_account_switch`.
- Different account ID calls `confirm_account_switch(old_profile, candidate_profile)` exactly once.
- Declining the second confirmation leaves the old gateway, service bundle, vault payload, and window account unchanged.
- Active downloads detected again immediately before commit abort the switch.

- [ ] **Step 2: Write parameterized rollback tests**

Parameterize failures at `build_online_services`, `bind_online_services`, `vault.save`, and `activate_content_account`. For every pre-commit failure assert the old binder is restored, the old encrypted session remains, candidate services shut down, candidate gateway disconnects, and scheduler admission is reopened.

- [ ] **Step 3: Run and verify failures**

Run: `pytest tests/test_controller.py -k "candidate_commit or candidate_rollback" -q`

Expected: FAIL because commit orchestration does not exist.

- [ ] **Step 4: Implement commit orchestration**

Implement `_finish_candidate_login()` and `_commit_candidate_services()` using this explicit boundary:

```python
candidate.profile = await candidate.gateway.account_profile()
if old_profile and candidate.profile.account_id != old_profile.account_id:
    if not await self.confirm_account_switch(old_profile, candidate.profile):
        await self._discard_candidate_login()
        return
if self.scheduler.snapshot().active_count:
    raise GatewayError("检测到活动下载，请暂停或等待完成后重试")

candidate_services = self.build_online_services(candidate.gateway, self.settings)
old_services = OnlineServices(self.gateway, self.planner, self.scheduler)
old_secrets = dict(self.secrets)
committed = False
try:
    self.scheduler.set_admission_open(False)
    await self._cancel_subscription_probe()
    await self._cancel_content_operations()
    self.bind_online_services(candidate_services)
    new_secrets = {**old_secrets, "session": candidate.gateway.export_session()}
    self.vault.save(new_secrets)
    self.gateway = candidate_services.gateway
    self.planner = candidate_services.planner
    self.scheduler = candidate_services.scheduler
    self.secrets = new_secrets
    await self.activate_content_account()
    committed = True
finally:
    if not committed:
        self.bind_online_services(old_services)
        self.gateway, self.planner, self.scheduler = (
            old_services.gateway, old_services.planner, old_services.scheduler
        )
        self.secrets = old_secrets
        self.vault.save(old_secrets)
        self.scheduler.set_admission_open(True)
```

After `committed = True`, clear the candidate reference, reopen candidate scheduler admission according to the current schedule, close the old scheduler/gateway with idempotent warning-only cleanup, update the account status, and ensure the connection monitor is running. A post-commit old cleanup failure must not restore old credentials.

- [ ] **Step 5: Run rollback and broader controller tests**

Run: `pytest tests/test_controller.py -k "candidate or account_access or session_expired" -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_downloader/controller.py tests/test_controller.py
git commit -m "feat: commit candidate accounts with rollback"
```

### Task 7: Production UI wiring and confirmations

**Files:**
- Modify: `src/telegram_downloader/app.py`
- Modify: `src/telegram_downloader/ui/main.py`
- Modify: `tests/test_app.py`
- Modify: `tests/ui/test_main_window.py`

- [ ] **Step 1: Write failing wiring tests**

Assert that emitting `window.login_requested` while a fake authorized gateway is installed opens `controller.account_status_dialog`, and that `begin_qr_login` remains uncalled. Assert that status-dialog reauthenticate and reconnect signals are connected through `AsyncActionBridge` with stable deduplication keys.

- [ ] **Step 2: Implement confirmation callbacks**

In `app.py`, add async `QMessageBox` callbacks:

- `confirm_reauthentication()`: explains that the old account remains until candidate success and blocks while active downloads exist.
- `confirm_account_switch(old, candidate)`: names both display names and defaults to Cancel.

Do not include account IDs, phone numbers, session values, or QR URLs in message text.

- [ ] **Step 3: Wire async actions**

Instantiate `AccountStatusDialog(window)`, pass it and both confirmation callbacks to the controller, and connect:

```python
async_actions.connect(
    window.login_requested,
    "account.access.open",
    controller.show_account_access,
)
async_actions.connect(
    account_status_dialog.reauthenticate_requested,
    "account.reauthenticate",
    controller.start_candidate_login,
)
async_actions.connect(
    account_status_dialog.reconnect_requested,
    "account.reconnect",
    controller.retry_telegram_connection,
)
```

Replace direct `window.login_requested.connect(controller.show_login)`.

- [ ] **Step 4: Cover shutdown and expiry**

Add candidate cleanup to `AppController.shutdown()`. Keep explicit session-expiry recovery behavior, but route notification clicks through `show_account_access()` so a manual notification click also cannot create a QR without explicit confirmation.

- [ ] **Step 5: Run app and UI integration tests**

Run: `pytest tests/test_app.py tests/ui/test_main_window.py tests/ui/test_account_status.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_downloader/app.py src/telegram_downloader/ui/main.py tests/test_app.py tests/ui/test_main_window.py
git commit -m "feat: wire safe account access flow"
```

### Task 8: Account-safety verification

**Files:**
- Modify: `docs/verification/2026-08-23-account-safe-relogin.md`

- [ ] **Step 1: Run focused tests**

Run: `pytest tests/test_account_access.py tests/ui/test_account_status.py tests/ui/test_login_dialog.py tests/test_controller.py tests/test_app.py -q`

Expected: PASS.

- [ ] **Step 2: Run static checks**

Run: `ruff check src/telegram_downloader/account_access.py src/telegram_downloader/ui/account_status.py src/telegram_downloader/controller.py src/telegram_downloader/app.py tests/test_account_access.py tests/ui/test_account_status.py tests/test_controller.py tests/test_app.py`

Expected: `All checks passed!`

- [ ] **Step 3: Record non-secret verification evidence**

Document exact test commands and results. For real-session checking, use an isolated encrypted session copy and record only whether QR creation was requested; never record credentials, account identifiers, QR URLs, group names, or message content.

- [ ] **Step 4: Commit**

```bash
git add docs/verification/2026-08-23-account-safe-relogin.md
git commit -m "test: verify account-safe relogin"
```
