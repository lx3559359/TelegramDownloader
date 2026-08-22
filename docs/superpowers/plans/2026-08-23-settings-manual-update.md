# Settings Manual Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make manual update checking immediately discoverable in a dedicated settings tab, persist the last successful check time, and retain zero startup update requests.

**Architecture:** Store one validated UTC timestamp in `AppSettings`, render update controls in a dedicated `关于与更新` tab, and keep all network and installer behavior in the existing controller/update coordinator boundary. The settings dialog displays structured manual-check states but never starts a check by itself.

**Tech Stack:** Python 3.12, dataclasses/JSON settings, PySide6, qasync, signed update coordinator, pytest, pytest-qt

---

## File map

- Modify `src/telegram_downloader/settings.py`: validated last-successful-check field and backward-compatible persistence.
- Modify `src/telegram_downloader/ui/settings.py`: dedicated About/Update tab and structured UI state.
- Modify `src/telegram_downloader/controller.py`: successful-check timestamp persistence.
- Modify `src/telegram_downloader/app.py`: result mapping and dialog refresh without saving unrelated fields.
- Modify `tests/test_settings.py`: timestamp validation/migration/round-trip.
- Modify `tests/ui/test_settings_dialog.py`: visible tab, button, busy and result states.
- Modify `tests/update/test_update_coordinator.py`: manual-only result behavior.
- Modify `tests/test_app.py`: production wiring and no-startup-check assertions.

### Task 1: Persist the last successful manual check

**Files:**
- Modify: `src/telegram_downloader/settings.py`
- Modify: `tests/test_settings.py`

- [ ] **Step 1: Write failing settings tests**

```python
def test_settings_round_trip_last_successful_update_check(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    expected = AppSettings(last_successful_update_check_utc="2026-08-23T02:20:00Z")

    store.save(expected)

    assert store.load() == expected


@pytest.mark.parametrize("value", ["yesterday", "2026-08-23", "2026-08-23T10:20:00"])
def test_settings_reject_non_utc_update_check_time(value: str) -> None:
    with pytest.raises(SettingsError, match="最近更新检查时间"):
        AppSettings(last_successful_update_check_utc=value)
```

- [ ] **Step 2: Run and verify the constructor fails**

Run: `pytest tests/test_settings.py -k "update_check_time" -q`

Expected: FAIL because `AppSettings` has no field.

- [ ] **Step 3: Add strict UTC text validation**

Add `last_successful_update_check_utc: str = ""` to `AppSettings`. In `__post_init__`, allow the empty string or a timezone-aware ISO 8601 timestamp ending in `Z`:

```python
def _validate_update_check_time(value: str) -> None:
    if value == "":
        return
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SettingsError("最近更新检查时间必须是 UTC ISO 8601 文本")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise SettingsError("最近更新检查时间格式无效") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise SettingsError("最近更新检查时间必须使用 UTC")
```

The existing JSON loader must default the missing field to `""`; `SettingsStore.save()` persists it while continuing to remove `check_updates_on_startup`.

- [ ] **Step 4: Run the settings suite**

Run: `pytest tests/test_settings.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/telegram_downloader/settings.py tests/test_settings.py
git commit -m "feat: persist manual update check time"
```

### Task 2: Dedicated About and Update tab

**Files:**
- Modify: `src/telegram_downloader/ui/settings.py`
- Modify: `tests/ui/test_settings_dialog.py`

- [ ] **Step 1: Write failing visibility tests**

```python
def test_about_update_tab_is_visible_and_dedicated(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(), application_version="0.16.0")
    qtbot.addWidget(dialog)

    labels = [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())]
    assert labels == ["常规", "下载路径", "后台与通知", "关于与更新"]
    assert dialog.tabs.indexOf(dialog.about_update_tab) == 3
    assert dialog.update_check_button.isVisibleTo(dialog.about_update_tab)
    assert "0.16.0" in dialog.update_version_label.text()
```

- [ ] **Step 2: Run and verify the tab is missing**

Run: `pytest tests/ui/test_settings_dialog.py -k "about_update" -q`

Expected: FAIL because the controls are embedded in the General form.

- [ ] **Step 3: Move controls into a dedicated tab**

Create `self.about_update_tab`, add it fourth, and move these controls out of the General `QFormLayout`:

- Product name and subtitle labels.
- Current version and `stable` channel.
- Last successful check label.
- `检查更新` button.
- Result label with word wrap.

Keep `update_check_requested` as the only network intent emitted by the dialog.

- [ ] **Step 4: Add structured state methods**

Implement:

```python
def set_update_busy(self, busy: bool) -> None:
    self.update_check_button.setEnabled(not busy)
    self.update_check_button.setText("正在检查…" if busy else "检查更新")

def set_update_result(self, text: str, *, state: str = "neutral") -> None:
    self.update_status_label.setProperty("updateState", state)
    self.update_status_label.setText(text)
    self.update_status_label.style().unpolish(self.update_status_label)
    self.update_status_label.style().polish(self.update_status_label)

def set_last_successful_update_check(self, utc_text: str) -> None:
    self._settings = replace(
        self._settings,
        last_successful_update_check_utc=utc_text,
    )
    self.update_last_checked_label.setText(self._format_update_check_time(utc_text))
```

