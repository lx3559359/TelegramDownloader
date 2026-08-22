import json

import pytest

from telegram_downloader.files import DownloadNamingSettings
from telegram_downloader.settings import (
    AppSettings,
    DownloadScheduleSettings,
    ProxySettings,
    SettingsError,
    SettingsStore,
    StorageMaintenanceSettings,
)


def test_settings_round_trip_is_atomic(tmp_path) -> None:
    path = tmp_path / "config" / "settings.json"
    store = SettingsStore(path)
    settings = AppSettings(
        api_id=123,
        concurrency=3,
        proxy=ProxySettings("socks5", "127.0.0.1", 1080, "u"),
    )

    store.save(settings)

    assert store.load() == settings
    assert not path.with_suffix(".json.tmp").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["proxy"]["kind"] == "socks5"


def test_missing_settings_use_safe_defaults(tmp_path) -> None:
    assert SettingsStore(tmp_path / "missing.json").load() == AppSettings()


def test_background_settings_have_safe_compatible_defaults(tmp_path) -> None:
    loaded = SettingsStore(tmp_path / "missing.json").load()

    assert loaded.close_to_tray is True
    assert loaded.notifications_enabled is True
    assert loaded.autostart_enabled is False
    assert loaded.tray_hint_shown is False
    assert loaded.download_schedule == DownloadScheduleSettings()
    assert loaded.download_naming == DownloadNamingSettings()


def test_storage_maintenance_defaults_are_fixed_and_opt_in() -> None:
    value = StorageMaintenanceSettings()

    assert value.automatic_enabled is False
    assert value.temp_retention_days == 7
    assert value.log_retention_days == 30
    assert value.thumbnail_limit_bytes == 1024**3
    assert value.thumbnail_target_bytes == 900 * 1024**2
    assert value.update_staging_retention_days == 7
    assert value.update_backup_keep_count == 1
    assert value.check_interval_seconds == 86400
    assert value.startup_delay_seconds == 300
    assert value.idle_required_seconds == 60
    assert value.busy_retry_seconds == 900


def test_old_settings_default_storage_maintenance_to_disabled(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"api_id":7,"concurrency":2}', encoding="utf-8")

    loaded = SettingsStore(path).load()

    assert loaded.api_id == 7
    assert loaded.concurrency == 2
    assert loaded.storage_maintenance == StorageMaintenanceSettings()


def test_storage_maintenance_enabled_round_trips_as_nested_json(tmp_path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    settings = AppSettings(
        storage_maintenance=StorageMaintenanceSettings(automatic_enabled=True)
    )

    store.save(settings)

    assert store.load() == settings
    assert json.loads(path.read_text(encoding="utf-8"))["storage_maintenance"][
        "automatic_enabled"
    ] is True


@pytest.mark.parametrize(
    "value",
    [
        {"automatic_enabled": 1},
        {"temp_retention_days": 8},
        {"temp_retention_days": 7.0},
        {"thumbnail_target_bytes": 1},
    ],
)
def test_storage_maintenance_rejects_unsupported_values(value) -> None:
    with pytest.raises(SettingsError):
        StorageMaintenanceSettings(**value)


def test_download_naming_settings_round_trip_as_nested_json(tmp_path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    naming = DownloadNamingSettings(
        "{year}/{month}/{source}/{media_type}",
        "{stem}_{message_id}{extension}",
    )
    settings = AppSettings(download_naming=naming)

    store.save(settings)

    assert store.load() == settings
    assert json.loads(path.read_text(encoding="utf-8"))["download_naming"] == {
        "directory_template": "{year}/{month}/{source}/{media_type}",
        "filename_template": "{stem}_{message_id}{extension}",
    }


@pytest.mark.parametrize(
    "value",
    [
        {"weekdays": []},
        {"weekdays": None},
        {"weekdays": [0, 7]},
        {"start_minute": -1},
        {"end_minute": 1440},
    ],
)
def test_download_schedule_rejects_invalid_values(value) -> None:
    with pytest.raises(SettingsError):
        DownloadScheduleSettings(**value)


def test_old_settings_json_loads_with_new_defaults(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"api_id":123,"concurrency":3}', encoding="utf-8")

    loaded = SettingsStore(path).load()

    assert loaded.api_id == 123
    assert loaded.download_schedule.enabled is False
    assert loaded.download_naming == DownloadNamingSettings()


def test_invalid_download_naming_json_is_rejected(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "download_naming": {
                    "directory_template": "../{source}",
                    "filename_template": "{original_name}",
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SettingsError, match="目录模板"):
        SettingsStore(path).load()


def test_old_settings_default_to_unlimited_speed(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "api_id": 1,
                "concurrency": 3,
                "proxy": {},
                "check_updates_on_startup": True,
            }
        ),
        encoding="utf-8",
    )

    assert SettingsStore(path).load().speed_limit_kib == 0


def test_speed_limit_round_trip_preserves_positional_settings_contract(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    expected = AppSettings(
        1,
        4,
        ProxySettings(),
        False,
        speed_limit_kib=2048,
    )

    store.save(expected)

    assert store.load() == expected
    assert expected.check_updates_on_startup is False
    assert expected.speed_limit_kib == 2048


@pytest.mark.parametrize(
    "settings",
    [
        {"api_id": -1, "concurrency": 3, "proxy": {}},
        {"api_id": 1, "concurrency": 0, "proxy": {}},
        {"api_id": 1, "concurrency": 6, "proxy": {}},
        {
            "api_id": 1,
            "concurrency": 3,
            "proxy": {"kind": "socks5", "host": "", "port": 1080},
        },
        {"api_id": 1, "concurrency": 3, "proxy": {}, "speed_limit_kib": -1},
        {"api_id": 1, "concurrency": 3, "proxy": {}, "speed_limit_kib": True},
        {
            "api_id": 1,
            "concurrency": 3,
            "proxy": {},
            "speed_limit_kib": 1_048_577,
        },
    ],
)
def test_invalid_settings_are_rejected(tmp_path, settings) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings), encoding="utf-8")

    with pytest.raises(SettingsError):
        SettingsStore(path).load()
