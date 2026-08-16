from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PySide6.QtGui import QFontDatabase


@lru_cache(maxsize=1)
def ensure_cjk_font() -> str:
    """Load a Windows CJK font explicitly for headless and packaged Qt runs."""
    windows = os.environ.get("WINDIR", "")
    candidates = ("msyh.ttc", "msyhl.ttc", "simhei.ttf")
    for name in candidates:
        path = Path(windows) / "Fonts" / name
        if not path.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id >= 0:
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                return families[0]
    return "Microsoft YaHei UI"


DARK_STYLESHEET = """
QMainWindow, QWidget {
    background: #0b111b;
    color: #e6edf7;
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 13px;
}

QWidget#navRail {
    background: #0f1724;
    border-right: 1px solid #243246;
}

QWidget#statsRail {
    background: #0d1521;
    border-left: 1px solid #243246;
}

QLabel#brandMark {
    min-width: 34px;
    min-height: 34px;
    max-width: 34px;
    max-height: 34px;
    border-radius: 9px;
    background: #22b8cf;
    color: #06141c;
    font-size: 18px;
    font-weight: 800;
}

QLabel#brandName {
    font-size: 15px;
    font-weight: 700;
}

QLabel#brandCaption, QLabel#muted, QLabel#fieldCaption {
    color: #8190a5;
}

QLabel#pageTitle {
    font-size: 24px;
    font-weight: 750;
}

QLabel#sectionTitle {
    color: #f4f7fb;
    font-size: 14px;
    font-weight: 650;
}

QLabel#accountBadge {
    padding: 6px 11px;
    border: 1px solid #45566e;
    border-radius: 13px;
    background: #172234;
    color: #aab7c9;
    font-weight: 600;
}

QLabel#accountBadge[connected="true"] {
    border-color: #2dd4bf;
    background: #123631;
    color: #72f1dd;
}

QFrame#card, QFrame#statCard {
    background: #111b2a;
    border: 1px solid #25354a;
    border-radius: 10px;
}

QFrame#contentPanel {
    background: #101a29;
    border: 1px solid #25364c;
    border-radius: 10px;
}

QSplitter#contentSplitter::handle {
    width: 8px;
    background: transparent;
}

QLabel#contentHint {
    padding: 8px 11px;
    border: 1px solid #29435a;
    border-radius: 7px;
    background: #102333;
    color: #93c5d7;
}

QLabel#diagnosticStatus {
    padding: 8px 11px;
    border: 1px solid #45566e;
    border-radius: 7px;
    background: #172234;
    color: #cbd5e1;
    font-weight: 650;
}

QLabel#diagnosticStatus[status="running"] {
    border-color: #2d7284;
    background: #102d3a;
    color: #67e8f9;
}

QLabel#diagnosticStatus[status="passed"] {
    border-color: #26725f;
    background: #123329;
    color: #6ee7b7;
}

QLabel#diagnosticStatus[status="warning"] {
    border-color: #8a651c;
    background: #332710;
    color: #fbbf24;
}

QLabel#diagnosticStatus[status="failed"] {
    border-color: #7c3d49;
    background: #301923;
    color: #fda4af;
}

QLabel#errorText {
    padding: 8px 11px;
    border: 1px solid #7c3d49;
    border-radius: 7px;
    background: #301923;
    color: #fda4af;
}

QLabel#selectionSummary {
    color: #67e8f9;
    font-weight: 650;
}

QTabWidget::pane {
    border: 1px solid #283950;
    border-radius: 8px;
    background: #0e1724;
}

QTabBar::tab {
    min-width: 92px;
    min-height: 32px;
    padding: 0 12px;
    border: 1px solid #283950;
    background: #111c2b;
    color: #8fa2ba;
}

QTabBar::tab:selected {
    border-color: #2d7284;
    background: #173341;
    color: #67e8f9;
}

QListView {
    border: 1px solid #283950;
    border-radius: 8px;
    background: #0e1724;
    color: #dfe9f6;
    selection-background-color: #18495a;
    selection-color: #f5fdff;
    outline: 0;
}

QListView::item {
    min-height: 38px;
    padding: 0 8px;
    border-bottom: 1px solid #18263a;
}

QLabel#statValue {
    color: #f8fbff;
    font-size: 22px;
    font-weight: 750;
}

QLabel#statAccent {
    color: #56d8ee;
    font-size: 22px;
    font-weight: 750;
}

QPushButton {
    min-height: 34px;
    padding: 0 13px;
    border: 1px solid #33455e;
    border-radius: 7px;
    background: #182538;
    color: #dbe6f5;
    font-weight: 600;
}

QPushButton:hover {
    background: #21334b;
    border-color: #4d6685;
}

QPushButton:pressed {
    background: #101b2b;
}

QPushButton:disabled {
    color: #58677a;
    background: #121c2a;
    border-color: #233145;
}

QPushButton:focus, QLineEdit:focus, QDateEdit:focus, QSpinBox:focus,
QCheckBox:focus, QTableView:focus {
    border: 1px solid #67e8f9;
}

QPushButton#primaryButton {
    background: #22b8cf;
    border-color: #22b8cf;
    color: #06141c;
    font-weight: 750;
}

QPushButton#primaryButton:hover {
    background: #4dd0e1;
    border-color: #4dd0e1;
}

QPushButton#navButton {
    min-height: 42px;
    padding-left: 14px;
    border: 1px solid transparent;
    background: transparent;
    text-align: left;
    color: #93a4ba;
}

QPushButton#navButton:hover {
    background: #162234;
    color: #e9f4ff;
}

QPushButton#navButton[active="true"] {
    background: #173341;
    border-color: #27566a;
    color: #67e8f9;
}

QLineEdit, QDateEdit, QSpinBox {
    min-height: 36px;
    padding: 0 10px;
    border: 1px solid #30425a;
    border-radius: 7px;
    background: #0c1522;
    color: #eef5ff;
    selection-background-color: #167a91;
}

QDateEdit::drop-down, QSpinBox::up-button, QSpinBox::down-button {
    width: 22px;
    border: 0;
    background: #172438;
}

QCheckBox {
    spacing: 7px;
    color: #c6d2e2;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #4a607d;
    border-radius: 4px;
    background: #0c1522;
}

QCheckBox::indicator:checked {
    background: #22b8cf;
    border-color: #67e8f9;
}

QTableView {
    border: 1px solid #283950;
    border-radius: 8px;
    background: #0e1724;
    alternate-background-color: #111c2b;
    color: #dfe9f6;
    gridline-color: #1e2d41;
    selection-background-color: #18495a;
    selection-color: #f5fdff;
}

QHeaderView::section {
    min-height: 34px;
    padding: 0 8px;
    border: 0;
    border-bottom: 1px solid #30425a;
    background: #152234;
    color: #91a4bc;
    font-weight: 650;
}

QProgressBar {
    min-height: 8px;
    max-height: 8px;
    border: 0;
    border-radius: 4px;
    background: #25344a;
    color: transparent;
}

QProgressBar::chunk {
    border-radius: 4px;
    background: #2dd4bf;
}

QScrollBar:vertical {
    width: 10px;
    border: 0;
    background: #0d1623;
}

QScrollBar::handle:vertical {
    min-height: 28px;
    border-radius: 5px;
    background: #344a65;
}
"""
