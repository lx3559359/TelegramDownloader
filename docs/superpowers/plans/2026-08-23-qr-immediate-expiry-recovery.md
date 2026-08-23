# QR Immediate-Expiry Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure TG 快取 never presents an already-expired Telegram login QR code and always performs one coordinated, recoverable refresh when a displayed code expires.

**Architecture:** Extend the gateway QR contract with a relative validity duration computed at response receipt, render countdowns against a monotonic clock, and route manual, UI-expiry, and Telethon-timeout refreshes through one generation-checked controller lock. Start the Telethon wait task before showing each QR code, reject tokens with less than five seconds remaining, and log only non-sensitive lifecycle metadata.

**Tech Stack:** Python 3.11+, asyncio, Telethon 1.44, PySide6/QTimer, qasync, pytest/pytest-qt/pytest-asyncio, Ruff.

---

## File map

- Modify `src/telegram_downloader/gateway.py`: add relative QR validity and an injectable UTC clock at the Telegram boundary.
- Modify `src/telegram_downloader/ui/login.py`: use a monotonic deadline and emit one generation-scoped expiry signal.
- Modify `src/telegram_downloader/controller.py`: validate QR lifetime, start listening before display, and unify refresh arbitration.
- Modify `src/telegram_downloader/app.py`: connect UI expiry through the async action bridge.
- Modify `src/telegram_downloader/ui/async_actions.py`: register the stable expiry action key.
- Modify `tests/test_gateway.py`: verify validity calculation and expired-response preservation.
- Modify `tests/ui/test_login_dialog.py`: verify monotonic countdown and one-shot expiry.
- Modify `tests/test_controller.py`: verify short-token rejection, listener ordering, refresh deduplication, and account isolation.
- Modify `tests/test_app.py`: verify production signal wiring and action policy coverage.
- Modify `tests/test_packaging_contract.py`: establish the local v0.18.1 release metadata contract.
- Modify `pyproject.toml`, `src/telegram_downloader/__init__.py`, and `installer/TelegramDownloader.iss`: align the local patch version.
- Create `docs/releases/v0.18.1.md`: describe the QR recovery behavior and compatibility boundary.
- Create `docs/verification/v0.18.1-qr-immediate-expiry-recovery.md`: record reproducible verification without sensitive login material.

## Task 1: Add relative validity to the gateway QR contract

**Files:**
- Modify: `src/telegram_downloader/gateway.py:69-73, 250-350, 435-443`
- Test: `tests/test_gateway.py:92-123`
- Test: `tests/test_controller.py` (mechanical constructor updates only in this task)

- [ ] **Step 1: Write failing gateway validity tests**

Add `timedelta` to the imports and replace the existing QR begin/refresh assertion test with deterministic validity assertions:

```python
from datetime import UTC, datetime, timedelta


@pytest.mark.asyncio
async def test_qr_login_info_carries_relative_validity_from_response_time() -> None:
    now = datetime(2026, 8, 23, 5, tzinfo=UTC)
    expires = now + timedelta(seconds=29.5)

    class FakeQr:
        def __init__(self) -> None:
            self.url = "tg://login?token=first"
            self.expires = expires
            self.waited = False

        async def recreate(self) -> None:
            self.url = "tg://login?token=refreshed"
            self.expires = expires + timedelta(seconds=1)

        async def wait(self) -> None:
            self.waited = True

    qr = FakeQr()

    class Client:
        async def qr_login(self):
            return qr

    gateway = TelethonGateway.from_client_for_test(
        Client(),
        utc_now=lambda: now,
    )

    info = await gateway.begin_qr_login()
    refreshed = await gateway.refresh_qr_login()
    state = await gateway.wait_qr_login()

    assert info == gateway_module.QrLoginInfo(
        "tg://login?token=first",
        expires,
        29.5,
    )
    assert refreshed == gateway_module.QrLoginInfo(
        "tg://login?token=refreshed",
        expires + timedelta(seconds=1),
        30.5,
    )
    assert state is AuthState.READY
    assert qr.waited is True


@pytest.mark.asyncio
async def test_qr_login_info_preserves_non_positive_validity_for_controller() -> None:
    now = datetime(2026, 8, 23, 5, tzinfo=UTC)

    class FakeQr:
        url = "tg://login?token=expired"
        expires = now - timedelta(seconds=2)

    class Client:
        async def qr_login(self):
            return FakeQr()

    gateway = TelethonGateway.from_client_for_test(
        Client(),
        utc_now=lambda: now,
    )

    info = await gateway.begin_qr_login()

    assert info.valid_for_seconds == -2
```

