import struct

from PySide6.QtGui import QImage

from telegram_downloader.branding import (
    APP_DISPLAY_NAME,
    APP_NAME,
    APP_SUBTITLE,
    app_icon_path,
    app_logo_path,
    resource_directory,
)


def test_approved_brand_contract() -> None:
    assert APP_NAME == "TG 快取"
    assert APP_SUBTITLE == "媒体下载器"
    assert APP_DISPLAY_NAME == "TG 快取 · 媒体下载器"


def test_brand_assets_are_complete_and_text_free() -> None:
    icon = app_icon_path()
    logo = app_logo_path()
    svg = resource_directory() / "tg_quick_fetch.svg"

    assert icon.name == "tg_quick_fetch.ico"
    assert icon.is_file()
    assert logo.is_file()
    assert svg.is_file()
    assert "<text" not in svg.read_text(encoding="utf-8").casefold()

    payload = icon.read_bytes()
    reserved, image_type, count = struct.unpack_from("<HHH", payload)
    assert (reserved, image_type, count) == (0, 1, 8)
    sizes = []
    for index in range(count):
        width, height, _, _, _, _, length, offset = struct.unpack_from(
            "<BBBBHHII",
            payload,
            6 + 16 * index,
        )
        sizes.append(256 if width == 0 else width)
        assert (256 if height == 0 else height) == sizes[-1]
        assert payload[offset : offset + 8] == b"\x89PNG\r\n\x1a\n"
        assert length > 8
    assert sizes == [16, 20, 24, 32, 48, 64, 128, 256]

    image = QImage(str(logo))
    assert image.size().width() == 256
    assert image.size().height() == 256
    assert image.pixelColor(0, 0).alpha() == 0
