# Search Single-Line Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore compact, fixed-height search results with a single-line elided summary and full text available by tooltip.

**Architecture:** Remove the dedicated wrapping delegate and all summary-height measurement signals/timers. Use Qt's default item delegate with `wordWrap=False`, `ElideRight`, the existing 78-pixel row height, and the model's existing full-text tooltip.

**Tech Stack:** Python 3.12, PySide6 model/view, pytest, pytest-qt

---

## File map

- Modify `src/telegram_downloader/ui/content_browser.py`: fixed table configuration and removal of summary measurement code.
- Delete `src/telegram_downloader/ui/wrapped_text.py`: no longer used by any view.
- Modify `tests/ui/test_content_browser.py`: fixed-height, elision, tooltip, and large-list assertions.
- Delete `tests/ui/test_wrapped_text.py`: obsolete wrapping behavior tests.
- Modify `docs/verification/2026-08-23-search-single-line-summary.md`: focused evidence.

### Task 1: Lock the required compact behavior with tests

**Files:**
- Modify: `tests/ui/test_content_browser.py`

- [ ] **Step 1: Write failing configuration tests**

```python
def test_result_summaries_are_single_line_elided(qtbot) -> None:
    page = ContentBrowserPage()
    qtbot.addWidget(page)

    assert page.result_table.wordWrap() is False
    assert page.result_table.textElideMode() is Qt.TextElideMode.ElideRight
    assert page.result_table.verticalHeader().defaultSectionSize() == 78
    assert not hasattr(page, "summary_delegate")
    assert not hasattr(page, "_row_resize_timer")
```

- [ ] **Step 2: Add the full-tooltip test**

Create a result with a 300-character excerpt, set it on `result_model`, and assert:

```python
index = page.result_model.index(0, 4)
assert page.result_model.data(index, Qt.ItemDataRole.DisplayRole) == excerpt
assert page.result_model.data(index, Qt.ItemDataRole.ToolTipRole) == excerpt
assert page.result_table.rowHeight(0) == 78
```

- [ ] **Step 3: Add the large-list no-resize test**

Set 10,000 model rows, scroll to the bottom, process Qt events, and assert every sampled row retains 78 pixels. Patch `QTableView.setRowHeight` after setup and assert scrolling causes zero calls.

- [ ] **Step 4: Run and verify failure against wrapping behavior**

Run: `pytest tests/ui/test_content_browser.py -k "single_line or no_resize" -q`

Expected: FAIL because wrapping, a custom delegate, and a resize timer are active.

- [ ] **Step 5: Commit the red tests**

```bash
git add tests/ui/test_content_browser.py
git commit -m "test: require compact search summaries"
```

### Task 2: Remove wrapping and dynamic row measurement

**Files:**
- Modify: `src/telegram_downloader/ui/content_browser.py`
- Delete: `src/telegram_downloader/ui/wrapped_text.py`
- Delete: `tests/ui/test_wrapped_text.py`

- [ ] **Step 1: Simplify table configuration**

Replace the wrapping block with:

```python
self.result_table.setIconSize(QSize(88, 60))
self.result_table.verticalHeader().setDefaultSectionSize(78)
self.result_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
self.result_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
self.result_table.setWordWrap(False)
self.result_table.setTextElideMode(Qt.TextElideMode.ElideRight)
```

Do not install an item delegate for column 4.

- [ ] **Step 2: Remove measurement-only imports and signals**

Remove `QRect`, `QStyleOptionViewItem`, `QTimer` imports only if no other code uses them. Remove connections to `_result_section_resized`, `_summary_model_reset`, `_summary_rows_changed`, and `_summary_data_changed`.

Keep the vertical scrollbar connection, but reduce its handler to thumbnail work:

```python
def _result_view_scrolled(self, _value: int) -> None:
    self.request_visible_thumbnails()
```

- [ ] **Step 3: Delete measurement methods and obsolete files**

Delete `_schedule_summary_resize`, `_resize_visible_result_rows`, `_result_section_resized`, `_summary_model_reset`, `_summary_rows_changed`, `_summary_data_changed`, and the summary-specific `changeEvent` branch. Delete `ui/wrapped_text.py` and its dedicated test file after confirming `rg -n "WrappedSummaryDelegate|wrapped_text" src tests` has no remaining production consumer.

- [ ] **Step 4: Run focused tests**

Run: `pytest tests/ui/test_content_browser.py tests/ui/test_content_models.py -q`

Expected: PASS.

- [ ] **Step 5: Run static checks**

Run: `ruff check src/telegram_downloader/ui/content_browser.py tests/ui/test_content_browser.py`

Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/telegram_downloader/ui/content_browser.py tests/ui/test_content_browser.py
git rm src/telegram_downloader/ui/wrapped_text.py tests/ui/test_wrapped_text.py
git commit -m "fix: keep search summaries on one line"
```

### Task 3: Search-list visual verification

**Files:**
- Create: `docs/verification/2026-08-23-search-single-line-summary.md`

- [ ] **Step 1: Render representative states**

Capture search results containing short and long summaries at 100% and 125% scaling in light and dark themes. Confirm fixed row heights, visible thumbnails, right-side ellipsis, no horizontal scrollbar, and full tooltip content.

- [ ] **Step 2: Verify bottom-of-list behavior**

Load 10,000 synthetic results, scroll to the final rows, and record that row height stays 78 and the last result remains reachable.

- [ ] **Step 3: Run focused tests again with fresh output**

Run: `pytest tests/ui/test_content_browser.py tests/ui/test_content_models.py -q`

Expected: PASS.

- [ ] **Step 4: Commit evidence**

```bash
git add docs/verification/2026-08-23-search-single-line-summary.md docs/verification/evidence/2026-08-23-search-single-line-summary
git commit -m "test: verify single-line search results"
```
