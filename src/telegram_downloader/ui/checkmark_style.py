from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QProxyStyle,
    QStyle,
    QStyleOption,
    QWidget,
)


class CheckmarkProxyStyle(QProxyStyle):
    """Paints consistent, explicit checkmarks for widgets and item views."""

    _INDICATORS = {
        QStyle.PrimitiveElement.PE_IndicatorCheckBox,
        QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck,
    }

    def drawPrimitive(
        self,
        element: QStyle.PrimitiveElement,
        option: QStyleOption,
        painter: QPainter,
        widget: QWidget | None = None,
    ) -> None:
        if element not in self._INDICATORS:
            super().drawPrimitive(element, option, painter, widget)
            return

        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        checked = bool(option.state & QStyle.StateFlag.State_On)
        side = min(18.0, float(option.rect.width()), float(option.rect.height()))
        left = option.rect.center().x() - side / 2
        top = option.rect.center().y() - side / 2
        indicator = QRectF(left, top, side, side).adjusted(0.75, 0.75, -0.75, -0.75)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(23, 168, 194, 105), 1.5))
            painter.drawRoundedRect(indicator.adjusted(-1.5, -1.5, 1.5, 1.5), 5, 5)

        if checked:
            fill = QColor("#17A8C2") if enabled else QColor("#D3DDE3")
            border = QColor("#17A8C2") if enabled else QColor("#B7C4CB")
        else:
            fill = QColor("#FFFFFF") if enabled else QColor("#F1F5F7")
            border = QColor("#9FB1C4") if enabled else QColor("#C7D1D7")
        painter.setBrush(fill)
        painter.setPen(QPen(border, 1.25))
        painter.drawRoundedRect(indicator, 4, 4)

        if checked:
            check_rect = indicator.adjusted(4.0, 4.0, -4.0, -4.0)
            check_color = QColor("#FFFFFF") if enabled else QColor("#B7C4CB")
            pen = QPen(
                check_color,
                2.0,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(pen)
            path = QPainterPath()
            path.moveTo(QPointF(check_rect.left(), check_rect.center().y()))
            path.lineTo(QPointF(check_rect.center().x() - 1, check_rect.bottom()))
            path.lineTo(QPointF(check_rect.right(), check_rect.top()))
            painter.drawPath(path)
        painter.restore()

    def pixelMetric(
        self,
        metric: QStyle.PixelMetric,
        option: QStyleOption | None = None,
        widget: QWidget | None = None,
    ) -> int:
        if metric in {
            QStyle.PixelMetric.PM_IndicatorWidth,
            QStyle.PixelMetric.PM_IndicatorHeight,
        }:
            return 18
        return super().pixelMetric(metric, option, widget)


def install_checkmark_style(application: QApplication) -> CheckmarkProxyStyle:
    current = application.style()
    if isinstance(current, CheckmarkProxyStyle):
        application.setProperty("telegramCheckmarkStyleInstalled", True)
        return current
    # QProxyStyle takes ownership of an explicitly supplied base style. Passing
    # QApplication.style() would delete the application-owned style when a
    # temporary proxy is destroyed, so let Qt create an independent base.
    proxy = CheckmarkProxyStyle()
    application.setStyle(proxy)
    application.setProperty("telegramCheckmarkStyleInstalled", True)
    return proxy
