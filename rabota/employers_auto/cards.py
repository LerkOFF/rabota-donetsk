"""Автокарточки: находим или заводим компанию по вакансии и привязываем к ней вакансию.

    resolve(conn, ...)            -> Resolution: id карточки, создана ли, причина отказа
    attach_vacancy(conn, row)     привязать одну вакансию (company_id, company_state, владелец, даты карточки)
    attach_pending(conn, limit)   привязать все новые вакансии, у которых есть название работодателя
    backfill(conn, dry_run)       то же по всей базе с отчётом; повторный запуск ничего не меняет

Дедуп: сначала по id работодателя у источника (hh: `company.id`, Работа России: ИНН), затем по нормализованному
названию и городу. Физлица, заглушки, профессии вместо названия и мусор карточек не получают (rabota.employers_auto.names).
Функции не вызывают commit: коммитит вызывающий (так работает и пробный прогон `--dry-run`).
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rabota.employers_auto import names
from rabota.lifecycle import later_of, parse_iso, to_utc_iso

# источники, где название работодателя приходит структурным полем, а не из текста поста
STRUCTURED_SOURCES = frozenset({"hh", "trudvsem", "mczn"})
BATCH = 500


@dataclass
class Resolution:
    company_id: int | None
    created: bool = False
    reason: str = "ok"
    name: str = ""


@dataclass
class Report:
    scanned: int = 0
    linked: int = 0            # привязано к уже существующей карточке
    created: int = 0           # заведено новых карточек
    skipped: Counter = field(default_factory=Counter)
    examples: dict[str, list[str]] = field(default_factory=dict)
    created_names: list[str] = field(default_factory=list)
    dry_run: bool = False

    def skip(self, reason: str, name: str) -> None:
        self.skipped[reason] += 1
        bucket = self.examples.setdefault(reason, [])
        if len(bucket) < 5 and name not in bucket:
            bucket.append(name)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run, "scanned": self.scanned, "linked": self.linked, "created": self.created,
            "skipped": dict(self.skipped), "skipped_total": sum(self.skipped.values()),
            "examples": self.examples, "created_names": self.created_names[:10],
        }


# ------------------------------------------------------------------ источник

def _extra_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def source_identity(source_type: str, extra: Any) -> tuple[str, str]:
    """(ссылка на работодателя у источника, его название у источника). Пустые строки, если источник их не даёт.

    hh: `company.id` и `company.name` (в вакансии бывает «Банк ПСБ, Старт карьеры», у работодателя «Банк ПСБ»).
    Работа России: ИНН (`company.inn`), иначе код организации."""
    data = _extra_dict(extra)
    company = data.get("company")
    if not isinstance(company, dict):
        return "", ""
    name = str(company.get("name") or company.get("shortname") or "").strip()
    if source_type == "hh":
        ident = company.get("id")
        return (f"hh:{ident}" if ident not in (None, "") else ""), name
    if source_type == "trudvsem":
        inn = str(company.get("inn") or "").strip()
        code = str(company.get("companycode") or company.get("ogrn") or "").strip()
        if inn.isdigit():
            return f"inn:{inn}", name
        if code.isdigit():
            return f"ogrn:{code}", name
    return "", name


# ------------------------------------------------------------------ поиск и создание

def _follow(conn: sqlite3.Connection, company_id: int) -> int:
    """Карточка, в которую слита эта (по цепочке); сама, если не слита."""
    for _ in range(10):
        row = conn.execute("SELECT merged_into FROM companies WHERE id = ?", (company_id,)).fetchone()
        if row is None or not row[0]:
            break
        company_id = int(row[0])
    return company_id


def _find_by_ref(conn: sqlite3.Connection, kind: str, ref: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM companies WHERE origin = 'auto' AND source_kind = ? AND source_ref = ? ORDER BY merged_into IS NOT NULL, id",
        (kind, ref),
    ).fetchone()
    return _follow(conn, int(row[0])) if row else None


def _find_by_norm(conn: sqlite3.Connection, norm: str, city: str) -> int | None:
    rows = conn.execute(
        "SELECT id, COALESCE(city, '') AS city FROM companies WHERE origin = 'auto' AND norm_name = ? AND merged_into IS NULL "
        "ORDER BY id",
        (norm,),
    ).fetchall()
    if not rows:
        gone = conn.execute(
            "SELECT id FROM companies WHERE origin = 'auto' AND norm_name = ? AND merged_into IS NOT NULL "
            "AND COALESCE(city, '') = ? ORDER BY id LIMIT 1", (norm, city)).fetchone()
        return _follow(conn, int(gone[0])) if gone else None
    for r in rows:
        if r["city"] == city:
            return int(r["id"])
    if not city:
        return int(rows[0]["id"])
    for r in rows:
        if not r["city"]:  # карточка без города подхватывает город вакансии
            return int(r["id"])
    return None


def resolve(
    conn: sqlite3.Connection, *, employer: str | None, city: str | None, source_type: str, extra: Any = None,
    profession: str | None = None, seen_at: str | None = None, now: datetime | None = None,
    cache: dict[Any, int] | None = None, create: bool = True,
) -> Resolution:
    """Найти или завести карточку для названия работодателя. create=False: только поиск (пробный прогон)."""
    ref, ref_name = source_identity(source_type, extra)
    name_text = ref_name if (ref and ref_name and source_type == "hh") else (employer or ref_name or "")
    decision = names.decide(name_text, profession, source_type=source_type)
    if not decision.ok and employer and name_text != employer:
        decision = names.decide(employer, profession, source_type=source_type)
    if not decision.ok or decision.info is None:
        return Resolution(None, False, decision.reason, (name_text or "").strip())
    info = decision.info
    city_value = (city or "").strip()
    cache = cache if cache is not None else {}
    keys: list[Any] = []
    if ref:
        keys.append(("ref", source_type, ref))
    keys.append(("norm", info.norm, city_value))
    for key in keys:
        if key in cache:
            return Resolution(cache[key], False, "ok", info.display)

    found = _find_by_ref(conn, source_type, ref) if ref else None
    if found is None:
        found = _find_by_norm(conn, info.norm, city_value)
    if found is not None:
        _enrich(conn, found, ref=ref, kind=source_type, city=city_value)
        for key in keys:
            cache[key] = found
        return Resolution(found, False, "ok", info.display)
    if not create:
        return Resolution(None, True, "ok", info.display)

    stamp = to_utc_iso(seen_at) or (now or datetime.now(UTC)).replace(microsecond=0).isoformat()
    try:
        cur = conn.execute(
            "INSERT INTO companies(owner_user_id, name, city, origin, norm_name, legal_form, source_kind, source_ref, "
            "first_seen_at, last_vacancy_at) VALUES (NULL,?,?,?,?,?,?,?,?,?)",
            (info.display, city_value or None, "auto", info.norm, info.legal_form or None, source_type or None,
             ref or None, stamp, stamp),
        )
    except sqlite3.IntegrityError:
        # параллельный воркер успел раньше: берём его карточку
        found = (_find_by_ref(conn, source_type, ref) if ref else None) or _find_by_norm(conn, info.norm, city_value)
        if found is None:
            return Resolution(None, False, "conflict", info.display)
        return Resolution(found, False, "ok", info.display)
    new_id = int(cur.lastrowid or 0)
    for key in keys:
        cache[key] = new_id
    return Resolution(new_id, True, "ok", info.display)


def _enrich(conn: sqlite3.Connection, company_id: int, *, ref: str, kind: str, city: str) -> None:
    """Известной карточке добавляем то, чего у неё не было: ссылку на работодателя у источника и город."""
    conn.execute(
        "UPDATE companies SET source_kind = COALESCE(source_kind, ?), source_ref = COALESCE(source_ref, ?) "
        "WHERE id = ? AND source_ref IS NULL AND ? != '' AND NOT EXISTS "
        "(SELECT 1 FROM companies c2 WHERE c2.origin = 'auto' AND c2.source_kind = ? AND c2.source_ref = ? AND c2.merged_into IS NULL)",
        (kind, ref, company_id, ref, kind, ref),
    )
    if city:
        conn.execute(
            "UPDATE companies SET city = ? WHERE id = ? AND (city IS NULL OR city = '') AND NOT EXISTS "
            "(SELECT 1 FROM companies c2 WHERE c2.origin = 'auto' AND c2.norm_name = companies.norm_name AND c2.city = ? "
            "AND c2.merged_into IS NULL AND c2.id != companies.id)",
            (city, company_id, city),
        )


# ------------------------------------------------------------------ привязка вакансий

_PENDING_SELECT = (
    "SELECT v.id, v.employer, v.city, v.profession, v.source_type, v.owner_user_id, v.published_at, v.last_seen_at, "
    "r.extra AS raw_extra FROM vacancies v LEFT JOIN raw_posts r ON r.source = v.source AND r.external_id = v.external_id "
    "WHERE v.company_id IS NULL AND v.company_state IS NULL AND v.kind = 'vacancy' AND v.origin != 'direct' "
    "AND v.employer IS NOT NULL AND TRIM(v.employer) != '' AND v.suspicious = 0 AND v.moderation_status = 'approved' "
    "AND v.status != 'hidden' ORDER BY v.id"
)


def _touch_company(conn: sqlite3.Connection, company_id: int, seen: str | None, first: str | None) -> None:
    if not (seen and first):
        return
    conn.execute(
        "UPDATE companies SET last_vacancy_at = CASE WHEN last_vacancy_at IS NULL OR last_vacancy_at < ? THEN ? ELSE last_vacancy_at END, "
        "first_seen_at = CASE WHEN first_seen_at IS NULL OR first_seen_at > ? THEN ? ELSE first_seen_at END WHERE id = ?",
        (seen, seen, first, first, company_id),
    )


def _link(conn: sqlite3.Connection, row: sqlite3.Row, company_id: int, owner: int | None) -> None:
    conn.execute(
        "UPDATE vacancies SET company_id = ?, company_state = 'linked', "
        "owner_user_id = COALESCE(owner_user_id, ?) WHERE id = ?",
        (company_id, owner, row["id"]),
    )
    seen = later_of(row["last_seen_at"], row["published_at"])
    first = to_utc_iso(row["published_at"]) or to_utc_iso(row["last_seen_at"])
    _touch_company(conn, company_id, seen, first)


def attach_rows(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], report: Report, *,
    now: datetime | None = None, cache: dict[Any, int] | None = None, owners: dict[int, int | None] | None = None,
) -> None:
    cache = cache if cache is not None else {}
    owners = owners if owners is not None else {}
    for row in rows:
        report.scanned += 1
        res = resolve(
            conn, employer=row["employer"], city=row["city"], source_type=row["source_type"], extra=row["raw_extra"],
            profession=row["profession"], seen_at=row["published_at"] or row["last_seen_at"], now=now, cache=cache,
        )
        if res.company_id is None:
            report.skip(res.reason, res.name or (row["employer"] or "").strip())
            if res.reason not in ("conflict",):
                conn.execute("UPDATE vacancies SET company_state = ? WHERE id = ?", (f"skip:{res.reason}", row["id"]))
            continue
        if res.created:
            report.created += 1
            report.created_names.append(res.name)
        else:
            report.linked += 1
        cid = int(res.company_id or 0)
        if cid not in owners:
            r = conn.execute("SELECT owner_user_id FROM companies WHERE id = ?", (cid,)).fetchone()
            owners[cid] = int(r[0]) if r and r[0] else None
        _link(conn, row, cid, owners[cid])


def attach_pending(conn: sqlite3.Connection, limit: int = 2000, now: datetime | None = None) -> Report:
    """Привязать вакансии, у которых есть название работодателя, а карточки ещё нет. Коммитит сам."""
    report = Report()
    rows = conn.execute(_PENDING_SELECT + " LIMIT ?", (limit,)).fetchall()
    attach_rows(conn, rows, report, now=now)
    conn.commit()
    return report


def attach_vacancy(conn: sqlite3.Connection, vacancy_id: int, now: datetime | None = None) -> int | None:
    """Привязать одну вакансию (после сохранения конвейером). Возвращает id карточки или None. Коммитит сам."""
    row = conn.execute(
        _PENDING_SELECT.replace("WHERE v.company_id IS NULL", "WHERE v.id = ? AND v.company_id IS NULL", 1)
        .replace(" ORDER BY v.id", ""),
        (vacancy_id,),
    ).fetchone()
    if row is None:
        return None
    report = Report()
    attach_rows(conn, [row], report, now=now)
    conn.commit()
    return conn.execute("SELECT company_id FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()[0]


def enabled(conn: sqlite3.Connection | None) -> bool:
    from rabota.admin import settings

    return bool(settings.effective(conn, "employer_auto_enabled"))


def backfill(conn: sqlite3.Connection, *, dry_run: bool = False, now: datetime | None = None, recheck: bool = False) -> Report:
    """Пройти по всем сохранённым вакансиям с названием работодателя и привязать их. Идемпотентно.

    dry_run=True: считает, что было бы сделано, и ничего не пишет. recheck=True: вакансии, которым раньше отказали
    (company_state = skip:...), проверяются заново, например после смены правил отсева."""
    report = Report(dry_run=dry_run)
    conn.commit()  # пробный прогон откатывается целиком: до него в соединении не должно быть чужих правок
    if recheck:
        conn.execute("UPDATE vacancies SET company_state = NULL WHERE company_state LIKE 'skip:%'")
    cache: dict[Any, int] = {}
    owners: dict[int, int | None] = {}
    last_id = 0
    while True:
        rows = conn.execute(
            _PENDING_SELECT.replace(" ORDER BY v.id", " AND v.id > ? ORDER BY v.id LIMIT ?"), (last_id, BATCH),
        ).fetchall()
        if not rows:
            break
        last_id = int(rows[-1]["id"])
        attach_rows(conn, rows, report, now=now, cache=cache, owners=owners)
        if not dry_run:
            conn.commit()
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return report


# ------------------------------------------------------------------ чтение для страниц

def get(conn: sqlite3.Connection, company_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    return dict(row) if row else None


def is_auto_unclaimed(row: dict[str, Any] | None) -> bool:
    return bool(row) and row.get("origin") == "auto" and not row.get("owner_user_id")  # type: ignore[union-attr]


def counts(conn: sqlite3.Connection, company_id: int) -> dict[str, int]:
    """Вакансии компании: открытые на сайте, в архиве и все (без дублей)."""
    from rabota.web import queries as q

    shown = int(conn.execute(
        f"SELECT COUNT(*) FROM vacancies WHERE {q.VISIBLE_SQL} AND company_id = ?", (company_id,)).fetchone()[0])
    archived = int(conn.execute(
        "SELECT COUNT(*) FROM vacancies WHERE company_id = ? AND status = 'archived' AND duplicate_of IS NULL "
        "AND kind = 'vacancy' AND suspicious = 0 AND moderation_status = 'approved'", (company_id,)).fetchone()[0])
    return {"visible": shown, "archived": archived, "total": shown + archived}


def refresh_dates(conn: sqlite3.Connection, company_id: int) -> None:
    """Пересчитать first_seen_at и last_vacancy_at по вакансиям (после слияния и переноса)."""
    row = conn.execute(
        "SELECT MIN(COALESCE(published_at, last_seen_at)) AS f, MAX(COALESCE(last_seen_at, published_at)) AS l "
        "FROM vacancies WHERE company_id = ?", (company_id,)).fetchone()
    if row and row["l"]:
        conn.execute(
            "UPDATE companies SET first_seen_at = COALESCE(?, first_seen_at), last_vacancy_at = ? WHERE id = ?",
            (to_utc_iso(row["f"]), to_utc_iso(row["l"]), company_id),
        )


def age_days(value: str | None, now: datetime) -> int | None:
    dt = parse_iso(value)
    return None if dt is None else max(0, (now - dt).days)