- [ ] **Step 2: Run the gateway tests and verify RED**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_gateway.py -k "qr_login_info" -q
```

Expected: FAIL because `from_client_for_test()` does not accept `utc_now` and `QrLoginInfo` has no `valid_for_seconds` field.

- [ ] **Step 3: Implement the relative validity contract**

Change the dataclass and gateway clocks as follows:

```python
@dataclass(frozen=True, slots=True)
class QrLoginInfo:
    url: str
    expires_at: datetime
    valid_for_seconds: float
```

Add the keyword-only clock to `TelethonGateway.__init__` without changing existing positional production arguments:

```python
def __init__(
    self,
    api_id: int,
    api_hash: str,
    session: str = "",
    proxy: ProxySettings | None = None,
    proxy_password: str = "",
    *,
    utc_now: Callable[[], datetime] | None = None,
) -> None:
    # Existing TelegramClient construction stays unchanged.
    self._utc_now = utc_now or (lambda: datetime.now(UTC))
```

Import `Callable` if it is not already present. Extend the test factory:

```python
@classmethod
def from_client_for_test(
    cls,
    client: object,
    *,
    # Existing keyword parameters stay unchanged.
    utc_now: Callable[[], datetime] | None = None,
    connected: bool = True,
) -> TelethonGateway:
    gateway = cls.__new__(cls)
    # Existing field assignments stay unchanged.
    gateway._utc_now = utc_now or (lambda: datetime.now(UTC))
    return gateway
```

Update `_qr_info()`:

```python
def _qr_info(self) -> QrLoginInfo:
    qr_login = self._require_qr_login()
    url = getattr(qr_login, "url", "")
    expires_at = self._utc_datetime(getattr(qr_login, "expires", None))
    if not isinstance(url, str) or not url.startswith("tg://login?token="):
        raise GatewayError("Telegram 未返回有效二维码")
    if expires_at is None:
        raise GatewayError("Telegram 未返回二维码过期时间")
    now = self._utc_datetime(self._utc_now())
    if now is None:
        raise GatewayError("本机时间无效，无法生成二维码")
    return QrLoginInfo(
        url,
        expires_at,
        (expires_at - now).total_seconds(),
    )
```

- [ ] **Step 4: Update existing test fixtures to the explicit contract**

Every `QrLoginInfo(url, expires)` in `tests/test_controller.py` must become `QrLoginInfo(url, expires, 60.0)`. Keep the new short-lifetime tests in later tasks at their explicit `0.0`, `3.0`, or `30.0` values. Do not add a default field value because that would let new call sites silently omit the validity contract.

- [ ] **Step 5: Run gateway and controller collection tests**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_gateway.py tests/test_controller.py --collect-only -q
& '.\.venv\Scripts\python.exe' -m pytest tests/test_gateway.py -k "qr" -q
```

Expected: collection succeeds and all gateway QR tests pass.

- [ ] **Step 6: Commit the gateway contract**

```powershell
git add src/telegram_downloader/gateway.py tests/test_gateway.py tests/test_controller.py
git commit -m "fix: carry relative QR validity"
```

## Task 2: Make the login countdown monotonic and one-shot

**Files:**
- Modify: `src/telegram_downloader/ui/login.py:1-66, 294-395`
- Test: `tests/ui/test_login_dialog.py:114-185`

- [ ] **Step 1: Write failing monotonic countdown tests**

Replace absolute-expiry calls in QR UI tests with the new API and add a controllable clock:

