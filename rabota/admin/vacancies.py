"""Раздел админки «Вакансии»: все записи таблицы vacancies, фильтры в адресе, карточка с историей, повтор.

В отличие от модерации (очередь работодателей) здесь видны все вакансии: скрытые, просроченные, дубли, чужих городов,
наборы на службу и услуги. Телефон, почта и ник в списке и карточке замаскированы до нажатия.
Состояние «в выдаче» считается тем же условием, что и сайт (rabota.web.queries.VISIBLE_SQL).
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from rabota import config
from rabota.admin import audit, stoplist
from rabota.admin.common import Page, clamp_page, clean_line, iso
from rabota.db import vacancy_row_to_dict
from rabota.lifecycle import parse_iso
from rabota.web import queries

PER_PAGE = 25
LOCAL_UTC_OFFSET = timedelta(hours=3)  # Донецк, московское время: границы периодов считает код, а не база
MAX_CHAIN = 8

STATUSES = {"active": "Активна", "hidden": "Скрыта", "expired": "Истекла", "closed": "Закрыта", "archived": "В архиве"}
MODERATION = {"pending": "На проверке", "approved": "Одобрена", "rejected": "Отклонена"}
ORIGINS = {"parsed": "Собрана из источника", "direct": "От работодателя на сайте"}
FLAGS = {
    "visible": "Сейчас в выдаче",
    "invisible": "Не в выдаче",
    "duplicate": "Дубли",
    "suspicious": "Подозрительные",
    "stale": "Срок жизни вышел",
    "foreign": "Город вне выдачи",
}
KINDS = {"vacancy": "Вакансия", "other": "Не вакансия (служба, услуги, реклама)"}
PERIODS = {
    "all": "Всё время", "today": "Сегодня", "yesterday": "Вчера", "7d": "7 дней", "30d": "30 дней", "month": "Месяц",
}
SORTS = {
    "created": ("Добавлена", "created_at"),
    "published": ("Опубликована", "COALESCE(published_at, created_at)"),
    "id": ("Номер", "id"),
    "pay": ("Оплата", queries.PAY_SQL),
    "profession": ("Профессия", "rfold(profession)"),
}
FOREIGN_CITY = "~foreign"

FLASH = {
    "duplicate_marked": "Вакансия отмечена повторяющейся и убрана из выдачи.",
    "duplicate_cleared": "Отметка о повторе снята.",
}

ACTION_LABELS = {
    "vacancy.approve": ("Одобрена и опубликована", "check-circle"),
    "vacancy.reject": ("Отклонена", "x-circle"),
    "vacancy.hide": ("Скрыта из выдачи", "lock-key"),
    "vacancy.restore": ("Возвращена в выдачу", "check-circle"),
    "vacancy.edit": ("Правка полей", "pencil-simple"),
    "vacancy.autohide": ("Скрыта автоматически по жалобам", "flag"),
    "vacancy.stoplist_hide": ("Скрыта по стоп-листу", "shield-warning"),
    "vacancy.duplicate": ("Отмечена повторяющейся", "copy"),
    "vacancy.unduplicate": ("Отметка о повторе снята", "copy"),
}


# ------------------------------------------------------------------ маскирование контактов

def mask_phone(value: str) -> str:
    """+79490000000 -> +7 949 ***-**-00. Не телефон остаётся с закрытой серединой."""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) == 11 and digits[0] in "78":
        return f"+7 {digits[1:4]} ***-**-{digits[-2:]}"
    return _mask_middle(str(value))


def mask_email(value: str) -> str:
    local, _, domain = str(value).partition("@")
    return f"{local[:1]}***@{domain}" if domain else _mask_middle(str(value))


def mask_nick(value: str) -> str:
    text = str(value).strip()
    at = "@" if text.startswith("@") else ""
    body = text.lstrip("@")
    return f"{at}{body[:1]}***" if body else text


def _mask_middle(text: str) -> str:
    return text if len(text) <= 4 else text[:2] + "*" * (len(text) - 4) + text[-2:]


_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{8,}\d")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_NICK_RE = re.compile(r"(?<![\w.*])@\w{3,}")


def mask_text(text: str) -> str:
    """Свободный текст: телефоны, почта и ники закрыты, остальное как есть."""

    def phone(m: re.Match[str]) -> str:
        chars = list(m.group(0))
        idx = [i for i, ch in enumerate(chars) if ch.isdigit()]
        if len(idx) < 9:
            return m.group(0)
        for i in idx[4:-2]:
            chars[i] = "*"
        return "".join(chars)

    out = _EMAIL_RE.sub(lambda m: mask_email(m.group(0)), text or "")
    out = _PHONE_RE.sub(phone, out)
    return _NICK_RE.sub(lambda m: mask_nick(m.group(0)), out)


def contact_items(contacts: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Контакты вакансии для показа: подпись, закрытый вид, полное значение, ссылка-кнопка не нужна."""
    c = contacts or {}
    out: list[dict[str, str]] = []
    for p in c.get("phones") or []:
        if isinstance(p, str) and p.strip():
            out.append({"kind": "Телефон", "masked": mask_phone(p), "full": p.strip()})
    for key, label in (("telegram", "Telegram"), ("max", "MAX")):
        if c.get(key):
            out.append({"kind": label, "masked": mask_nick(str(c[key])), "full": str(c[key])})
    if c.get("email"):
        out.append({"kind": "Почта", "masked": mask_email(str(c["email"])), "full": str(c["email"])})
    return out


