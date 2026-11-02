"""Распознавание афиш без платной модели: Tesseract читает текст, дальше работает обычный разбор вакансий.

Включается LLM_PROVIDER=ocr. Нужны пакеты tesseract-ocr и tesseract-ocr-rus (команда tesseract в PATH).

Осторожность вместо догадок: вакансия с картинки принимается, только если из текста получилась вакансия
и в ней есть контакт (телефон или ник Telegram). Без контакта карточка бесполезна, а шум распознавания
чаще всего и даёт такие обрывки. Ответ кэшируется по хешу картинки, одна картинка читается один раз.
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from rabota.models import RawPost
from rabota.vision.images import ImageError, open_image
from rabota.vision.providers import ProviderError, VisionProvider, empty_result, normalize_output

log = logging.getLogger("rabota.vision")

LANGS = "rus+eng"
MIN_SIDE = 1600        # мелкие афиши увеличиваем: Tesseract плохо читает буквы ниже ~20 px
MAX_SIDE = 3000
MIN_TEXT_CHARS = 30    # меньше: логотип или баннер без текста
TIMEOUT = 60


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def prepare(image_bytes: bytes) -> bytes:
    """Серое изображение с контрастом; светлый текст на тёмном фоне переворачиваем в тёмный на светлом."""
    from PIL import ImageOps, ImageStat

    try:
        img = open_image(image_bytes).convert("L")
    except ImageError as exc:
        raise ProviderError(str(exc)) from exc
    side = max(img.size)
    if side < MIN_SIDE:
        scale = MIN_SIDE / side
        img = img.resize((round(img.width * scale), round(img.height * scale)))
    elif side > MAX_SIDE:
        scale = MAX_SIDE / side
        img = img.resize((round(img.width * scale), round(img.height * scale)))
    img = ImageOps.autocontrast(img, cutoff=1)
    if ImageStat.Stat(img).mean[0] < 110:
        img = ImageOps.invert(img)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def ocr_text(image_bytes: bytes) -> str:
    """Текст картинки. ProviderError, если Tesseract не установлен или не ответил."""
    if not tesseract_available():
        raise ProviderError("tesseract не установлен")
    png = prepare(image_bytes)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "in.png"
        path.write_bytes(png)
        try:
            proc = subprocess.run(
                ["tesseract", str(path), "stdout", "-l", LANGS, "--psm", "6"],
                capture_output=True, timeout=TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError("tesseract не ответил за отведённое время") from exc
    if proc.returncode != 0:
        raise ProviderError(f"tesseract завершился с кодом {proc.returncode}")
    return proc.stdout.decode("utf-8", errors="replace")


def _vacancy_dict(v) -> dict[str, Any]:
    contacts = v.contacts or {}
    return {
        "profession": v.profession, "pay_min": v.pay_min, "pay_max": v.pay_max, "pay_period": v.pay_period,
        "city": v.city, "district": v.district, "schedule": v.schedule, "requirements": v.requirements,
        "employer": v.employer, "phones": list(contacts.get("phones") or []), "telegram": contacts.get("telegram"),
        "extra_text": None,
    }


def result_from_text(text: str) -> dict[str, Any]:
    """Ответ в формате провайдеров по распознанному тексту."""
    from rabota.pipeline.run import analyze

    text = text.strip()
    if len(text) < MIN_TEXT_CHARS:
        return empty_result("other", text)
    analysis = analyze(RawPost("ocr", "ocr", "0", "", text))
    if analysis.kind != "vacancy":
        return empty_result(analysis.kind, text)
    vacancies = [_vacancy_dict(v) for v in analysis.vacancies]
    vacancies = [v for v in vacancies if v["phones"] or v["telegram"]]
    if not vacancies:
        return empty_result("other", text)
    return normalize_output({"kind": "vacancy", "vacancies": vacancies, "raw_text": text})


class OcrProvider(VisionProvider):
    cacheable = True

    def extract(self, image_bytes: bytes, hint_text: str = "") -> dict[str, Any]:
        return result_from_text(ocr_text(image_bytes))
