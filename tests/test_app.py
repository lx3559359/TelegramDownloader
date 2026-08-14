from PySide6.QtWidgets import QMessageBox

from telegram_downloader import app
from telegram_downloader.content_browser import ContentBrowserService


def test_standard_button_selection_accepts_pyside_integer_result() -> None:
    yes = QMessageBox.StandardButton.Yes

    assert app._standard_button_selected(yes.value, yes) is True


def test_create_application_initializes_project_local_content_services(
    tmp_path,
) -> None:
    application, loop, controller = app.create_application(tmp_path)

    try:
        assert isinstance(controller.content_browser, ContentBrowserService)
        assert (
            controller.content_browser.catalog.database
            == (tmp_path / "data" / "database" / "catalog.sqlite3").resolve()
        )
        assert (
            controller.content_browser.thumbnails.root
            == (tmp_path / "data" / "cache" / "thumbnails").resolve()
        )
        assert controller.window.content_page is not None
    finally:
        controller.window.close()
        loop.close()
        application.processEvents()
