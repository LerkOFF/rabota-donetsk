"""Конвейер разбора текста: RawPost -> список Vacancy, склейка дублей, запуск по базе.

    from rabota.pipeline.run import process, dedupe, assign_duplicates
    vacancies = process(raw)          # [] для рекламы, услуг, набора на службу и заглушек
    assign_duplicates(vacancies)      # пометить дубли в пачке в памяти
    dedupe(conn)                      # то же в базе

Командная строка: `python -m rabota.pipeline.run` разбирает все сырые посты из базы, которых ещё нет в vacancies.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from rabota.models import RawPost, Vacancy
from rabota.pipeline.classify import classify
from rabota.pipeline.clean import clean_text
from rabota.pipeline.contacts import extract_contacts
from rabota.pipeline.dedupe import (
    also_published_in,
    assign_duplicates,
    attach_text,
    dedupe,
    make_dedupe_key,
)
from rabota.pipeline.homoglyphs import fix_homoglyphs
from rabota.pipeline.normalize import apply_structured, extra_text, normalize_segment
from rabota.pipeline.split import split_post

__all__ = [
    "Analysis",
    "also_published_in",
    "analyze",
    "assign_duplicates",
    "classify_post",
    "dedupe",
    "process",
    "process_db",
]


@dataclass
class Analysis:
    kind: str
    cleaned: str = ""
    vacancies: list[Vacancy] = field(default_factory=list)


def _post_text(raw: RawPost) -> str:
    text = raw.text or ""
    if not text.strip() and raw.extra:
        text = extra_text(raw.extra)
    return text


def classify_post(raw: RawPost) -> str:
    """Вид поста: vacancy, service, military, ad, other."""
    cleaned = clean_text(_post_text(raw))
    return classify(cleaned, raw.extra)


def _share_single_pay(vacancies: list[Vacancy]) -> None:
    """Если во всём посте одна и та же оплата, вакансии без собственной оплаты её наследуют."""
    paid = {(v.pay_min, v.pay_max, v.pay_period) for v in vacancies if v.pay_min is not None or v.pay_max is not None}
    if len(paid) != 1:
        return
    src = next(v for v in vacancies if v.pay_min is not None or v.pay_max is not None)
    for v in vacancies:
        if v.pay_min is None and v.pay_max is None:
            v.pay_min, v.pay_max, v.pay_period = src.pay_min, src.pay_max, src.pay_period
            v.pay_monthly_estimate = src.pay_monthly_estimate
            v.pay_daily = v.pay_daily or src.pay_daily


def analyze(raw: RawPost) -> Analysis:
    """Полный разбор поста: вид и вакансии. Для не-вакансий список вакансий пуст."""
    text = _post_text(raw)
    fixed = fix_homoglyphs(text)
    cleaned = clean_text(fixed, homoglyphs=False)
    kind = classify(cleaned, raw.extra)
    if kind != "vacancy":
        return Analysis(kind, cleaned)
    post_contacts = extract_contacts(fixed, raw.source)
    segments = split_post(cleaned)
    vacancies: list[Vacancy] = []
    for seg in segments:
        vac = normalize_segment(seg, raw, post_contacts=post_contacts, post_text=cleaned)
        if vac is None:
            continue
        if raw.extra:
            apply_structured(vac, raw.extra)
        vac.dedupe_key = make_dedupe_key(vac.profession, vac.contacts.get("phones"), seg.full or cleaned)
        attach_text(vac, cleaned)
        vacancies.append(vac)
    if len(vacancies) > 1:
        _share_single_pay(vacancies)
    if not vacancies:
        return Analysis("ad", cleaned)
    return Analysis("vacancy", cleaned, vacancies)


def process(raw: RawPost) -> list[Vacancy]:
    """RawPost -> список вакансий. Пустой список для не-вакансий, несколько для поста со списком должностей."""
    return analyze(raw).vacancies


def process_db(conn: sqlite3.Connection, *, limit: int | None = None) -> dict[str, Any]:
    """Разобрать сырые посты из базы, которых ещё нет в vacancies, и склеить дубли."""
    from rabota.admin.stoplist import filter_vacancies
    from rabota.db import load_raw, save_vacancy

    known = {(r["source"], r["external_id"]) for r in conn.execute("SELECT source, external_id FROM vacancies")}
    stats: dict[str, Any] = {"posts": 0, "vacancies": 0, "stoplisted": 0, "kinds": {}}
    for raw in load_raw(conn, limit=limit):
        if (raw.source, raw.external_id) in known:
            continue
        stats["posts"] += 1
        result = analyze(raw)
        stats["kinds"][result.kind] = stats["kinds"].get(result.kind, 0) + 1
        kept, dropped = filter_vacancies(conn, result.vacancies)  # стоп-лист: такие вакансии не публикуются
        stats["stoplisted"] += dropped
        for vac in kept:
            save_vacancy(conn, vac)
            stats["vacancies"] += 1
    stats["duplicates"] = dedupe(conn)
    return stats


if __name__ == "__main__":  # pragma: no cover
    from rabota.db import connect

    connection = connect()
    print(process_db(connection))