```python
class MonotonicClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_qr_countdown_uses_monotonic_deadline_and_expires_once(qtbot) -> None:
    clock = MonotonicClock()
    dialog = LoginDialog(monotonic_now=clock)
    qtbot.addWidget(dialog)
    expired_generations: list[int] = []
    dialog.qr_expired.connect(expired_generations.append)

    dialog.show_qr("tg://login?token=abc_123", 30.0, 7)

    assert dialog.qr_countdown.text() == "二维码将在 30 秒后刷新"
    clock.value += 30.0
    dialog._tick_qr_countdown()
    dialog._tick_qr_countdown()

    assert expired_generations == [7]
    assert dialog.qr_countdown.text() == "二维码已过期，正在生成新二维码…"
    assert dialog.qr_countdown_timer.isActive() is False


def test_new_qr_generation_rearms_countdown_after_expiry(qtbot) -> None:
    clock = MonotonicClock()
    dialog = LoginDialog(monotonic_now=clock)
    qtbot.addWidget(dialog)
    expired_generations: list[int] = []
    dialog.qr_expired.connect(expired_generations.append)

    dialog.show_qr("tg://login?token=first", 5.0, 1)
    clock.value += 5.0
    dialog._tick_qr_countdown()
    dialog.show_qr("tg://login?token=second", 29.0, 2)

    assert expired_generations == [1]
    assert dialog.qr_countdown.text() == "二维码将在 29 秒后刷新"
    assert dialog.qr_countdown_timer.isActive() is True
```

Update existing tests to call `dialog.show_qr(url, 60.0, generation)` and remove `datetime`, `timedelta`, and `UTC` imports when no longer used.

- [ ] **Step 2: Run the UI tests and verify RED**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
& '.\.venv\Scripts\python.exe' -m pytest tests/ui/test_login_dialog.py -k "qr" -q
```

Expected: FAIL because `LoginDialog` has no `monotonic_now` parameter or `qr_expired` signal, and `show_qr()` still expects `expires_at`.

- [ ] **Step 3: Implement the monotonic countdown**

Replace wall-clock imports with `from time import monotonic` and add `Callable`:

```python
from collections.abc import Callable
from time import monotonic
```

Add the signal and constructor state:

```python
class LoginDialog(QDialog):
    credentials_submitted = Signal(int, str, object, str)
    qr_refresh_requested = Signal()
    qr_expired = Signal(int)
    # Existing signals stay unchanged.

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        monotonic_now: Callable[[], float] = monotonic,
    ) -> None:
        super().__init__(parent)
        self._monotonic_now = monotonic_now
        self._qr_deadline: float | None = None
        self._qr_generation: int | None = None
        self._qr_expiry_emitted = False
        # Existing UI construction stays unchanged.
```

Replace the QR methods:

```python
def show_qr(self, url: str, valid_for_seconds: float, generation: int) -> None:
    if valid_for_seconds <= 0:
        raise ValueError("二维码有效期必须大于零")
    image = render_qr_image(url)
    self.qr_image.setPixmap(QPixmap.fromImage(image))
    self._qr_deadline = self._monotonic_now() + valid_for_seconds
    self._qr_generation = generation
    self._qr_expiry_emitted = False
    self.show_page(LoginPage.QR)
    self._tick_qr_countdown()
    self.qr_countdown_timer.start()
    self.adjustSize()

def update_qr_countdown(self, seconds: int) -> None:
    if seconds > 0:
        self.qr_countdown.setText(f"二维码将在 {seconds} 秒后刷新")
    else:
        self.qr_countdown.setText("二维码已过期，正在生成新二维码…")

def _tick_qr_countdown(self) -> None:
    if self._qr_deadline is None:
        return
    seconds = max(0, ceil(self._qr_deadline - self._monotonic_now()))
    self.update_qr_countdown(seconds)
    if seconds > 0 or self._qr_expiry_emitted:
        return
    self._qr_expiry_emitted = True
    self.qr_countdown_timer.stop()
    if self._qr_generation is not None:
        self.qr_expired.emit(self._qr_generation)

def _clear_qr(self) -> None:
    self.qr_countdown_timer.stop()
    self._qr_deadline = None
    self._qr_generation = None
    self._qr_expiry_emitted = False
    self.qr_image.clear()
    self.qr_countdown.setText("正在生成二维码…")
    self.qr_status.setText("等待手机扫码确认")
