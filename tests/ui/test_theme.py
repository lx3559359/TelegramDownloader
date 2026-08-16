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
        'QPushButton#navButton[active="true"]',
        "QLineEdit",
        "QComboBox",
        "QTableView",
        "QListView",
        "QScrollArea",
        "QTextBrowser",
        "QStatusBar",
    ):
        assert selector in APP_STYLESHEET