# ------------------------------------------------------------------ фильтры

def period_bounds(key: str, now: datetime) -> tuple[str, str] | None:
    """Границы периода [от, до) в UTC для сравнения с created_at. Сутки считаются по местному времени."""
    if key not in PERIODS or key == "all":
        return None
    local = now + LOCAL_UTC_OFFSET
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if key == "today":
        start, end = day, day + timedelta(days=1)
    elif key == "yesterday":
        start, end = day - timedelta(days=1), day
    elif key == "7d":
        start, end = day - timedelta(days=6), day + timedelta(days=1)
    elif key == "30d":
        start, end = day - timedelta(days=29), day + timedelta(days=1)
    else:  # month
        start = day.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
    fmt = "%Y-%m-%d %H:%M:%S"
    return (start - LOCAL_UTC_OFFSET).strftime(fmt), (end - LOCAL_UTC_OFFSET).strftime(fmt)


@dataclass
class Filters:
    q: str = ""
    prof: str = ""
    text: str = ""
    status: str = ""
    mod: str = ""
    origin: str = ""
    source: str = ""
    city: str = ""
    flag: str = ""
    kind: str = ""
    period: str = "all"
    sort: str = "created"
    dir: str = "desc"
    page: int = 1
    extra: dict[str, str] = field(default_factory=dict)

    def params(self, **override: Any) -> dict[str, str]:
        """Только отличающееся от умолчаний: короткий адрес, который можно отправить."""
        base = {
            "q": self.q, "prof": self.prof, "text": self.text, "status": self.status, "mod": self.mod,
            "origin": self.origin, "source": self.source, "city": self.city, "flag": self.flag, "kind": self.kind,
            "period": self.period if self.period != "all" else "",
            "sort": self.sort if self.sort != "created" else "",
            "dir": self.dir if self.dir != "desc" else "",
            "page": str(self.page) if self.page > 1 else "",
        }
        base.update({k: str(v) for k, v in override.items()})
        return {k: v for k, v in base.items() if v}

    def query(self, **override: Any) -> str:
        return urlencode(self.params(**override))

    def query_without_page(self, **override: Any) -> str:
        return urlencode({k: v for k, v in self.params(**override).items() if k != "page"})

    @property
    def active(self) -> bool:
        return any((self.q, self.prof, self.text, self.status, self.mod, self.origin, self.source, self.city, self.flag,
                    self.kind)) or self.period != "all"


def _pick(value: str, allowed: Any) -> str:
    return value if value in allowed else ""


