"""Подборки вакансий как посадочные страницы: /podborki, /podborki/<подборка>, /podborki/<подборка>/<район или профессия>.

Подборка это именованное условие поверх обычной выдачи (те же видимые вакансии, что в ленте). Страница
индексируется, только если вакансий не меньше `SELECTION_MIN` и подборка не совпадает целиком с выдачей по району или
профессии (иначе это дубль). Остальное отдаётся с noindex и в карту сайта не попадает.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from rabota.db import vacancy_row_to_dict
from rabota.web import format as fmt
from rabota.web import queries as q

SELECTION_MIN = 5  # порог индексации подборки и её сочетаний с районом или профессией
COMBO_LIMIT = 40  # не больше стольких сочетаний на подборку и вид в карте сайта и в ссылках

_NO_EXP = "(rfold(experience) LIKE 'не нужен%' OR rfold(experience) LIKE 'без опыт%')"
_TEXT = "rfold(COALESCE(title_raw, '') || ' ' || COALESCE(summary, '') || ' ' || COALESCE(requirements, ''))"


@dataclass(frozen=True)
class Selection:
    slug: str
    heading: str  # заголовок h1 и начало title
    short: str  # короткая фраза для сочетаний с профессией: "Бариста: подработка"
    nav: str  # подпись на чипе
    icon: str
    clause: str  # условие SQL без параметров
    lead: str  # что в подборку входит, одной фразой
    rule: str  # как отбираются вакансии, честно
    tips: tuple[str, ...]
    articles: tuple[str, ...]  # slug статей для "Читайте также"


SELECTIONS: tuple[Selection, ...] = (
    Selection(
        "podrabotka-na-segodnya", "Подработка на сегодня", "подработка", "Подработка", "calendar-blank",
        "employment IN ('подработка', 'на сегодня')",
        "Подработка в Донецке: выходы на день, смену или выходные.",
        "В подборку попадают вакансии с пометкой «подработка» или «на сегодня».",
        ("Для разовой работы уточняйте по телефону, нужен ли выход сегодня, где собираются и как платят: на месте или потом.",
         "Если просят заплатить за место или «залог», это не работа. Подробнее в статье о том, как отличить мошенников."),
        ("kak-otlichit-moshennikov", "podrabotka-studentu", "grafik-2-2-i-smennye-grafiki"),
    ),
    Selection(
        "ezhednevnaya-vyplata", "Вакансии с ежедневной выплатой", "выплата каждый день", "Выплата каждый день", "lightning",
        "pay_daily = 1",
        "Работа, где платят в конце смены или каждый день.",
        "В подборку попадают вакансии, где работодатель пишет, что платит каждый день или по окончании смены.",
        (("Ежедневная выплата чаще встречается на складах, в общепите и на разовых объектах. Уточните, как именно платят: "
          "наличными или на карту и когда."),
         "Попросите назвать ставку за смену до выхода и договоритесь, во сколько и кто выдаёт деньги."),
        ("kak-otlichit-moshennikov", "grafik-2-2-i-smennye-grafiki", "podrabotka-studentu"),
    ),
    Selection(
        "rabota-bez-opyta", "Работа без опыта", "без опыта", "Без опыта", "check-circle",
        _NO_EXP,
        "Вакансии Донецка, где опыт не нужен и обучают на месте.",
        "В подборку попадают вакансии, где опыт указан как «не нужен» или «без опыта».",
        (("Без опыта берут на склады, в торговлю, общепит, на стройку и в доставку. В резюме и на собеседовании делайте упор на "
          "обучаемость, аккуратность и готовность выходить по графику."),
         "Спросите, будет ли испытательный срок и как он оплачивается."),
        ("kak-sostavit-rezyume", "kak-projti-sobesedovanie", "dokumenty-dlya-trudoustroystva-v-dnr"),
    ),
    Selection(
        "rabota-dlya-studentov", "Работа для студентов", "для студентов", "Студентам", "graduation-cap",
        f"({_TEXT} LIKE '%студент%' OR (employment IN ('подработка', 'на сегодня') AND {_NO_EXP}))",
        "Работа и подработка, которую можно совмещать с учёбой.",
        "В подборку попадают вакансии, где упомянуты студенты, и подработки без требований к опыту.",
        ("Заранее скажите работодателю про расписание занятий и сессию: гибкий график договаривается до выхода, а не после.",
         "Для подработки по найму и для разовых заказов нужны разные документы, об этом в статье для студентов."),
        ("podrabotka-studentu", "kak-sostavit-rezyume", "samozanyatost-v-dnr"),
    ),
    Selection(
        "vakansii-18-plus", "Вакансии для 18+", "от 18 лет", "С 18 лет", "user-circle",
        f"((age_min IS NOT NULL AND age_min <= 18) OR {_TEXT} LIKE '%18+%' OR {_TEXT} LIKE '%с 18 лет%' "
        f"OR {_TEXT} LIKE '%от 18 лет%')",
        "Вакансии, на которые можно устроиться с 18 лет.",
        "В подборку попадают вакансии, где указан возраст от 18 лет или меньше, либо есть пометка «18+». "
        "Если возраст в вакансии не указан, её здесь нет: уточняйте у работодателя.",
        ("С 18 лет вы подписываете договор сами, без согласия родителей. Возьмите паспорт и документы, которые просит работодатель.",),
        ("dokumenty-dlya-trudoustroystva-v-dnr", "kak-sostavit-rezyume", "kak-projti-sobesedovanie"),
    ),
    Selection(
        "udalennaya-rabota", "Удалённая работа", "удалённо", "Удалённо", "desktop",
        "work_format = 'удалённо'",
        "Работа из дома: без поездок в офис.",
        "В подборку попадают вакансии с форматом «удалённо».",
        (("Удалённая работа чаще всего просит стабильный интернет и компьютер. Если просят заплатить за доступ, обучение или "
          "«стартовый набор», это повод отказаться."),
         "Уточните, оформляют ли вас официально или по договору, и как выплачивается оплата."),
        ("kak-otlichit-moshennikov", "samozanyatost-v-dnr", "kak-iskat-rabotu-cherez-telegram-i-sayt"),
    ),
    Selection(
        "vahta", "Вахта", "вахта", "Вахта", "hard-hat",
        "work_format = 'вахта'",
        "Вахтовая работа: длинные смены с отдыхом между ними.",
        "В подборку попадают вакансии с форматом «вахта».",
        ("На вахте важно заранее выяснить, кто оплачивает дорогу и проживание, длительность смены и порядок выплат.",
         "Все условия просите подтвердить письменно, до выезда."),
        ("grafik-2-2-i-smennye-grafiki", "kak-otlichit-moshennikov", "dokumenty-dlya-trudoustroystva-v-dnr"),
    ),
)
BY_SLUG = {s.slug: s for s in SELECTIONS}


def get(slug: str) -> Selection | None:
    return BY_SLUG.get(slug)


# ------------------------------------------------------------------ условия и запросы

def scope_clause(kind: str, value: str) -> tuple[str, list[Any]]:
    """Район или профессия, в которой смотрим подборку."""
    if kind == "district":
        return "rfold(district) = ?", [q.fold(value)]
    if kind == "profession":
        return "rfold(profession) = ?", [q.fold(value)]
    return "1", []


def _where(sel: Selection | None, scope: tuple[str, str] | None = None) -> tuple[str, list[Any]]:
    parts = [q.VISIBLE_SQL]
    args: list[Any] = []
    if sel:
        parts.append(sel.clause)
    if scope:
        sql, a = scope_clause(*scope)
        parts.append(sql)
        args += a
    return " AND ".join(parts), args


def count(conn: sqlite3.Connection, sel: Selection | None, scope: tuple[str, str] | None = None) -> int:
    where, args = _where(sel, scope)
    return int(conn.execute(f"SELECT COUNT(*) FROM vacancies WHERE {where}", args).fetchone()[0])


def page(conn: sqlite3.Connection, sel: Selection, scope: tuple[str, str] | None, page_no: int,
         per_page: int = q.PER_PAGE) -> q.Page:
    where, args = _where(sel, scope)
    total = int(conn.execute(f"SELECT COUNT(*) FROM vacancies WHERE {where}", args).fetchone()[0])
    pages = max(1, -(-total // per_page))
    page_no = min(max(page_no, 1), pages)
    rows = conn.execute(
        f"SELECT *, {q.DUP_COUNT_SQL} FROM vacancies WHERE {where} ORDER BY {q.ORDER_SQL} LIMIT ? OFFSET ?",
        [*args, per_page, (page_no - 1) * per_page]).fetchall()
    return q.Page([vacancy_row_to_dict(r) for r in rows], total, page_no, per_page)


def counts(conn: sqlite3.Connection, scope: tuple[str, str] | None = None) -> dict[str, int]:
    """Сколько вакансий в каждой подборке (при необходимости внутри района или профессии): один запрос."""
    cols = ", ".join(f"SUM(CASE WHEN {s.clause} THEN 1 ELSE 0 END) AS c{i}" for i, s in enumerate(SELECTIONS))
    where, args = _where(None, scope)
    row = conn.execute(f"SELECT {cols} FROM vacancies WHERE {where}", args).fetchone()
    return {s.slug: int(row[f"c{i}"] or 0) for i, s in enumerate(SELECTIONS)}


def facts(conn: sqlite3.Connection, sel: Selection, scope: tuple[str, str] | None) -> dict[str, Any]:
    """Цифры для вводного текста: оплата по периодам, районы, профессии. Формат как у queries.listing_facts."""
    where, args = _where(sel, scope)
    pay_rows = conn.execute(
        f"SELECT pay_period AS period, COUNT(*) AS n, MIN(COALESCE(pay_min, pay_max)) AS lo, "
        f"MAX(COALESCE(pay_max, pay_min)) AS hi FROM vacancies WHERE {where} "
        f"AND (pay_min IS NOT NULL OR pay_max IS NOT NULL) AND pay_period IS NOT NULL AND pay_period != '' "
        f"GROUP BY pay_period ORDER BY n DESC, pay_period", args).fetchall()

    def top(column: str, limit: int) -> list[tuple[str, int]]:
        rows = conn.execute(
            f"SELECT MIN({column}) AS v, COUNT(*) AS n FROM vacancies WHERE {where} AND {column} IS NOT NULL "
            f"AND {column} != '' GROUP BY rfold({column}) ORDER BY n DESC, v LIMIT {int(limit)}", args).fetchall()
        return [(r["v"], r["n"]) for r in rows]

    return {
        "pay": [dict(r) for r in pay_rows if r["lo"] and r["hi"]],
        "districts": top("district", 5),
        "professions": top("profession", 8),
    }


def combos(conn: sqlite3.Connection, sel: Selection, kind: str, min_count: int = SELECTION_MIN,
           limit: int = COMBO_LIMIT) -> list[tuple[str, int]]:
    """Сочетания подборки с районом или профессией, у которых хватает вакансий и которые не повторяют страницу района
    или профессии целиком. Значение и число вакансий, больше сначала."""
    column = "district" if kind == "district" else "profession"
    where = f"{q.VISIBLE_SQL} AND {column} IS NOT NULL AND {column} != ''"
    inner = conn.execute(
        f"SELECT MIN({column}) AS v, COUNT(*) AS n, SUM(CASE WHEN {sel.clause} THEN 1 ELSE 0 END) AS m "
        f"FROM vacancies WHERE {where} GROUP BY rfold({column}) HAVING m >= ? AND m < n "
        f"ORDER BY m DESC, v LIMIT ?", (min_count, limit)).fetchall()
    out = []
    for r in inner:
        value = r["v"].lower() if kind == "profession" else r["v"]
        out.append((value, int(r["m"])))
    return out


def resolve_scope(conn: sqlite3.Connection, raw: str) -> tuple[str, str] | None:
    """Район или профессия из адреса, как записано в базе (регистр и ё). Сначала район."""
    raw = raw.strip()[:80]
    name = q.canonical_name(conn, "district", raw)
    if name:
        return "district", name
    name = q.canonical_name(conn, "profession", raw)
    if name:
        return "profession", name.lower()
    return None


def is_indexable(total: int, scope_total: int | None, base_total: int) -> bool:
    """Подборка индексируется: вакансий хватает и она не повторяет выдачу целиком (для подборки без области
    сравнивается со всей лентой, для сочетания с районом или профессией с их выдачей)."""
    if total < SELECTION_MIN:
        return False
    whole = base_total if scope_total is None else scope_total
    return total < whole


# ------------------------------------------------------------------ тексты

def scope_label(scope: tuple[str, str] | None) -> str:
    if not scope:
        return ""
    kind, value = scope
    return fmt.district_label(value) if kind == "district" else value[:1].upper() + value[1:]


def heading(sel: Selection, scope: tuple[str, str] | None) -> str:
    if not scope:
        return f"{sel.heading} в Донецке"
    kind, value = scope
    if kind == "district":
        return f"{sel.heading}: {fmt.district_label(value)}"
    return f"{scope_label(scope)}: {sel.short}"


def title_key(sel: Selection, scope: tuple[str, str] | None, total: int) -> str:
    """Ключевая часть title: без названия сайта, оно добавится само."""
    count = fmt.count_label(total, "вакансия", "вакансии", "вакансий")
    if not scope:
        return f"{sel.heading} в Донецке: {count}"
    kind, value = scope
    if kind == "district":
        return f"{sel.heading}: {fmt.district_label(value)}, {count}"
    return f"{scope_label(scope)}: {sel.short}, {count}"


def description(sel: Selection, scope: tuple[str, str] | None, total: int, facts_: dict[str, Any]) -> str:
    count = fmt.count_label(total, "вакансия", "вакансии", "вакансий")
    place = f", {fmt.district_label(scope[1])}" if scope and scope[0] == "district" else ""
    who = f" по профессии «{scope[1]}»" if scope and scope[0] == "profession" else ""
    text = f"{sel.heading}{place}{who}: {count} в Донецке и ДНР."
    pays = facts_.get("pay") or []
    if pays:
        row = pays[0]
        lo, hi = fmt.money(row["lo"]), fmt.money(row["hi"])
        span = f"{lo} ₽" if lo == hi else f"от {lo} до {hi} ₽"
        text += f" Оплата {span} {fmt.PERIOD_LABELS.get(row['period'], row['period'])}."
    text += f" {sel.lead}"
    return text


def lead_paragraphs(sel: Selection, scope: tuple[str, str] | None, total: int, facts_: dict[str, Any]) -> list[str]:
    """Вводный текст: что это за подборка, сколько вакансий, оплата, где больше всего. Только цифры из базы."""
    count = fmt.count_label(total, "актуальная вакансия", "актуальные вакансии", "актуальных вакансий")
    where = ""
    if scope:
        where = f" в районе «{fmt.district_label(scope[1])}»" if scope[0] == "district" else f" по профессии «{scope[1]}»"
    parts = [f"{sel.lead} Сейчас на сайте {count}{where}.", sel.rule]
    pays = facts_.get("pay") or []
    if pays:
        bits = []
        for row in pays[:2]:
            lo, hi = fmt.money(row["lo"]), fmt.money(row["hi"])
            span = f"{lo} ₽" if lo == hi else f"от {lo} до {hi} ₽"
            period = fmt.PERIOD_LABELS.get(row["period"], row["period"])
            bits.append(f"{span} {period} ({fmt.count_label(row['n'], 'вакансия', 'вакансии', 'вакансий')})")
        parts.append("Оплата, где она указана: " + "; ".join(bits) + ".")
    else:
        parts.append("Оплата в этих вакансиях не указана, её нужно уточнять по телефону.")
    top = [(d, n) for d, n in (facts_.get("districts") or [])[:3] if n >= 2]
    if not scope and top:
        parts.append("Больше всего вакансий в районах: " + ", ".join(f"{fmt.district_label(d)} ({n})" for d, n in top) + ".")
    return parts


def quick_chips(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Быстрые чипы подборок для главной: только непустые, подпись и число."""
    totals = counts(conn)
    return [{"slug": s.slug, "label": s.nav, "n": totals[s.slug], "icon": s.icon, "href": f"/podborki/{s.slug}"}
            for s in SELECTIONS if totals[s.slug] > 0]


def index_pages(conn: sqlite3.Connection) -> dict[str, list[tuple[str, int, str]]]:
    """Все подборки, которые должны быть в индексе: адрес, число вакансий, подпись. Виды: base, district, profession."""
    base_total = int(conn.execute(f"SELECT COUNT(*) FROM vacancies WHERE {q.VISIBLE_SQL}").fetchone()[0])
    totals = counts(conn)
    out: dict[str, list[tuple[str, int, str]]] = {"base": [], "district": [], "profession": []}
    for s in SELECTIONS:
        if is_indexable(totals[s.slug], None, base_total):
            out["base"].append((f"/podborki/{s.slug}", totals[s.slug], s.heading))
        for value, n in combos(conn, s, "district"):
            out["district"].append((f"/podborki/{s.slug}/{value}", n, f"{s.nav}: {fmt.district_label(value)}"))
        for value, n in combos(conn, s, "profession"):
            out["profession"].append((f"/podborki/{s.slug}/{value}", n, f"{value[:1].upper() + value[1:]}: {s.short}"))
    return out
