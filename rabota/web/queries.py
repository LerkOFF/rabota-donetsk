"""Запросы сайта к таблицам vacancies и complaints. Только параметризованный SQL.

В ленту попадают записи kind="vacancy", status="active", без duplicate_of и без пометки
suspicious. Набор на службу (kind="military"), услуги, реклама и дубли не показываются нигде.
"""
from __future__ import annotations

import hashlib
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from rabota import config
from rabota.db import connect, save_vacancy, vacancy_row_to_dict
from rabota.models import Vacancy

PER_PAGE = 20
MAX_QUERY_LEN = 80

# последняя проверка срока жизни прямо в запросе: вакансия без публикаций дольше TTL не показывается,
# даже если фоновая команда expire давно не запускалась
VISIBLE_SQL = (
    "kind = 'vacancy' AND status = 'active' AND duplicate_of IS NULL AND suspicious = 0 AND moderation_status = 'approved' "
    f"AND (COALESCE(last_seen_at, published_at) IS NULL "
    f"OR COALESCE(last_seen_at, published_at) >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-{int(config.VACANCY_TTL_DAYS)} days'))"
)
ORDER_SQL = "COALESCE(published_at, created_at) DESC, id DESC"

# Оплата в месяц для сравнения и сортировки: у месячных вакансий верхняя граница, у остальных расчёт сайта
PAY_SQL = "COALESCE(CASE WHEN pay_period = 'месяц' THEN COALESCE(pay_max, pay_min) END, pay_monthly_estimate)"
SORTS = ("date", "pay")
MAX_PAY_FROM = 5_000_000

# Окончания для грубой основы слова: "кассира", "кассиров", "кассиры" -> "кассир"
_ENDINGS = (
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими",
    "ов", "ев", "ей", "ом", "ем", "ах", "ях", "ым", "им", "ой", "ая", "яя", "ое", "ее", "ые", "ие", "ую", "юю",
    "а", "я", "у", "ю", "е", "ы", "и", "о", "ь", "й",
)


def fold(text: str) -> str:
    """Нижний регистр и ё=е. Работает для кириллицы (в SQLite LIKE регистр различает)."""
    return text.lower().replace("ё", "е")


def stem(word: str) -> str:
    w = fold(word)
    for ending in _ENDINGS:
        if w.endswith(ending) and len(w) - len(ending) >= 4:
            return w[: -len(ending)]
    return w


def search_terms(query: str) -> list[list[str]]:
    """Слова запроса -> варианты основы. Каждое слово обязано встретиться (И), варианты через ИЛИ."""
    terms: list[list[str]] = []
    for raw in query.replace(",", " ").split():
        s = stem(raw)
        if len(s) < 2:
            continue
        variants = [s]
        if s.endswith("ц") and len(s) > 3:  # продавца -> продавец
            variants.append(s[:-1] + "ец")
        terms.append(variants)
    return terms[:6]


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


DUP_COUNT_SQL = "(SELECT COUNT(*) FROM vacancies d WHERE d.duplicate_of = vacancies.id) AS dup_count"