```

- [ ] **Step 4: Run all login-dialog tests**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
& '.\.venv\Scripts\python.exe' -m pytest tests/ui/test_login_dialog.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the UI lifecycle**

```powershell
git add src/telegram_downloader/ui/login.py tests/ui/test_login_dialog.py
git commit -m "fix: use monotonic QR countdown"
```

## Task 3: Reject short-lived tokens and listen before display

**Files:**
- Modify: `src/telegram_downloader/controller.py:60-80, 500-570, 913-1045`
- Test: `tests/test_controller.py:1100-1310`

- [ ] **Step 1: Write failing short-token recovery tests**

Add these controller tests using future `expires_at` values only as metadata; behavior must follow `valid_for_seconds`:

```python
@pytest.mark.asyncio
async def test_short_lived_qr_is_recreated_before_it_is_displayed() -> None:
    expires = datetime(2026, 8, 23, 5, tzinfo=UTC)
    waiting = asyncio.Event()

    class Gateway:
        def __init__(self) -> None:
            self.refresh_calls = 0

        async def begin_qr_login(self):
            return QrLoginInfo("tg://login?token=stale", expires, 1.0)

        async def refresh_qr_login(self):
            self.refresh_calls += 1
            return QrLoginInfo("tg://login?token=fresh", expires, 29.0)

        async def wait_qr_login(self):
            waiting.set()
            await asyncio.Event().wait()

    class Dialog:
        def __init__(self) -> None:
            self.shown: list[tuple[str, float, int]] = []
            self.errors: list[str] = []

        def show_qr(self, url, valid_for_seconds, generation):
            assert waiting.is_set()
            self.shown.append((url, valid_for_seconds, generation))

        def show_qr_status(self, _text):
            pass

        def show_error(self, text):
            self.errors.append(text)

    gateway = Gateway()
    dialog = Dialog()
    controller = AppController.for_test(gateway=gateway, login_dialog=dialog)

    try:
        await controller.begin_qr_login()

        assert gateway.refresh_calls == 1
        assert dialog.shown == [("tg://login?token=fresh", 29.0, 2)]
        assert dialog.errors == []
    finally:
        await controller._cancel_qr_wait()


@pytest.mark.asyncio
async def test_two_short_lived_qr_tokens_stop_without_display_or_loop() -> None:
    expires = datetime(2026, 8, 23, 5, tzinfo=UTC)

    class Gateway:
        def __init__(self) -> None:
            self.refresh_calls = 0

        async def begin_qr_login(self):
            return QrLoginInfo("tg://login?token=stale", expires, 0.0)

        async def refresh_qr_login(self):
            self.refresh_calls += 1
            return QrLoginInfo("tg://login?token=still-stale", expires, 3.0)

        async def wait_qr_login(self):
            raise AssertionError("short-lived QR must not be awaited")

    class Dialog:
        def __init__(self) -> None:
            self.shown = 0
            self.error = ""

        def show_qr(self, *_args):
            self.shown += 1

        def show_qr_status(self, _text):
            pass

        def show_error(self, text):
            self.error = text

    gateway = Gateway()
    dialog = Dialog()
    controller = AppController.for_test(gateway=gateway, login_dialog=dialog)

    await controller.begin_qr_login()

    assert gateway.refresh_calls == 1
    assert dialog.shown == 0
    assert dialog.error == "二维码有效期异常，请检查系统时间或网络后重试"
    assert controller._qr_wait_task is None
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_controller.py -k "short_lived_qr or two_short_lived" -q
```

Expected: FAIL because short-lived QR codes are displayed and `show_qr` is called before the wait coroutine registers.

- [ ] **Step 3: Add the validity threshold and one-rebuild helper**

At module level:

```python
_MIN_QR_VALIDITY_SECONDS = 5.0
_QR_VALIDITY_ERROR = "二维码有效期异常，请检查系统时间或网络后重试"
```

Import `isfinite` from `math` and add `QrLoginInfo` to the existing `from telegram_downloader.gateway import (...)` list.

Add this method to `AppController`:

```python
async def _displayable_qr_info(self, gateway: Any, info: QrLoginInfo) -> QrLoginInfo:
    if self._qr_lifetime_is_usable(info):
        return info
    _LOGGER.info(
        "qr-rejected-short-ttl (ttl_seconds=%s context=%s)",
        self._qr_ttl_metric(info.valid_for_seconds),
        self._qr_login_context(),
    )
    refreshed = await gateway.refresh_qr_login()
    if not self._qr_lifetime_is_usable(refreshed):
        raise GatewayError(_QR_VALIDITY_ERROR)
    return refreshed

@staticmethod
def _qr_lifetime_is_usable(info: QrLoginInfo) -> bool:
    return isfinite(info.valid_for_seconds) and (
        info.valid_for_seconds >= _MIN_QR_VALIDITY_SECONDS
    )

@staticmethod
def _qr_ttl_metric(value: float) -> int:
    return max(0, int(value)) if isfinite(value) else -1

def _qr_login_context(self) -> str:
    return "candidate" if self._candidate_login is not None else "initial"
