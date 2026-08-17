# Telegram Auth Expiry Unified Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route background subscription authorization failures into one idempotent re-login flow, verify authorization before reporting a healthy connection, and preserve only safe authorization reason codes in logs and diagnostics.

**Architecture:** `TelethonGateway` owns translation from Telethon exception classes to a fixed `AuthorizationFailureReason`. `SubscriptionScheduler` persists the blocked rule and then awaits an injected callback; `AppController` owns the callback, coalesces concurrent expiry reports, clears the invalid session, and opens the existing login flow. Connection health always verifies both transport and authorization, while diagnostics serialize only a whitelisted safe reason code.

**Tech Stack:** Python 3.12, asyncio, Telethon, PySide6/qasync, pytest/pytest-asyncio, Ruff, SQLite-backed subscription state.

---

## File map

- `src/telegram_downloader/gateway.py`: define safe authorization reasons, attach them to `SessionExpiredError`, map Telethon failures, and make connection tests prove authorization.
- `src/telegram_downloader/diagnostic_probes.py`: expose the safe authorization reason in Telegram diagnostic metrics.
- `src/telegram_downloader/diagnostic_store.py`: whitelist and validate the new diagnostic metric.
- `src/telegram_downloader/subscription_scheduler.py`: invoke the injected asynchronous session-expiry callback after persisting `auth_required`.
- `src/telegram_downloader/controller.py`: verify authorization before showing “连接正常” and coalesce all expiry reports into one global re-login operation.
- `src/telegram_downloader/app.py`: wire scheduler failures to the controller and expose the retained safe reason to graphical diagnostics.
- `tests/test_gateway.py`: gateway reason mapping and unauthenticated connection checks.
- `tests/test_diagnostic_probes.py`: diagnostic reason emission without raw exception text.
- `tests/test_diagnostic_store.py`: diagnostic reason whitelist acceptance and rejection.
- `tests/test_subscription_scheduler.py`: persistence-before-callback ordering and callback failure isolation.
- `tests/test_controller.py`: connected-but-unauthorized behavior, transient failures, idempotent recovery, logging, and reset after login.
- `tests/test_app.py`: production callback and diagnostic wiring.

### Task 1: Add typed, safe authorization failure reasons at the gateway boundary

**Files:**
- Modify: `src/telegram_downloader/gateway.py:17-82,216-303,502-511,838-844`
- Test: `tests/test_gateway.py:1-24,559-590`

- [ ] **Step 1: Write failing gateway tests**

Add `AuthorizationFailureReason` to the gateway imports and replace the single unregistered-key assertion with complete safe mapping coverage:

```python
class AuthKeyDuplicatedError(Exception):
    pass


class AuthKeyInvalidError(Exception):
    pass


class AuthKeyUnregisteredError(Exception):
    pass


class SessionRevokedError(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "expected_reason"),
    [
        (AuthKeyDuplicatedError, AuthorizationFailureReason.AUTH_KEY_DUPLICATED),
        (AuthKeyInvalidError, AuthorizationFailureReason.AUTH_KEY_INVALID),
        (AuthKeyUnregisteredError, AuthorizationFailureReason.AUTH_KEY_UNREGISTERED),
        (SessionRevokedError, AuthorizationFailureReason.SESSION_REVOKED),
    ],
)
async def test_account_profile_maps_authorization_errors_to_safe_reason(
    error_type,
    expected_reason,
) -> None:
    class Client:
        async def get_me(self):
            raise error_type("secret server detail")

    gateway = TelethonGateway.from_client_for_test(
        Client(),
        authorization_errors=(error_type,),
        authorization_error_reasons={error_type: expected_reason},
    )

    with pytest.raises(SessionExpiredError) as caught:
        await gateway.account_profile()

    assert str(caught.value) == "Telegram 登录已失效，请重新扫码登录"
    assert caught.value.reason is expected_reason
    assert "secret server detail" not in str(caught.value)


@pytest.mark.asyncio
async def test_connection_check_rejects_connected_but_unauthorized_client() -> None:
    class Client:
        async def connect(self):
            pass

        async def get_me(self):
            return None

    gateway = TelethonGateway.from_client_for_test(Client())

    with pytest.raises(SessionExpiredError) as caught:
        await gateway.test_connection()

    assert caught.value.reason is AuthorizationFailureReason.NOT_AUTHORIZED
```

