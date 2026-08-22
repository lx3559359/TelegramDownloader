from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QLineEdit

from telegram_downloader.settings import (
    AppSettings,
    DownloadScheduleSettings,
    ProxySettings,
)
from telegram_downloader.ui.effects import ElevationLevel
from telegram_downloader.ui.settings import SettingsDialog
from telegram_downloader.ui.theme import APP_STYLESHEET


def test_round_trip_manual_proxy_form(qtbot) -> None:
    settings = AppSettings(
        123,
        4,
        ProxySettings("http", "127.0.0.1", 8080, "u"),
        False,
        speed_limit_kib=2048,
    )
    dialog = SettingsDialog(settings, proxy_password="secret")
    qtbot.addWidget(dialog)

    assert dialog.styleSheet() == APP_STYLESHEET
    assert dialog.dialog_surface.objectName() == "dialogSurface"
    assert dialog.dialog_surface.property("elevation") == ElevationLevel.MAJOR.value
    assert isinstance(dialog.dialog_surface.graphicsEffect(), QGraphicsDropShadowEffect)
    assert dialog.values() == settings
    assert dialog.concurrency.minimum() == 1
    assert dialog.concurrency.maximum() == 5
    assert dialog.concurrency_label.text() == "文件并发"
    assert tuple(
        dialog.speed_limit.itemData(index)
        for index in range(dialog.speed_limit.count())
    ) == (0, 256, 512, 1024, 2048, 5120, 10240, 20480, 51200)
    assert dialog.speed_limit.currentData() == 2048
    assert dialog.proxy_password.echoMode() is QLineEdit.EchoMode.Password
    assert dialog.proxy_password.text() == "secret"


def test_proxy_test_emits_without_accepting_dialog(qtbot) -> None:
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)
    dialog.proxy_kind.setCurrentIndex(dialog.proxy_kind.findData("socks5"))
    dialog.proxy_host.setText("127.0.0.1")
    dialog.proxy_port.setValue(1080)

    with qtbot.waitSignal(dialog.test_proxy_requested, timeout=500) as signal:
        qtbot.mouseClick(dialog.test_button, Qt.MouseButton.LeftButton)

    assert signal.args[0] == ProxySettings("socks5", "127.0.0.1", 1080)
    assert dialog.result() == 0


def test_invalid_proxy_shows_error_and_does_not_accept(qtbot) -> None:
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)
    dialog.proxy_kind.setCurrentIndex(dialog.proxy_kind.findData("http"))
    dialog.proxy_host.clear()
    dialog.proxy_port.setValue(8080)

    qtbot.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)

    assert dialog.result() == 0
    assert "代理" in dialog.error_label.text()


def test_valid_save_emits_without_closing_until_runtime_apply_succeeds(qtbot) -> None:
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.save_requested, timeout=500):
        qtbot.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)

    assert dialog.result() == 0


def test_thumbnail_cache_clear_emits_without_closing_dialog(qtbot) -> None:
    dialog = SettingsDialog(
        AppSettings(),
        thumbnail_cache_bytes=3 * 1024 * 1024,
    )
    qtbot.addWidget(dialog)

    assert dialog.thumbnail_cache_size.text() == "3.0 MB"
    with qtbot.waitSignal(
        dialog.thumbnail_cache_clear_requested,
        timeout=500,
    ):
        qtbot.mouseClick(
            dialog.thumbnail_cache_clear_button,
            Qt.MouseButton.LeftButton,
        )

    assert dialog.result() == 0
    dialog.set_thumbnail_cache_bytes(0)
    assert dialog.thumbnail_cache_size.text() == "0 B"


def test_cache_and_save_busy_states_are_immediate(qtbot) -> None:
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)

    dialog.set_thumbnail_cache_busy(True)
    dialog.set_save_busy(True)

    assert dialog.thumbnail_cache_clear_button.isEnabled() is False
    assert "清理" in dialog.thumbnail_cache_clear_button.text()
    assert dialog.save_button.isEnabled() is False
    assert "保存" in dialog.save_button.text()

    dialog.set_thumbnail_cache_busy(False)
    dialog.set_save_busy(False)
    assert dialog.thumbnail_cache_clear_button.isEnabled() is True
    assert dialog.save_button.isEnabled() is True


def test_background_tab_round_trips_all_values(qtbot) -> None:
    settings = AppSettings(
        close_to_tray=False,
        notifications_enabled=False,
        autostart_enabled=True,
        tray_hint_shown=True,
        download_schedule=DownloadScheduleSettings(
            True,
            (0, 2, 4),
            22 * 60,
            2 * 60,
        ),
    )
    dialog = SettingsDialog(settings, autostart_available=True)
    qtbot.addWidget(dialog)

    assert dialog.tabs.count() == 2
    assert [dialog.tabs.tabText(index) for index in range(2)] == [
        "常规",
        "后台与通知",
    ]
    assert dialog.values() == settings


def test_unavailable_integrations_and_disabled_schedule_disable_controls(qtbot) -> None:
    dialog = SettingsDialog(
        AppSettings(),
        autostart_available=False,
        tray_available=False,
    )
    qtbot.addWidget(dialog)

    assert dialog.autostart.isEnabled() is False
    assert dialog.close_to_tray.isEnabled() is False
    assert all(not widget.isEnabled() for widget in dialog.schedule_detail_widgets)

    dialog.schedule_enabled.setChecked(True)
    assert all(widget.isEnabled() for widget in dialog.schedule_detail_widgets)
