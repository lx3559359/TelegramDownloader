from __future__ import annotations

from pathlib import Path

APP_NAME = "TG 快取"
APP_SUBTITLE = "媒体下载器"
APP_DISPLAY_NAME = f"{APP_NAME} · {APP_SUBTITLE}"
APP_CHANNEL = "stable"


def resource_directory() -> Path:
    return Path(__file__).resolve().parent / "resources"


def app_icon_path() -> Path:
    return resource_directory() / "tg_quick_fetch.ico"


def app_logo_path() -> Path:
    return resource_directory() / "tg_quick_fetch-256.png"