- [ ] **Step 2: Run the gateway tests and verify RED**

Run:

```powershell
& '.venv/Scripts/python.exe' -m pytest -q tests/test_gateway.py -k 'authorization_errors_to_safe_reason or connected_but_unauthorized'
```

Expected: collection or assertion failure because `AuthorizationFailureReason`, the exception `reason`, and test factory mapping do not exist.

- [ ] **Step 3: Implement the safe gateway contract**

Add the enum and backward-compatible exception constructor:

```python
class AuthorizationFailureReason(StrEnum):
    AUTH_KEY_DUPLICATED = "auth-key-duplicated"
    AUTH_KEY_INVALID = "auth-key-invalid"
    AUTH_KEY_UNREGISTERED = "auth-key-unregistered"
    SESSION_REVOKED = "session-revoked"
    NOT_AUTHORIZED = "not-authorized"
    UNKNOWN = "unknown"


class SessionExpiredError(GatewayError):
    def __init__(
        self,
        message: str = "Telegram 登录已失效，请重新扫码登录",
        *,
        reason: AuthorizationFailureReason = AuthorizationFailureReason.UNKNOWN,
    ) -> None:
        super().__init__(message)
        self.reason = reason
```

In `TelethonGateway.__init__`, retain both the existing tuple and an exact type-to-reason map:

```python
self._authorization_error_reasons = {
    errors.AuthKeyDuplicatedError: AuthorizationFailureReason.AUTH_KEY_DUPLICATED,
    errors.AuthKeyInvalidError: AuthorizationFailureReason.AUTH_KEY_INVALID,
    errors.AuthKeyUnregisteredError: AuthorizationFailureReason.AUTH_KEY_UNREGISTERED,
    errors.SessionRevokedError: AuthorizationFailureReason.SESSION_REVOKED,
}
self._authorization_errors = tuple(self._authorization_error_reasons)
```

Extend `from_client_for_test` with an optional mapping and default unknown reasons for existing tests:

```python
authorization_error_reasons: Mapping[
    type[BaseException], AuthorizationFailureReason
] | None = None,
```

```python
gateway._authorization_errors = authorization_errors
gateway._authorization_error_reasons = dict(
    authorization_error_reasons
    or {error_type: AuthorizationFailureReason.UNKNOWN for error_type in authorization_errors}
)
```

Update `_raise_mapped` to discard raw Telegram text while retaining the safe reason:

```python
for error_type, reason in self._authorization_error_reasons.items():
    if isinstance(error, error_type):
        raise SessionExpiredError(reason=reason) from error
if isinstance(error, self._authorization_errors):
    raise SessionExpiredError() from error
```

Update `test_connection()` so a connected transport without an authenticated account is an authorization failure:

```python
async def test_connection(self) -> None:
    try:
        await self._client.connect()
        if await self._client.get_me() is None:
            raise SessionExpiredError(
                reason=AuthorizationFailureReason.NOT_AUTHORIZED
            )
    except GatewayError:
        raise
    except Exception as exc:
        self._raise_mapped(exc)
```

Import `Mapping` from `collections.abc`.

- [ ] **Step 4: Run gateway tests and verify GREEN**

Run:

```powershell
& '.venv/Scripts/python.exe' -m pytest -q tests/test_gateway.py
```

Expected: all gateway tests pass.

- [ ] **Step 5: Commit the gateway unit**

```powershell
git add -- src/telegram_downloader/gateway.py tests/test_gateway.py
git commit -m "fix: classify Telegram authorization failures safely"
```

### Task 2: Add the safe authorization reason to diagnostics

**Files:**
- Modify: `src/telegram_downloader/diagnostic_probes.py:424-465`
- Modify: `src/telegram_downloader/diagnostic_store.py:31-123,543-570`
- Test: `tests/test_diagnostic_probes.py:385-428`
- Test: `tests/test_diagnostic_store.py`