def open_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Соединение на один запрос: схема гарантирована, регистр кириллицы в SQL поддержан."""
    conn = connect(db_path)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vac_dup ON vacancies(duplicate_of)")
    conn.create_function("rfold", 1, lambda s: fold(s) if isinstance(s, str) else s, deterministic=True)
    return conn


@dataclass(frozen=True)
class Filters:
    q: str = ""
    city: str = ""
    district: str = ""
    category: str = ""
    work_format: str = ""
    pay_period: str = ""
    no_experience: bool = False
    side_job: bool = False
    daily: bool = False
    adult: bool = False
    direct: bool = False  # только вакансии, которые разместили работодатели на сайте
    pay_from: int = 0  # оплата в месяц от, рублей; 0 - не задано
    sort: str = ""  # "" - по дате (при поиске сначала точные совпадения), "pay" - по оплате
    profession: str = ""  # точное совпадение, для страниц профессий

    @classmethod
    def from_params(cls, params: Mapping[str, str]) -> Filters:
        def s(name: str, limit: int = 60) -> str:
            return " ".join((params.get(name) or "").split())[:limit]

        def flag(name: str) -> bool:
            return (params.get(name) or "").strip().lower() in {"1", "on", "true", "yes"}

        return cls(
            q=s("q", MAX_QUERY_LEN),
            city=s("city"),
            district=s("district"),
            category=s("category", 30),
            work_format=s("work_format", 30),
            pay_period=s("pay_period", 20),
            no_experience=flag("no_experience"),
            side_job=flag("side_job"),
            daily=flag("daily"),
            adult=flag("adult"),
            direct=flag("direct"),
            pay_from=_parse_pay_from(params.get("pay_from")),
            sort=params.get("sort") if params.get("sort") in SORTS and params.get("sort") != "date" else "",
        )

    @property
    def active_count(self) -> int:
        """Сколько фильтров включено (поисковая строка не считается)."""
        return sum(
            1
            for v in (self.city, self.district, self.category, self.work_format, self.pay_period,
                      self.no_experience, self.side_job, self.daily, self.adult, self.direct, self.pay_from)
            if v
        )

    @property
    def is_empty(self) -> bool:
        return self.active_count == 0 and not self.q and not self.sort

    def query_string(self, **overrides: Any) -> str:
        data: dict[str, str] = {}
        merged = {
            "q": self.q, "city": self.city, "district": self.district, "category": self.category,
            "work_format": self.work_format, "pay_period": self.pay_period,
            "no_experience": self.no_experience, "side_job": self.side_job,
            "daily": self.daily, "adult": self.adult, "direct": self.direct,
            "pay_from": self.pay_from, "sort": self.sort,
        }
        merged.update(overrides)
        for key, value in merged.items():
            if value is True:
                data[key] = "1"
            elif value and value is not False:
                data[key] = str(value)
        return urlencode(data)

    def without(self, **names: Any) -> Filters:
        return replace(self, **names)


def _parse_pay_from(value: str | None) -> int:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())[:9]
    return min(int(digits), MAX_PAY_FROM) if digits else 0


def _where(f: Filters) -> tuple[str, list[Any], list[Any], str]:
    """WHERE, его параметры, параметры для ранжирования и выражение ранжирования."""
    clauses = [VISIBLE_SQL]
    args: list[Any] = []
    rank_sql = "0 + 0"
    rank_args: list[Any] = []
    if f.profession:
        clauses.append("rfold(profession) = ?")
        args.append(fold(f.profession))
    if f.city:
        clauses.append("rfold(city) = ?")
        args.append(fold(f.city))
    if f.district:
        clauses.append("rfold(district) = ?")
        args.append(fold(f.district))
    if f.category:
        clauses.append("category = ?")
        args.append(f.category)
    if f.work_format:
        clauses.append("work_format = ?")
        args.append(f.work_format)
    if f.pay_period:
        clauses.append("pay_period = ?")
        args.append(f.pay_period)
    if f.no_experience:
        clauses.append("(rfold(experience) LIKE 'не нужен%' OR rfold(experience) LIKE 'без опыт%')")
    if f.side_job:
        clauses.append("employment IN ('подработка', 'на сегодня')")
    if f.daily:
        clauses.append("pay_daily = 1")
    if f.adult:
        clauses.append("(age_min IS NULL OR age_min <= 18)")
    if f.direct:
        clauses.append("origin = 'direct'")
    if f.pay_from:
        clauses.append(f"{PAY_SQL} >= ?")
        args.append(f.pay_from)
    if f.q:
        fields = ("profession", "title_raw", "summary", "employer", "city", "district", "category")
        if not search_terms(f.q):  # одни знаки препинания: ничего осмысленного не найти
            clauses.append("0")
        for variants in search_terms(f.q):
            ors = []
            for variant in variants:
                pattern = f"%{_like_escape(variant)}%"
                for col in fields:
                    ors.append(f"rfold({col}) LIKE ? ESCAPE '\\'")
                    args.append(pattern)
            clauses.append("(" + " OR ".join(ors) + ")")
        first = search_terms(f.q)
        if first:
            rank_sql = "CASE WHEN " + " OR ".join(
                "rfold(profession) LIKE ? ESCAPE '\\'" for _ in first[0]
            ) + " THEN 0 ELSE 1 END"
            rank_args = [f"%{_like_escape(v)}%" for v in first[0]]
    return " AND ".join(clauses), args, rank_args, rank_sql


@dataclass
class Page:
    items: list[dict[str, Any]]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def parse_page(value: str | None) -> int:
    try:
        n = int(value or "1")
    except ValueError:
        return 1
    return min(max(n, 1), 10_000)


def search(conn: sqlite3.Connection, f: Filters, page: int = 1, per_page: int = PER_PAGE) -> Page:
    where, args, rank_args, rank_sql = _where(f)
    total = conn.execute(f"SELECT COUNT(*) FROM vacancies WHERE {where}", args).fetchone()[0]
    pages = max(1, -(-total // per_page))
    page = min(max(page, 1), pages)
    if f.sort == "pay":
        order, order_args = f"({PAY_SQL} IS NULL), {PAY_SQL} DESC, {ORDER_SQL}", []
    else:
        order, order_args = f"{rank_sql}, {ORDER_SQL}", rank_args
    rows = conn.execute(
        f"SELECT *, {DUP_COUNT_SQL} FROM vacancies WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*args, *order_args, per_page, (page - 1) * per_page],
    ).fetchall()
    return Page([vacancy_row_to_dict(r) for r in rows], total, page, per_page)


def latest(conn: sqlite3.Connection, limit: int = 8) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT *, {DUP_COUNT_SQL} FROM vacancies WHERE {VISIBLE_SQL} ORDER BY {ORDER_SQL} LIMIT ?", (limit,)
    ).fetchall()
    return [vacancy_row_to_dict(r) for r in rows]


def get_vacancy(conn: sqlite3.Connection, vacancy_id: int) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT *, {DUP_COUNT_SQL} FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
    return vacancy_row_to_dict(row) if row else None


def _also_published_in_fallback(conn: sqlite3.Connection, vacancy_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT source_type, source, source_url FROM vacancies WHERE duplicate_of = ? AND kind = 'vacancy' "
        "AND status != 'hidden' ORDER BY COALESCE(published_at, created_at), id",
        (vacancy_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def also_published_in(conn: sqlite3.Connection, vacancy_id: int) -> list[dict[str, Any]]:
    """Где ещё опубликована вакансия: source_type, source, source_url по каждому дублю.

    Если в конвейере есть своя функция (rabota.pipeline.dedupe), берём её, иначе простой запрос.
    """
    try:
        from rabota.pipeline.dedupe import also_published_in as pipeline_impl
    except ImportError:
        return _also_published_in_fallback(conn, vacancy_id)
    return [dict(item) for item in pipeline_impl(conn, vacancy_id)]


def is_listable(v: dict[str, Any]) -> bool:
    return (
        v["kind"] == "vacancy" and v["status"] == "active" and v["duplicate_of"] is None and not v["suspicious"]
        and (v.get("moderation_status") or "approved") == "approved"
    )


def similar(conn: sqlite3.Connection, v: dict[str, Any], limit: int = 4) -> list[dict[str, Any]]:
    """Похожие: та же профессия, затем та же категория; тот же город в приоритете."""
    rows = conn.execute(
        f"""SELECT *, {DUP_COUNT_SQL} FROM vacancies
            WHERE {VISIBLE_SQL} AND id != ? AND (rfold(profession) = ? OR category = ?)
            ORDER BY CASE WHEN rfold(profession) = ? THEN 0 ELSE 1 END,
                     CASE WHEN rfold(city) = ? THEN 0 ELSE 1 END, {ORDER_SQL}
            LIMIT ?""",
        (v["id"], fold(v["profession"]), v["category"], fold(v["profession"]), fold(v["city"] or ""), limit),
    ).fetchall()
    return [vacancy_row_to_dict(r) for r in rows]


def _counts(conn: sqlite3.Connection, column: str, limit: int | None = None, extra: str = "") -> list[tuple[str, int]]:
    sql = (
        f"SELECT {column} AS v, COUNT(*) AS n FROM vacancies WHERE {VISIBLE_SQL} "
        f"AND {column} IS NOT NULL AND {column} != '' {extra} GROUP BY {column} ORDER BY n DESC, v"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [(r["v"], r["n"]) for r in conn.execute(sql)]


def facets(conn: sqlite3.Connection) -> dict[str, list[tuple[str, int]]]:
    """Значения для выпадающих списков фильтров и для ссылок SEO-страниц."""
    return {
        "cities": _counts(conn, "city"),
        "districts": _counts(conn, "district"),
        "categories": _counts(conn, "category"),
        "work_formats": _counts(conn, "work_format"),
        "pay_periods": _counts(conn, "pay_period"),
        "professions": _counts(conn, "profession"),
    }


def district_city_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Район -> город (для подписей в списке районов)."""
    rows = conn.execute(
        f"SELECT district, city, COUNT(*) n FROM vacancies WHERE {VISIBLE_SQL} AND district IS NOT NULL "
        "AND district != '' GROUP BY district, city ORDER BY n DESC"
    ).fetchall()
    result: dict[str, str] = {}
    for r in rows:
        result.setdefault(r["district"], r["city"] or "")
    return result


