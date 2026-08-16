from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QWidget


class ElevationLevel(StrEnum):
    MAJOR = "major"
    SECONDARY = "secondary"


@dataclass(frozen=True, slots=True)
class ShadowSpec:
    blur_radius: float
    x_offset: float
    y_offset: float
    color: tuple[int, int, int, int]


_SHADOWS = {
    ElevationLevel.MAJOR: ShadowSpec(40, 1, 8, (38, 52, 72, 116)),
    ElevationLevel.SECONDARY: ShadowSpec(26, 0, 5, (49, 65, 85, 84)),
}


def apply_elevation(
    widget: QWidget,
    level: ElevationLevel,
) -> QGraphicsDropShadowEffect:
    current = widget.graphicsEffect()
    if (
        isinstance(current, QGraphicsDropShadowEffect)
        and widget.property("elevation") == level.value
    ):
        return current

    spec = _SHADOWS[level]
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(spec.blur_radius)
    effect.setOffset(spec.x_offset, spec.y_offset)
    effect.setColor(QColor(*spec.color))
    widget.setGraphicsEffect(effect)
    widget.setProperty("elevation", level.value)
    return effect
