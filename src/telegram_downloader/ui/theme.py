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
QFrame#card, QFrame#statCard {
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

QLabel#brandMark {
    min-width: 34px;
    min-height: 34px;
    max-width: 34px;
    max-height: 34px;
    border-radius: 9px;
    background: #17A8C2;
    color: #FFFFFF;
    font-size: 18px;
    font-weight: 800;
}
QLabel#brandName { color: #172033; font-size: 15px; font-weight: 700; }
QLabel#pageTitle { color: #172033; font-size: 24px; font-weight: 750; }
QLabel#sectionTitle { color: #1F2937; font-size: 14px; font-weight: 650; }
QLabel#muted, QLabel#fieldCaption, QLabel#brandCaption { color: #66758A; }
QLabel#statValue { color: #172033; font-size: 22px; font-weight: 750; }
QLabel#statAccent { color: #0E8FA8; font-size: 22px; font-weight: 750; }
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
QLabel#diagnosticStatus[status="running"] {
    border-color: #8DD6E2; background: #E7F8FB; color: #087F96;
}
QLabel#diagnosticStatus[status="passed"] {
    border-color: #9DD9C7; background: #EAF8F2; color: #176B55;
}
QLabel#diagnosticStatus[status="warning"] {
    border-color: #E7C878; background: #FFF8E5; color: #8A6418;
}
QLabel#diagnosticStatus[status="failed"] {
    border-color: #E7A8B3; background: #FFF0F3; color: #A33C50;
}
QLabel#errorText, QLabel#errorBanner {
    padding: 8px 11px; border: 1px solid #E7A8B3; border-radius: 7px;
    background: #FFF0F3; color: #A33C50;
}
QLabel#selectionSummary { color: #087F96; font-weight: 650; }

QPushButton {
    min-height: 34px; padding: 0 13px; border: 1px solid #C7D1DC;
    border-radius: 7px; background: #F7F9FC; color: #334155; font-weight: 600;
}
QPushButton:hover {
    border-color: #92A9BB; background: #EEF4F8; color: #213547;
}
QPushButton:pressed { background: #E3EAF1; }
QPushButton:disabled {
    border-color: #DCE3EA; background: #EDF1F5; color: #9AA8B8;
}
QPushButton#primaryButton {
    border-color: #17A8C2; background: #17A8C2;
    color: #FFFFFF; font-weight: 750;
}
QPushButton#primaryButton:hover {
    border-color: #0E8FA8; background: #0E8FA8;
}
QPushButton#navButton {
    min-height: 42px; padding-left: 14px; border: 1px solid transparent;
    background: transparent; text-align: left; color: #66758A;
}
QPushButton#navButton:hover {
    border-color: #D2DCE6; background: #EEF3F8; color: #26384A;
}
QPushButton#navButton[active="true"] {
    border-color: #8DD6E2;
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #F4FDFF, stop: 1 #DDF5F8
    );
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
QDateEdit::drop-down, QSpinBox::up-button,
QSpinBox::down-button, QComboBox::drop-down {
    width: 22px; border: 0; background: #EDF3F8;
}
QPushButton:focus, QLineEdit:focus, QDateEdit:focus, QSpinBox:focus,
QComboBox:focus, QCheckBox:focus, QTableView:focus, QListView:focus {
    border: 1px solid #17A8C2;
}
QCheckBox { spacing: 7px; color: #334155; }
QCheckBox::indicator {
    width: 16px; height: 16px; border: 1px solid #9FB1C4;
    border-radius: 4px; background: #FFFFFF;
}
QCheckBox::indicator:checked {
    border-color: #17A8C2; background: #17A8C2;
}

QTableView, QListView, QTextBrowser, QScrollArea {
    border: 1px solid #D5DEE7; border-radius: 8px; background: #FFFFFF;
    color: #25344A; selection-background-color: #DDF5F8;
    selection-color: #173744;
}
QTableView {
    alternate-background-color: #F7FAFC; gridline-color: #E2E8F0;
}
QListView::item {
    min-height: 38px; padding: 0 8px; border-bottom: 1px solid #EDF1F5;
}
QTableView::item:hover, QListView::item:hover { background: #EEF9FB; }
QHeaderView::section {
    min-height: 34px; padding: 0 8px; border: 0;
    border-bottom: 1px solid #D5DEE7;
    background: #EEF3F8; color: #596B82; font-weight: 650;
}
QHeaderView {
    background: #EEF3F8; color: #596B82;
}
QTableCornerButton::section {
    border: 0; border-bottom: 1px solid #D5DEE7; background: #EEF3F8;
}
QTabWidget::pane {
    border: 1px solid #D5DEE7; border-radius: 8px; background: #FFFFFF;
}
QTabBar::tab {
    min-width: 92px; min-height: 32px; padding: 0 12px;
    border: 1px solid #D5DEE7; background: #EDF2F7; color: #64748B;
}
QTabBar::tab:selected {
    border-color: #8DD6E2; background: #E4F7FA; color: #087F96;
}
QProgressBar {
    min-height: 8px; max-height: 8px; border: 0;
    border-radius: 4px; background: #DCE6EF; color: transparent;
}
QProgressBar::chunk { border-radius: 4px; background: #17A8C2; }
QSplitter::handle { background: transparent; }
QScrollBar:vertical { width: 10px; border: 0; background: #EDF2F7; }
QScrollBar::handle:vertical {
    min-height: 28px; border-radius: 5px; background: #A6B8CA;
}
QStatusBar {
    border-top: 1px solid #D5DEE7; background: #F2F5F8; color: #66758A;
}
QMessageBox { background: #F7F9FC; color: #1F2937; }

QWidget#accountContentPage,
QWidget#accountContentPage QWidget#accountContentDialogColumn,
QWidget#accountContentPage QWidget#accountContentSearchColumn,
QWidget#accountContentPage QTabWidget,
QWidget#accountContentPage QTabBar,
QWidget#accountContentPage QWidget#accountContentTabPage,
QWidget#accountContentPage QSplitter#contentSplitter,
QWidget#accountContentPage QSplitter#contentSplitter::handle {
    background: transparent;
}
QWidget#accountContentPage QTabWidget QStackedWidget {
    background: #FFFFFF;
}
"""

DARK_STYLESHEET = APP_STYLESHEET
