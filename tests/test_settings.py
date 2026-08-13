import json

import pytest

from telegram_downloader.settings import (
    AppSettings,
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
    ],
)
def test_invalid_settings_are_rejected(tmp_path, settings) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings), encoding="utf-8")

    with pytest.raises(SettingsError):
        SettingsStore(path).load()
