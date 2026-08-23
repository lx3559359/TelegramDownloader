from __future__ import annotations

import importlib
import importlib.util


def test_api_guide_module_exposes_only_official_portal_url() -> None:
    spec = importlib.util.find_spec("telegram_downloader.ui.api_guide")

    assert spec is not None
    module = importlib.import_module("telegram_downloader.ui.api_guide")
    assert module.API_PORTAL_URL == "https://my.telegram.org/apps"
