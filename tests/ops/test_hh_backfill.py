"""Добавка полных требований для старых вакансий hh."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from rabota.db import connect, save_raw, save_vacancy
from rabota.models import RawPost, Vacancy
from rabota.ops import hh_backfill

PAGE = (Path(__file__).resolve().parent.parent / "fixtures" / "hh" / "vacancy.html").read_text(encoding="utf-8")
URL = "https://hh.ru/vacancy/134290618"


def test_backfill_extends_requirements_and_keeps_other_fields(tmp_path):
    db = tmp_path / "t.db"
    conn = connect(db)
    extra = {"vacancyId": 134290618, "name": "Оператор call-центра", "company": {"visibleName": "ZebraCall"},
             "area": {"name": "Донецк"}, "snippet": {"req": "У тебя есть ноутбук."}}
    save_raw(conn, RawPost("hh", "hh.ru", "134290618", URL, "Оператор call-центра\nТребования: У тебя есть ноутбук.", None, [], extra))
    vid = save_vacancy(conn, Vacancy(profession="Оператор колл-центра", source_type="hh", source="hh.ru", source_url=URL,
                                     requirements="У тебя есть ноутбук", city="Донецк", status="active"))
    conn.close()
    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(200, text=PAGE))
        assert hh_backfill.run(client=httpx.Client(), pause=0, db_path=db) == 1
    row = connect(db).execute("SELECT requirements, city FROM vacancies WHERE id = ?", (vid,)).fetchone()
    assert "Тихое место" in row["requirements"] and "Грамотная речь" in row["requirements"]
    assert row["city"] == "Донецк"
    assert json.loads(connect(db).execute("SELECT extra FROM raw_posts").fetchone()["extra"])["vacancyId"] == 134290618