- [ ] **Step 1: Write failing diagnostic probe tests**

Update the expired connection case to carry a fixed reason and assert that only the safe metric appears:

```python
expired = await probe_telegram(
    Gateway(
        SessionExpiredError(
            "private-session",
            reason=AuthorizationFailureReason.SESSION_REVOKED,
        )
    )
)

assert expired.metrics == {"authorizationReason": "session-revoked"}
assert "private-session" not in repr(dict(expired.metrics))
assert "private-session" not in expired.summary
```

Also add a retained-reason case for diagnostics run after the controller has already detached the invalid gateway:

```python
retained = await probe_telegram(
    None,
    authorization_reason=AuthorizationFailureReason.AUTH_KEY_DUPLICATED,
)

assert (retained.status, retained.code) == (
    DiagnosticStatus.FAILED,
    "telegram-session-expired",
)
assert retained.metrics == {"authorizationReason": "auth-key-duplicated"}
```

- [ ] **Step 2: Write failing diagnostic store privacy tests**

Add a helper result and verify the fixed value is accepted while arbitrary text is rejected:

```python
def telegram_expired_result(reason: str) -> DiagnosticResult:
    return DiagnosticResult(
        "telegram",
        "Telegram 连接",
        DiagnosticStatus.FAILED,
        "telegram-session-expired",
        "Telegram 登录会话已失效",
        3,
        {"authorizationReason": reason},
    )


def test_report_accepts_only_whitelisted_authorization_reasons(tmp_path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    store = DiagnosticReportStore(paths, secrets=set())
    safe = DiagnosticReport.build(
        "0.11.0",
        NOW,
        NOW + timedelta(seconds=1),
        (telegram_expired_result("auth-key-duplicated"),),
    )

    assert store.save(safe).is_file()

    unsafe = DiagnosticReport.build(
        "0.11.0",
        NOW,
        NOW + timedelta(seconds=1),
        (telegram_expired_result("raw secret server text"),),
    )
    with pytest.raises(DiagnosticPrivacyError):
        store.serialize(unsafe)
```

- [ ] **Step 3: Run diagnostic tests and verify RED**

Run:

```powershell
& '.venv/Scripts/python.exe' -m pytest -q tests/test_diagnostic_probes.py tests/test_diagnostic_store.py -k 'telegram or authorization'
```

Expected: failures because `probe_telegram` has no retained reason parameter and the store rejects `authorizationReason` unconditionally.

- [ ] **Step 4: Implement diagnostic emission and strict validation**

Extend `probe_telegram` without changing its user-visible text:

```python
async def probe_telegram(
    gateway: ConnectionProbe | None,
    *,
    authorization_reason: AuthorizationFailureReason | None = None,
) -> DiagnosticResult:
    if authorization_reason is not None:
        return _telegram_authorization_failure(authorization_reason)
    if gateway is None:
        return _result(
            "telegram",
            "Telegram 连接",
            DiagnosticStatus.SKIPPED,
            "telegram-not-configured",
            "尚未建立可检查的 Telegram 会话",
        )
    try:
        await gateway.test_connection()
    except asyncio.CancelledError:
        raise
    except SessionExpiredError as error:
        return _telegram_authorization_failure(error.reason)
    except TransientNetworkError:
        return _result(
            "telegram",
            "Telegram 连接",
            DiagnosticStatus.WARNING,
            "telegram-network-unavailable",
            "暂时无法连接 Telegram 服务",
        )
    except Exception:
        return _result(
            "telegram",
            "Telegram 连接",
            DiagnosticStatus.FAILED,
            "telegram-check-failed",
            "Telegram 连接检查失败",
        )
    return _result(
        "telegram",
        "Telegram 连接",
        DiagnosticStatus.PASSED,
        "telegram-connected",
        "Telegram 登录会话和连接正常",
    )
```

```python
def _telegram_authorization_failure(
    reason: AuthorizationFailureReason,
) -> DiagnosticResult:
    return _result(
        "telegram",
        "Telegram 连接",
        DiagnosticStatus.FAILED,
        "telegram-session-expired",
        "Telegram 登录会话已失效",
        {"authorizationReason": reason.value},
    )
```

