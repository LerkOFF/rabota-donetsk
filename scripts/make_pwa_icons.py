"""Рисует значки сайта из знака (rabota/web/static/favicon.svg) средствами Pillow, без внешних сервисов.

Запуск: .venv/bin/python scripts/make_pwa_icons.py
Результат в rabota/web/static/icons/:
  icon-192.png, icon-512.png     значки установки на телефон (скруглённые), icon-512 же идёт логотипом в разметке Organization
  maskable-512.png               фон до краёв, знак в безопасной зоне
  apple-touch-icon.png           180x180 без прозрачности
  favicon-48.png, favicon-120.png   для поисковых систем (Google берёт кратные 48, Яндекс просит от 120)
  favicon.ico                    16, 32 и 48 в одном файле, его отдаёт сайт по адресу /favicon.ico

Знак в svg задан в квадрате 32x32 (портфель с коралловым замком); здесь повторены его контуры,
при смене знака этот файл нужно обновить вместе с favicon.svg и логотипом в base.html.
"""
from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "rabota" / "web" / "static" / "icons"
ACCENT = (37, 99, 235, 255)
WHITE = (255, 255, 255, 255)
CORAL = (255, 148, 120, 255)
SS = 8  # рисуем в 8 раз крупнее и уменьшаем: гладкие края даже на 16 пикселях
CENTER = (16.0, 15.75)  # центр знака в системе 32x32 (по высоте знак идёт от 6 до 25.5)


def glyph(d: ImageDraw.ImageDraw, tr, k: float) -> None:
    """Портфель: ручка, корпус, шов и замок. tr переводит точку svg в пиксели, k это пикселей на единицу svg."""

    def box(x0, y0, x1, y1):
        return (*tr((x0, y0)), *tr((x1, y1)))

    d.rounded_rectangle(box(10.4, 6.0, 21.6, 14.0), radius=3.8 * k, fill=WHITE)  # ручка снаружи
    d.rounded_rectangle(box(12.8, 8.4, 19.2, 14.0), radius=1.4 * k, fill=ACCENT)  # ручка внутри
    d.rounded_rectangle(box(5.5, 11.0, 26.5, 25.5), radius=3.4 * k, fill=WHITE)  # корпус
    d.rectangle(box(5.5, 16.85, 26.5, 18.35), fill=ACCENT)  # шов
    d.rounded_rectangle(box(13.6, 15.4, 18.4, 20.0), radius=1.3 * k, fill=CORAL)  # замок


def draw(size: int, *, rounded: bool, glyph_scale: float) -> Image.Image:
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if rounded:
        d.rounded_rectangle((0, 0, big - 1, big - 1), radius=big * 8 // 32, fill=ACCENT)
    else:
        d.rectangle((0, 0, big, big), fill=ACCENT)
    k = big * glyph_scale / 32  # знак занимает glyph_scale от стороны и стоит по центру
    ox = big / 2 - CENTER[0] * k
    oy = big / 2 - CENTER[1] * k

    def tr(p):
        return (ox + p[0] * k, oy + p[1] * k)

    glyph(d, tr, k)
    return img.resize((size, size), Image.LANCZOS)


def write_ico(path: Path, images: list[Image.Image]) -> None:
    """ICO из PNG-кадров: каждый размер нарисован заново, а не уменьшен из одного большого."""
    blobs = []
    for im in images:
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        blobs.append(buf.getvalue())
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for im, blob in zip(images, blobs, strict=True):
        w, h = im.size
        entries += struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    path.write_bytes(head + entries + b"".join(blobs))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    draw(192, rounded=True, glyph_scale=0.86).save(OUT / "icon-192.png", optimize=True)
    draw(512, rounded=True, glyph_scale=0.86).save(OUT / "icon-512.png", optimize=True)
    # maskable: фон до краёв, знак в безопасной зоне (центральные 80 процентов)
    draw(512, rounded=False, glyph_scale=0.6).save(OUT / "maskable-512.png", optimize=True)
    draw(180, rounded=False, glyph_scale=0.7).convert("RGB").save(OUT / "apple-touch-icon.png", optimize=True)
    # вкладка браузера и выдача поиска: знак крупнее, чтобы читался на 16 пикселях
    draw(48, rounded=True, glyph_scale=0.94).save(OUT / "favicon-48.png", optimize=True)
    draw(120, rounded=True, glyph_scale=0.9).save(OUT / "favicon-120.png", optimize=True)
    write_ico(OUT / "favicon.ico", [draw(s, rounded=True, glyph_scale=1.0 if s <= 32 else 0.94) for s in (16, 32, 48)])


if __name__ == "__main__":
    main()
