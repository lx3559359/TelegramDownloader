import importlib

import pytest


def render_qr_image(value: str):
    module = importlib.import_module("telegram_downloader.ui.qr")
    return module.render_qr_image(value)


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


@pytest.mark.parametrize(
    "value",
    ["", "https://example.com/qr", "tg://login?token=", "tg://login?token=has space"],
)
def test_render_qr_image_rejects_non_telegram_login_values(value: str) -> None:
    with pytest.raises(ValueError, match="二维码登录地址无效"):
        render_qr_image(value)