In `diagnostic_store.py`, import `AuthorizationFailureReason`, add the metric to the Telegram allowlist, and validate its exact value set:

```python
"telegram": frozenset({"authorizationReason"}),
```

```python
_AUTHORIZATION_REASON_METRICS = frozenset({"authorizationReason"})
_SAFE_AUTHORIZATION_REASONS = frozenset(
    reason.value for reason in AuthorizationFailureReason
)
```

```python
elif key in _AUTHORIZATION_REASON_METRICS:
    valid = isinstance(value, str) and value in _SAFE_AUTHORIZATION_REASONS
```

- [ ] **Step 5: Run diagnostic tests and verify GREEN**

Run:

```powershell
& '.venv/Scripts/python.exe' -m pytest -q tests/test_diagnostic_probes.py tests/test_diagnostic_store.py
```

Expected: all diagnostic probe and store tests pass.

- [ ] **Step 6: Commit the diagnostic unit**

```powershell
git add -- src/telegram_downloader/diagnostic_probes.py src/telegram_downloader/diagnostic_store.py tests/test_diagnostic_probes.py tests/test_diagnostic_store.py
git commit -m "fix: report safe Telegram authorization reasons"
```

### Task 3: Notify global recovery from the subscription scheduler

**Files:**
- Modify: `src/telegram_downloader/subscription_scheduler.py:1-56,166-173`
- Test: `tests/test_subscription_scheduler.py:1-18,329-364`

- [ ] **Step 1: Write failing scheduler callback tests**

Add one ordering test and one callback-isolation test:

```python
@pytest.mark.asyncio
async def test_auth_failure_is_persisted_before_global_callback() -> None:
    service = Service(rule("r1"))
    error = SessionExpiredError(
        reason=AuthorizationFailureReason.SESSION_REVOKED
    )
    service.outcomes = [error]
    events: list[tuple[str, object]] = []

    async def expired(caught: SessionExpiredError) -> None:
        events.append(("callback", caught.reason))
        assert service.rules["r1"].state is SubscriptionState.AUTH_REQUIRED

    scheduler = SubscriptionScheduler(
        service,
        clock=lambda: NOW,
        foreground_busy=lambda: False,
        on_session_expired=expired,
        idle_delay=0.01,
    )
    scheduler.set_account("a1")
    scheduler.start()

    await wait_until(lambda: bool(events))
    assert events == [("callback", AuthorizationFailureReason.SESSION_REVOKED)]
    assert scheduler.running is True
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_auth_callback_failure_does_not_stop_scheduler() -> None:
    service = Service(rule("r1"))
    service.outcomes = [SessionExpiredError(), report(rule("r1"))]

    async def broken_callback(_error: SessionExpiredError) -> None:
        raise RuntimeError("callback failed")

    scheduler = SubscriptionScheduler(
        service,
        clock=lambda: NOW,
        foreground_busy=lambda: False,
        on_session_expired=broken_callback,
        idle_delay=0.01,
    )
    scheduler.set_account("a1")
    scheduler.start()

    await wait_until(lambda: len(service.runtime_calls) == 1)
    assert scheduler.running is True
    await scheduler.shutdown()
```

- [ ] **Step 2: Run scheduler tests and verify RED**

Run:

```powershell
& '.venv/Scripts/python.exe' -m pytest -q tests/test_subscription_scheduler.py -k 'global_callback or callback_failure'
```

Expected: constructor failure because `on_session_expired` is not accepted.

- [ ] **Step 3: Implement the asynchronous callback boundary**

Import `Awaitable`, add a no-op callback, and store the injected callback:

```python
from collections.abc import Awaitable, Callable


async def _ignore_session_expired(_error: SessionExpiredError) -> None:
    return None
```

```python
on_session_expired: Callable[
    [SessionExpiredError], Awaitable[None]
] | None = None,
```

```python
self.on_session_expired = on_session_expired or _ignore_session_expired
```

Persist before callback and keep callback implementation failures from terminating the scheduler loop:

