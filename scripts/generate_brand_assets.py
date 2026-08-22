from __future__ import annotations

import argparse
import struct
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
ROOT = Path(__file__).resolve().parents[1]
RESOURCE_DIR = ROOT / "src" / "telegram_downloader" / "resources"
SVG_PATH = RESOURCE_DIR / "tg_quick_fetch.svg"
ICO_PATH = RESOURCE_DIR / "tg_quick_fetch.ico"
PNG_PATH = RESOURCE_DIR / "tg_quick_fetch-256.png"


def render_png(svg_payload: bytes, size: int) -> bytes:
    renderer = QSvgRenderer(QByteArray(svg_payload))
    if not renderer.isValid():
        raise RuntimeError("brand SVG is invalid")
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    output = QBuffer()
    output.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(output, "PNG"):
        raise RuntimeError("brand PNG rendering failed")
    return bytes(output.data())


def pack_ico(images: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = []
    payloads = []
    for size, payload in images:
        entries.append(
            struct.pack(
                "<BBBBHHII",
                0 if size == 256 else size,
                0 if size == 256 else size,
                0,
                0,
                1,
                32,
                len(payload),
                offset,
            )
        )
        payloads.append(payload)
        offset += len(payload)
    return header + b"".join(entries) + b"".join(payloads)


def generated_assets() -> tuple[bytes, bytes]:
    svg_payload = SVG_PATH.read_bytes()
    images = [(size, render_png(svg_payload, size)) for size in SIZES]
    return pack_ico(images), images[-1][1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    ico_payload, png_payload = generated_assets()
    if args.check:
        if not ICO_PATH.is_file() or not PNG_PATH.is_file():
            raise SystemExit("brand assets are missing")
        if ICO_PATH.read_bytes() != ico_payload or PNG_PATH.read_bytes() != png_payload:
            raise SystemExit("brand assets are not reproducible")
        print("brand assets are reproducible")
        return 0
    RESOURCE_DIR.mkdir(parents=True, exist_ok=True)
    ICO_PATH.write_bytes(ico_payload)
    PNG_PATH.write_bytes(png_payload)
    print("brand assets generated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
