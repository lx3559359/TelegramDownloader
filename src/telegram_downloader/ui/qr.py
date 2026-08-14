from __future__ import annotations

import re

import qrcode
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from qrcode.constants import ERROR_CORRECT_M

_QR_LOGIN_URL = re.compile(r"^tg://login\?token=[A-Za-z0-9_-]+$")
_MAX_MODULE_PIXELS = 8
_DEFAULT_MAX_SIDE = 300


def render_qr_image(value: str, *, max_side: int = _DEFAULT_MAX_SIDE) -> QImage:
    if _QR_LOGIN_URL.fullmatch(value) is None:
        raise ValueError("二维码登录地址无效")

    if max_side < 1:
        raise ValueError("QR viewport must be positive")

    qr = qrcode.QRCode(
        error_correction=ERROR_CORRECT_M,
        box_size=1,
        border=4,
    )
    qr.add_data(value)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    module_pixels = min(_MAX_MODULE_PIXELS, max_side // len(matrix))
    if module_pixels < 1:
        raise ValueError("QR viewport is smaller than the QR module count")
    side = len(matrix) * module_pixels
    image = QImage(side, side, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)

    painter = QPainter(image)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(Qt.GlobalColor.black)
    for row, values in enumerate(matrix):
        for column, filled in enumerate(values):
            if filled:
                painter.drawRect(
                    column * module_pixels,
                    row * module_pixels,
                    module_pixels,
                    module_pixels,
                )
    painter.end()
    return image