def parse(params: Mapping[str, str]) -> Filters:
    def get(name: str) -> str:
        return str(params.get(name, "") or "")

    try:
        page = min(max(int(get("page") or 1), 1), 100_000)
    except ValueError:
        page = 1
    sort = get("sort") if get("sort") in SORTS else "created"
    return Filters(
        q=clean_line(get("q"), 80), prof=clean_line(get("prof"), 60), text=clean_line(get("text"), 80),
        status=_pick(get("status"), STATUSES), mod=_pick(get("mod"), MODERATION), origin=_pick(get("origin"), ORIGINS),
        source=clean_line(get("source"), 30), city=clean_line(get("city"), 60), flag=_pick(get("flag"), FLAGS),
        kind=_pick(get("kind"), KINDS), period=get("period") if get("period") in PERIODS else "all",
        sort=sort, dir="asc" if get("dir") == "asc" else "desc", page=page,
    )


def _fold_function(conn: sqlite3.Connection) -> None:
    conn.create_function("rfold", 1, lambda s: stoplist.fold(s) if isinstance(s, str) else s, deterministic=True)


def _stale_cutoff(now: datetime) -> str:
    return (now - timedelta(days=int(config.VACANCY_TTL_DAYS))).strftime("%Y-%m-%dT%H:%M:%S")


def _foreign_sql() -> tuple[str, list[str]]:
    cities = list(config.SERVICE_CITIES)
    if not cities:
        return "0", []
    return f"(city IS NOT NULL AND city != '' AND city NOT IN ({', '.join('?' * len(cities))}))", cities


def build_where(f: Filters, now: datetime) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    q = f.q
    if q:
        digits = re.sub(r"\D", "", q)
        if q.isdigit() and len(q) <= 7:
            clauses.append("id = ?")
            args.append(int(q))
        elif len(digits) >= 6 and not re.search(r"[^\d\s+\-()]", q):
            clauses.append("contacts LIKE ?")
            args.append(f"%{digits[-10:]}%")
        else:
            like = f"%{stoplist.fold(q)}%"
            clauses.append(
                "(rfold(profession) LIKE ? OR rfold(title_raw) LIKE ? OR rfold(employer) LIKE ? OR contacts LIKE ?"
                " OR company_id IN (SELECT id FROM companies WHERE rfold(name) LIKE ?))"
            )
            args += [like, like, like, f"%{q}%", like]
    if f.prof:
        clauses.append("rfold(profession) LIKE ?")
        args.append(f"%{stoplist.fold(f.prof)}%")
    if f.text:
        like = f"%{stoplist.fold(f.text)}%"
        clauses.append("(rfold(summary) LIKE ? OR rfold(requirements) LIKE ? OR rfold(title_raw) LIKE ?)")
        args += [like, like, like]
    if f.status:
        clauses.append("status = ?")
        args.append(f.status)
    if f.mod:
        clauses.append("moderation_status = ?")
        args.append(f.mod)
    if f.origin:
        clauses.append("origin = ?")
        args.append(f.origin)
    if f.source:
        clauses.append("source_type = ?")
        args.append(f.source)
    if f.city == FOREIGN_CITY:
        sql, cities = _foreign_sql()
        clauses.append(sql)
        args += cities
    elif f.city:
        clauses.append("rfold(city) = ?")
        args.append(stoplist.fold(f.city))
    if f.kind == "vacancy":
        clauses.append("kind = 'vacancy'")
    elif f.kind == "other":
        clauses.append("kind != 'vacancy'")
    if f.flag == "visible":
        clauses.append(queries.VISIBLE_SQL)
    elif f.flag == "invisible":
        clauses.append(f"NOT ({queries.VISIBLE_SQL})")
    elif f.flag == "duplicate":
        clauses.append("duplicate_of IS NOT NULL")
    elif f.flag == "suspicious":
        clauses.append("suspicious = 1")
    elif f.flag == "stale":
        clauses.append("status = 'active' AND COALESCE(last_seen_at, published_at) < ?")
        args.append(_stale_cutoff(now))
    elif f.flag == "foreign":
        sql, cities = _foreign_sql()
        clauses.append(sql)
        args += cities
    bounds = period_bounds(f.period, now)
    if bounds:
        clauses.append("datetime(created_at) >= ? AND datetime(created_at) < ?")
        args += list(bounds)
    return (" AND ".join(clauses) or "1"), args


