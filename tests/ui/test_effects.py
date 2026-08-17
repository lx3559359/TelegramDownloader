from PySide6.QtWidgets import QFrame, QGraphicsDropShadowEffect

from telegram_downloader.ui.effects import ElevationLevel, apply_elevation


def test_major_and_secondary_elevations_have_distinct_strengths(qtbot) -> None:
    major = QFrame()
    secondary = QFrame()
    qtbot.addWidget(major)
    qtbot.addWidget(secondary)

    major_effect = apply_elevation(major, ElevationLevel.MAJOR)
    secondary_effect = apply_elevation(secondary, ElevationLevel.SECONDARY)

    assert isinstance(major_effect, QGraphicsDropShadowEffect)
    assert isinstance(secondary_effect, QGraphicsDropShadowEffect)
    assert major.property("elevation") == "major"
    assert secondary.property("elevation") == "secondary"
    assert major_effect.blurRadius() == 40
    assert major_effect.offset().x() == 1
    assert major_effect.offset().y() == 8
    assert major_effect.color().alpha() == 116
    assert secondary_effect.blurRadius() == 26
    assert secondary_effect.offset().x() == 0
    assert secondary_effect.offset().y() == 5
    assert secondary_effect.color().alpha() == 84


def test_each_card_owns_one_idempotent_shadow_effect(qtbot) -> None:
    first = QFrame()
    second = QFrame()
    qtbot.addWidget(first)
    qtbot.addWidget(second)

    first_effect = apply_elevation(first, ElevationLevel.MAJOR)
    repeated = apply_elevation(first, ElevationLevel.MAJOR)
    second_effect = apply_elevation(second, ElevationLevel.MAJOR)

    assert repeated is first_effect
    assert second_effect is not first_effect
    assert first.graphicsEffect() is first_effect
    assert second.graphicsEffect() is second_effect