`values()` must preserve `self._settings.last_successful_update_check_utc` so saving unrelated settings cannot erase the newly recorded time.

- [ ] **Step 5: Test busy, success, blocked, and error display**

Run: `pytest tests/ui/test_settings_dialog.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_downloader/ui/settings.py tests/ui/test_settings_dialog.py
git commit -m "feat: add settings update tab"
```

### Task 3: Record successful checks without saving draft settings

**Files:**
- Modify: `src/telegram_downloader/controller.py`
- Modify: `src/telegram_downloader/app.py`
- Modify: `tests/update/test_update_coordinator.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write failing controller persistence test**

```python
@pytest.mark.asyncio
async def test_manual_update_success_records_utc_without_draft_settings() -> None:
    class Coordinator:
        async def startup(self, _prompt, _shutdown):
            return UpdateStartupResult.NO_UPDATE

    store = RecordingSettingsStore(AppSettings(concurrency=3))
    controller = AppController.for_test(
        settings_store=store,
        settings=store.current,
        update_coordinator=Coordinator(),
        utc_now=lambda: datetime(2026, 8, 23, 2, 20, tzinfo=UTC),
    )

    result = await controller.check_for_updates()

    assert result is UpdateStartupResult.NO_UPDATE
    assert store.saved.concurrency == 3
    assert store.saved.last_successful_update_check_utc == "2026-08-23T02:20:00Z"
```

- [ ] **Step 2: Run and verify the timestamp is not saved**

Run: `pytest tests/update/test_update_coordinator.py -k "records_utc" -q`

Expected: FAIL.

- [ ] **Step 3: Add an injected UTC clock and persistence helper**

Add `utc_now` to `AppController.__init__()` with default `lambda: datetime.now(UTC)`. After `NO_UPDATE` and every successfully validated available-update result, call:

```python
def _record_successful_update_check(self) -> str:
    stamp = self._utc_now().astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    updated = replace(self.settings, last_successful_update_check_utc=stamp)
    self.settings_store.save(updated)
    self.settings = updated
    return stamp
```

Do not call this helper for `BLOCKED`, transport exceptions, signature errors, download failures, or installer launch failures.

- [ ] **Step 4: Refresh the open dialog after a successful result**

In `app.py`'s `check_updates()` wrapper, after awaiting the controller task, map results to state and call `dialog.set_last_successful_update_check(controller.settings.last_successful_update_check_utc)` only when the stored timestamp changed.

Use these state mappings:

- `NO_UPDATE` → success, `当前已是最新正式版`
- `LAUNCHED` → success, `更新安装程序已启动`
- `DECLINED` → neutral, `已取消更新`
- `BLOCKED` → warning, `更新检查暂不可用，请稍后重试`
- exception → error, existing safe error mapping

Use the existing `UpdateStartupResult.DECLINED` value for cancellation; do not add a second cancellation enum.

- [ ] **Step 5: Prove draft settings are untouched**

In `tests/test_app.py`, change the open dialog concurrency widget without saving, click update check, and assert the persisted concurrency remains the controller's prior value while the timestamp changes.

Run: `pytest tests/update/test_update_coordinator.py tests/test_app.py -k "update" -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_downloader/controller.py src/telegram_downloader/app.py tests/update/test_update_coordinator.py tests/test_app.py
git commit -m "feat: report manual update checks in settings"
```

### Task 4: Prove startup remains update-silent

**Files:**
- Modify: `tests/test_app.py`
- Modify: `tests/update/test_update_coordinator.py`
- Create: `docs/verification/2026-08-23-settings-manual-update.md`

- [ ] **Step 1: Strengthen startup tests**

Use a coordinator whose `startup()` raises if called. Start the controller in foreground and background modes, process the event loop, and assert zero calls. Keep the source-level assertion that no startup or notification assembly invokes `controller.check_for_updates` automatically.

- [ ] **Step 2: Run focused update tests**

Run: `pytest tests/test_settings.py tests/ui/test_settings_dialog.py tests/update/test_update_coordinator.py tests/test_app.py -k "update or settings" -q`

Expected: PASS.

- [ ] **Step 3: Run Ruff**

Run: `ruff check src/telegram_downloader/settings.py src/telegram_downloader/ui/settings.py src/telegram_downloader/controller.py src/telegram_downloader/app.py tests/test_settings.py tests/ui/test_settings_dialog.py tests/update/test_update_coordinator.py tests/test_app.py`

Expected: `All checks passed!`

- [ ] **Step 4: Record evidence**

Document the dedicated tab visibility, no-update/new-update/error result coverage, button recovery, persisted UTC timestamp, and zero startup calls. Do not perform or claim a production release in this task.

- [ ] **Step 5: Commit**

```bash
git add tests/test_app.py tests/update/test_update_coordinator.py docs/verification/2026-08-23-settings-manual-update.md
git commit -m "test: verify discoverable manual updates"
```