```python
except SessionExpiredError as error:
    self._record_failure(
        rule,
        SubscriptionState.AUTH_REQUIRED,
        None,
        "Telegram 登录已失效",
    )
    try:
        await self.on_session_expired(error)
    except asyncio.CancelledError:
        raise
    except Exception as callback_error:
        _LOGGER.error(
            "subscription auth callback failed (%s)",
            type(callback_error).__name__,
        )
```

Define `_LOGGER = logging.getLogger("telegram_downloader.subscription_scheduler")`.

- [ ] **Step 4: Run scheduler tests and verify GREEN**

Run:

```powershell
& '.venv/Scripts/python.exe' -m pytest -q tests/test_subscription_scheduler.py
```

Expected: all subscription scheduler tests pass and no unhandled task exception is printed.

- [ ] **Step 5: Commit the scheduler unit**

```powershell
git add -- src/telegram_downloader/subscription_scheduler.py tests/test_subscription_scheduler.py
git commit -m "fix: escalate subscription authorization expiry"
```

### Task 4: Make global recovery idempotent and verify authorization before healthy status

**Files:**
- Modify: `src/telegram_downloader/controller.py:376-459,497-558,1987-2055`
- Test: `tests/test_controller.py:104-129,1949-2005,2568-2643`

- [ ] **Step 1: Write failing connected-but-unauthorized and transient verification tests**

Use `ContentWindowFake` and a counting vault to prove that a connected socket is insufficient:

Import `AuthorizationFailureReason` with the other gateway types before adding the tests.

```python
@pytest.mark.asyncio
async def test_connected_transport_requires_authorized_account() -> None:
    class Gateway:
        def is_connected(self) -> bool:
            return True

        async def test_connection(self) -> None:
            raise SessionExpiredError(
                reason=AuthorizationFailureReason.AUTH_KEY_INVALID
            )

        async def disconnect(self) -> None:
            pass

    vault = Vault()
    vault.value = {"session": "saved", "api_hash": "hash"}
    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=Gateway(),
        vault=vault,
        secrets=vault.load(),
        window=window,
    )
    shown: list[str] = []
    controller.show_login = lambda: shown.append("login")

    assert await controller.ensure_telegram_online() is False
    assert "连接正常" not in window.content_page.connection_states
    assert window.content_page.logged_in is False
    assert "session" not in controller.secrets
    assert shown == ["login"]


@pytest.mark.asyncio
async def test_authorization_check_network_failure_keeps_saved_session() -> None:
    class Gateway:
        def is_connected(self) -> bool:
            return True

        async def test_connection(self) -> None:
            raise TransientNetworkError("offline")

    vault = Vault()
    vault.value = {"session": "saved", "api_hash": "hash"}
    window = ContentWindowFake()
    controller = AppController.for_test(
        gateway=Gateway(),
        vault=vault,
        secrets=vault.load(),
        window=window,
    )

    assert await controller.ensure_telegram_online() is False
    assert controller.secrets["session"] == "saved"
    assert window.content_page.connection_retryable[-1] is True
```

- [ ] **Step 2: Write a failing concurrent recovery and safe logging test**

Import `telegram_downloader.controller as controller_module` so the test can replace only the module logger.

```python
@pytest.mark.asyncio
async def test_concurrent_session_expiry_runs_one_relogin_flow(monkeypatch) -> None:
    class Gateway:
        def __init__(self) -> None:
            self.disconnects = 0

        async def disconnect(self) -> None:
            self.disconnects += 1

    class Logger:
        def __init__(self) -> None:
            self.calls = []

        def warning(self, template, *args):
            self.calls.append((template, args))

    logger = Logger()
    monkeypatch.setattr(controller_module, "_LOGGER", logger)
    gateway = Gateway()
    vault = Vault()
    vault.value = {"session": "saved", "api_hash": "hash"}
    controller = AppController.for_test(
        gateway=gateway,
        vault=vault,
        secrets=vault.load(),
    )
    shown: list[str] = []
    controller.show_login = lambda: shown.append("login")
    error = SessionExpiredError(
        "private server text",
        reason=AuthorizationFailureReason.AUTH_KEY_DUPLICATED,
    )

    await asyncio.gather(
        controller._handle_session_expired(error),
        controller._handle_session_expired(error),
    )

    assert gateway.disconnects == 1
    assert shown == ["login"]
    assert controller.last_authorization_failure_reason is (
        AuthorizationFailureReason.AUTH_KEY_DUPLICATED
    )
    serialized = repr(logger.calls)
    assert "auth-key-duplicated" in serialized
    assert "private server text" not in serialized
```