def listing(conn: sqlite3.Connection, f: Filters, now: datetime, per_page: int = PER_PAGE) -> Page:
    _fold_function(conn)
    where, args = build_where(f, now)
    total = int(conn.execute(f"SELECT COUNT(*) FROM vacancies WHERE {where}", args).fetchone()[0])
    p = clamp_page(f.page, total, per_page)
    f.page = p
    column = SORTS[f.sort][1]
    direction = "ASC" if f.dir == "asc" else "DESC"
    order = f"({column}) IS NULL, ({column}) {direction}, id {direction}" if f.sort == "pay" else f"{column} {direction}, id {direction}"
    rows = conn.execute(
        f"SELECT * FROM vacancies WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", [*args, per_page, (p - 1) * per_page]
    ).fetchall()
    items = [vacancy_row_to_dict(r) for r in rows]
    for item in items:
        item["visibility"] = visibility(item, now)
    return Page(items, total, p, per_page)


def counts(conn: sqlite3.Connection, now: datetime) -> dict[str, int]:
    """Плитки над списком: всего, в выдаче, на проверке, скрыто, истекло, дублей, вне города."""

    def one(where: str, *args: Any) -> int:
        return int(conn.execute(f"SELECT COUNT(*) FROM vacancies WHERE {where}", args).fetchone()[0])

    foreign_sql, cities = _foreign_sql()
    return {
        "all": one("1"), "visible": one(queries.VISIBLE_SQL), "pending": one("moderation_status = 'pending'"),
        "hidden": one("status = 'hidden'"), "expired": one("status = 'expired'"), "archived": one("status = 'archived'"),
        "duplicate": one("duplicate_of IS NOT NULL"), "foreign": one(foreign_sql, *cities),
    }


def facets(conn: sqlite3.Connection) -> dict[str, list[tuple[str, int]]]:
    def distinct(column: str, limit: int = 60) -> list[tuple[str, int]]:
        rows = conn.execute(
            f"SELECT {column} AS v, COUNT(*) AS n FROM vacancies WHERE {column} IS NOT NULL AND {column} != '' "
            f"GROUP BY {column} ORDER BY n DESC, v LIMIT {int(limit)}"
        ).fetchall()
        return [(r["v"], r["n"]) for r in rows]

    return {"cities": distinct("city"), "sources": distinct("source_type")}


# ------------------------------------------------------------------ состояние вакансии