```

Extend the second short-token test with a parametrized `float("nan")` case so non-finite durations cannot reach the dialog.

- [ ] **Step 4: Start waiting before the QR becomes visible**

Make `_show_qr_and_wait` asynchronous and pass the selected gateway explicitly so a candidate switch cannot redirect a running generation:

```python
async def _show_qr_and_wait(self, gateway: Any, info: QrLoginInfo) -> None:
    self._qr_generation += 1
    generation = self._qr_generation
    task = asyncio.create_task(self._wait_for_qr(gateway, generation))
    self._qr_wait_task = task
    if self._candidate_login is not None:
        self._candidate_login.qr_wait_task = task
    await asyncio.sleep(0)
    if generation != self._qr_generation or task.done():
        if task.done():
            with suppress(asyncio.CancelledError):
                await task
        return
    self._display_qr(info, generation)

def _display_qr(self, info: QrLoginInfo, generation: int) -> None:
    self.login_dialog.show_qr(info.url, info.valid_for_seconds, generation)
    self.login_dialog.show_qr_status("等待手机扫码确认")
    _LOGGER.info(
        "qr-created (generation=%s ttl_seconds=%s context=%s)",
        generation,
        max(0, int(info.valid_for_seconds)),
        self._qr_login_context(),
    )
```

Update begin and manual refresh call sites:

```python
info = await gateway.begin_qr_login()
info = await self._displayable_qr_info(gateway, info)
await self._show_qr_and_wait(gateway, info)
```

Change `_wait_for_qr` to accept the captured gateway:

```python
async def _wait_for_qr(self, gateway: Any, generation: int) -> None:
    # Existing loop and finish handling remain, but do not call _login_gateway().
```

- [ ] **Step 5: Run focused controller tests**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_controller.py -k "qr_login or short_lived_qr or phone_fallback" -q
```

Expected: all selected tests pass after adapting fake dialogs to `show_qr(url, valid_for_seconds, generation)`.

- [ ] **Step 6: Commit token validation and listener ordering**

```powershell
git add src/telegram_downloader/controller.py tests/test_controller.py
git commit -m "fix: validate QR before display"
```

## Task 4: Unify and deduplicate all expiry refresh paths

**Files:**
- Modify: `src/telegram_downloader/controller.py:520-570, 932-1045`
- Modify: `src/telegram_downloader/app.py:1190-1210, 1490-1510`
- Modify: `src/telegram_downloader/ui/async_actions.py:35-60`
- Test: `tests/test_controller.py:1130-1260`
- Test: `tests/test_app.py:40-65, 850-905`

- [ ] **Step 1: Write failing concurrent-expiry deduplication test**

Add a test that fires the same expiry generation twice while the first refresh is blocked:

```python
@pytest.mark.asyncio
async def test_ui_expiry_refresh_is_generation_scoped_and_deduplicated() -> None:
    expires = datetime(2026, 8, 23, 5, tzinfo=UTC)
    wait_started = asyncio.Event()
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    class Gateway:
        def __init__(self) -> None:
            self.refresh_calls = 0

        async def begin_qr_login(self):
            return QrLoginInfo("tg://login?token=first", expires, 29.0)

        async def refresh_qr_login(self):
            self.refresh_calls += 1
            refresh_started.set()
            await release_refresh.wait()
            return QrLoginInfo("tg://login?token=second", expires, 29.0)

        async def wait_qr_login(self):
            wait_started.set()
            await asyncio.Event().wait()

    class Dialog:
        def __init__(self) -> None:
            self.generations: list[int] = []

        def show_qr(self, _url, _ttl, generation):
            self.generations.append(generation)

        def show_qr_status(self, _text):
            pass

        def show_error(self, _text):
            pass

    gateway = Gateway()
    dialog = Dialog()
    controller = AppController.for_test(gateway=gateway, login_dialog=dialog)

    await controller.begin_qr_login()
    generation = dialog.generations[-1]
    first = asyncio.create_task(controller.refresh_expired_qr(generation))
    await refresh_started.wait()
    second = asyncio.create_task(controller.refresh_expired_qr(generation))
    release_refresh.set()
    await asyncio.gather(first, second)

    try:
        assert gateway.refresh_calls == 1
        assert len(dialog.generations) == 2
        assert dialog.generations[0] == generation
        assert dialog.generations[1] > generation
    finally:
        await controller._cancel_qr_wait()
```

