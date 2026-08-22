import json

import pytest

from telegram_downloader.settings import (
    AppSettings,
    DownloadScheduleSettings,
    ProxySettings,
    SettingsError,
    SettingsStore,
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
