from __future__ import annotations

import re

import qrcode
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from qrcode.constants import ERROR_CORRECT_M

_QR_LOGIN_URL = re.compile(r"^tg://login\?token=[A-Za-z0-9_-]+$")
_BOX_SIZE = 8


def render_qr_image(value: str) -> QImage:
    if _QR_LOGIN_URL.fullmatch(value) is None:
        raise ValueError("二维码登录地址无效")

    qr = qrcode.QRCode(
        error_correction=ERROR_CORRECT_M,
        box_size=_BOX_SIZE,
        border=4,
    )
    qr.add_data(value)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    side = len(matrix) * _BOX_SIZE
    image = QImage(side, side, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)

    painter = QPainter(image)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(Qt.GlobalColor.black)
    for row, values in enumerate(matrix):
        for column, filled in enumerate(values):
            if filled:
                painter.drawRect(
                    column * _BOX_SIZE,
                    row * _BOX_SIZE,
                    _BOX_SIZE,
                    _BOX_SIZE,
                )
    painter.end()
    return image