Update the existing timeout auto-refresh test so its first `wait_qr_login()` raises `TimeoutError`, its second wait remains pending, and it asserts one new generation rather than reuse of the same wait task.

- [ ] **Step 2: Run refresh tests and verify RED**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_controller.py -k "expired_qr or expiry_refresh" -q
```

Expected: FAIL because `refresh_expired_qr()` and the shared refresh lock do not exist.

- [ ] **Step 3: Implement generation-checked refresh arbitration**

Initialize one lock in `AppController.__init__`:

```python
self._qr_refresh_lock = asyncio.Lock()
```

Add the public UI entry and private shared method:

```python
async def refresh_expired_qr(self, generation: int) -> None:
    await self._refresh_qr(expected_generation=generation)

async def refresh_qr_login(self) -> None:
    requested_generation = self._qr_generation
    await self._refresh_qr(expected_generation=requested_generation)

async def _refresh_qr(self, *, expected_generation: int) -> None:
    async with self._qr_refresh_lock:
        if expected_generation != self._qr_generation:
            _LOGGER.info(
                "qr-refresh-deduplicated (generation=%s current_generation=%s)",
                expected_generation,
                self._qr_generation,
            )
            return
        gateway = self._login_gateway()
        if gateway is None:
            self.login_dialog.show_error("请先填写 API 凭据")
            return
        try:
            await self._cancel_qr_wait()
            _LOGGER.info(
                "qr-refresh-started (generation=%s context=%s)",
                self._qr_generation,
                self._qr_login_context(),
            )
            info = await gateway.refresh_qr_login()
            info = await self._displayable_qr_info(gateway, info)
            await self._show_qr_and_wait(gateway, info)
        except TransientNetworkError as error:
            if self._candidate_login is None:
                from telegram_downloader.ui.login import LoginPage

                self._prefill_login()
                self.login_dialog.show_page(LoginPage.CREDENTIALS)
            self.login_dialog.show_error(self._safe_error(error))
        except Exception as error:
            self.login_dialog.show_error(self._safe_error(error))
```

Capturing the manual request's generation makes a click queued behind an automatic refresh stale after the automatic path advances the generation, so it cannot perform a second sequential refresh.

In `_wait_for_qr`, replace the inline recreate branch:

```python
except TimeoutError:
    await self._refresh_qr(expected_generation=generation)
    return
```

Because `_cancel_qr_wait()` detects `asyncio.current_task()`, timeout-driven refresh can retire its own generation without self-cancellation.

- [ ] **Step 4: Write failing production wiring test**

Add `"login.qr.expired": ActionPolicy.DEDUPLICATE` to the expected policy map in `tests/test_app.py`. Extend the existing async action wiring test:

```python
actions = {
    # Existing action mappings stay unchanged.
    "refresh_expired_qr": "login.qr.expired",
}

controller.login_dialog.qr_expired.emit(42)
```

The patched `refresh_expired_qr` test coroutine must record payload `42`, and the assertion must require exactly one call.

- [ ] **Step 5: Run the app wiring test and verify RED**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_app.py -k "async_action or qr_expired" -q
```

Expected: FAIL because the policy and signal connection are missing.

- [ ] **Step 6: Connect the expiry signal through AsyncActionBridge**

In `src/telegram_downloader/ui/async_actions.py` add:

```python
"login.qr.expired": ActionPolicy.DEDUPLICATE,
```

In `src/telegram_downloader/app.py`, beside manual QR refresh wiring, add:

```python
async_actions.connect_payload(
    login_dialog.qr_expired,
    "login.qr.expired",
    controller.refresh_expired_qr,
    hooks=login_hooks("qr.refresh"),
)
```