def stats(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    since = (now - timedelta(hours=24)).isoformat()

    def one(extra: str, args: Sequence[Any] = ()) -> int:
        return int(conn.execute(f"SELECT COUNT(*) FROM vacancies WHERE {VISIBLE_SQL} {extra}", args).fetchone()[0])

    return {
        "total": one(""),
        "fresh": one("AND published_at >= ?", (since,)),
        "no_experience": one("AND (rfold(experience) LIKE 'не нужен%' OR rfold(experience) LIKE 'без опыт%')"),
        "daily": one("AND pay_daily = 1"),
        "direct": one("AND origin = 'direct'"),
        "side_job": one("AND employment IN ('подработка', 'на сегодня')"),
    }


def _grouped(conn: sqlite3.Connection, column: str, min_count: int) -> list[tuple[str, int, str | None]]:
    """Значение поля, число вакансий, дата последней публикации. Регистр и ё не создают лишних строк."""
    rows = conn.execute(
        f"SELECT MIN({column}) AS v, COUNT(*) AS n, MAX(COALESCE(published_at, created_at)) AS ts FROM vacancies "
        f"WHERE {VISIBLE_SQL} AND {column} IS NOT NULL AND {column} != '' GROUP BY rfold({column}) "
        f"HAVING COUNT(*) >= ? ORDER BY n DESC, v",
        (min_count,),
    ).fetchall()
    return [(r["v"], r["n"], r["ts"]) for r in rows]


def sitemap_entries(conn: sqlite3.Connection, min_count: int = 3) -> dict[str, list[Any]]:
    """Всё, что должно попасть в карту сайта. Профессии, районы и города: только с числом вакансий не меньше min_count
    (тонкие страницы закрыты noindex). Компании: с профилем и хотя бы одной открытой вакансией.
    Для каждой строки: значение (или id), дата последней публикации."""
    vacancies = conn.execute(
        f"SELECT id, COALESCE(published_at, created_at) AS ts FROM vacancies WHERE {VISIBLE_SQL} "
        f"ORDER BY {ORDER_SQL} LIMIT 200000"
    ).fetchall()
    companies = conn.execute(
        f"SELECT company_id AS id, MAX(COALESCE(published_at, created_at)) AS ts FROM vacancies "
        f"WHERE {VISIBLE_SQL} AND company_id IN (SELECT id FROM companies) GROUP BY company_id ORDER BY company_id"
    ).fetchall()
    latest_ts = conn.execute(
        f"SELECT MAX(COALESCE(published_at, created_at)) FROM vacancies WHERE {VISIBLE_SQL}"
    ).fetchone()[0]
    return {
        "vacancies": [(r["id"], r["ts"]) for r in vacancies],
        "professions": [(v, ts) for v, _, ts in _grouped(conn, "profession", min_count)],
        "districts": [(v, ts) for v, _, ts in _grouped(conn, "district", min_count)],
        "cities": [(v, ts) for v, _, ts in _grouped(conn, "city", min_count)],
        "companies": [(r["id"], r["ts"]) for r in companies],
        "latest": latest_ts,
    }


def indexable_counts(conn: sqlite3.Connection, min_count: int = 3) -> dict[str, list[tuple[str, int]]]:
    """Профессии, районы и города с достаточной выдачей: на них ведут ссылки перелинковки."""
    return {
        "professions": [(v, n) for v, n, _ in _grouped(conn, "profession", min_count)],
        "districts": [(v, n) for v, n, _ in _grouped(conn, "district", min_count)],
        "cities": [(v, n) for v, n, _ in _grouped(conn, "city", min_count)],
    }


def listing_facts(conn: sqlite3.Connection, f: Filters) -> dict[str, Any]:
    """Цифры для вводного текста страницы-посадки: оплата по периодам, районы, доли без опыта и с выплатой в день.
    Всё считается по тем же вакансиям, что видны на странице."""
    where, args, _, _ = _where(f)
    pay_rows = conn.execute(
        f"SELECT pay_period AS period, COUNT(*) AS n, MIN(COALESCE(pay_min, pay_max)) AS lo, "
        f"MAX(COALESCE(pay_max, pay_min)) AS hi FROM vacancies WHERE {where} "
        f"AND (pay_min IS NOT NULL OR pay_max IS NOT NULL) AND pay_period IS NOT NULL AND pay_period != '' "
        f"GROUP BY pay_period ORDER BY n DESC, pay_period",
        args,
    ).fetchall()

    def top(column: str, limit: int) -> list[tuple[str, int]]:
        rows = conn.execute(
            f"SELECT {column} AS v, COUNT(*) AS n FROM vacancies WHERE {where} AND {column} IS NOT NULL "
            f"AND {column} != '' GROUP BY {column} ORDER BY n DESC, v LIMIT {int(limit)}",
            args,
        ).fetchall()
        return [(r["v"], r["n"]) for r in rows]

    def count(extra: str) -> int:
        return int(conn.execute(f"SELECT COUNT(*) FROM vacancies WHERE {where} AND {extra}", args).fetchone()[0])

    return {
        "pay": [dict(r) for r in pay_rows if r["lo"] and r["hi"]],
        "districts": top("district", 5),
        "cities": top("city", 5),
        "professions": top("profession", 8),
        "categories": top("category", 3),
        "no_experience": count("(rfold(experience) LIKE 'не нужен%' OR rfold(experience) LIKE 'без опыт%')"),
        "daily": count("pay_daily = 1"),
        "direct": count("origin = 'direct'"),
        "side_job": count("employment IN ('подработка', 'на сегодня')"),
    }


def related_professions(conn: sqlite3.Connection, category: str, exclude: str, min_count: int = 3) -> list[tuple[str, int]]:
    """Другие профессии той же сферы: для перелинковки между страницами профессий."""
    if not category:
        return []
    rows = conn.execute(
        f"SELECT MIN(profession) AS v, COUNT(*) AS n FROM vacancies WHERE {VISIBLE_SQL} AND category = ? "
        "AND profession IS NOT NULL AND profession != '' AND rfold(profession) != ? GROUP BY rfold(profession) "
        "HAVING COUNT(*) >= ? ORDER BY n DESC, v LIMIT 12",
        (category, fold(exclude), min_count),
    ).fetchall()
    return [(r["v"], r["n"]) for r in rows]


def canonical_name(conn: sqlite3.Connection, column: str, name: str) -> str | None:
    """Как значение записано в базе (регистр и ё как в данных), по произвольному написанию из адреса."""
    row = conn.execute(
        f"SELECT MIN({column}) FROM vacancies WHERE {VISIBLE_SQL} AND rfold({column}) = ?", (fold(name),)
    ).fetchone()
    return row[0] if row and row[0] else None


def get_company(conn: sqlite3.Connection, company_id: int) -> dict[str, Any] | None:
    """Профиль работодателя из таблицы companies (её заполняют кабинеты). Нет записи: None."""
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    return dict(row) if row else None


def company_vacancies(conn: sqlite3.Connection, company_id: int, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT *, {DUP_COUNT_SQL} FROM vacancies WHERE {VISIBLE_SQL} AND company_id = ? ORDER BY {ORDER_SQL} LIMIT ?",
        (company_id, limit),
    ).fetchall()
    return [vacancy_row_to_dict(r) for r in rows]


def search_hints(conn: sqlite3.Connection, limit: int = 40) -> dict[str, list[str]]:
    """Подсказки для строки поиска: частые профессии, города, районы."""
    professions = [name for name, _ in _counts(conn, "profession", limit)]
    cities = [name for name, _ in _counts(conn, "city", 12)]
    districts = [name for name, _ in _counts(conn, "district", 30)]
    return {"professions": professions, "cities": cities, "districts": districts}


def add_complaint(
    conn: sqlite3.Connection, vacancy_id: int, reason: str, *, code: str = "", reporter_email: str | None = None,
    reporter_key: str | None = None,
) -> int:
    """Жалоба на вакансию. Почта заявителя и отпечаток нужны админке: ответ по почте и автоскрытие при жалобах разных людей."""
    from rabota.admin import complaints

    return complaints.add(conn, vacancy_id, reason, code=code, reporter_email=reporter_email, reporter_key=reporter_key)


def add_site_vacancy(conn: sqlite3.Connection, v: Vacancy, premoderate: bool = True) -> int:
    """Сохраняет вакансию из формы. По умолчанию она ждёт проверки (status="hidden")."""
    v.source_type = "site"
    v.source = "razmestit"
    v.external_id = uuid.uuid4().hex
    v.published_at = datetime.now(UTC).isoformat(timespec="seconds")
    v.kind = "vacancy"
    v.status = "hidden" if premoderate else "active"
    phones = (v.contacts or {}).get("phones") or []
    key_src = f"{v.profession.lower()}|{(phones or [''])[0]}|{v.city or ''}"
    v.dedupe_key = hashlib.sha1(key_src.encode()).hexdigest()[:16]
    vacancy_id = save_vacancy(conn, v)
    if premoderate:  # ждёт решения в админке: до одобрения в выдаче и карте сайта не появляется
        conn.execute("UPDATE vacancies SET moderation_status = 'pending' WHERE id = ?", (vacancy_id,))
        conn.commit()
    return vacancy_id
