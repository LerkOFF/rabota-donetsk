"""Рисует значки установки на телефон из знака сайта (rabota/web/static/favicon.svg) средствами Pillow.

Запуск: .venv/bin/python scripts/make_pwa_icons.py
Результат: rabota/web/static/icons/icon-192.png, icon-512.png, maskable-512.png, apple-touch-icon.png.
Знак в svg задан в квадрате 32x32; здесь повторены его контуры, а при смене знака этот файл нужно обновить.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "rabota" / "web" / "static" / "icons"
ACCENT = (37, 99, 235, 255)
WHITE = (255, 255, 255, 255)
CORAL = (255, 148, 120, 255)
SS = 4  # рисуем в 4 раза крупнее и уменьшаем: гладкие края


def bezier(p0, p1, p2, p3, steps=24):
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t**2, t**3
        pts.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return pts


def mountain():
    pts = [(4, 25), (12.5, 9.5)]
    pts += bezier((12.5, 9.5), (13.5, 7.7), (15.4, 7.7), (16.4, 9.5))
    pts += [(19, 14), (21.2, 10.7)]
    pts += bezier((21.2, 10.7), (22.1, 9.3), (23.9, 9.3), (24.8, 10.7))
    pts += [(29, 25)]
    return pts


def draw(size: int, *, rounded: bool, glyph_scale: float) -> Image.Image:
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if rounded:
        d.rounded_rectangle((0, 0, big - 1, big - 1), radius=big * 8 // 32, fill=ACCENT)
    else:
        d.rectangle((0, 0, big, big), fill=ACCENT)
    # знак занимает glyph_scale от стороны и стоит по центру: центр знака в svg это (16.5, 16)
    k = big * glyph_scale / 32
    ox = big / 2 - 16.5 * k
    oy = big / 2 - 16.0 * k

    def tr(p):
        return (ox + p[0] * k, oy + p[1] * k)

    d.polygon([tr(p) for p in mountain()], fill=WHITE)
    cx, cy = tr((23.5, 7))
    r = 2.6 * k
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=CORAL)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    draw(192, rounded=True, glyph_scale=0.86).save(OUT / "icon-192.png", optimize=True)
    draw(512, rounded=True, glyph_scale=0.86).save(OUT / "icon-512.png", optimize=True)
    # maskable: фон до краёв, знак в безопасной зоне (центральные 80 процентов)
    draw(512, rounded=False, glyph_scale=0.6).save(OUT / "maskable-512.png", optimize=True)
    draw(180, rounded=False, glyph_scale=0.7).convert("RGB").save(OUT / "apple-touch-icon.png", optimize=True)


if __name__ == "__main__":
    main()
