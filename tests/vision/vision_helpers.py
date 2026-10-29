"""Помощники тестов картинок: генерация картинок на лету, подмена провайдера."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from rabota.vision.providers import VisionProvider, empty_result

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "vision"
SIDECAR_DIR = FIXTURES / "sidecar"


def make_image(seed: int = 0, size: int = 256, fmt: str = "PNG") -> bytes:
    """Картинка с узнаваемой структурой: разные seed дают далёкие друг от друга хеши."""
    img = Image.new("RGB", (size, size), (240, 240, 240))
    d = ImageDraw.Draw(img)
    rnd = seed * 2654435761 % 2**32
    for i in range(14):
        rnd = (rnd * 1103515245 + 12345) % 2**31
        x, y = rnd % (size - 60), (rnd >> 8) % (size - 60)
        w = 20 + (rnd >> 16) % 80
        d.rectangle((x, y, x + w, y + 10 + (rnd >> 4) % 70), fill=((rnd >> 3) % 255, (rnd >> 5) % 255, (rnd >> 7) % 255))
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


class CountingProvider(VisionProvider):
    """Отдаёт заданный ответ и считает вызовы."""

    def __init__(self, result: dict[str, Any] | None = None, cacheable: bool = False):
        self.result = result or empty_result("ad")
        self.cacheable = cacheable
        self.calls: list[tuple[int, str]] = []

    def extract(self, image_bytes: bytes, hint_text: str = "") -> dict[str, Any]:
        self.calls.append((len(image_bytes), hint_text))
        return self.result
