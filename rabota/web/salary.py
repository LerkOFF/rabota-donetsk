"""Зарплаты по нашим данным: чистые функции расчёта и запросы к базе.

Считаются только видимые вакансии (те же условия, что в ленте) с указанной оплатой. Для каждой вакансии берётся одно
число: середина вилки, а если указана одна граница, то она. Выбросы отсекаются по правилу межквартильного размаха.
Показывается медиана и "типичная вилка" (от первого до третьего квартиля). Меньше `MIN_SAMPLE` вакансий после очистки:
цифр нет и блок не рисуется. Подробности: docs/SEO.md и docs/CONTENT.md.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rabota.web import format as fmt
from rabota.web import queries as q

MIN_SAMPLE = 5  # меньше вакансий с оплатой: блока нет
PERIODS = ("месяц", "смена")  # считаем по месяцу и по смене, часы и задачи несравнимы
# разумные границы оплаты, рублей: всё за ними считается опечаткой разбора и в расчёт не идёт
SANE = {"месяц": (5_000, 1_000_000), "смена": (200, 100_000)}
ROUND_TO = {"месяц": 100, "смена": 50}
IQR_FACTOR = 1.5
PERIOD_NOUN = {"месяц": "в месяц", "смена": "за смену"}


@dataclass(frozen=True)
class SalaryStat:
    period: str
    n: int  # вакансий в расчёте, после отсечения выбросов
    dropped: int  # сколько отсечено как выбросы или опечатки
    median: int
    low: int  # первый квартиль
    high: int  # третий квартиль
    minimum: int
    maximum: int

    @property
    def period_text(self) -> str:
        return PERIOD_NOUN.get(self.period, self.period)

    @property
    def range_text(self) -> str:
        if self.low == self.high:
            return f"{fmt.money(self.low)} ₽"
        return f"от {fmt.money(self.low)} до {fmt.money(self.high)} ₽"

    @property
    def median_text(self) -> str:
        return f"{fmt.money(self.median)} ₽"


def vacancy_value(pay_min: int | None, pay_max: int | None) -> float | None:
    """Одно число по вакансии: середина вилки, одна граница как есть, пусто если оплаты нет."""
    lo, hi = pay_min or None, pay_max or None
    if lo and hi:
        return (min(lo, hi) + max(lo, hi)) / 2
    return float(lo or hi) if (lo or hi) else None


def percentile(sorted_values: Sequence[float], p: float) -> float:
    """Процентиль с линейной интерполяцией (как numpy по умолчанию). p от 0 до 1, список отсортирован."""
    if not sorted_values:
        raise ValueError("пустой набор")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * min(max(p, 0.0), 1.0)
    base = int(pos)
    frac = pos - base
    nxt = sorted_values[min(base + 1, len(sorted_values) - 1)]
    return float(sorted_values[base]) + (float(nxt) - float(sorted_values[base])) * frac


def median(values: Sequence[float]) -> float:
    return percentile(sorted(values), 0.5)


def trim_outliers(values: Sequence[float], factor: float = IQR_FACTOR) -> list[float]:
    """Убирает значения за пределами [Q1 - factor*IQR, Q3 + factor*IQR]. Меньше четырёх значений: как есть."""
    data = sorted(values)
    if len(data) < 4:
        return data
    q1, q3 = percentile(data, 0.25), percentile(data, 0.75)
    spread = (q3 - q1) * factor
    lo, hi = q1 - spread, q3 + spread
    return [v for v in data if lo <= v <= hi]


def round_to(value: float, step: int) -> int:
    return int(round(value / step) * step)


def compute(period: str, raw: Sequence[float], min_sample: int = MIN_SAMPLE) -> SalaryStat | None:
    """Статистика по одному периоду. None, если после очистки меньше min_sample вакансий."""
    lo_ok, hi_ok = SANE.get(period, (1, 10**9))
    sane = [v for v in raw if lo_ok <= v <= hi_ok]
    clean = trim_outliers(sane)
    if len(clean) < min_sample:
        return None
    step = ROUND_TO.get(period, 100)
    return SalaryStat(
        period=period, n=len(clean), dropped=len(raw) - len(clean),
        median=round_to(percentile(clean, 0.5), step),
        low=round_to(percentile(clean, 0.25), step), high=round_to(percentile(clean, 0.75), step),
        minimum=round_to(clean[0], step), maximum=round_to(clean[-1], step),
    )


def best(stats: Sequence[SalaryStat]) -> SalaryStat | None:
    """Главная цифра страницы: период с наибольшим числом вакансий, при равенстве месячный."""
    if not stats:
        return None
    return min(stats, key=lambda s: (-s.n, PERIODS.index(s.period) if s.period in PERIODS else 9))


# ------------------------------------------------------------------ запросы

def _rows(conn: sqlite3.Connection, profession: str = "") -> list[sqlite3.Row]:
    where = f"{q.VISIBLE_SQL} AND (pay_min IS NOT NULL OR pay_max IS NOT NULL) AND pay_period IN ('месяц', 'смена')"
    args: list[Any] = []
    if profession:
        where += " AND rfold(profession) = ?"
        args.append(q.fold(profession))
    return conn.execute(
        f"SELECT profession, pay_min, pay_max, pay_period, COALESCE(published_at, created_at) AS ts "
        f"FROM vacancies WHERE {where}", args).fetchall()


def stats_for(conn: sqlite3.Connection, profession: str) -> list[SalaryStat]:
    """Статистика по профессии: по месяцу и по смене, только те периоды, где вакансий хватает."""
    groups: dict[str, list[float]] = {p: [] for p in PERIODS}
    for r in _rows(conn, profession):
        v = vacancy_value(r["pay_min"], r["pay_max"])
        if v is not None:
            groups[r["pay_period"]].append(v)
    return [s for p in PERIODS if (s := compute(p, groups[p]))]


def all_professions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Профессии, у которых хватает вакансий с оплатой: название, главная цифра, все периоды, дата последней вакансии."""
    by_prof: dict[str, dict[str, Any]] = {}
    for r in _rows(conn):
        v = vacancy_value(r["pay_min"], r["pay_max"])
        if v is None:
            continue
        key = q.fold(r["profession"])
        entry = by_prof.setdefault(key, {"name": key, "values": {p: [] for p in PERIODS}, "last": ""})
        entry["values"][r["pay_period"]].append(v)
        entry["last"] = max(entry["last"], r["ts"] or "")
    out: list[dict[str, Any]] = []
    for entry in by_prof.values():
        stats = [s for p in PERIODS if (s := compute(p, entry["values"][p]))]
        main = best(stats)
        if main:
            out.append({"name": entry["name"], "stats": stats, "main": main, "last": entry["last"]})
    out.sort(key=lambda e: (-e["main"].n, e["name"]))
    return out


