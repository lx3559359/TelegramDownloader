# Account Content Selection and W3 White-Card Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复账号内容搜索结果无法勾选的问题，并按已确认的 W3 方案重构该页面，使选择操作易点按、文字不越界、结果列宽稳定、白色卡片具有清晰立体层级。

**Architecture:** 保持 `SearchResultTableModel` 作为选择状态的唯一数据源，在首列安装一个只负责鼠标/键盘切换的专用委托；把账号内容页拆为左侧列表卡、右上筛选卡、右下结果卡，并通过账号内容页专属对象名和后代选择器隔离浅色主题。业务信号、搜索/预览/入队数据流及其他页面的深色主题不变。

**Tech Stack:** Python 3.12、PySide6/Qt Model-View、pytest-qt、Ruff、PyInstaller、PowerShell

---

**Execution prerequisite:** 开始 Task 1 前先调用 `superpowers:using-git-worktrees`，在隔离工作树中基于当前 `main` 创建 `codex/account-content-white-cards`；随后按用户选择使用 `superpowers:subagent-driven-development` 或在当前任务内逐项执行。

## Task 1: Normalize Qt check-state values in the model

**Files:**
- Modify: `tests/ui/test_content_models.py`
- Modify: `src/telegram_downloader/ui/content_models.py`

- [ ] **Step 1: Add a failing regression test for the integer value sent by Qt**

Add a focused test beside `test_result_model_selection_roles_and_disabled_rows`:

```python
def test_result_model_accepts_integer_check_state_once(qtbot) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    model = SearchResultTableModel()
    values = search_results(now)
    model.set_results(values)
    changed: list[tuple[str, bool]] = []
    model.selection_changed.connect(
        lambda result_id, selected: changed.append((result_id, selected))
    )
    index = model.index(0, 0)

    assert model.setData(index, 2, Qt.ItemDataRole.CheckStateRole)
    assert model.data(index, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    assert changed == [(values[0].id, True)]

    assert model.setData(index, 2, Qt.ItemDataRole.CheckStateRole)
    assert changed == [(values[0].id, True)]
```

- [ ] **Step 2: Run the focused test and confirm the current bug**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_models.py::test_result_model_accepts_integer_check_state_once -q
```

Expected: FAIL because integer `2` is interpreted as unchecked.

- [ ] **Step 3: Normalize the incoming value before comparison**

In `SearchResultTableModel.setData()`, replace the direct enum comparison with:

```python
try:
    requested_state = Qt.CheckState(value)
except (TypeError, ValueError):
    return False
requested = requested_state == Qt.CheckState.Checked
```

Keep the existing early guards, no-op handling, `dataChanged`, and `selection_changed` emission order unchanged.

- [ ] **Step 4: Run model tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_models.py -q
```

Expected: PASS, including enum callers, integer callers, and disabled rows.

- [ ] **Step 5: Commit the model fix**

```powershell
git add src/telegram_downloader/ui/content_models.py tests/ui/test_content_models.py
git commit -m "fix: normalize result checkbox state"
```

## Task 2: Make the whole selection cell clickable and keyboard accessible

**Files:**
- Create: `src/telegram_downloader/ui/check_delegate.py`
- Modify: `src/telegram_downloader/ui/content_browser.py`
- Modify: `tests/ui/test_content_browser.py`

- [ ] **Step 1: Add failing interaction tests**

Add a helper that displays one result and returns the first selection index. Then add tests that:

