import importlib

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor


def render_qr_image(value: str, **options):
    module = importlib.import_module("telegram_downloader.ui.qr")
    return module.render_qr_image(value, **options)


def image_bytes(image) -> bytes:
    return bytes(image.constBits())


def test_render_qr_image_is_deterministic_square_and_fileless(tmp_path) -> None:
    first = render_qr_image("tg://login?token=abc_123")
    second = render_qr_image("tg://login?token=abc_123")
    different = render_qr_image("tg://login?token=xyz-789")

    assert not first.isNull()
    assert first.width() == first.height()
    assert first.width() >= 200
    assert image_bytes(first) == image_bytes(second)
    assert image_bytes(first) != image_bytes(different)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("token_length", [8, 43, 86, 128])
def test_render_qr_image_fits_max_side_with_integer_modules(token_length: int) -> None:
    image = render_qr_image(
        f"tg://login?token={'a' * token_length}",
        max_side=300,
    )

    assert image.width() == image.height()
    assert 200 <= image.width() <= 300


def test_render_qr_image_preserves_four_module_quiet_zone() -> None:
    image = render_qr_image(f"tg://login?token={'a' * 43}", max_side=300)
    black = QColor(Qt.GlobalColor.black).rgb()
    black_points = [
        (x, y)
        for y in range(image.height())
        for x in range(image.width())
        if image.pixel(x, y) == black
    ]

    quiet = min(min(x for x, _ in black_points), min(y for _, y in black_points))
    assert quiet % 4 == 0
    module_pixels = quiet // 4
    assert module_pixels >= 1
    white = QColor(Qt.GlobalColor.white)
    assert all(
        image.pixelColor(x, y) == white
        for y in range(quiet)
        for x in range(image.width())
    )
    assert all(
        image.pixelColor(x, y) == white
        for y in range(image.height() - quiet, image.height())
        for x in range(image.width())
    )
    assert all(
        image.pixelColor(x, y) == white
        for y in range(image.height())
        for x in range(quiet)
    )
    assert all(
        image.pixelColor(x, y) == white
        for y in range(image.height())
        for x in range(image.width() - quiet, image.width())
    )


def test_render_qr_image_rejects_viewport_smaller_than_module_count() -> None:
    with pytest.raises(ValueError):
        render_qr_image("tg://login?token=abc_123", max_side=16)


@pytest.mark.parametrize(
    "value",
    ["", "https://example.com/qr", "tg://login?token=", "tg://login?token=has space"],
)
def test_render_qr_image_rejects_non_telegram_login_values(value: str) -> None:
    with pytest.raises(ValueError, match="二维码登录地址无效"):
        render_qr_image(value)
