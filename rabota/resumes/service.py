"""Резюме без привязки к вебу. Функции получают соединение SQLite (строки как словари) и время.

Своё резюме: список, создание (до MAX_RESUMES), правка, скрытие, обновление, удаление.
Работодатель: поиск среди резюме с видимостью "всем работодателям", карточка (также резюме из его откликов),
открытие контактов по кнопке с учётом просмотров, приглашение с созданием переписки.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from rabota.accounts.tokens import iso
from rabota.messaging import rules as msg_rules
from rabota.messaging.rules import MessagingError
from rabota.pipeline.dictionaries import norm
from rabota.resumes import rules
from rabota.web import format as fmt
from rabota.web.queries import VISIBLE_SQL


class ResumeError(Exception):
    """Ожидаемая ошибка с понятным текстом. code нужен коду и тестам."""

    def __init__(self, code: str, text: str, **extra: Any):
        super().__init__(text)
        self.code = code
        self.text = text
        self.extra = extra


class NotFound(ResumeError):
    def __init__(self, what: str = "Резюме не найдено"):
        super().__init__("not_found", what)


def _rows(conn: sqlite3.Connection, sql: str, args: Any = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _page(total: int, page: int, per_page: int) -> dict[str, Any]:
    pages = max(1, math.ceil(total / per_page))
    page = min(max(1, page), pages)
    return {"total": total, "page": page, "pages": pages, "has_prev": page > 1, "has_next": page < pages,
            "offset": (page - 1) * per_page, "per_page": per_page}


# ------------------------------------------------------------------ представление

def decode(row: dict[str, Any]) -> dict[str, Any]:
    """Строка таблицы -> словарь с развёрнутыми списками и готовыми подписями."""
    r = dict(row)
    r["work_formats"] = rules.loads(r.get("work_formats"), [])
    r["employments"] = rules.loads(r.get("employments"), [])
    r["skills"] = rules.loads(r.get("skills"), [])
    r["places"] = rules.loads(r.get("experience"), [])
    r["relocate"] = bool(r.get("relocate"))
    r["remote"] = bool(r.get("remote"))
    r["admin_hidden"] = bool(r.get("admin_hidden"))
    r["title"] = r.get("title") or rules.ucfirst(r.get("profession") or "")
    return r


def present(resume: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Подписи для показа: зарплата, опыт, условия, район, свежесть, заполненность."""
    r = dict(resume)
    pay = int(r["pay_min"]) if r.get("pay_min") else 0
    r["pay_text"] = (
        f"от {fmt.money(pay)}{fmt.NBSP}₽ {rules.PAY_PERIOD_LABELS.get(r.get('pay_period') or '', '')}".strip()
        if pay else "Зарплата не указана"
    )
    years = r.get("experience_years")
    if years is None:
        r["experience_text"] = "Стаж не указан"
    elif years == 0:
        r["experience_text"] = "Без опыта"
    else:
        r["experience_text"] = f"Стаж {years}{fmt.NBSP}{fmt.plural(years, 'год', 'года', 'лет')}"
    r["location"] = fmt.location_label(r.get("city"), r.get("district"))
    r["format_labels"] = [rules.WORK_FORMAT_LABELS[f] for f in r["work_formats"] if f in rules.WORK_FORMAT_LABELS]
    r["employment_labels"] = [rules.EMPLOYMENT_LABELS[e] for e in r["employments"] if e in rules.EMPLOYMENT_LABELS]
    flags = []
    if r["relocate"]:
        flags.append("Готов к переезду")
    if r["remote"]:
        flags.append("Готов работать удалённо")
    r["flags"] = flags
    r["visibility_label"] = rules.VISIBILITY_LABELS.get(r.get("visibility") or "hidden", "")
    updated = fmt.parse_dt(r.get("updated_at"))
    r["updated_label"] = fmt.date_label(updated, now) if updated else ""
    r["stale"] = bool(updated and now - updated > timedelta(days=rules.STALE_DAYS))
    r["percent"], r["tips"] = rules.completeness(r)
    r["live"] = r.get("visibility") != "hidden" and not r.get("admin_hidden") and not r["stale"]
    r["phone_pretty"] = fmt.phone_pretty(r["contact_phone"]) if r.get("contact_phone") else ""
    r["phone_href"] = fmt.phone_href(r["contact_phone"]) if r.get("contact_phone") else ""
    r["telegram_href"] = f"https://t.me/{r['contact_telegram']}" if r.get("contact_telegram") else ""
    return r