- [ ] **Step 7: Run controller and app QR suites**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
& '.\.venv\Scripts\python.exe' -m pytest tests/test_controller.py -k "qr or candidate_login or phone" -q
& '.\.venv\Scripts\python.exe' -m pytest tests/test_app.py -k "async_action or login" -q
```

Expected: all selected tests pass, one wait task remains active after a refresh, and no current-account service is torn down.

- [ ] **Step 8: Commit unified refresh handling**

```powershell
git add src/telegram_downloader/controller.py src/telegram_downloader/app.py src/telegram_downloader/ui/async_actions.py tests/test_controller.py tests/test_app.py
git commit -m "fix: coordinate QR expiry refresh"
```

## Task 5: Lock down privacy and account-isolation regressions

**Files:**
- Modify: `tests/test_controller.py`
- Modify: `src/telegram_downloader/controller.py` only if a test exposes unsafe logging

- [ ] **Step 1: Write failing privacy and candidate-isolation assertions**

Add a logging test around the short-token path:

```python
@pytest.mark.asyncio
async def test_qr_lifecycle_logs_metadata_without_token(caplog) -> None:
    expires = datetime(2026, 8, 23, 5, tzinfo=UTC)
    private_url = "tg://login?token=private_qr_token"

    class Gateway:
        async def begin_qr_login(self):
            return QrLoginInfo(private_url, expires, 1.0)

        async def refresh_qr_login(self):
            return QrLoginInfo(private_url, expires, 1.0)

    controller = AppController.for_test(gateway=Gateway())

    with caplog.at_level(logging.INFO, logger="telegram_downloader.controller"):
        await controller.begin_qr_login()

    assert "qr-rejected-short-ttl" in caplog.text
    assert "ttl_seconds=" in caplog.text
    assert private_url not in caplog.text
    assert "private_qr_token" not in caplog.text
```

Extend the candidate cancellation test to retain references to the active gateway, planner, scheduler, content browser, and secrets before expiry. After `refresh_expired_qr(candidate_generation)` fails, assert every reference and secret mapping is unchanged and only the candidate gateway is eligible for cleanup.

- [ ] **Step 2: Run the privacy/isolation tests**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_controller.py -k "lifecycle_logs or candidate" -q
& '.\.venv\Scripts\python.exe' -m pytest tests/test_logging.py -q
```

Expected: tests pass if lifecycle logs contain only event, integer TTL, generation, context, and safe exception type. If any token appears, change the production log arguments rather than weakening the assertion.

- [ ] **Step 3: Run all authentication regression tests**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
& '.\.venv\Scripts\python.exe' -m pytest tests/test_gateway.py tests/test_controller.py tests/test_app.py tests/test_account_access.py tests/ui/test_login_dialog.py -q
```

Expected: all tests pass, including QR, phone code, 2FA, session expiry, safe account navigation, candidate commit/rollback, cancellation, and shutdown.

- [ ] **Step 4: Commit the regression contract**

```powershell
git add tests/test_controller.py src/telegram_downloader/controller.py
git commit -m "test: protect QR recovery privacy"
```

## Task 6: Prepare local v0.18.1 release metadata

**Files:**
- Modify: `tests/test_packaging_contract.py:129-190`
- Modify: `pyproject.toml:7`
- Modify: `src/telegram_downloader/__init__.py:1`
- Modify: `installer/TelegramDownloader.iss:1-3`
- Create: `docs/releases/v0.18.1.md`

- [ ] **Step 1: Change the packaging contract first and verify RED**

Rename the existing version contract to `test_v0181_version_and_qr_expiry_recovery_contract_are_consistent`. Change its version and release-note assertions to:

```python
release_notes = root / "docs/releases/v0.18.1.md"

assert project["project"]["version"] == "0.18.1"
assert '__version__ = "0.18.1"' in package_init
assert '#define AppVersion "0.18.1"' in installer
assert release_notes.is_file()
notes = release_notes.read_text(encoding="utf-8")
assert "# TG 快取 v0.18.1" in notes
assert all(
    term in notes
    for term in (
        "二维码",
        "立即过期",
        "单调时钟",
        "自动刷新",
        "手动刷新",
        "手机号",
        "两步验证",
        "敏感信息",
    )
)
```

Keep all unrelated runtime, README, resource, and packaging assertions in that contract unchanged.

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_packaging_contract.py -k "v0181" -q
```

Expected: FAIL because source metadata is still `0.18.0` and `docs/releases/v0.18.1.md` does not exist.

- [ ] **Step 2: Align the three version sources**

Set these exact values:

```toml
# pyproject.toml
version = "0.18.1"
```

```python
# src/telegram_downloader/__init__.py
__version__ = "0.18.1"
```

```iss
; installer/TelegramDownloader.iss
#ifndef AppVersion
  #define AppVersion "0.18.1"
#endif
```

- [ ] **Step 3: Add concise v0.18.1 release notes**

Create `docs/releases/v0.18.1.md` with this content:

