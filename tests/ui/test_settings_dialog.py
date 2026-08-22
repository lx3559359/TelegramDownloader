from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QGraphicsDropShadowEffect, QLineEdit

from telegram_downloader.branding import APP_CHANNEL, APP_NAME, APP_SUBTITLE
from telegram_downloader.files import DownloadNamingSettings
from telegram_downloader.settings import (
    AppSettings,
    DownloadScheduleSettings,
    DownloadStorageSettings,
    ProxySettings,
    StorageMaintenanceSettings,
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
    assert dialog.concurrency_label.text() == "全局媒体槽"
    assert tuple(
        dialog.speed_limit.itemData(index)
        for index in range(dialog.speed_limit.count())
    ) == (0, 256, 512, 1024, 2048, 5120, 10240, 20480, 51200)
    assert dialog.speed_limit.currentData() == 2048
    assert dialog.proxy_password.echoMode() is QLineEdit.EchoMode.Password
    assert dialog.proxy_password.text() == "secret"


def test_manual_update_button_emits_and_reports_result(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(), application_version="0.16.0")
    qtbot.addWidget(dialog)

    assert not hasattr(dialog, "check_updates")
    labels = [
        dialog.tabs.tabText(index) for index in range(dialog.tabs.count())
    ]
    assert labels == ["常规", "下载路径", "后台与通知", "关于与更新"]
    assert dialog.tabs.indexOf(dialog.about_update_tab) == 3
    assert dialog.product_name_label.text() == APP_NAME
    assert dialog.product_subtitle_label.text() == APP_SUBTITLE
    assert dialog.update_check_button.isVisibleTo(dialog.about_update_tab)
    assert "0.16.0" in dialog.update_version_label.text()
    assert APP_CHANNEL in dialog.update_channel_label.text()
    assert "尚未检查" in dialog.update_last_checked_label.text()
    with qtbot.waitSignal(dialog.update_check_requested, timeout=500):
        qtbot.mouseClick(
            dialog.update_check_button,
            Qt.MouseButton.LeftButton,
        )
    dialog.set_update_busy(True)
    assert dialog.update_check_button.isEnabled() is False
    assert "正在检查" in dialog.update_check_button.text()
    dialog.set_update_result("当前已是最新正式版", state="success")
    assert dialog.update_status_label.text() == "当前已是最新正式版"
    assert dialog.update_status_label.property("updateState") == "success"
    assert 'QLabel#updateStatus[updateState="success"]' in APP_STYLESHEET
    assert 'QLabel#updateStatus[updateState="warning"]' in APP_STYLESHEET
    assert 'QLabel#updateStatus[updateState="error"]' in APP_STYLESHEET
    dialog.set_last_successful_update_check("2026-08-23T02:20:00Z")
    assert "2026-08-23" in dialog.update_last_checked_label.text()
    dialog.set_update_busy(False)
    assert dialog.update_check_button.isEnabled() is True
    assert dialog.values().check_updates_on_startup is False
    assert (
        dialog.values().last_successful_update_check_utc
        == "2026-08-23T02:20:00Z"
    )


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


def test_thumbnail_cache_shortcut_emits_new_and_deprecated_signals(qtbot) -> None:
    dialog = SettingsDialog(
        AppSettings(),
        thumbnail_cache_bytes=3 * 1024 * 1024,
    )
    qtbot.addWidget(dialog)

    assert dialog.thumbnail_cache_size.text() == "3.0 MB"
    storage_requests = []
    deprecated_requests = []
    dialog.storage_maintenance_requested.connect(lambda: storage_requests.append(True))
    dialog.thumbnail_cache_clear_requested.connect(
        lambda: deprecated_requests.append(True)
    )
    qtbot.mouseClick(
        dialog.thumbnail_cache_clear_button,
        Qt.MouseButton.LeftButton,
    )

    assert dialog.thumbnail_cache_clear_button.text() == "前往存储空间"
    assert storage_requests == [True]
    assert deprecated_requests == [True]
    assert dialog.result() == 0
    dialog.set_thumbnail_cache_bytes(0)
    assert dialog.thumbnail_cache_size.text() == "0 B"


def test_cache_and_save_busy_states_are_immediate(qtbot) -> None:
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)

    dialog.set_thumbnail_cache_busy(True)
    dialog.set_save_busy(True)

    assert dialog.thumbnail_cache_clear_button.isEnabled() is False
    assert "打开" in dialog.thumbnail_cache_clear_button.text()
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

    assert dialog.tabs.count() == 4
    assert [dialog.tabs.tabText(index) for index in range(4)] == [
        "常规",
        "下载路径",
        "后台与通知",
        "关于与更新",
    ]
    assert dialog.values() == settings