- [ ] **Step 3: Run focused controller tests and verify RED**

Run:

```powershell
& '.venv/Scripts/python.exe' -m pytest -q tests/test_controller.py -k 'connected_transport_requires or authorization_check_network or concurrent_session_expiry'
```

Expected: connected gateway is accepted without calling `test_connection`, and duplicate recovery is not coalesced.

- [ ] **Step 4: Implement authorization verification before healthy UI state**

Refactor `ensure_telegram_online()` into transport recovery followed by an authorization check:

```python
async def ensure_telegram_online(self) -> bool:
    page = self._content_page()
    if self.gateway is None:
        page.set_logged_in(False)
        page.set_connection_state(
            "请先登录 Telegram；已保存的搜索历史仍可查看",
            retryable=False,
        )
        self.show_login()
        return False

    recovered = False

    def attempt(value: tuple[int, int]) -> None:
        number, total = value
        text = (
            "正在连接 Telegram…"
            if number == 1
            else f"正在重连（{number}/{total}）…"
        )
        page.set_connection_state(text, retryable=False)

    if not self._gateway_is_connected(self.gateway):
        try:
            await self.connection_recovery.ensure_connected(self.gateway, attempt)
            recovered = True
        except SessionExpiredError as error:
            await self._handle_session_expired(error)
            return False
        except TransientNetworkError:
            self._show_connection_retryable(page)
            return False
        except Exception as error:
            safe = self._safe_error(error)
            page.set_logged_in(False)
            page.set_connection_state(f"连接失败：{safe}", retryable=True)
            self._show_status(f"Telegram 连接失败：{safe}")
            return False

    try:
        await self._verify_gateway_authorized(self.gateway)
    except SessionExpiredError as error:
        await self._handle_session_expired(error)
        return False
    except TransientNetworkError:
        self._show_connection_retryable(page)
        return False
    except Exception as error:
        safe = self._safe_error(error)
        page.set_logged_in(False)
        page.set_connection_state(f"连接失败：{safe}", retryable=True)
        self._show_status(f"Telegram 连接失败：{safe}")
        return False

    page.set_logged_in(True)
    page.set_connection_state(
        "连接已恢复" if recovered else "连接正常",
        retryable=False,
    )
    if recovered:
        self._resume_subscriptions_after_connection()
    return True
```

Keep old lightweight fakes compatible while enforcing the production protocol:

```python
@staticmethod
async def _verify_gateway_authorized(gateway: object) -> None:
    method = getattr(gateway, "test_connection", None)
    if callable(method):
        await method()
```

Extract the existing retryable UI update so both transport and verification network failures have identical behavior and never clear the session:

```python
def _show_connection_retryable(self, page: object) -> None:
    page.set_logged_in(False)
    page.set_connection_state(
        "重连失败，请检查网络或代理后重试",
        retryable=True,
    )
    self._show_status("Telegram 重连失败，请检查网络或代理")
```

- [ ] **Step 5: Implement one idempotent global expiry flow**

Initialize controller state:

```python
self._session_expiry_lock = asyncio.Lock()
self._session_expiry_handled = False
self._last_authorization_failure_reason: AuthorizationFailureReason | None = None
```

Expose the retained safe reason read-only:

```python
@property
def last_authorization_failure_reason(
    self,
) -> AuthorizationFailureReason | None:
    return self._last_authorization_failure_reason
```

Wrap `_handle_session_expired` in the lock and guard, set both account pages offline, and log only the reason value:

