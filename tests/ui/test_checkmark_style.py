from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QStyle, QStyleOptionButton

from telegram_downloader.ui.checkmark_style import (
    CheckmarkProxyStyle,
    install_checkmark_style,
)
from telegram_downloader.ui.theme import APP_STYLESHEET


def render_indicator(
    style,
    state: QStyle.StateFlag,
    element: QStyle.PrimitiveElement = QStyle.PrimitiveElement.PE_IndicatorCheckBox,
) -> QImage:
    image = QImage(24, 24, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    option = QStyleOptionButton()
    option.rect = QRect(3, 3, 18, 18)
    option.state = state
    style.drawPrimitive(
        element,
        option,
        painter,
    )
    painter.end()
    return image


def image_colors(image: QImage):
    return [
        image.pixelColor(x, y)
        for x in range(image.width())
        for y in range(image.height())
    ]


def test_checked_indicator_contains_brand_fill_and_white_check(qapp) -> None:
    style = CheckmarkProxyStyle()

    image = render_indicator(
        style,
        QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_On,
    )

    colors = image_colors(image)
    assert sum(color.red() < 40 and color.green() > 130 for color in colors) > 20
    assert sum(min(color.red(), color.green(), color.blue()) > 235 for color in colors) >= 4


def test_unchecked_indicator_has_no_brand_fill(qapp) -> None:
    style = CheckmarkProxyStyle()

    image = render_indicator(style, QStyle.StateFlag.State_Enabled)

    colors = image_colors(image)
    assert sum(color.red() < 40 and color.green() > 130 for color in colors) == 0
    assert sum(min(color.red(), color.green(), color.blue()) > 235 for color in colors) > 20


def test_disabled_checked_indicator_is_not_solid_brand_color(qapp) -> None:
    style = CheckmarkProxyStyle()

    image = render_indicator(style, QStyle.StateFlag.State_On)

    colors = image_colors(image)
    assert sum(color.red() < 40 and color.green() > 130 for color in colors) == 0
    assert any(150 <= color.red() <= 230 for color in colors if color.alpha())


def test_item_view_checked_indicator_also_contains_white_check(qapp) -> None:
    style = CheckmarkProxyStyle()

    image = render_indicator(
        style,
        QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_On,
        QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck,
    )

    colors = image_colors(image)
    assert sum(min(color.red(), color.green(), color.blue()) > 235 for color in colors) >= 4


def test_focus_state_draws_pixels_outside_indicator(qapp) -> None:
    style = CheckmarkProxyStyle()

    image = render_indicator(
        style,
        QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_HasFocus,
    )

    assert image.pixelColor(2, 12).alpha() > 0


def test_install_checkmark_style_is_idempotent(qapp) -> None:
    first = install_checkmark_style(qapp)
    second = install_checkmark_style(qapp)

    assert first is second
    assert qapp.style() is first
    assert qapp.property("telegramCheckmarkStyleInstalled") is True


def test_theme_does_not_override_proxy_indicator_painting() -> None:
    assert "QCheckBox::indicator" not in APP_STYLESHEET