def visibility(v: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """Видна ли вакансия посетителям и почему нет. Повторяет условие VISIBLE_SQL на уровне одной записи."""
    reasons: list[tuple[str, str, str]] = []  # текст, вид метки, значок
    if v.get("kind") != "vacancy":
        reasons.append(("Не вакансия", "mute", "info"))
    if v.get("moderation_status") == "pending":
        reasons.append(("На проверке", "warn", "clock"))
    elif v.get("moderation_status") == "rejected":
        reasons.append(("Отклонена", "danger", "x-circle"))
    status = v.get("status")
    if status == "hidden" and v.get("moderation_status") == "approved":
        reasons.append(("Скрыта", "mute", "lock-key"))
    elif status == "expired":
        reasons.append(("Истекла", "mute", "clock"))
    elif status == "archived":
        reasons.append(("В архиве", "mute", "hourglass"))
    elif status == "closed":
        reasons.append(("Закрыта", "mute", "lock-simple"))
    elif status not in ("active", "hidden"):
        reasons.append((str(status), "mute", ""))
    if v.get("duplicate_of"):
        reasons.append((f"Дубль №{v['duplicate_of']}", "info", "copy"))
    if v.get("suspicious"):
        reasons.append(("Подозрительная", "danger", "warning"))
    seen = parse_iso(v.get("last_seen_at") or v.get("published_at"))
    if status == "active" and seen is not None and seen < now - timedelta(days=int(config.VACANCY_TTL_DAYS)):
        reasons.append(("Срок жизни вышел", "mute", "hourglass"))
    city = (v.get("city") or "").strip()
    if city and config.SERVICE_CITIES and city not in config.SERVICE_CITIES:
        reasons.append((f"Город вне выдачи: {city}", "mute", "map-pin"))
    return {"visible": not reasons, "reasons": reasons}


# ------------------------------------------------------------------ карточка

def get(conn: sqlite3.Connection, vacancy_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM vacancies WHERE id=?", (vacancy_id,)).fetchone()
    return vacancy_row_to_dict(row) if row else None


def raw_post(conn: sqlite3.Connection, v: Mapping[str, Any]) -> dict[str, Any] | None:
    """Исходный пост источника, из которого собрана вакансия (у размещённых на сайте его нет)."""
    if not v.get("source") or not v.get("external_id"):
        return None
    row = conn.execute("SELECT * FROM raw_posts WHERE source=? AND external_id=?", (v["source"], v["external_id"])).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        images = json.loads(d.get("image_urls") or "[]")
    except ValueError:
        images = []
    d["images"] = len(images) if isinstance(images, list) else 0
    d["masked_text"] = mask_text(d.get("text") or "")
    try:
        extra = json.loads(d.get("extra") or "{}")
    except ValueError:
        extra = {}
    d["extra_text"] = mask_text(json.dumps(extra, ensure_ascii=False, indent=1)[:2000]) if extra else ""
    d["url_safe"] = d["url"] if str(d.get("url") or "").startswith(("http://", "https://")) else ""
    return d


def _details_text(raw: str) -> str:
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(data, dict):
        return str(data)
    parts: list[str] = []
    for key, val in data.items():
        if isinstance(val, dict):
            inner = ", ".join(f"{k}: {v}" for k, v in val.items())
            parts.append(f"{key} ({inner})")
        elif val not in ("", None):
            parts.append(f"{key}: {val}")
    return "; ".join(parts)


def history(conn: sqlite3.Connection, v: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    """События по вакансии от новых к старым: создание, публикация, модерация, жалобы, действия из журнала."""
    vid = int(v["id"])
    events: list[dict[str, Any]] = []

    def add(at: str | None, title: str, text: str = "", icon: str = "info", kind: str = "system", who: str = "") -> None:
        if at:
            events.append({"at": at, "title": title, "text": text, "icon": icon, "kind": kind, "who": who})

    if v.get("origin") == "direct":
        add(v.get("created_at"), "Размещена работодателем на сайте", "Ушла на проверку модератору." if v.get("moderation_status") == "pending"
            else "", "briefcase", "created")
    else:
        add(v.get("created_at"), "Добавлена в базу", f"Источник: {v.get('source_type') or 'не указан'}"
            f"{', ' + v['source'] if v.get('source') else ''}.", "briefcase", "created")
    if v.get("published_at") and v.get("published_at") != v.get("created_at"):
        add(v["published_at"], "Опубликована в источнике", "", "arrow-square-out", "source")
    if v.get("last_seen_at") and v.get("last_seen_at") not in (v.get("published_at"), v.get("created_at")):
        add(v["last_seen_at"], "Последний раз подтверждена", "От этой даты считается срок жизни.", "check-circle", "source")
    seen = parse_iso(v.get("last_seen_at") or v.get("published_at"))
    if v.get("status") in ("expired", "archived") and seen is not None:
        add(iso(seen + timedelta(days=int(config.VACANCY_TTL_DAYS))), "Срок жизни истёк", f"Срок: {config.VACANCY_TTL_DAYS} дн.", "hourglass", "system")
    if v.get("status") == "archived" and v.get("archived_at"):
        add(v["archived_at"], "Перенесена в архив", "Из выдачи убрана, строка и поля карточки сохранены.", "hourglass", "system")

    for c in conn.execute("SELECT * FROM complaints WHERE vacancy_id=? ORDER BY id", (vid,)).fetchall():
        c = dict(c)
        add(c["created_at"], f"Жалоба №{c['id']}", c.get("reason") or "", "flag", "complaint")
        if c.get("resolved_at"):
            label = {"hidden": "вакансия скрыта", "stoplisted": "скрыта и в стоп-листе", "dismissed": "жалоба отклонена"}.get(
                c.get("resolution") or "", c.get("resolution") or "закрыта")
            add(c["resolved_at"], f"Жалоба №{c['id']} закрыта", label.capitalize(), "check", "complaint")

    for r in conn.execute(
        "SELECT * FROM audit_log WHERE target_type='vacancy' AND target_id=? ORDER BY id", (str(vid),)
    ).fetchall():
        label, icon = ACTION_LABELS.get(r["action"], (r["action"], "file-text"))
        add(r["at"], label, _details_text(r["details"]), icon, "audit", r["actor_email"])

    for r in conn.execute("SELECT id, created_at FROM vacancies WHERE duplicate_of=? ORDER BY id", (vid,)).fetchall():
        add(r["created_at"], f"Найден повтор: вакансия №{r['id']}", "", "copy", "dup")

    def key(e: dict[str, Any]) -> tuple[float, int]:
        dt = parse_iso(e["at"])
        return (dt.timestamp() if dt else 0.0, 0)

    events.sort(key=key, reverse=True)
    return events


def related(conn: sqlite3.Connection, v: Mapping[str, Any]) -> dict[str, Any]:
    vid = int(v["id"])
    main = None
    if v.get("duplicate_of"):
        row = conn.execute("SELECT id, profession, status FROM vacancies WHERE id=?", (v["duplicate_of"],)).fetchone()
        main = dict(row) if row else {"id": v["duplicate_of"], "profession": "запись удалена", "status": ""}
    dups = [dict(r) for r in conn.execute(
        "SELECT id, profession, source_type, source, status FROM vacancies WHERE duplicate_of=? ORDER BY id LIMIT 30", (vid,))]
    apps = int(conn.execute("SELECT COUNT(*) FROM applications WHERE vacancy_id=?", (vid,)).fetchone()[0])
    company = None
    if v.get("company_id"):
        row = conn.execute("SELECT id, name FROM companies WHERE id=?", (v["company_id"],)).fetchone()
        company = dict(row) if row else None
    return {"main": main, "duplicates": dups, "applications": apps, "company": company}


def field_rows(v: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Все поля записи по порядку. Контакты здесь не выводятся: они закрыты отдельным блоком."""

    def yes(x: Any) -> str:
        return "да" if x else "нет"

    def val(x: Any) -> str:
        return "" if x is None else str(x)

    pay = ""
    if v.get("pay_min") or v.get("pay_max"):
        pay = f"{val(v.get('pay_min')) or '?'} - {val(v.get('pay_max')) or '?'} ₽, {v.get('pay_period') or 'период не указан'}"
    return [
        ("Номер", val(v.get("id"))), ("Профессия", val(v.get("profession"))), ("Заголовок в источнике", val(v.get("title_raw"))),
        ("Тип записи", val(v.get("kind"))), ("Сфера", val(v.get("category"))), ("Формат", val(v.get("work_format"))),
        ("Занятость", val(v.get("employment"))), ("График", val(v.get("schedule"))), ("Оплата", pay),
        ("Выплата каждый день", yes(v.get("pay_daily"))), ("Оценка в месяц", val(v.get("pay_monthly_estimate"))),
        ("Город", val(v.get("city"))), ("Район или улица", val(v.get("district"))), ("Опыт", val(v.get("experience"))),
        ("Возраст от", val(v.get("age_min"))), ("Требования", val(v.get("requirements"))), ("Работодатель", val(v.get("employer"))),
        ("Статус", STATUSES.get(v.get("status") or "", val(v.get("status")))),
        ("Модерация", MODERATION.get(v.get("moderation_status") or "", val(v.get("moderation_status")))),
        ("Пометка модерации", val(v.get("moderation_note"))), ("Происхождение", ORIGINS.get(v.get("origin") or "", val(v.get("origin")))),
        ("Тип источника", val(v.get("source_type"))), ("Источник", val(v.get("source"))),
        ("Внешний номер", val(v.get("external_id"))), ("Дубль вакансии", val(v.get("duplicate_of"))),
        ("Подозрительная", yes(v.get("suspicious"))), ("Ключ склейки", val(v.get("dedupe_key"))),
        ("Добавлена", val(v.get("created_at"))), ("Опубликована", val(v.get("published_at"))),
        ("Подтверждена", val(v.get("last_seen_at"))),
    ]


def card_extras(conn: sqlite3.Connection, v: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "history": history(conn, v, now), "raw": raw_post(conn, v), "rel": related(conn, v),
        "fields": [(k, x) for k, x in field_rows(v) if x not in ("", "None")], "visibility": visibility(v, now),
        "contact_items": contact_items(v.get("contacts")),
    }


# ------------------------------------------------------------------ повтор

def _root_of(conn: sqlite3.Connection, vacancy_id: int) -> int | None:
    """Главная вакансия цепочки: идём по duplicate_of до записи без ссылки. Петля даёт None."""
    seen: set[int] = set()
    current = vacancy_id
    for _ in range(MAX_CHAIN):
        if current in seen:
            return None
        seen.add(current)
        row = conn.execute("SELECT duplicate_of FROM vacancies WHERE id=?", (current,)).fetchone()
        if row is None:
            return None
        if row["duplicate_of"] is None:
            return current
        current = int(row["duplicate_of"])
    return None


def mark_duplicate(
    conn: sqlite3.Connection, vacancy_id: int, main_id: int, actor: dict[str, Any] | None, ip: str = "",
    now: datetime | None = None,
) -> int:
    """Отметить вакансию повторяющейся: она уходит из выдачи, главной становится вакансия main_id (или её главная).

    Те, кто уже был повтором этой вакансии, переносятся к новой главной: цепочек не бывает. Возвращает номер главной.
    """
    if vacancy_id == main_id:
        raise ValueError("Вакансия не может быть повтором самой себя.")
    row = conn.execute("SELECT id, duplicate_of FROM vacancies WHERE id=?", (vacancy_id,)).fetchone()
    if row is None:
        raise ValueError("Вакансия не найдена.")
    main = conn.execute("SELECT id, kind FROM vacancies WHERE id=?", (main_id,)).fetchone()
    if main is None:
        raise ValueError(f"Вакансии №{main_id} нет: укажите номер существующей.")
    root = _root_of(conn, main_id)
    if root is None:
        raise ValueError("Цепочка повторов зациклена, выберите другую главную вакансию.")
    if root == vacancy_id:
        raise ValueError(f"Вакансия №{main_id} сама повтор этой вакансии.")
    conn.execute("UPDATE vacancies SET duplicate_of=? WHERE id=?", (root, vacancy_id))
    conn.execute("UPDATE vacancies SET duplicate_of=? WHERE duplicate_of=?", (root, vacancy_id))
    conn.commit()
    audit.log(conn, actor, "vacancy.duplicate", "vacancy", vacancy_id,
              {"главная": root, "было": row["duplicate_of"] or ""}, ip=ip, now=now)
    return root


def clear_duplicate(conn: sqlite3.Connection, vacancy_id: int, actor: dict[str, Any] | None, ip: str = "",
                    now: datetime | None = None) -> bool:
    row = conn.execute("SELECT duplicate_of FROM vacancies WHERE id=?", (vacancy_id,)).fetchone()
    if row is None or row["duplicate_of"] is None:
        return False
    conn.execute("UPDATE vacancies SET duplicate_of=NULL WHERE id=?", (vacancy_id,))
    conn.commit()
    audit.log(conn, actor, "vacancy.unduplicate", "vacancy", vacancy_id, {"была главная": row["duplicate_of"]}, ip=ip, now=now)
    return True


__all__ = ["Filters", "card_extras", "clear_duplicate", "counts", "facets", "listing", "mark_duplicate", "parse"]
