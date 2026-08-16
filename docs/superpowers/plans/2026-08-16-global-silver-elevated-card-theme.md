# Global Silver Elevated Card Theme Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the complete TelegramDownloader UI—main shell, every functional page, side rails, and every custom dialog—to one light silver-gradient theme with strong major-card shadows and medium nested-card shadows.

**Architecture:** Keep all color and Qt stylesheet rules in `ui/theme.py`, and add one focused `ui/effects.py` module that owns the two elevation specifications and installs independent `QGraphicsDropShadowEffect` instances. Pages declare semantic surfaces through stable object names and exposed widget attributes; business models, signals, storage, and Telegram behavior remain unchanged.

**Tech Stack:** Python 3.12, PySide6 6.11, Qt Style Sheets, `QGraphicsDropShadowEffect`, pytest, pytest-qt, Ruff, PyInstaller.

---

## File structure

- Create `src/telegram_downloader/ui/effects.py`: elevation enum, immutable shadow specifications, and idempotent effect installation.
- Create `tests/ui/test_effects.py`: isolated effect-level and instance-ownership tests.
- Create `tests/ui/test_theme.py`: global palette and selector contract tests.
- Modify `src/telegram_downloader/ui/theme.py`: canonical light-silver application stylesheet plus compatibility alias.
- Modify `src/telegram_downloader/ui/main.py`: elevated shell rails, task source/queue/detail cards, integrity subcard, and statistic subcards.
- Modify `src/telegram_downloader/ui/content_browser.py`: migrate the existing W3 cards to the shared elevation system.
- Modify `src/telegram_downloader/ui/subscriptions.py`: elevate subscription and diagnostic group surfaces, including the editor dialog.
- Modify `src/telegram_downloader/ui/diagnostics.py`: split progress, results, and actions into the confirmed hierarchy.
- Modify `src/telegram_downloader/ui/login.py`: add an elevated dialog surface.
- Modify `src/telegram_downloader/ui/settings.py`: add an elevated dialog surface.
- Modify `src/telegram_downloader/ui/media_preview.py`: apply the global theme and add an elevated preview surface.
- Modify `src/telegram_downloader/ui/update_dialog.py`: apply the global theme and add an elevated update surface.
- Modify `tests/ui/test_main_window.py`, `tests/ui/test_content_browser.py`, `tests/ui/test_subscriptions.py`, `tests/ui/test_diagnostics.py`, `tests/ui/test_login_dialog.py`, `tests/ui/test_settings_dialog.py`, `tests/ui/test_media_preview.py`, and `tests/ui/test_update_dialog.py`: structural, elevation, and layout regressions.
- Create `docs/verification/v0.11.2-global-silver-elevated-card-theme.md`: measured automated and real-window evidence without changing the release version.

## Task 1: Shared elevation primitives

**Files:**
- Create: `tests/ui/test_effects.py`
- Create: `src/telegram_downloader/ui/effects.py`

- [ ] **Step 1: Write the failing elevation tests**

```python
from PySide6.QtWidgets import QFrame, QGraphicsDropShadowEffect

from telegram_downloader.ui.effects import ElevationLevel, apply_elevation


def test_major_and_secondary_elevations_have_distinct_strengths(qtbot) -> None:
    major = QFrame()
    secondary = QFrame()
    qtbot.addWidget(major)
    qtbot.addWidget(secondary)

    major_effect = apply_elevation(major, ElevationLevel.MAJOR)
    secondary_effect = apply_elevation(secondary, ElevationLevel.SECONDARY)

    assert isinstance(major_effect, QGraphicsDropShadowEffect)
    assert isinstance(secondary_effect, QGraphicsDropShadowEffect)
    assert major.property("elevation") == "major"
    assert secondary.property("elevation") == "secondary"
    assert major_effect.blurRadius() == 40
    assert major_effect.offset().x() == 1
    assert major_effect.offset().y() == 8
    assert major_effect.color().alpha() == 116
    assert secondary_effect.blurRadius() == 26
    assert secondary_effect.offset().x() == 0
    assert secondary_effect.offset().y() == 5
    assert secondary_effect.color().alpha() == 84


def test_each_card_owns_one_idempotent_shadow_effect(qtbot) -> None:
    first = QFrame()
    second = QFrame()
    qtbot.addWidget(first)
    qtbot.addWidget(second)

    first_effect = apply_elevation(first, ElevationLevel.MAJOR)
    repeated = apply_elevation(first, ElevationLevel.MAJOR)
    second_effect = apply_elevation(second, ElevationLevel.MAJOR)

    assert repeated is first_effect
    assert second_effect is not first_effect
    assert first.graphicsEffect() is first_effect
    assert second.graphicsEffect() is second_effect
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/ui/test_effects.py -q
```