```python
async def _handle_session_expired(self, error: SessionExpiredError) -> None:
    self._last_authorization_failure_reason = error.reason
    async with self._session_expiry_lock:
        if self._session_expiry_handled:
            return
        self._session_expiry_handled = True
        _LOGGER.warning(
            "Telegram authorization expired (reason=%s)",
            error.reason.value,
        )
        await self.connection_recovery.cancel()
        await self._cancel_subscription_probe()
        await self._cancel_content_operations()
        page = self._content_page()
        subscription_page = self._subscription_page()
        if self.content_browser is not None:
            go_offline = getattr(self.content_browser, "go_offline", None)
            if go_offline is not None:
                go_offline()
        self.subscriptions.go_offline()
        self.subscription_scheduler.set_account(None)

        self.secrets.pop("session", None)
        self.vault.save(self.secrets)
        self.window.set_account(None)
        page.set_logged_in(False)
        page.set_connection_state(
            "Telegram 登录已失效，请重新登录",
            retryable=False,
        )
        subscription_page.set_logged_in(False)
        page.show_error("Telegram 登录已失效，请重新扫码登录")

        previous_scheduler = self.scheduler
        previous_gateway = self.gateway
        self.gateway = None
        self.planner = None
        self.scheduler = _NullScheduler()
        with suppress(Exception):
            await previous_scheduler.shutdown()
        if previous_gateway is not None:
            with suppress(Exception):
                await previous_gateway.disconnect()

        api_hash = self.secrets.get("api_hash", "")
        if self.gateway_factory is not None and self.settings.api_id > 0 and api_hash:
            fresh_gateway = self.gateway_factory(
                self.settings.api_id,
                api_hash,
                "",
                self.settings.proxy,
                self.secrets.get("proxy_password", ""),
            )
            self.gateway = fresh_gateway
            try:
                await fresh_gateway.connect()
            except Exception as reconnect_error:
                _LOGGER.warning(
                    "fresh Telegram connection failed (%s)",
                    type(reconnect_error).__name__,
                )
            if self.service_builder is not None:
                services = self.service_builder(
                    fresh_gateway,
                    self.settings,
                )
                if len(services) == 3:
                    self.planner, self.scheduler, self.content_browser = services
                else:
                    self.planner, self.scheduler = services

        self.show_login()
```

Import `AuthorizationFailureReason` from `gateway`. Do not log or display `str(error)` because tests may construct the exception with private text.

After `_finish_login()` has exported the new session and obtained the account name, reset the guard and retained diagnostic reason before activating content and subscriptions:

```python
self._session_expiry_handled = False
self._last_authorization_failure_reason = None
```

- [ ] **Step 6: Run controller tests and verify GREEN**

Run:

```powershell
& '.venv/Scripts/python.exe' -m pytest -q tests/test_controller.py tests/test_subscription_controller.py
```

Expected: all controller and subscription controller tests pass.

- [ ] **Step 7: Commit the controller unit**

```powershell
git add -- src/telegram_downloader/controller.py tests/test_controller.py
git commit -m "fix: unify Telegram session expiry recovery"
```

### Task 5: Wire production scheduler recovery and retained diagnostics

**Files:**
- Modify: `src/telegram_downloader/app.py:348-386,453-477`
- Test: `tests/test_app.py:119-176`

- [ ] **Step 1: Write failing application wiring tests**

Import `pytest`, `AuthorizationFailureReason`, and `SessionExpiredError` at the top of `tests/test_app.py`.

Extend the application construction test to invoke the installed scheduler callback without starting the scheduler:

```python
auth_events: list[AuthorizationFailureReason] = []

async def record_expiry(error: SessionExpiredError) -> None:
    auth_events.append(error.reason)

controller._handle_session_expired = record_expiry
loop.run_until_complete(
    controller.subscription_scheduler.on_session_expired(
        SessionExpiredError(
            reason=AuthorizationFailureReason.SESSION_REVOKED
        )
    )
)
assert auth_events == [AuthorizationFailureReason.SESSION_REVOKED]
```

Add a focused helper test showing that graphical diagnostics can report the retained safe reason after the invalid gateway is detached:

