"""Распознавание афиш через Tesseract: текст в формат провайдеров. Сам Tesseract в тестах подменён."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from rabota.vision import ocr, providers

POSTER = """ТРЕБУЕТСЯ ПРОДАВЕЦ-КАССИР
в магазин продуктов, Донецк, Ворошиловский район
График 2/2, оплата от 3500 за смену
Звоните: +7 949 123-45-67"""


def png(color=200, size=(300, 200)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (color, color, color)).save(out, format="PNG")
    return out.getvalue()


def test_poster_text_becomes_vacancy_with_contact():
    res = ocr.result_from_text(POSTER)
    assert res["kind"] == "vacancy"
    v = res["vacancies"][0]
    assert v["profession"].lower() in ("кассир", "продавец", "продавец-кассир")
    assert v["phones"] == ["+79491234567"]
    assert res["raw_text"].startswith("ТРЕБУЕТСЯ")


def test_vacancy_without_contact_is_rejected():
    res = ocr.result_from_text("ТРЕБУЕТСЯ ПРОДАВЕЦ-КАССИР в магазин продуктов, Донецк, график 2/2, оплата от 3500 за смену")
    assert res["kind"] == "other" and res["vacancies"] == []


def test_short_text_is_not_a_vacancy():
    assert ocr.result_from_text("Логотип")["kind"] == "other"


def test_provider_uses_ocr_text(monkeypatch):
    monkeypatch.setattr(ocr, "ocr_text", lambda data: POSTER)
    res = ocr.OcrProvider().extract(png())
    assert res["kind"] == "vacancy" and res["vacancies"][0]["phones"]


def test_missing_tesseract_is_provider_error(monkeypatch):
    monkeypatch.setattr(ocr, "tesseract_available", lambda: False)
    with pytest.raises(providers.ProviderError):
        ocr.ocr_text(png())


def test_prepare_upscales_small_to_working_width():
    small = Image.open(io.BytesIO(ocr.prepare(png(size=(300, 200)))))
    assert small.width >= 2000 and small.mode == "L"


def test_variants_have_original_inverted_flat_and_binary_forms():
    v = ocr.build_variants(png(color=20, size=(300, 200)))
    assert set(v) == {"orig", "inv", "flat", "flatinv", "bin", "bininv"}
    assert v["inv"].getpixel((5, 5)) > 128  # тёмный фон в инверсии стал светлым
    assert set(v["bin"].getextrema()) <= {0, 255}


def test_provider_from_env_ocr(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ocr")
    monkeypatch.setattr(ocr, "tesseract_available", lambda: True)
    assert isinstance(providers.provider_from_env(), ocr.OcrProvider)
    monkeypatch.setattr(ocr, "tesseract_available", lambda: False)
    assert isinstance(providers.provider_from_env(), providers.NullProvider)