Expected: collection fails because `telegram_downloader.ui.effects` does not exist.

- [ ] **Step 3: Implement the minimal shared elevation module**

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QWidget


class ElevationLevel(str, Enum):
    MAJOR = "major"
    SECONDARY = "secondary"


@dataclass(frozen=True, slots=True)
class ShadowSpec:
    blur_radius: float
    x_offset: float
    y_offset: float
    color: tuple[int, int, int, int]


_SHADOWS = {
    ElevationLevel.MAJOR: ShadowSpec(40, 1, 8, (38, 52, 72, 116)),
    ElevationLevel.SECONDARY: ShadowSpec(26, 0, 5, (49, 65, 85, 84)),
}


def apply_elevation(
    widget: QWidget,
    level: ElevationLevel,
) -> QGraphicsDropShadowEffect:
    current = widget.graphicsEffect()
    if (
        isinstance(current, QGraphicsDropShadowEffect)
        and widget.property("elevation") == level.value
    ):
        return current
    spec = _SHADOWS[level]
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(spec.blur_radius)
    effect.setOffset(spec.x_offset, spec.y_offset)
    effect.setColor(QColor(*spec.color))
    widget.setGraphicsEffect(effect)
    widget.setProperty("elevation", level.value)
    return effect
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/ui/test_effects.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit the primitive**

```powershell
git add src/telegram_downloader/ui/effects.py tests/ui/test_effects.py
git commit -m "feat: add shared card elevation effects"
```

## Task 2: Global light-silver stylesheet

**Files:**
- Create: `tests/ui/test_theme.py`
- Modify: `src/telegram_downloader/ui/theme.py`

- [ ] **Step 1: Write the failing global-theme contract tests**

```python
from telegram_downloader.ui.theme import APP_STYLESHEET, DARK_STYLESHEET


def test_application_theme_is_light_silver_and_keeps_compatibility_alias() -> None:
    assert DARK_STYLESHEET == APP_STYLESHEET
    for token in (
        "#F7F9FC",
        "#E6EBF2",
        "#FFFFFF",
        "#F8FAFC",
        "#EEF2F6",
        "#CCD5DF",
        "#17A8C2",
    ):
        assert token in APP_STYLESHEET
    for retired in ("#0b111b", "#0f1724", "#111b2a", "#0e1724"):
        assert retired not in APP_STYLESHEET


def test_application_theme_covers_every_surface_family() -> None:
    for selector in (
        "QMainWindow",
        "QDialog",
        "QWidget#navRail",
        "QWidget#statsRail",
        "QFrame#elevatedCard",
        "QFrame#elevatedSubCard",
        "QPushButton#navButton[active=\"true\"]",
        "QLineEdit",
        "QComboBox",
        "QTableView",
        "QListView",
        "QScrollArea",
        "QTextBrowser",
        "QStatusBar",
    ):
        assert selector in APP_STYLESHEET
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/ui/test_theme.py -q
```

Expected: import fails because `APP_STYLESHEET` is not defined.

- [ ] **Step 3: Replace the dark palette with the canonical application stylesheet**

Keep `ensure_cjk_font()` unchanged. Replace the existing stylesheet definition with `APP_STYLESHEET`, then set `DARK_STYLESHEET = APP_STYLESHEET` below it. The stylesheet must use these exact surface rules as its base:

```python
APP_STYLESHEET = """
QMainWindow, QDialog {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #F7F9FC, stop: 1 #E6EBF2
    );
    color: #1F2937;
}
QWidget {
    background: transparent;
    color: #1F2937;
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 13px;
}
QWidget#navRail, QWidget#statsRail,
QFrame#elevatedCard, QFrame#dialogSurface,
QFrame#accountContentCard, QFrame#contentPanel {
    border: 1px solid #CCD5DF;
    border-radius: 14px;
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #FFFFFF, stop: 0.18 #F8FAFC, stop: 1 #EEF2F6
    );
}
QFrame#elevatedSubCard {
    border: 1px solid #D5DDE6;
    border-radius: 11px;
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #FFFFFF, stop: 1 #F1F4F8
    );
}
"""

DARK_STYLESHEET = APP_STYLESHEET
```