```python
@pytest.mark.asyncio
async def test_telegram_health_uses_retained_authorization_reason() -> None:
    controller = SimpleNamespace(
        gateway=None,
        last_authorization_failure_reason=(
            AuthorizationFailureReason.AUTH_KEY_DUPLICATED
        ),
    )

    result = await app._telegram_health(controller)

    assert result.code == "telegram-session-expired"
    assert result.metrics == {
        "authorizationReason": "auth-key-duplicated"
    }
```

- [ ] **Step 2: Run application tests and verify RED**

Run:

```powershell
& '.venv/Scripts/python.exe' -m pytest -q tests/test_app.py -k 'project_local_content_services or retained_authorization_reason'
```

Expected: scheduler callback is the no-op default and `_telegram_health` is not a testable top-level helper.

- [ ] **Step 3: Wire the callback and diagnostic helper**

Move the nested Telegram health logic into a focused top-level helper:

```python
async def _telegram_health(controller: AppController) -> DiagnosticResult:
    reason = controller.last_authorization_failure_reason
    if reason is not None:
        return await probe_telegram(None, authorization_reason=reason)
    gateway_value = controller.gateway
    if gateway_value is None:
        return await probe_telegram(None)

    class RecoveredConnection:
        async def test_connection(self) -> None:
            await controller.connection_recovery.ensure_connected(gateway_value)
            await gateway_value.test_connection()

    return await probe_telegram(RecoveredConnection())
```

The `_FunctionDiagnosticProbe` closure calls `_telegram_health(controller_ref["controller"])`.

Define the scheduler bridge before constructing `SubscriptionScheduler`:

```python
async def subscription_session_expired(error: SessionExpiredError) -> None:
    controller = controller_ref.get("controller")
    if controller is not None:
        await controller._handle_session_expired(error)
```

Pass it into the scheduler:

```python
on_session_expired=subscription_session_expired,
```

- [ ] **Step 4: Run application and focused integration tests and verify GREEN**

Run:

```powershell
& '.venv/Scripts/python.exe' -m pytest -q tests/test_app.py tests/test_subscription_scheduler.py tests/test_controller.py tests/test_diagnostic_probes.py tests/test_diagnostic_store.py
```

Expected: all focused gateway-to-UI recovery and diagnostic tests pass.

- [ ] **Step 5: Commit the production wiring unit**

```powershell
git add -- src/telegram_downloader/app.py tests/test_app.py
git commit -m "fix: wire background authorization recovery"
```

### Task 6: Full regression, privacy inspection, build, and packaged smoke test

**Files:**
- Verify: `src/telegram_downloader/`
- Verify: `tests/`
- Verify: `dist/TelegramDownloader/`

- [ ] **Step 1: Run the complete test and lint script**

Run:

```powershell
& './scripts/test.ps1'
```

Expected: the full pytest suite passes and Ruff reports no errors.

- [ ] **Step 2: Inspect every authorization log/report path for privacy**

Run:

```powershell
rg -n "authorizationReason|authorization expired|SessionExpiredError|str\(error\)" src tests
```

Expected: user-facing paths use fixed Chinese text; logging and diagnostic serialization use only `AuthorizationFailureReason.value`; no Telegram raw exception text is interpolated.

- [ ] **Step 3: Build the Windows package**

Run:

```powershell
& './scripts/build.ps1'
```

Expected: exit code 0 and `dist/TelegramDownloader/TelegramDownloader.exe` exists.

- [ ] **Step 4: Run packaged smoke checks**

Run:

```powershell
& './scripts/smoke.ps1'
```

Expected: output ends with `PACKAGED_SMOKE_OK`.

- [ ] **Step 5: Verify the final diff and commit history**

Run:

```powershell
git status --short
git log --oneline -6
git diff HEAD~5..HEAD --check
```

Expected: source and tests are committed, the working tree has no unintended changes, and `git diff --check` emits no output.

- [ ] **Step 6: Record final verification evidence**

Report the exact full-suite pass count, Ruff result, build exit code, packaged smoke result, changed commits, and the fact that the running v0.11.0 process was not hot-patched. Do not publish a release or overwrite the user's active portable directory without separate authorization.