# ------------------------------------------------------------------ своё резюме

def list_own(conn: sqlite3.Connection, user_id: int, now: datetime) -> list[dict[str, Any]]:
    rows = _rows(conn, "SELECT * FROM resumes WHERE user_id = ? ORDER BY updated_at DESC, id DESC", (user_id,))
    items = [present(decode(r), now) for r in rows]
    stats = view_stats(conn, [r["id"] for r in items])
    for r in items:
        r.update(stats.get(r["id"], {"views": 0, "contacts": 0}))
    return items


def get_own(conn: sqlite3.Connection, resume_id: int, user_id: int) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT * FROM resumes WHERE id = ? AND user_id = ?", (resume_id, user_id))
    return decode(rows[0]) if rows else None


def count_own(conn: sqlite3.Connection, user_id: int) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM resumes WHERE user_id = ?", (user_id,)).fetchone()[0])


def to_form(resume: dict[str, Any]) -> dict[str, Any]:
    """Значения формы из сохранённого резюме (для правки)."""
    form: dict[str, Any] = {
        "profession": resume.get("title") or "", "city": resume.get("city") or "", "district": resume.get("district") or "",
        "relocate": resume.get("relocate"), "remote": resume.get("remote"),
        "pay_min": resume.get("pay_min") or "", "pay_period": resume.get("pay_period") or "",
        "work_formats": resume.get("work_formats") or [], "employments": resume.get("employments") or [],
        "schedule": resume.get("schedule") or "",
        "experience_years": "" if resume.get("experience_years") is None else resume["experience_years"],
        "skills": ", ".join(resume.get("skills") or []), "about": resume.get("about") or "",
        "contact_phone": resume.get("contact_phone") or "", "contact_telegram": resume.get("contact_telegram") or "",
        "visibility": resume.get("visibility") or "hidden",
        "consent": bool(resume.get("consent_at")) and not resume.get("consent_withdrawn_at")
        and resume.get("consent_version") == rules.consent_version(),
        "places": list(resume.get("places") or []),
    }
    return form


def _consent_state(existing: dict[str, Any] | None, clean: dict[str, Any], stamp: str) -> dict[str, Any]:
    at = (existing or {}).get("consent_at")
    version = (existing or {}).get("consent_version")
    withdrawn = (existing or {}).get("consent_withdrawn_at")
    if clean["visibility"] == "hidden":
        if at and not withdrawn:
            withdrawn = stamp
    elif not at or withdrawn or version != rules.consent_version():
        at, version, withdrawn = stamp, rules.consent_version(), None
    return {"consent_at": at, "consent_version": version, "consent_withdrawn_at": withdrawn}


def _values(clean: dict[str, Any], consent: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": clean["profession"], "profession": norm(clean["profession"]), "city": clean["city"],
        "district": clean["district"] or None, "district_key": norm(clean["district"]),
        "relocate": int(clean["relocate"]), "remote": int(clean["remote"]),
        "pay_min": clean["pay_min"], "pay_period": clean["pay_period"] or None,
        "work_formats": json.dumps(clean["work_formats"], ensure_ascii=False),
        "employments": json.dumps(clean["employments"], ensure_ascii=False), "schedule": clean["schedule"],
        "experience_years": clean["experience_years"],
        "experience": json.dumps(clean["places"], ensure_ascii=False),
        "skills": json.dumps(clean["skills"], ensure_ascii=False), "skills_key": norm(" ".join(clean["skills"])),
        "about": clean["about"], "contact_phone": clean["contact_phone"] or None,
        "contact_telegram": clean["contact_telegram"] or None, "visibility": clean["visibility"], **consent,
    }