Append these explicit light control rules to the same string. `QFrame#accountContentCard` and `QFrame#contentPanel` are temporary compatibility selectors so this commit remains green; Task 4 removes them after migrating the widgets.

```css
QLabel#pageTitle { color: #172033; font-size: 24px; font-weight: 750; }
QLabel#sectionTitle { color: #1F2937; font-size: 14px; font-weight: 650; }
QLabel#muted, QLabel#fieldCaption, QLabel#brandCaption { color: #66758A; }
QLabel#accountBadge {
    padding: 6px 11px; border: 1px solid #C5D0DC; border-radius: 13px;
    background: #F1F4F8; color: #58677A; font-weight: 600;
}
QLabel#accountBadge[connected="true"] {
    border-color: #8DD9CC; background: #E7F8F3; color: #13725F;
}
QLabel#contentHint {
    padding: 8px 11px; border: 1px solid #C7DDE7; border-radius: 7px;
    background: #EDF8FB; color: #376578;
}
QLabel#diagnosticStatus {
    padding: 8px 11px; border: 1px solid #C5D0DC; border-radius: 7px;
    background: #F1F4F8; color: #475569; font-weight: 650;
}
QLabel#diagnosticStatus[status="running"] { border-color: #8DD6E2; background: #E7F8FB; color: #087F96; }
QLabel#diagnosticStatus[status="passed"] { border-color: #9DD9C7; background: #EAF8F2; color: #176B55; }
QLabel#diagnosticStatus[status="warning"] { border-color: #E7C878; background: #FFF8E5; color: #8A6418; }
QLabel#diagnosticStatus[status="failed"] { border-color: #E7A8B3; background: #FFF0F3; color: #A33C50; }
QLabel#errorText, QLabel#errorBanner {
    padding: 8px 11px; border: 1px solid #E7A8B3; border-radius: 7px;
    background: #FFF0F3; color: #A33C50;
}
QLabel#selectionSummary { color: #087F96; font-weight: 650; }

QPushButton {
    min-height: 34px; padding: 0 13px; border: 1px solid #C7D1DC;
    border-radius: 7px; background: #F7F9FC; color: #334155; font-weight: 600;
}
QPushButton:hover { border-color: #92A9BB; background: #EEF4F8; color: #213547; }
QPushButton:pressed { background: #E3EAF1; }
QPushButton:disabled { border-color: #DCE3EA; background: #EDF1F5; color: #9AA8B8; }
QPushButton#primaryButton { border-color: #17A8C2; background: #17A8C2; color: #FFFFFF; font-weight: 750; }
QPushButton#primaryButton:hover { border-color: #0E8FA8; background: #0E8FA8; }
QPushButton#navButton { min-height: 42px; padding-left: 14px; border: 1px solid transparent; background: transparent; text-align: left; color: #66758A; }
QPushButton#navButton:hover { border-color: #D2DCE6; background: #EEF3F8; color: #26384A; }
QPushButton#navButton[active="true"] {
    border-color: #8DD6E2; background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #F4FDFF,stop:1 #DDF5F8);
    color: #087F96;
}

QLineEdit, QDateEdit, QSpinBox, QComboBox {
    min-height: 36px; padding: 0 10px; border: 1px solid #C7D1DC;
    border-radius: 7px; background: #FFFFFF; color: #1F2937;
    selection-background-color: #9EE5EF;
}
QComboBox QAbstractItemView {
    border: 1px solid #C7D1DC; background: #FFFFFF; color: #1F2937;
    selection-background-color: #DDF5F8; selection-color: #173744;
}
QDateEdit::drop-down, QSpinBox::up-button, QSpinBox::down-button, QComboBox::drop-down {
    width: 22px; border: 0; background: #EDF3F8;
}
QPushButton:focus, QLineEdit:focus, QDateEdit:focus, QSpinBox:focus,
QComboBox:focus, QCheckBox:focus, QTableView:focus, QListView:focus {
    border: 1px solid #17A8C2;
}
QCheckBox { spacing: 7px; color: #334155; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #9FB1C4; border-radius: 4px; background: #FFFFFF; }
QCheckBox::indicator:checked { border-color: #17A8C2; background: #17A8C2; }

QTableView, QListView, QTextBrowser, QScrollArea {
    border: 1px solid #D5DEE7; border-radius: 8px; background: #FFFFFF;
    color: #25344A; selection-background-color: #DDF5F8; selection-color: #173744;
}
QTableView { alternate-background-color: #F7FAFC; gridline-color: #E2E8F0; }
QListView::item { min-height: 38px; padding: 0 8px; border-bottom: 1px solid #EDF1F5; }
QTableView::item:hover, QListView::item:hover { background: #EEF9FB; }
QHeaderView::section {
    min-height: 34px; padding: 0 8px; border: 0; border-bottom: 1px solid #D5DEE7;
    background: #EEF3F8; color: #596B82; font-weight: 650;
}
QTabWidget::pane { border: 1px solid #D5DEE7; border-radius: 8px; background: #FFFFFF; }
QTabBar::tab { min-width: 92px; min-height: 32px; padding: 0 12px; border: 1px solid #D5DEE7; background: #EDF2F7; color: #64748B; }
QTabBar::tab:selected { border-color: #8DD6E2; background: #E4F7FA; color: #087F96; }
QProgressBar { min-height: 8px; max-height: 8px; border: 0; border-radius: 4px; background: #DCE6EF; color: transparent; }
QProgressBar::chunk { border-radius: 4px; background: #17A8C2; }
QSplitter::handle { background: transparent; }
QScrollBar:vertical { width: 10px; border: 0; background: #EDF2F7; }
QScrollBar::handle:vertical { min-height: 28px; border-radius: 5px; background: #A6B8CA; }
QStatusBar { border-top: 1px solid #D5DEE7; background: #F2F5F8; color: #66758A; }
QMessageBox { background: #F7F9FC; color: #1F2937; }
```

