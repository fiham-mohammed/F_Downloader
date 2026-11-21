"""Regenerates browser_extension/icons/icon{16,32,48,128}.png.

Usage (from the project root):
    .venv\\Scripts\\python tools\\generate_extension_icons.py
"""
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen

OUT_DIR = Path(__file__).resolve().parent.parent / "browser_extension" / "icons"


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    s = float(size)

    grad = QLinearGradient(0, 0, s, s)
    grad.setColorAt(0.0, QColor("#16243a"))
    grad.setColorAt(1.0, QColor("#0b1220"))
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(0, 0, s, s), s * 0.22, s * 0.22)
    p.fillPath(bg, QBrush(grad))
    if size >= 32:
        p.setPen(QPen(QColor("#2a3c5c"), max(1.0, s / 64)))
        p.drawPath(bg)

    blue = QColor("#3b82f6")
    p.setPen(Qt.NoPen)
    p.setBrush(blue)
    # Arrow shaft
    shaft_w = s * 0.16
    p.drawRoundedRect(QRectF(s / 2 - shaft_w / 2, s * 0.18, shaft_w, s * 0.34), shaft_w * 0.3, shaft_w * 0.3)
    # Arrow head
    head = QPainterPath()
    head.moveTo(QPointF(s * 0.26, s * 0.46))
    head.lineTo(QPointF(s * 0.74, s * 0.46))
    head.lineTo(QPointF(s * 0.50, s * 0.70))
    head.closeSubpath()
    p.drawPath(head)
    # Tray
    p.setBrush(QColor("#e7edf7"))
    p.drawRoundedRect(QRectF(s * 0.22, s * 0.76, s * 0.56, s * 0.09), s * 0.04, s * 0.04)
    p.end()
    return img


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 128):
        path = OUT_DIR / f"icon{size}.png"
        if not render(size).save(str(path), "PNG"):
            print(f"Failed to write {path}", file=sys.stderr)
            return 1
        print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
