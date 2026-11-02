"""Движок чтения афиш: выбор лучшего прогона по качеству текста, чтение заголовка в полосах, склейка с подписью поста.

Сам Tesseract подменён: прогоны возвращают заготовленный текст. Настоящее чтение афиш: test_vision_posters.py.
"""
from __future__ import annotations

import io
import itertools
from pathlib import Path

import pytest
from PIL import Image

from rabota.vision import ocr
from rabota.vision.ocr_quality import assess, readable
from rabota.vision.providers import ProviderError

TEXT_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "vision" / "ocr_text"
JUNK_TEXT = '„оо\nee hE\n| ] >\nsf a cf\n=\n.) №:\npce @\n+79495055755'
GOOD_TEXT = "ИЩЕМ АВТОМОЙЩИКОВ\nКого ищем: опытных автомойщиков\nГрафик 2/2, зарплата процент от выработки\n+7 949 505-57-55"


def jpeg(size=(534, 800), color=30) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (color, color, color)).save(out, format="JPEG")
    return out.getvalue()


def test_meaningful_text_scores_above_longer_noise():
    good, junk = assess(GOOD_TEXT), assess(JUNK_TEXT * 5)
    assert good.score > junk.score
    assert good.has_profession and good.phones == 1
    assert readable(GOOD_TEXT) and not readable(JUNK_TEXT * 5)


def test_best_run_is_chosen_by_quality_not_length(monkeypatch):
    by_psm = {3: JUNK_TEXT * 6, 11: GOOD_TEXT}
    monkeypatch.setattr(ocr, "_tesseract", lambda path, psm: by_psm.get(psm, ""))
    monkeypatch.setattr(ocr, "FULL_PLAN", (("orig", 3), ("orig", 11)))
    reading = ocr.read_image(jpeg())
    assert reading.best.psm == 11
    assert "АВТОМОЙЩИКОВ" in reading.text


def test_headline_found_in_strip_is_added_to_the_front(monkeypatch):
    base = "Кого ищем: опытных сотрудников\nГрафик 2/2, зарплата процент от выработки\n+7 949 505-57-55\nОбучение и спецодежда"

    def fake(path, psm):
        return "ИЩЕМ АВТОМОЙЩИКОВ в новую студию" if psm == 6 and "strip" in str(path) else base

    monkeypatch.setattr(ocr, "_tesseract", fake)
    monkeypatch.setattr(ocr, "FULL_PLAN", (("orig", 3),))
    reading = ocr.read_image(jpeg())
    assert reading.text.splitlines()[0] == "ИЩЕМ АВТОМОЙЩИКОВ в новую студию"
    assert "Кого ищем" in reading.text


def test_no_strips_for_an_empty_picture(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(ocr, "_tesseract", lambda path, psm: calls.append(psm) or "")
    monkeypatch.setattr(ocr, "FULL_PLAN", (("orig", 3), ("orig", 11)))
    assert ocr.read_image(jpeg()).text.strip() == ""
    assert len(calls) == 2


def test_failed_runs_are_skipped_but_all_failed_is_provider_error(monkeypatch):
    def flaky(path, psm):
        if psm == 3:
            raise ProviderError("timeout")
        return GOOD_TEXT

    monkeypatch.setattr(ocr, "_tesseract", flaky)
    monkeypatch.setattr(ocr, "FULL_PLAN", (("orig", 3), ("orig", 11)))
    assert "АВТОМОЙЩИКОВ" in ocr.read_image(jpeg()).text

    def dead(path, psm):
        raise ProviderError("нет")

    monkeypatch.setattr(ocr, "_tesseract", dead)
    with pytest.raises(ProviderError):
        ocr.read_image(jpeg())


def test_budget_stops_further_runs(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(ocr, "_tesseract", lambda path, psm: calls.append(psm) or "")
    monkeypatch.setattr(ocr, "FULL_PLAN", (("orig", 3), ("orig", 11), ("orig", 12)))
    ocr.read_image(jpeg(), budget=-1.0)
    assert len(calls) == 1  # первый прогон обязателен, остальные после срока не идут


def test_strip_boxes_cover_the_whole_height_with_overlap():
    boxes = ocr._strip_boxes(1000, 7, 0.4)
    assert boxes[0][0] == 0 and boxes[-1][1] == 1000
    assert all(b[0] < a[1] for a, b in itertools.pairwise(boxes))  # соседние полосы перекрываются


def test_unreadable_noise_never_becomes_a_vacancy():
    res = ocr.result_from_text(JUNK_TEXT * 4)
    assert res["kind"] == "other" and res["vacancies"] == []


def test_poster_text_and_caption_are_joined():
    caption = "По всем вопросам обращаться по телефону:\n+7 949 505-57-55"
    assert ocr.combine_texts("Афиша", caption).endswith(caption)
    assert ocr.combine_texts("Афиша +7 949 505-57-55", "+7 949 505-57-55") == "Афиша +7 949 505-57-55"
    assert ocr.combine_texts("Афиша", "") == "Афиша"


def test_recorded_detailing_poster_text_gives_the_vacancy():
    text = (TEXT_DIR / "poster_detailing.txt").read_text(encoding="utf-8")
    res = ocr.result_from_text(text, "📞 По всем вопросам обращаться по телефону:\n+7 949 505-57-55")
    assert res["kind"] == "vacancy"
    v = res["vacancies"][0]
    assert v["profession"] == "автомойщик" and v["phones"] == ["+79495055755"]
    assert v["city"] == "Донецк" and v["district"] == "Ленинский" and v["schedule"] == "2/2"
    assert "выработки" in v["extra_text"] and "График" in v["extra_text"]


def test_recorded_posters_give_professions_and_phones():
    sales = ocr.result_from_text((TEXT_DIR / "poster_sales_christmas.txt").read_text(encoding="utf-8"))
    assert sales["vacancies"][0]["profession"] == "продавец" and sales["vacancies"][0]["phones"] == ["+79494890599"]
    cleaners = ocr.result_from_text((TEXT_DIR / "poster_cleaners_banner.txt").read_text(encoding="utf-8"))
    assert cleaners["vacancies"][0]["profession"] == "уборщик" and cleaners["vacancies"][0]["phones"]


def test_short_phone_without_code_is_a_contact():
    text = (TEXT_DIR / "poster_detailing.txt").read_text(encoding="utf-8").replace("+79495055755", "")
    assert ocr.short_phone("Звонок на 335 бесплатный с Феникса") == "335"
    assert ocr.short_phone("Заработная плата 45000") is None
    assert ocr.short_phone(text) is None


def test_latin_lookalikes_become_russian_only_inside_russian_lines():
    assert ocr.fix_latin_lookalikes("OT выработки или оклад") == "ОТ выработки или оклад"
    assert ocr.fix_latin_lookalikes("Koch Chemie") == "Koch Chemie"


def test_conditions_text_keeps_confident_condition_lines_only():
    got = ocr.conditions_text("Зарплата; процент | Re\nот выработки или\nоклад + % (обсудим)\nГрафик: 2/2, 3/3 или 5/2\nмусор xq zz\n")
    assert got and "Зарплата" in got and "График: 2/2" in got and "Re" not in got.split()