```python
def test_selection_cell_click_and_space_toggle_once(qtbot) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    page = ContentBrowserPage()
    page.resize(996, 650)
    qtbot.addWidget(page)
    page.show()
    page.set_results([result(now, "r1", 1)])
    qtbot.waitUntil(lambda: page.result_table.visualRect(
        page.result_model.index(0, 0)
    ).isValid())
    changed: list[tuple[str, bool]] = []
    page.result_model.selection_changed.connect(
        lambda result_id, selected: changed.append((result_id, selected))
    )
    index = page.result_model.index(0, 0)
    rect = page.result_table.visualRect(index)

    qtbot.mouseClick(
        page.result_table.viewport(),
        Qt.MouseButton.LeftButton,
        pos=rect.topRight() - QPoint(6, -rect.height() // 2),
    )
    assert page.result_model.data(index, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    assert changed == [("r1", True)]

    page.result_table.setCurrentIndex(index)
    page.result_table.setFocus()
    qtbot.keyClick(page.result_table, Qt.Key.Key_Space)
    assert page.result_model.data(index, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked
    assert changed == [("r1", True), ("r1", False)]
```

Also add a parameterized test using `replace(result(...), available=False)` and `replace(result(...), queued=True)`; mouse release anywhere in column 0 and Space must leave the check state unchanged and emit no signal. Use `QPoint` from `PySide6.QtCore`.

- [ ] **Step 2: Run the interaction tests and confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_browser.py -k "selection_cell or disabled_selection" -q
```

Expected: FAIL because the stock delegate only toggles its small indicator and does not own the full-cell target consistently.

- [ ] **Step 3: Implement a dedicated first-column delegate**

Create `check_delegate.py`:

```python
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem


class FullCellCheckDelegate(QStyledItemDelegate):
    def editorEvent(self, event, model, option: QStyleOptionViewItem, index) -> bool:
        if not index.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            return False
        if event.type() == QEvent.Type.MouseButtonRelease:
            if not isinstance(event, QMouseEvent):
                return False
            if event.button() != Qt.MouseButton.LeftButton:
                return False
            if not option.rect.contains(event.position().toPoint()):
                return False
        elif event.type() == QEvent.Type.KeyPress:
            if not isinstance(event, QKeyEvent):
                return False
            if event.key() not in (Qt.Key.Key_Space, Qt.Key.Key_Select):
                return False
        else:
            return False

        current = Qt.CheckState(
            model.data(index, Qt.ItemDataRole.CheckStateRole)
        )
        requested = (
            Qt.CheckState.Unchecked
            if current == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
        return model.setData(
            index,
            requested,
            Qt.ItemDataRole.CheckStateRole,
        )
```

In `ContentBrowserPage._build_search_panel()`, retain the delegate on the page and install it only for column 0:

```python
self.selection_delegate = FullCellCheckDelegate(self.result_table)
self.result_table.setItemDelegateForColumn(0, self.selection_delegate)
```

Do not change `SelectRows`, `NoEditTriggers`, or the preview-column double-click connection.

- [ ] **Step 4: Run interaction and preview tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_browser.py -k "selection or preview" -q
```

Expected: PASS; one user action emits one selection signal, disabled rows do nothing, and preview double-click still emits the result id.

- [ ] **Step 5: Commit the interaction fix**

```powershell
git add src/telegram_downloader/ui/check_delegate.py src/telegram_downloader/ui/content_browser.py tests/ui/test_content_browser.py
git commit -m "fix: expand result selection hit target"
```

## Task 3: Split the account-content workspace into three cards

**Files:**
- Modify: `src/telegram_downloader/ui/content_browser.py`
- Modify: `tests/ui/test_content_browser.py`

- [ ] **Step 1: Add failing structure and minimum-size tests**

Extend `test_page_contains_content_browser_controls` or add a focused test asserting:

```python
assert page.objectName() == "accountContentPage"
assert page.dialog_card.objectName() == "accountContentCard"
assert page.filter_card.objectName() == "accountContentCard"
assert page.results_card.objectName() == "accountContentCard"
assert page.dialog_card.minimumWidth() == 210
assert page.dialog_card.maximumWidth() == 270
assert page.search_column.minimumWidth() >= 680
assert page.date_from.minimumWidth() >= 132
assert page.date_to.minimumWidth() >= 132
assert page.limit_input.minimumWidth() >= 90
```

Verify that `error_label.parentWidget()` is `filter_card`, so expanding error text cannot cover the result card.

- [ ] **Step 2: Run the structure test and confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_browser.py -k "card_structure" -q
```

Expected: FAIL because the page currently has two monolithic dark panels and one horizontal filter row.

- [ ] **Step 3: Refactor the page composition without changing signals**

In `ContentBrowserPage._build_ui()`:

- set the page object name to `accountContentPage`;
- keep title, subtitle, connection hint, and reconnect button at the top;
- retain the horizontal `QSplitter`, but add a transparent `self.dialog_column` wrapper containing `self.dialog_card`; set the card min/max to 210/270, set splitter sizes so the card starts at about 230px after wrapper margins, and give `self.search_column` a minimum width of 680;
- give both splitter wrappers 10px shadow-safe inner margins and keep 12px between the two right cards, so the 40px blurred effects retain a visible falloff instead of being cut at card edges.

Replace `_build_search_panel()` with a light wrapper that composes `_build_filter_card()` and `_build_results_card()`. Move only widgets between containers; preserve all existing public widget attributes and signal connections.

Use a `QGridLayout` in the filter card:

```python
filter_grid = QGridLayout()
filter_grid.addWidget(QLabel("开始日期"), 0, 0)
filter_grid.addWidget(self.date_from, 0, 1)
filter_grid.addWidget(QLabel("结束日期（含）"), 0, 2)
filter_grid.addWidget(self.date_to, 0, 3)
filter_grid.addWidget(QLabel("数量上限"), 0, 4)
filter_grid.addWidget(self.limit_input, 0, 5)
filter_grid.setColumnStretch(6, 1)
```

Set both date editors to minimum width 132 and the limit spin box to minimum width 90. Put the media label and six media checkboxes on a separate row. Create `error_label` inside the filter card after progress widgets.

- [ ] **Step 4: Run all account-content page tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_browser.py -q
```

Expected: PASS; existing search, history, thumbnail, preview, progress, and queue behavior remains intact.

- [ ] **Step 5: Commit the card and filter layout**

```powershell
git add src/telegram_downloader/ui/content_browser.py tests/ui/test_content_browser.py
git commit -m "refactor: structure account content cards"
```

## Task 4: Stabilize result columns and text overflow behavior

**Files:**
- Modify: `src/telegram_downloader/ui/content_models.py`
- Modify: `src/telegram_downloader/ui/content_browser.py`
- Modify: `tests/ui/test_content_models.py`
- Modify: `tests/ui/test_content_browser.py`

- [ ] **Step 1: Add failing model and table-layout tests**

In `test_content_models.py`, assert that the summary column provides the full excerpt:

```python
assert model.data(
    model.index(0, 3), Qt.ItemDataRole.ToolTipRole
) == results[0].excerpt
```

In `test_content_browser.py`, replace the old 112×84/96px expectations and add:

```python
assert page.result_table.iconSize() == QSize(88, 60)
assert page.result_table.verticalHeader().defaultSectionSize() == 78
assert page.result_table.wordWrap() is False
assert page.result_table.textElideMode() == Qt.TextElideMode.ElideRight

header = page.result_table.horizontalHeader()
fixed_widths = {0: 52, 1: 96, 2: 132, 4: 58, 5: 82, 6: 64}
for column, width in fixed_widths.items():
    assert header.sectionResizeMode(column) == QHeaderView.ResizeMode.Fixed
    assert page.result_table.columnWidth(column) == width
assert header.sectionResizeMode(3) == QHeaderView.ResizeMode.Stretch
```

Show the page at `996 × 650`, load a result with a long excerpt, wait for layout, and assert `result_table.horizontalScrollBar().maximum() == 0` plus a positive summary-column width.

- [ ] **Step 2: Run focused model/layout tests and confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_models.py tests/ui/test_content_browser.py -k "tooltip or column or content_browser_controls" -q
```

Expected: FAIL on old icon/row sizes, `ResizeToContents`, wrapping, and generic filename tooltip.

- [ ] **Step 3: Implement deterministic sizing and summary tooltip**

In `SearchResultTableModel.data()`:

```python
if role == Qt.ItemDataRole.ToolTipRole:
    if index.column() == 3:
        return result.excerpt
    return result.original_name
```

Configure the result table as follows:

```python
self.result_table.setIconSize(QSize(88, 60))
self.result_table.verticalHeader().setDefaultSectionSize(78)
self.result_table.setWordWrap(False)
self.result_table.setTextElideMode(Qt.TextElideMode.ElideRight)
result_header = self.result_table.horizontalHeader()
for column, width in {0: 52, 1: 96, 2: 132, 4: 58, 5: 82, 6: 64}.items():
    result_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
    self.result_table.setColumnWidth(column, width)
result_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
```

Do not apply automatic content resizing to any fixed column.

- [ ] **Step 4: Run model and page tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_models.py tests/ui/test_content_browser.py -q
```

Expected: PASS with no horizontal result-table scrollbar at the tested minimum content size.

- [ ] **Step 5: Commit stable table sizing**

```powershell
git add src/telegram_downloader/ui/content_models.py src/telegram_downloader/ui/content_browser.py tests/ui/test_content_models.py tests/ui/test_content_browser.py
git commit -m "style: stabilize account result table layout"
```

## Task 5: Apply the W3 page-only white-card theme and real shadows

**Files:**
- Modify: `src/telegram_downloader/ui/content_browser.py`
- Modify: `src/telegram_downloader/ui/theme.py`
- Modify: `tests/ui/test_content_browser.py`

- [ ] **Step 1: Add failing scope and shadow tests**

Add assertions that each of the three card attributes has a `QGraphicsDropShadowEffect` with the approved depth range:

```python
for card in (page.dialog_card, page.filter_card, page.results_card):
    effect = card.graphicsEffect()
    assert isinstance(effect, QGraphicsDropShadowEffect)
    assert 36 <= effect.blurRadius() <= 42
    assert 6 <= effect.yOffset() <= 8
```

Add a stylesheet scope regression assertion in a theme test or `test_content_browser.py`:

```python
assert "QWidget#accountContentPage" in DARK_STYLESHEET
assert "QFrame#accountContentCard" in DARK_STYLESHEET
assert "QFrame#contentPanel" in DARK_STYLESHEET
```

This proves the new light rules are page-specific while the existing dark panel rules remain available to other pages.

- [ ] **Step 2: Run the theme test and confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_browser.py -k "shadow or theme_scope" -q
```

Expected: FAIL because cards do not yet carry graphics effects or W3 selectors.

- [ ] **Step 3: Add a reusable card-shadow helper**

Import `QColor` and `QGraphicsDropShadowEffect`, then add:

```python
@staticmethod
def _apply_card_shadow(card: QFrame) -> None:
    shadow = QGraphicsDropShadowEffect(card)
    shadow.setBlurRadius(40)
    shadow.setOffset(0, 7)
    shadow.setColor(QColor(35, 50, 70, 78))
    card.setGraphicsEffect(shadow)
```

Call it once for `dialog_card`, `filter_card`, and `results_card`. Do not add per-row shadows.

- [ ] **Step 4: Add W3 selectors without replacing the global dark theme**

Append account-page descendant rules to `DARK_STYLESHEET` using these approved tokens:

- `QWidget#accountContentPage`: `qlineargradient` canvas from `#E3EBF4` to `#DCE6F0`;
- `QFrame#accountContentCard`: card gradient `#FFFFFF` to `#F5F8FC`, border `#D3DEE9`, 10–12px radius;
- page labels: primary `#1F2A3D`, secondary `#64748B`, table header `#596B82`;
- page inputs, tabs, tables, and list views: white/near-white surfaces with cool-gray borders;
- hovered/selected rows: pale cyan only;
- `#primaryButton`: retain cyan `#17A8C2` with a darker hover/pressed state.

Every light override must begin with `QWidget#accountContentPage` or target `QFrame#accountContentCard`; do not alter shared selectors such as bare `QLabel`, `QTableView`, or `QFrame#contentPanel`.

- [ ] **Step 5: Run UI tests and Ruff**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui/test_content_browser.py tests/ui/test_content_models.py -q
.\.venv\Scripts\ruff.exe check src tests
```

Expected: both commands PASS.

- [ ] **Step 6: Commit the W3 visual treatment**

```powershell
git add src/telegram_downloader/ui/content_browser.py src/telegram_downloader/ui/theme.py tests/ui/test_content_browser.py
git commit -m "style: add elevated white account content cards"
```

## Task 6: Full verification, frozen build, and visual acceptance

**Files:**
- Verify: `src/telegram_downloader/ui/content_models.py`
- Verify: `src/telegram_downloader/ui/check_delegate.py`
- Verify: `src/telegram_downloader/ui/content_browser.py`
- Verify: `src/telegram_downloader/ui/theme.py`
- Verify: `tests/ui/test_content_models.py`
- Verify: `tests/ui/test_content_browser.py`
- Update if needed: `docs/superpowers/specs/2026-08-16-account-content-selection-white-card-layout-design.md`

- [ ] **Step 1: Run the complete automated test and lint suite**

Run:

```powershell
.\scripts\test.ps1
```

Expected: all pytest tests pass and Ruff reports no errors.

- [ ] **Step 2: Build and smoke-test the frozen application**

Close only the project-local packaged executable if it is running, then run:

```powershell
.\scripts\build.ps1
```

Expected: PyInstaller build succeeds, `scripts/smoke.ps1` passes, portable ZIP validation passes, and project-local user data hashes are preserved.

- [ ] **Step 3: Perform real-window interaction acceptance at both target sizes**

Launch `dist\TelegramDownloader\TelegramDownloader.exe` with the existing test-safe local profile and verify at `1180 × 720` and `1280 × 780`:

- click near the left and right edges of every available first-column cell and confirm the checkbox plus “已选 N 项” update once;
- focus the first column and press Space twice, confirming one transition per press;
- confirm unavailable and queued rows cannot be changed;
- exercise 全选、反选、加入下载队列, and double-click preview;
- verify every filter label and fixed result column is fully readable, long summaries are elided only in the summary column, and full summary text appears in the tooltip;
- verify the three W3 cards have visible, unclipped depth against the pale gray-blue canvas and other navigation pages remain dark.

- [ ] **Step 4: Compare same-state screenshots**

Capture the updated account-content page at the same viewport and populated-result state as the supplied reference. Place the reference and updated screenshot side by side, then inspect for cropped text, bad spacing, wrong radii, shadow clipping, uneven card alignment, and selection-target regressions. Fix and repeat the focused tests plus screenshot comparison if any mismatch remains.

- [ ] **Step 5: Review the final diff for scope and accidental artifacts**

Run:

```powershell
git diff --check
git status --short
git diff --stat 29321ba..HEAD
git diff 29321ba..HEAD -- src/telegram_downloader/ui tests/ui docs/superpowers/specs
```

Expected: no whitespace errors, no version/tag/release changes, no temporary screenshots or profiles staged, and no changes outside the approved UI/model/test/spec scope.

- [ ] **Step 6: Request code review and address only verified findings**

Use the `superpowers:requesting-code-review` skill against the implementation range. Re-run the relevant focused tests after every accepted correction.

- [ ] **Step 7: Create the final implementation commit if verification required changes**

```powershell
git add src/telegram_downloader/ui tests/ui docs/superpowers/specs
git commit -m "test: verify account content selection layout"
```

Skip this commit when the worktree is already clean. Do not push, tag, bump a version, or publish in this task.