def test_download_naming_tab_round_trips_and_previews_custom_templates(qtbot) -> None:
    naming = DownloadNamingSettings(
        "{year}/{month}/{source}/{media_type}",
        "{message_date}_{message_id}_{original_name}",
    )
    dialog = SettingsDialog(AppSettings(download_naming=naming))
    qtbot.addWidget(dialog)

    assert dialog.directory_template.currentText() == naming.directory_template
    assert dialog.filename_template.currentText() == naming.filename_template
    assert dialog.values().download_naming == naming
    assert str(
        dialog.default_download_root
        / "2026"
        / "08"
        / "示例频道"
        / "video"
        / "2026-08-22_12345_video.mp4"
    ) in dialog.naming_preview.text()

    assert [
        dialog.directory_template.itemText(index)
        for index in range(dialog.directory_template.count())
    ] == [
        "{source}/{year_month}/{media_type}",
        "{year}/{month}/{source}/{media_type}",
        "{source}/{message_date}",
    ]
    assert [
        dialog.filename_template.itemText(index)
        for index in range(dialog.filename_template.count())
    ] == [
        "{original_name}",
        "{stem}_{message_id}{extension}",
        "{message_date}_{message_id}_{original_name}",
    ]


def test_download_root_uses_folder_browser_and_round_trips(
    qtbot,
    monkeypatch,
    tmp_path,
) -> None:
    default = tmp_path / "app" / "downloads"
    selected = tmp_path / "media"
    default.mkdir(parents=True)
    selected.mkdir()
    dialog = SettingsDialog(AppSettings(), default_download_root=default)
    qtbot.addWidget(dialog)
    assert dialog.download_root.isReadOnly() is True
    assert dialog.download_root.cursorPosition() == 0
    monkeypatch.setattr(
        QFileDialog,
        "getExistingDirectory",
        lambda *_args, **_kwargs: str(selected),
    )

    qtbot.mouseClick(
        dialog.browse_download_root_button,
        Qt.MouseButton.LeftButton,
    )

    assert dialog.download_root.text() == str(selected.resolve())
    assert dialog.download_root.cursorPosition() == 0
    assert dialog.values().download_storage.root == str(selected.resolve())
    assert str(selected.resolve()) in dialog.naming_preview.text()


def test_cancel_folder_browser_keeps_value_and_reset_restores_default(
    qtbot,
    monkeypatch,
    tmp_path,
) -> None:
    default = tmp_path / "downloads"
    selected = tmp_path / "selected"
    default.mkdir()
    selected.mkdir()
    settings = AppSettings(
        download_storage=DownloadStorageSettings(str(selected.resolve()))
    )
    dialog = SettingsDialog(settings, default_download_root=default)
    qtbot.addWidget(dialog)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_a, **_k: "")

    qtbot.mouseClick(
        dialog.browse_download_root_button,
        Qt.MouseButton.LeftButton,
    )
    assert dialog.download_root.text() == str(selected.resolve())

    qtbot.mouseClick(
        dialog.reset_download_root_button,
        Qt.MouseButton.LeftButton,
    )

    assert dialog.download_root.text() == str(default.resolve())
    assert dialog.download_root.cursorPosition() == 0
    assert dialog.values().download_storage.root == ""


def test_settings_values_preserve_storage_maintenance_policy(qtbot) -> None:
    settings = AppSettings(
        storage_maintenance=StorageMaintenanceSettings(automatic_enabled=True)
    )
    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)

    assert dialog.values().storage_maintenance == settings.storage_maintenance


def test_download_naming_tab_rejects_unsafe_template_before_save(qtbot) -> None:
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)
    emitted = []
    dialog.save_requested.connect(lambda: emitted.append(True))

    dialog.directory_template.setEditText("../{source}")
    qtbot.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)

    assert emitted == []
    assert dialog.error_label.isHidden() is False
    assert "安全相对路径" in dialog.error_label.text()
    assert "模板错误" in dialog.naming_preview.text()


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
