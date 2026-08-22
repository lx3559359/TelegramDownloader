from PySide6.QtCore import Qt

from telegram_downloader.ui.main import MainWindow
from telegram_downloader.ui.maintenance import MaintenancePage


def test_maintenance_page_contains_only_health_and_storage_tabs(qtbot) -> None:
    page = MaintenancePage()
    qtbot.addWidget(page)

    assert page.tabs.count() == 2
    assert page.tabs.widget(0) is page.diagnostics_page
    assert page.tabs.widget(1) is page.storage_page
    assert page.tabs.tabText(0) == "健康诊断"
    assert page.tabs.tabText(1) == "存储空间"


def test_switching_to_storage_emits_activation_only_on_transition(qtbot) -> None:
    page = MaintenancePage()
    qtbot.addWidget(page)

    with qtbot.waitSignal(page.storage_page.activated, timeout=500):
        page.show_storage()
    assert page.tabs.currentWidget() is page.storage_page
    page.show_health()
    assert page.tabs.currentWidget() is page.diagnostics_page


def test_main_window_maintenance_navigation_preserves_compatibility(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()

    assert window.diagnostics_page is window.maintenance_page.diagnostics_page
    assert window.storage_page is window.maintenance_page.storage_page
    assert window.diagnostics_nav_button is window.maintenance_nav_button
    with qtbot.waitSignal(window.diagnostics_activated, timeout=500):
        qtbot.mouseClick(window.maintenance_nav_button, Qt.MouseButton.LeftButton)

    assert window.page_stack.currentWidget() is window.maintenance_page
    assert window.statistics_panel.isHidden() is True
    assert window.maintenance_nav_button.property("active") is True


def test_diagnostics_route_forces_health_but_maintenance_route_preserves_tab(
    qtbot,
) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.maintenance_page.show_storage()

    window.show_page("maintenance")
    assert window.maintenance_page.tabs.currentWidget() is window.storage_page
    window.show_page("diagnostics")

    assert window.page_stack.currentWidget() is window.maintenance_page
    assert window.maintenance_page.tabs.currentWidget() is window.diagnostics_page