# ------------------------------------------------------------------ тексты

def title_for(profession: str, stat: SalaryStat | None) -> str:
    label = profession[:1].upper() + profession[1:]
    if stat:
        return f"{label}: зарплата в Донецке, {fmt.money(stat.median)} ₽ {stat.period_text}"
    return f"{label}: зарплата в Донецке"


def description_for(profession: str, stat: SalaryStat | None, today: datetime) -> str:
    if not stat:
        return (f"Зарплата по профессии «{profession}» в Донецке: пока мало вакансий с указанной оплатой, "
                f"чтобы посчитать честную цифру. Смотрите вакансии и оплату в них.")
    return (f"Средняя зарплата: {profession} в Донецке. Медиана {fmt.money(stat.median)} ₽ {stat.period_text}, "
            f"вилка {stat.range_text}, по {stat.n} {plural_vacancies(stat.n)} на {today.strftime('%d.%m.%Y')}.")


def plural_vacancies(n: int) -> str:
    """Для "по 12 вакансиям": дательный падеж."""
    return fmt.plural(n, "вакансии", "вакансиям", "вакансиям")


def faq_for(profession: str, stats: Sequence[SalaryStat], today: datetime) -> list[tuple[str, str]]:
    """Вопросы и ответы страницы зарплаты. Только то, что следует из данных, без выдуманного."""
    main = best(stats)
    if not main:
        return []
    date = today.strftime("%d.%m.%Y")
    label = profession
    faq = [
        (f"Сколько получает {label} в Донецке",
         (f"По вакансиям на нашем сайте медиана {fmt.money(main.median)} ₽ {main.period_text}. "
          f"Половина вакансий платит в пределах {main.range_text}. Посчитано по {main.n} вакансиям на {date}.")),
        (f"От чего зависит зарплата: {label}",
         ("От района и заведения, графика, опыта и от того, оформляют ли вас официально. "
          "Часть вакансий указывает вилку, тогда мы берём её середину.")),
        ("Как считается средняя зарплата",
         ("Берём открытые вакансии с указанной оплатой, для каждой одно число (середину вилки), убираем явные "
          "опечатки и выбросы и показываем медиану. Это оплата из объявлений, а не фактические выплаты.")),
    ]
    return faq
