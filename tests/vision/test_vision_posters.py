"""Настоящее чтение афиш Tesseract на образцах из tests/fixtures/vision (скачаны из каналов Telegram).

Нужен tesseract с языком rus (на сервере стоит, см. docs/DEPLOY.md); без него тесты пропускаются. Путь к языковым
данным можно задать переменной OCR_TESSDATA. Профессия и телефон проверяются жёстко, остальные поля мягко:
считаются, но не валят тест (измеренное качество записано в docs/PARSING.md).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from vision_helpers import FIXTURES

from rabota.models import RawPost
from rabota.pipeline.quality import profession_problem
from rabota.vision import ocr
from rabota.vision.extract import build_vacancies

EXPECTED = json.loads((FIXTURES / "posters.json").read_text(encoding="utf-8"))


def _have_rus() -> bool:
    if not shutil.which("tesseract"):
        return False
    env = dict(os.environ)
    if os.environ.get("OCR_TESSDATA"):
        env["TESSDATA_PREFIX"] = os.environ["OCR_TESSDATA"]
    out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, env=env, check=False).stdout
    return "rus" in out.split()


pytestmark = pytest.mark.skipif(not _have_rus(), reason="нет tesseract с языком rus")


@pytest.fixture(autouse=True)
def tessdata(monkeypatch):
    if os.environ.get("OCR_TESSDATA"):
        monkeypatch.setenv("TESSDATA_PREFIX", os.environ["OCR_TESSDATA"])


_cache: dict[str, dict] = {}


def read(name: str) -> dict:
    if name not in _cache:
        data = (Path(FIXTURES) / name).read_bytes()
        _cache[name] = ocr.result_from_text(ocr.ocr_text(data), EXPECTED[name]["hint"])
    return _cache[name]


def test_fixtures_are_small_and_listed():
    for name in EXPECTED:
        assert (FIXTURES / name).is_file(), name
        assert (FIXTURES / name).stat().st_size <= 300 * 1024, name


@pytest.mark.parametrize("name", [n for n, e in EXPECTED.items() if e["kinds"] == ["vacancy"]])
def test_vacancy_posters_give_profession_and_phone(name):
    exp, res = EXPECTED[name], read(name)
    assert res["kind"] == "vacancy", name
    professions = [v["profession"] for v in res["vacancies"]]
    assert exp["profession"] in professions, professions
    v = next(v for v in res["vacancies"] if v["profession"] == exp["profession"])
    assert v["phones"], "контакт с афиши не найден"
    for phone in exp["phones"]:
        assert phone in v["phones"]
    assert all(profession_problem(p) is None for p in professions)


def test_target_poster_detailing_soft_fields():
    """Афиша из бага: автомойщик, Ленинский район, график 2/2, телефон из подписи и с афиши. Остальное по возможности."""
    exp, res = EXPECTED["poster_detailing.jpg"], read("poster_detailing.jpg")
    v = res["vacancies"][0]
    assert v["profession"] == "автомойщик" and v["phones"] == ["+79495055755"]
    soft = {k: v[k] for k in exp["soft"]}
    assert soft == exp["soft"], soft
    assert v["extra_text"] and "выработки" in v["extra_text"]
    raw = RawPost("telegram", "Vakansiii_dnr", "31217", "https://t.me/Vakansiii_dnr/31217", exp["hint"], image_urls=["x"])
    vacancies, _ = build_vacancies(res, raw)
    assert [x.profession for x in vacancies] == ["автомойщик"]
    assert vacancies[0].contacts["phones"] == ["+79495055755"] and vacancies[0].district == "Ленинский"


@pytest.mark.parametrize("name", [n for n, e in EXPECTED.items() if e["kinds"] != ["vacancy"]])
def test_non_vacancies_and_photos_give_no_garbage_vacancy(name):
    res = read(name)
    assert res["kind"] in EXPECTED[name]["kinds"], (name, res["kind"])
    for v in res["vacancies"]:
        assert profession_problem(v["profession"]) is None


def test_measured_quality_on_all_fixtures():
    """Итог для docs/PARSING.md: сколько афиш с вакансией дали осмысленную вакансию."""
    posters = [n for n, e in EXPECTED.items() if "vacancy" in e["kinds"] and e["kinds"] == ["vacancy"]]
    ok = sum(1 for n in posters if read(n)["kind"] == "vacancy" and read(n)["vacancies"])
    assert ok == len(posters)