def create(conn: sqlite3.Connection, user_id: int, clean: dict[str, Any], now: datetime) -> int:
    if count_own(conn, user_id) >= rules.MAX_RESUMES:
        raise ResumeError(
            "limit", f"У вас уже {rules.MAX_RESUMES} резюме: это предел. Удалите ненужное или отредактируйте одно из них.")
    stamp = iso(now)
    values = _values(clean, _consent_state(None, clean, stamp))
    cols = ["user_id", "created_at", "updated_at", *values]
    cur = conn.execute(
        f"INSERT INTO resumes({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [user_id, stamp, stamp, *values.values()],
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def update(conn: sqlite3.Connection, resume_id: int, user_id: int, clean: dict[str, Any], now: datetime) -> None:
    existing = get_own(conn, resume_id, user_id)
    if existing is None:
        raise NotFound()
    stamp = iso(now)
    values = _values(clean, _consent_state(existing, clean, stamp))
    sets = ", ".join(f"{c} = ?" for c in values)
    conn.execute(f"UPDATE resumes SET {sets}, updated_at = ? WHERE id = ? AND user_id = ?",
                 [*values.values(), stamp, resume_id, user_id])
    conn.commit()


def hide(conn: sqlite3.Connection, resume_id: int, user_id: int, now: datetime) -> None:
    """Скрыть от всех. Это же отзыв согласия на передачу: показать снова можно только через форму с галочкой."""
    existing = get_own(conn, resume_id, user_id)
    if existing is None:
        raise NotFound()
    if existing["visibility"] == "hidden":
        return
    conn.execute(
        "UPDATE resumes SET visibility = 'hidden', consent_withdrawn_at = COALESCE(consent_withdrawn_at, ?),"
        " updated_at = ? WHERE id = ?",
        (iso(now), iso(now), resume_id),
    )
    conn.commit()


def refresh(conn: sqlite3.Connection, resume_id: int, user_id: int, now: datetime) -> None:
    """Обновить дату: резюме снова считается свежим для поиска."""
    if get_own(conn, resume_id, user_id) is None:
        raise NotFound()
    conn.execute("UPDATE resumes SET updated_at = ? WHERE id = ? AND user_id = ?", (iso(now), resume_id, user_id))
    conn.commit()


def delete(conn: sqlite3.Connection, resume_id: int, user_id: int) -> None:
    if get_own(conn, resume_id, user_id) is None:
        raise NotFound()
    conn.execute("DELETE FROM resume_views WHERE resume_id = ?", (resume_id,))
    conn.execute("UPDATE applications SET resume_id = NULL WHERE resume_id = ?", (resume_id,))
    conn.execute("DELETE FROM resumes WHERE id = ? AND user_id = ?", (resume_id, user_id))
    conn.commit()


def erase_user_data(conn: sqlite3.Connection, user_id: int) -> None:
    """Удаление аккаунта: резюме пользователя, просмотры его резюме и его собственные просмотры чужих."""
    conn.execute("DELETE FROM resume_views WHERE employer_user_id = ?", (user_id,))
    ids = [r[0] for r in conn.execute("SELECT id FROM resumes WHERE user_id = ?", (user_id,))]
    for rid in ids:
        conn.execute("DELETE FROM resume_views WHERE resume_id = ?", (rid,))
        conn.execute("UPDATE applications SET resume_id = NULL WHERE resume_id = ?", (rid,))
    conn.execute("DELETE FROM resumes WHERE user_id = ?", (user_id,))
    conn.commit()


# ------------------------------------------------------------------ резюме в отклике

def apply_options(conn: sqlite3.Connection, user_id: int, now: datetime) -> list[dict[str, Any]]:
    """Резюме, которые можно приложить к отклику: не скрытые и не скрытые модератором. Свежие первыми."""
    rows = _rows(
        conn,
        "SELECT * FROM resumes WHERE user_id = ? AND visibility <> 'hidden' AND admin_hidden = 0 AND consent_at IS NOT NULL"
        " AND consent_withdrawn_at IS NULL ORDER BY updated_at DESC, id DESC",
        (user_id,),
    )
    return [present(decode(r), now) for r in rows]


def check_for_apply(conn: sqlite3.Connection, resume_id: int, user_id: int) -> int:
    """Номер резюме, которое можно приложить к отклику этого пользователя, иначе ResumeError."""
    row = conn.execute(
        "SELECT id FROM resumes WHERE id = ? AND user_id = ? AND visibility <> 'hidden' AND admin_hidden = 0"
        " AND consent_at IS NOT NULL AND consent_withdrawn_at IS NULL",
        (resume_id, user_id),
    ).fetchone()
    if row is None:
        raise ResumeError("bad_resume", "Это резюме нельзя приложить: оно скрыто или удалено. Выберите другое.")
    return int(row[0])


def application_resume(conn: sqlite3.Connection, application_id: int, viewer: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """Резюме, приложенное к отклику, для краткого блока на странице отклика.

    Работодателю: только пока резюме доступно ему (не скрыто, не удалено). Соискателю: его собственное резюме.
    """
    row = conn.execute(
        "SELECT a.resume_id, a.user_id FROM applications a WHERE a.id = ? AND a.resume_id IS NOT NULL", (application_id,)
    ).fetchone()
    if row is None:
        return None
    if viewer["role"] == "employer":
        found = get_for_employer(conn, int(row["resume_id"]), int(viewer["id"]), now)
        return found
    if int(row["user_id"]) != int(viewer["id"]):
        return None
    own = get_own(conn, int(row["resume_id"]), int(viewer["id"]))
    return present(own, now) if own else None


# ------------------------------------------------------------------ учёт просмотров

def view_stats(conn: sqlite3.Connection, ids: list[int]) -> dict[int, dict[str, int]]:
    """Сколько разных работодателей смотрели резюме и сколько открыли контакты."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out: dict[int, dict[str, int]] = {i: {"views": 0, "contacts": 0} for i in ids}
    for rid, kind, n in conn.execute(
        f"SELECT resume_id, kind, COUNT(DISTINCT employer_user_id) FROM resume_views WHERE resume_id IN ({marks})"
        " GROUP BY resume_id, kind", ids,
    ):
        out[int(rid)]["views" if kind == "view" else "contacts"] = int(n)
    return out


def record_view(conn: sqlite3.Connection, resume_id: int, employer_id: int, now: datetime) -> None:
    """Просмотр карточки; повторные просмотры тем же работодателем в течение суток считаются одним."""
    since = iso(now - timedelta(days=1))
    if conn.execute(
        "SELECT 1 FROM resume_views WHERE resume_id = ? AND employer_user_id = ? AND kind = 'view' AND at >= ?",
        (resume_id, employer_id, since),
    ).fetchone():
        return
    conn.execute("INSERT INTO resume_views(resume_id, employer_user_id, kind, at) VALUES (?,?,?,?)",
                 (resume_id, employer_id, "view", iso(now)))
    conn.commit()


def contact_opened(conn: sqlite3.Connection, resume_id: int, employer_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM resume_views WHERE resume_id = ? AND employer_user_id = ? AND kind = 'contact'",
        (resume_id, employer_id),
    ).fetchone() is not None


def open_contact(conn: sqlite3.Connection, resume_id: int, employer_id: int, now: datetime) -> bool:
    """Открыть контакты по кнопке. True, если открыты сейчас впервые. Повтор бесплатен, суточный предел защищает от выгрузки."""
    if contact_opened(conn, resume_id, employer_id):
        return False
    since = iso(now - timedelta(days=1))
    used = conn.execute(
        "SELECT COUNT(*) FROM resume_views WHERE employer_user_id = ? AND kind = 'contact' AND at >= ?",
        (employer_id, since),
    ).fetchone()[0]
    if used >= rules.MAX_CONTACTS_PER_DAY:
        raise ResumeError(
            "contact_limit",
            f"За сутки можно открыть контакты не больше {rules.MAX_CONTACTS_PER_DAY} раз. Попробуйте завтра или напишите кандидату приглашение.",
        )
    conn.execute("INSERT INTO resume_views(resume_id, employer_user_id, kind, at) VALUES (?,?,?,?)",
                 (resume_id, employer_id, "contact", iso(now)))
    conn.commit()
    return True


# ------------------------------------------------------------------ работодатель: поиск и карточка

def _fresh_after(now: datetime) -> str:
    return iso(now - timedelta(days=rules.STALE_DAYS))


# Резюме, которое видно в поиске работодателю :emp: открыто всем, согласие действует, владелец активен и не заблокировал его
_SEARCHABLE = (
    "r.visibility = 'public' AND r.admin_hidden = 0 AND r.consent_at IS NOT NULL AND r.consent_withdrawn_at IS NULL"
    " AND r.updated_at >= :fresh AND u.status = 'active'"
)
_NOT_BLOCKED = (
    "NOT EXISTS (SELECT 1 FROM user_blocks b WHERE b.blocker_user_id = r.user_id AND b.blocked_user_id = :emp)"
)
# Резюме из отклика на вакансию этого работодателя (видимость "только по отклику" или "всем")
_VIA_APPLICATION = (
    "r.visibility IN ('public','open') AND r.admin_hidden = 0 AND r.consent_at IS NOT NULL AND r.consent_withdrawn_at IS NULL"
    " AND u.status = 'active' AND EXISTS (SELECT 1 FROM applications a JOIN vacancies v ON v.id = a.vacancy_id"
    " WHERE a.resume_id = r.id AND v.owner_user_id = :emp AND a.withdrawn_at IS NULL)"
)

SORTS = {"fresh": ("Сначала свежие", "r.updated_at DESC, r.id DESC"),
         "exp": ("Сначала с большим опытом", "COALESCE(r.experience_years, -1) DESC, r.updated_at DESC, r.id DESC")}


def parse_filters(params: Any) -> dict[str, Any]:
    """Фильтры поиска из строки запроса: всё проверено, мусор отбрасывается."""
    def get(name: str, limit: int = 60) -> str:
        return " ".join(str(params.get(name) or "").split())[:limit]

    pay_to = "".join(ch for ch in get("pay_to", 9) if ch.isdigit())
    period = get("period", 20)
    employment = get("employment", 20)
    work_format = get("format", 20)
    exp = get("exp", 2)
    sort = get("sort", 10)
    return {
        "q": get("q", 80), "district": get("district"),
        "pay_to": int(pay_to) if pay_to and 0 < int(pay_to) <= rules.MAX_PAY else None,
        "period": period if period in rules.PAY_PERIOD_LABELS else "месяц",
        "exp": exp if exp in rules.EXPERIENCE_FILTERS else "",
        "employment": employment if employment in rules.EMPLOYMENT_LABELS else "",
        "format": work_format if work_format in rules.WORK_FORMAT_LABELS else "",
        "sort": sort if sort in SORTS else "fresh",
    }


def search(conn: sqlite3.Connection, employer_id: int, filters: dict[str, Any], now: datetime, page: int = 1) -> dict[str, Any]:
    where = [_SEARCHABLE, _NOT_BLOCKED]
    args: dict[str, Any] = {"emp": employer_id, "fresh": _fresh_after(now)}
    if filters["q"]:
        args["q"] = "%" + norm(filters["q"]).replace("%", "").replace("_", "") + "%"
        where.append("(r.profession LIKE :q OR r.skills_key LIKE :q)")
    if filters["district"]:
        args["district"] = "%" + norm(filters["district"]).replace("%", "").replace("_", "") + "%"
        where.append("r.district_key LIKE :district")
    if filters["pay_to"]:
        args["pay_to"] = filters["pay_to"]
        args["period"] = filters["period"]
        where.append("(r.pay_min IS NULL OR (r.pay_period = :period AND r.pay_min <= :pay_to))")
    if filters["exp"] == "0":
        where.append("r.experience_years = 0")
    elif filters["exp"]:
        args["exp"] = int(filters["exp"])
        where.append("r.experience_years >= :exp")
    if filters["employment"]:
        args["employment"] = f'%"{filters["employment"]}"%'
        where.append("r.employments LIKE :employment")
    if filters["format"]:
        args["format"] = f'%"{filters["format"]}"%'
        if filters["format"] == "удалённо":
            where.append("(r.work_formats LIKE :format OR r.remote = 1)")
        else:
            where.append("r.work_formats LIKE :format")
    clause = " AND ".join(where)
    base = "FROM resumes r JOIN users u ON u.id = r.user_id"
    total = int(conn.execute(f"SELECT COUNT(*) {base} WHERE {clause}", args).fetchone()[0])
    meta = _page(total, page, rules.PER_PAGE)
    rows = _rows(
        conn,
        f"SELECT r.*, u.name AS owner_name {base} WHERE {clause} ORDER BY {SORTS[filters['sort']][1]}"
        " LIMIT :limit OFFSET :offset",
        {**args, "limit": rules.PER_PAGE, "offset": meta["offset"]},
    )
    items = []
    for row in rows:
        r = present(decode(row), now)
        r["display_name"] = rules.short_name(row["owner_name"])
        items.append(r)
    return {**meta, "items": items}


def total_searchable(conn: sqlite3.Connection, employer_id: int, now: datetime) -> int:
    return int(conn.execute(
        f"SELECT COUNT(*) FROM resumes r JOIN users u ON u.id = r.user_id WHERE {_SEARCHABLE} AND {_NOT_BLOCKED}",
        {"emp": employer_id, "fresh": _fresh_after(now)},
    ).fetchone()[0])


def get_for_employer(conn: sqlite3.Connection, resume_id: int, employer_id: int, now: datetime) -> dict[str, Any] | None:
    """Резюме, доступное этому работодателю (из поиска или из его отклика), иначе None. Контакты здесь не раскрываются."""
    rows = _rows(
        conn,
        "SELECT r.*, u.name AS owner_name, u.id AS owner_id,"
        f" CASE WHEN {_SEARCHABLE} THEN 1 ELSE 0 END AS in_search,"
        f" CASE WHEN {_VIA_APPLICATION} THEN 1 ELSE 0 END AS via_application"
        " FROM resumes r JOIN users u ON u.id = r.user_id WHERE r.id = :id",
        {"id": resume_id, "emp": employer_id, "fresh": _fresh_after(now)},
    )
    if not rows:
        return None
    row = rows[0]
    blocked = conn.execute(
        "SELECT 1 FROM user_blocks WHERE blocker_user_id = ? AND blocked_user_id = ?", (row["owner_id"], employer_id)
    ).fetchone()
    if blocked or not (row["in_search"] or row["via_application"]):
        return None
    r = present(decode(row), now)
    r["display_name"] = rules.short_name(row["owner_name"])
    r["full_name"] = row["owner_name"]
    r["in_search"] = bool(row["in_search"])
    r["via_application"] = bool(row["via_application"])
    return r


def employer_vacancies(conn: sqlite3.Connection, employer_id: int) -> list[dict[str, Any]]:
    """Опубликованные вакансии работодателя: на них можно пригласить."""
    return _rows(
        conn,
        f"SELECT id, profession, city FROM vacancies WHERE owner_user_id = ? AND origin = 'direct' AND {VISIBLE_SQL}"
        " ORDER BY id DESC LIMIT 50",
        (employer_id,),
    )


def default_invite_text(resume: dict[str, Any], vacancy: dict[str, Any] | None) -> str:
    name = (resume.get("display_name") or "").split(" ")[0]
    title = rules.ucfirst(vacancy["profession"]) if vacancy else "нашу вакансию"
    return (
        f"Здравствуйте{', ' + name if name and name != 'Соискатель' else ''}! Ваше резюме нам подходит. "
        f"Приглашаем вас обсудить вакансию «{title}». Когда вам удобно поговорить?"
    )


def existing_thread(conn: sqlite3.Connection, employer_id: int, seeker_id: int, vacancy_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM conversations WHERE employer_user_id = ? AND seeker_user_id = ? AND vacancy_id = ?",
        (employer_id, seeker_id, vacancy_id),
    ).fetchone()
    return int(row[0]) if row else None


def invite(
    conn: sqlite3.Connection, *, resume_id: int, employer: dict[str, Any], vacancy_id: int, text: str, now: datetime
) -> dict[str, int]:
    """Приглашение: переписка по вакансии работодателя и первое сообщение. Возвращает номера переписки и сообщения."""
    from rabota.messaging.service import blocked_pair

    resume = get_for_employer(conn, resume_id, int(employer["id"]), now)
    if resume is None:
        raise NotFound()
    vacancy = next((v for v in employer_vacancies(conn, int(employer["id"])) if v["id"] == vacancy_id), None)
    if vacancy is None:
        raise ResumeError("bad_vacancy", "Выберите одну из своих опубликованных вакансий.")
    seeker_id = int(resume["owner_id"])
    if blocked_pair(conn, int(employer["id"]), seeker_id)[1]:
        raise NotFound()
    try:
        body = msg_rules.check_text(text, rules.MAX_INVITE_TEXT, what="Приглашение")
    except MessagingError as exc:
        raise ResumeError("bad_text", exc.text) from None
    thread = existing_thread(conn, int(employer["id"]), seeker_id, vacancy_id)
    if thread is not None:
        raise ResumeError("exists", "Вы уже переписываетесь с этим кандидатом по этой вакансии.", conversation_id=thread)
    since = iso(now - timedelta(days=1))
    sent = conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE started_by = ? AND application_id IS NULL AND created_at >= ?",
        (employer["id"], since),
    ).fetchone()[0]
    if sent >= rules.MAX_INVITES_PER_DAY:
        raise ResumeError("invite_limit", f"За сутки можно отправить не больше {rules.MAX_INVITES_PER_DAY} приглашений.")
    stamp = iso(now)
    cur = conn.execute(
        "INSERT INTO conversations(employer_user_id, seeker_user_id, vacancy_id, application_id, started_by, created_at,"
        " last_message_at, title) VALUES (?,?,?,?,?,?,?,?)",
        (employer["id"], seeker_id, vacancy_id, None, employer["id"], stamp, stamp, (vacancy["profession"] or "")[:120]),
    )
    conversation_id = int(cur.lastrowid or 0)
    message = conn.execute(
        "INSERT INTO messages(conversation_id, sender_user_id, body, created_at) VALUES (?,?,?,?)",
        (conversation_id, employer["id"], body, stamp),
    )
    conn.commit()
    return {"conversation_id": conversation_id, "message_id": int(message.lastrowid or 0)}