```markdown
# TG 快取 v0.18.1

## 修复

- 修复登录二维码刚生成便在电脑端显示“立即过期”的问题。程序会拒绝剩余时间不足的令牌，并在展示前安全地重新生成。
- 二维码倒计时改用单调时钟，不再受系统时间同步、时区调整或墙上时钟跳变影响。
- 自动刷新、手动刷新和 Telegram 超时现在共享 generation 去重，避免并行刷新让正在扫描的二维码失效。
- Telegram 登录监听会在二维码显示前启动，缩小扫码与监听注册之间的竞态窗口。

## 兼容与隐私

- 首次登录、候选账号重新登录、手机号验证码、两步验证、取消和会话加密流程保持兼容。
- 生命周期诊断只记录取整后的剩余秒数、generation、登录上下文和安全异常类型，不记录二维码、令牌、API 凭据、手机号、验证码、密码或会话等敏感信息。
```

- [ ] **Step 4: Run the packaging contract and commit metadata**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_packaging_contract.py -k "v0181" -q
```

Expected: PASS.

Commit:

```powershell
git add pyproject.toml src/telegram_downloader/__init__.py installer/TelegramDownloader.iss docs/releases/v0.18.1.md tests/test_packaging_contract.py
git commit -m "release: prepare TG Quick Fetch 0.18.1"
```

## Task 7: Full verification and evidence

**Files:**
- Create: `docs/verification/v0.18.1-qr-immediate-expiry-recovery.md`

- [ ] **Step 1: Run Ruff and the complete test suite**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
& '.\.venv\Scripts\python.exe' -m ruff check src tests scripts
& '.\.venv\Scripts\python.exe' -m pytest -q
```

Expected: Ruff prints `All checks passed!`; pytest reports no failures, errors, warnings caused by the change, or leaked background tasks.

- [ ] **Step 2: Run a real Telegram QR lifecycle probe without sensitive output**

Use the existing encrypted credentials in an isolated runtime copy. The probe may print only numeric TTL, display count, refresh count, and fixed success markers. It must not print a QR URL, token, API ID, API Hash, phone number, account identity, session string, dialog title, group name, or message content.

Run the source app classes long enough to require one automatic refresh and require:

```text
FIRST_TTL_SECONDS >= 5
FIRST_LABEL_POSITIVE=True
DISPLAY_COUNT >= 2
SECOND_TTL_SECONDS >= 5
PARALLEL_WAIT_PEAK=1
QR_TOKEN_OUTPUT=False
LIVE_QR_RECOVERY_OK
```

If encrypted credentials are unavailable, do not create or request new credentials. Record the missing local prerequisite and rely on the deterministic gateway/controller/UI integration test instead.

- [ ] **Step 3: Build and smoke-test both Windows packages**

Run the repository packaging entry point used by the previous release:

```powershell
& '.\scripts\build.ps1'
& '.\scripts\build-installer.ps1'
```

Expected output includes:

```text
PACKAGED_SMOKE_OK
INSTALLER_SMOKE_OK
```

Do not publish, move a release tag, or change online update pointers in this task; release publication requires the user's separate explicit instruction.

- [ ] **Step 4: Write verification evidence**

Create `docs/verification/v0.18.1-qr-immediate-expiry-recovery.md` with these concrete headings and populate them only from fresh command output:

```markdown
# v0.18.1 二维码立即过期修复验证

## 修复范围

## 定向测试

## 完整测试与 Ruff

## 真实 Telegram 生命周期验证

## 便携版与安装版冒烟测试

## 隐私边界

## 尚未执行的发布操作
```

The privacy section must state that no QR token, API credential, phone number, account identity, confirmation code, password, session, group name, or message content was recorded. The final section must state that no online release was published unless the user explicitly authorizes it later.

- [ ] **Step 5: Inspect the final diff and commit evidence**

Run:

```powershell
git diff --check
git status --short
git diff main...HEAD --stat
```

Expected: no whitespace errors and only QR recovery source, tests, design/plan, and verification evidence are changed.

Commit:

```powershell
git add docs/verification/v0.18.1-qr-immediate-expiry-recovery.md
git commit -m "docs: record QR expiry recovery evidence"
```

- [ ] **Step 6: Request code review before integration**

Invoke `superpowers:requesting-code-review`, review all changes from `main` to `HEAD`, resolve every correctness or privacy issue, and rerun the affected focused tests. Do not merge or publish until verification is fresh and the user authorizes those operations.