Remove the old account-content-only palette block but keep its layout selectors (`accountContentPage`, transparent columns, and splitter handles) without color duplication.

- [ ] **Step 4: Run theme and existing account-content tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/ui/test_theme.py tests/ui/test_content_browser.py -q
```

Expected: theme and all existing account-content tests pass because the temporary compatibility selector remains available.

- [ ] **Step 5: Commit the theme foundation**

```powershell
git add src/telegram_downloader/ui/theme.py tests/ui/test_theme.py
git commit -m "style: establish global silver application theme"
```

## Task 3: Main shell, task center, and real-time overview

**Files:**
- Modify: `tests/ui/test_main_window.py`
- Modify: `src/telegram_downloader/ui/main.py`

- [ ] **Step 1: Add failing structural and elevation tests**

Add `QGraphicsDropShadowEffect` and `ElevationLevel` imports, then add:

```python
def test_main_shell_and_task_cards_use_confirmed_elevation_hierarchy(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    for panel in (window.navigation_panel, window.statistics_panel):
        assert isinstance(panel.graphicsEffect(), QGraphicsDropShadowEffect)
        assert panel.property("elevation") == ElevationLevel.SECONDARY.value

    for card in (
        window.source_card,
        window.task_queue_card,
        window.task_detail_card,
    ):
        assert card.objectName() == "elevatedCard"
        assert card.property("elevation") == ElevationLevel.MAJOR.value

    for card in (*window.stat_cards, window.current_task_card, window.integrity_progress_panel):
        assert card.objectName() == "elevatedSubCard"
        assert card.property("elevation") == ElevationLevel.SECONDARY.value
    assert window.task_queue_card.isAncestorOf(window.task_empty_hint)
    assert window.task_queue_card.isAncestorOf(window.task_table)
    assert window.task_detail_card.isAncestorOf(window.task_item_table)
```

Also add this visible-layout test at `1180 × 720`:

```python
def test_task_center_actions_stay_inside_cards_at_minimum_size(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(1180, 720)
    window.show()
    qtbot.wait(20)

    assert window.task_queue_card.height() > 0
    assert window.task_detail_card.height() > 0
    for button in (
        window.pause_button,
        window.resume_button,
        window.prioritize_button,
        window.retry_button,
        window.verify_tasks_button,
        window.archive_button,
        window.restore_button,
        window.open_button,
    ):
        assert button.isVisible()
        bottom_right = button.mapTo(
            window.task_queue_card,
            button.rect().bottomRight(),
        )
        assert window.task_queue_card.rect().contains(bottom_right)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/ui/test_main_window.py::test_main_shell_and_task_cards_use_confirmed_elevation_hierarchy -q
```

Expected: fails because the named panel/card attributes and elevations do not yet exist.

- [ ] **Step 3: Refactor the main shell without changing behavior**

Import `APP_STYLESHEET`, `ElevationLevel`, and `apply_elevation`. Store the navigation panel before adding it, add 12px root margins and spacing, and elevate both side rails:

```python
self.setStyleSheet(APP_STYLESHEET)
root_layout.setContentsMargins(12, 12, 12, 12)
root_layout.setSpacing(12)
self.navigation_panel = self._build_navigation()
apply_elevation(self.navigation_panel, ElevationLevel.SECONDARY)
root_layout.addWidget(self.navigation_panel)
...
self.statistics_panel = self._build_statistics()
apply_elevation(self.statistics_panel, ElevationLevel.SECONDARY)
```

In `_build_workspace()`:

- assign `self.source_card = self._build_source_card()`, name it `elevatedCard`, and apply major elevation;
- create `self.task_queue_card = QFrame()`, call `setObjectName("elevatedCard")`, and place the queue heading, filters, task table, empty hint, and existing batch-action row inside it;
- assign `self.task_detail_card` to the existing detail frame, rename it `elevatedCard`, and apply major elevation;
- convert `integrity_progress_panel` to `QFrame("elevatedSubCard")`, give it `8, 7, 8, 7` margins, and apply secondary elevation;
- put only `task_queue_card` and `task_detail_card` in the vertical splitter.

In `_build_statistics()` expose and elevate the nested surfaces:

```python
self.stat_cards = (speed_card, completed_card, remaining_card)
for card in self.stat_cards:
    card.setObjectName("elevatedSubCard")
    apply_elevation(card, ElevationLevel.SECONDARY)
self.current_task_card = current
self.current_task_card.setObjectName("elevatedSubCard")
apply_elevation(self.current_task_card, ElevationLevel.SECONDARY)
```

Keep `task_table`, `task_item_table`, every existing button attribute, signal connection, model, and action-state method unchanged.

- [ ] **Step 4: Run main-window tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/ui/test_main_window.py -q
```

Expected: all main-window tests pass, including the new minimum-size test.

- [ ] **Step 5: Commit the main shell**

```powershell
git add src/telegram_downloader/ui/main.py tests/ui/test_main_window.py
git commit -m "style: elevate main shell and task center cards"
```

## Task 4: Account-content card migration

**Files:**
- Modify: `tests/ui/test_content_browser.py`
- Modify: `src/telegram_downloader/ui/content_browser.py`
- Modify: `src/telegram_downloader/ui/theme.py`

- [ ] **Step 1: Replace the page-specific shadow test with a failing global contract**

```python
from telegram_downloader.ui.effects import ElevationLevel
from telegram_downloader.ui.theme import APP_STYLESHEET


def test_account_content_cards_use_shared_major_elevation(qtbot) -> None:
    page = ContentBrowserPage()
    qtbot.addWidget(page)

    effects = []
    for card in (page.dialog_card, page.filter_card, page.results_card):
        assert card.objectName() == "elevatedCard"
        assert card.property("elevation") == ElevationLevel.MAJOR.value
        effect = card.graphicsEffect()
        assert isinstance(effect, QGraphicsDropShadowEffect)
        effects.append(effect)
    assert len({id(effect) for effect in effects}) == 3
    assert "QFrame#elevatedCard" in APP_STYLESHEET
```

Update the existing card-structure test to expect `elevatedCard` for all three cards while retaining every width, elision, and parent assertion.

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/ui/test_content_browser.py::test_account_content_cards_use_shared_major_elevation -q
```

Expected: fails because cards are still named `accountContentCard` and use the local helper.

- [ ] **Step 3: Migrate to the shared effect**

Remove the local `QColor`, `QGraphicsDropShadowEffect`, and `_apply_card_shadow()` implementation. Import `ElevationLevel` and `apply_elevation`, set each card object name to `elevatedCard`, and install effects in the existing loop:

```python
for card in (self.dialog_card, self.filter_card, self.results_card):
    apply_elevation(card, ElevationLevel.MAJOR)
```

Keep `accountContentPage`, all three layout columns, minimum widths, table configuration, checkbox delegate, and search behavior unchanged.
Remove `QFrame#accountContentCard` and `QFrame#contentPanel` from the compatibility selector list in `APP_STYLESHEET` after all three widgets use `elevatedCard`.

- [ ] **Step 4: Run all account-content tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/ui/test_content_browser.py tests/ui/test_content_models.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the migration**

```powershell
git add src/telegram_downloader/ui/content_browser.py src/telegram_downloader/ui/theme.py tests/ui/test_content_browser.py
git commit -m "refactor: share account content elevation system"
```

## Task 5: Automatic-subscription and health-diagnostic hierarchy

**Files:**
- Modify: `tests/ui/test_subscriptions.py`
- Modify: `tests/ui/test_diagnostics.py`
- Modify: `src/telegram_downloader/ui/subscriptions.py`
- Modify: `src/telegram_downloader/ui/diagnostics.py`

- [ ] **Step 1: Add failing page hierarchy tests**

Add these tests with `QGraphicsDropShadowEffect` and `ElevationLevel` imports:

```python
def test_subscription_page_uses_major_and_nested_silver_cards(qtbot) -> None:
    page = SubscriptionPage()
    qtbot.addWidget(page)

    assert page.subscription_card.objectName() == "elevatedCard"
    assert page.subscription_card.property("elevation") == ElevationLevel.MAJOR.value
    for card in (page.diagnostic_card, page.history_card, page.probe_card):
        assert card.objectName() == "elevatedSubCard"
        assert card.property("elevation") == ElevationLevel.SECONDARY.value
    assert page.subscription_card.isAncestorOf(page.rule_table)
    assert page.diagnostic_card.isAncestorOf(page.run_history_table)
    assert page.diagnostic_card.isAncestorOf(page.probe_sample_table)


def test_diagnostics_page_separates_progress_results_and_actions(qtbot) -> None:
    page = DiagnosticsPage()
    qtbot.addWidget(page)

    for card in (page.progress_card, page.results_card):
        assert card.objectName() == "elevatedCard"
        assert card.property("elevation") == ElevationLevel.MAJOR.value
    assert page.actions_card.objectName() == "elevatedSubCard"
    assert page.actions_card.property("elevation") == ElevationLevel.SECONDARY.value
    assert page.status_banner.graphicsEffect() is None
    assert page.results_card.isAncestorOf(page.table)
    assert page.actions_card.isAncestorOf(page.start_button)
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/ui/test_subscriptions.py::test_subscription_page_uses_major_and_nested_silver_cards tests/ui/test_diagnostics.py::test_diagnostics_page_separates_progress_results_and_actions -q
```

Expected: fails because the semantic card attributes do not exist.

- [ ] **Step 3: Implement the subscription hierarchy**

Import the shared elevation helpers. Store the current outer card as `self.subscription_card`, rename it `elevatedCard`, and apply major elevation. Store the current detail frame as `self.diagnostic_card`, rename it `elevatedSubCard`, and apply secondary elevation.

Convert `history_panel` and `probe_panel` from `QWidget` to `QFrame`, expose them as `self.history_card` and `self.probe_card`, name both `elevatedSubCard`, give each 10px contents margins, and apply secondary elevation. Preserve splitter factors, table models, progress state, and all existing buttons/signals.

Use these exact elevation assignments around the existing layouts:

```python
self.subscription_card = QFrame()
self.subscription_card.setObjectName("elevatedCard")
apply_elevation(self.subscription_card, ElevationLevel.MAJOR)

self.diagnostic_card = QFrame()
self.diagnostic_card.setObjectName("elevatedSubCard")
apply_elevation(self.diagnostic_card, ElevationLevel.SECONDARY)

self.history_card = QFrame()
self.history_card.setObjectName("elevatedSubCard")
apply_elevation(self.history_card, ElevationLevel.SECONDARY)

self.probe_card = QFrame()
self.probe_card.setObjectName("elevatedSubCard")
apply_elevation(self.probe_card, ElevationLevel.SECONDARY)
```

- [ ] **Step 4: Implement the diagnostic hierarchy**

Expose the current progress frame as `self.progress_card`, rename it `elevatedCard`, and apply major elevation. Create `self.results_card = QFrame()` with object name `elevatedCard`; move the existing results table, privacy note, error label, and a new `self.actions_card` into its vertical layout. Put the existing button row inside `actions_card`, name it `elevatedSubCard`, and apply secondary elevation. Apply major elevation to `results_card`; keep `status_banner` directly on the page with no graphics effect.

Use these exact surface declarations:

```python
self.progress_card = QFrame()
self.progress_card.setObjectName("elevatedCard")
apply_elevation(self.progress_card, ElevationLevel.MAJOR)

self.results_card = QFrame()
self.results_card.setObjectName("elevatedCard")
apply_elevation(self.results_card, ElevationLevel.MAJOR)

self.actions_card = QFrame(self.results_card)
self.actions_card.setObjectName("elevatedSubCard")
apply_elevation(self.actions_card, ElevationLevel.SECONDARY)
```

- [ ] **Step 5: Run both complete page suites and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/ui/test_subscriptions.py tests/ui/test_diagnostics.py tests/ui/test_subscription_diagnostics.py -q
```

Expected: all tests pass, including existing busy-state, signal, safe-error, and minimum-size checks.

- [ ] **Step 6: Commit both pages**

```powershell
git add src/telegram_downloader/ui/subscriptions.py src/telegram_downloader/ui/diagnostics.py tests/ui/test_subscriptions.py tests/ui/test_diagnostics.py
git commit -m "style: elevate subscription and diagnostic pages"
```

## Task 6: Every custom dialog uses the silver elevated surface

**Files:**
- Modify: `src/telegram_downloader/ui/login.py`
- Modify: `src/telegram_downloader/ui/settings.py`
- Modify: `src/telegram_downloader/ui/subscriptions.py`
- Modify: `src/telegram_downloader/ui/media_preview.py`
- Modify: `src/telegram_downloader/ui/update_dialog.py`
- Modify: `tests/ui/test_login_dialog.py`
- Modify: `tests/ui/test_settings_dialog.py`
- Modify: `tests/ui/test_subscriptions.py`
- Modify: `tests/ui/test_media_preview.py`
- Modify: `tests/ui/test_update_dialog.py`

- [ ] **Step 1: Add one failing silver-surface assertion to each dialog suite**

Add `QGraphicsDropShadowEffect`, `ElevationLevel`, and `APP_STYLESHEET` imports, then use this exact assertion pattern after constructing each dialog:

```python
assert dialog.styleSheet() == APP_STYLESHEET
assert dialog.dialog_surface.objectName() == "dialogSurface"
assert dialog.dialog_surface.property("elevation") == ElevationLevel.MAJOR.value
assert isinstance(dialog.dialog_surface.graphicsEffect(), QGraphicsDropShadowEffect)
```

Add it for `LoginDialog`, `SettingsDialog`, `SubscriptionEditorDialog`, `MediaPreviewDialog`, and `UpdateDialog`. Keep each test in its existing suite so constructors continue using real domain inputs.

- [ ] **Step 2: Run the five dialog tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/ui/test_login_dialog.py tests/ui/test_settings_dialog.py tests/ui/test_subscriptions.py tests/ui/test_media_preview.py tests/ui/test_update_dialog.py -q
```

Expected: the new assertions fail because `dialog_surface` is absent; existing behavioral assertions remain green.

- [ ] **Step 3: Wrap each dialog body in one major surface**

For each dialog:

1. call `ensure_cjk_font()`;
2. call `self.setStyleSheet(APP_STYLESHEET)`;
3. make the outer `QVBoxLayout(self)` use `16, 16, 16, 18` margins;
4. create `self.dialog_surface = QFrame(self)` with object name `dialogSurface`;
5. install major elevation;
6. add `dialog_surface` to the outer layout;
7. move the dialog's current content layout to `QVBoxLayout(self.dialog_surface)` while preserving its existing content margins and spacing.

The shared production pattern is:

```python
outer = QVBoxLayout(self)
outer.setContentsMargins(16, 16, 16, 18)
self.dialog_surface = QFrame(self)
self.dialog_surface.setObjectName("dialogSurface")
apply_elevation(self.dialog_surface, ElevationLevel.MAJOR)
outer.addWidget(self.dialog_surface)
layout = QVBoxLayout(self.dialog_surface)
```

Add `QFrame` imports where missing. Do not alter dialog signals, validation, sensitive-field masking, preview scaling, signed-update acceptance, or button roles. The global `QDialog` and `QMessageBox` selectors cover standard confirmation dialogs that are created through static Qt helpers.

- [ ] **Step 4: Run all dialog suites and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/ui/test_login_dialog.py tests/ui/test_settings_dialog.py tests/ui/test_subscriptions.py tests/ui/test_media_preview.py tests/ui/test_update_dialog.py -q
```

Expected: all tests pass; QR remains 300×300, preview aspect ratio remains intact, settings round-trips, and update acceptance remains explicit.

- [ ] **Step 5: Commit all custom dialogs**

```powershell
git add src/telegram_downloader/ui/login.py src/telegram_downloader/ui/settings.py src/telegram_downloader/ui/subscriptions.py src/telegram_downloader/ui/media_preview.py src/telegram_downloader/ui/update_dialog.py tests/ui/test_login_dialog.py tests/ui/test_settings_dialog.py tests/ui/test_subscriptions.py tests/ui/test_media_preview.py tests/ui/test_update_dialog.py
git commit -m "style: apply elevated silver surfaces to dialogs"
```

## Task 7: Full regression, actual-window QA, and verification record

**Files:**
- Create: `docs/verification/v0.11.2-global-silver-elevated-card-theme.md`
- Regression fixes must modify only the owning source file and its existing test file from Tasks 1–6.

- [ ] **Step 1: Run the complete automated gate**

Run:

```powershell
.\scripts\test.ps1
```

Expected: every pytest test and Ruff check passes with no warnings or errors.

- [ ] **Step 2: Run the real Windows application at both required sizes**

Launch the application from the isolated worktree and inspect `1180 × 720` and `1280 × 780`. Visit task center, account content, automatic subscriptions, health diagnostics, login, settings, subscription editor, media preview, and update dialog. For each surface verify:

- no dark-theme remnants;
- visible white-silver gradient;
- major shadow clearly stronger than nested shadow;
- no clipped, doubled, or blackened shadow edges;
- no text overflow, control compression, or hidden action buttons;
- navigation, page switching, table scrolling, splitter dragging, and dialog resizing remain responsive.

Use the real click path for every navigation button and open each dialog through the application where available. For the signed update dialog, use the existing deterministic test manifest in a local QA harness rather than contacting a release endpoint.

- [ ] **Step 3: Fix each visual defect through a fresh RED/GREEN cycle**

For every defect, first add a focused regression assertion to the owning test suite, run it to observe the expected failure, then change the smallest owning layout/style/effect code and rerun that suite. Do not weaken fixed widths, text elision, or existing business assertions to make a visual test pass.

- [ ] **Step 4: Build and smoke-test the frozen application**

Run:

```powershell
.\scripts\build.ps1
.\scripts\smoke.ps1
```

Expected: build succeeds and smoke test prints `PACKAGED_SMOKE_OK`.

- [ ] **Step 5: Record measured evidence**

Create `docs/verification/v0.11.2-global-silver-elevated-card-theme.md` containing the current date, implementation commit, exact test count and duration, Ruff result, `PACKAGED_SMOKE_OK`, inspected window sizes, pages/dialogs opened, and any remaining visual limitations. State explicitly that the application version and public `v0.11.2` tag were not changed.

- [ ] **Step 6: Run final diff and repository checks**

Run:

```powershell
git diff --check
git status --short --branch
git log --oneline --decorate -8
```

Expected: only the intended implementation and verification evidence are present; no build output, local state, credentials, or screenshots are tracked.

- [ ] **Step 7: Commit the verification record**

```powershell
git add docs/verification/v0.11.2-global-silver-elevated-card-theme.md
git commit -m "docs: verify global silver elevated card theme"
```

Do not merge, push, bump the version, create a tag, or publish until the user separately requests integration or release.
