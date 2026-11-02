"""Раздел админки «Работодатели»: список компаний, карточка, отметка «проверенный работодатель», блокировка.

Проверенным работодателя делает администратор после проверки телефона или документов (`companies.verified_at`).
У проверенного вакансии без подозрительных признаков публикуются сразу (rabota.employers.automod), на странице
компании и в карточках вакансий виден значок. Блокировка закрывает аккаунт владельца, скрывает его вакансии и страницу
компании; разблокировка возвращает только те вакансии, которые скрыла сама блокировка.
Каждое действие пишется в журнал (`audit_log`, цель `company`).
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from rabota.accounts import service
from rabota.admin import audit, stoplist
from rabota.admin.common import Page, clamp_page, clean_line, iso
from rabota.employers import automod, metrics

PER_PAGE = 25
BLOCK_NOTE = "Работодатель заблокирован"

VERIFIED = {"yes": "Проверенные", "no": "Не проверенные"}
STATES = {"active": "Работают", "blocked": "Заблокированные"}
PERIODS = {"all": "Всё время", "7d": "Новые за 7 дней", "30d": "Новые за 30 дней"}
SORTS = {
    "created": ("Регистрация", "c.created_at"),
    "name": ("Название", "rfold(c.name)"),
    "vacancies": ("Вакансии", "vacancies_total"),
    "complaints": ("Жалобы", "complaints_open"),
}

FLASH = {
    "employer_verified": "Работодатель отмечен как проверенный.",
    "employer_unverified": "Отметка о проверке снята.",
    "employer_blocked": "Работодатель заблокирован: аккаунт закрыт, вакансии скрыты.",
    "employer_unblocked": "Блокировка снята, скрытые блокировкой вакансии возвращены.",
}

ACTION_LABELS = {
    "employer.verify": "Отмечен как проверенный",
    "employer.unverify": "Отметка о проверке снята",
    "employer.block": "Заблокирован",
    "employer.unblock": "Блокировка снята",
}


@dataclass
class Filters:
    q: str = ""
    verified: str = ""
    state: str = ""
    period: str = "all"
    sort: str = "created"
    dir: str = "desc"
    page: int = 1

    @property
    def active(self) -> bool:
        return bool(self.q or self.verified or self.state or self.period != "all")

    def params(self, **override: Any) -> dict[str, str]:
        base = {
            "q": self.q, "verified": self.verified, "state": self.state,
            "period": self.period if self.period != "all" else "", "sort": self.sort if self.sort != "created" else "",
            "dir": self.dir if self.dir != "desc" else "", "page": str(self.page) if self.page > 1 else "",
        }
        base.update({k: str(v) for k, v in override.items()})
        return {k: v for k, v in base.items() if v}

    def query(self, **override: Any) -> str:
        from urllib.parse import urlencode

        return urlencode(self.params(**override))

    def query_without_page(self, **override: Any) -> str:
        return self.query(page="", **override)


def parse(params: Mapping[str, str]) -> Filters:
    def get(name: str) -> str:
        return str(params.get(name, "") or "")

    try:
        page = min(max(int(get("page") or 1), 1), 100_000)
    except ValueError:
        page = 1
    return Filters(
        q=clean_line(get("q"), 80), verified=get("verified") if get("verified") in VERIFIED else "",
        state=get("state") if get("state") in STATES else "", period=get("period") if get("period") in PERIODS else "all",
        sort=get("sort") if get("sort") in SORTS else "created", dir="asc" if get("dir") == "asc" else "desc", page=page,
    )


def _where(f: Filters, now: datetime) -> tuple[str, list[Any]]:
    clauses = ["c.merged_into IS NULL"]  # слитые карточки открываются редиректом и в списке не нужны
    args: list[Any] = []
    if f.q:
        if f.q.isdigit():
            clauses.append("c.id = ?")
            args.append(int(f.q))
        else:
            like = f"%{stoplist.fold(f.q)}%"
            clauses.append("(rfold(c.name) LIKE ? OR rfold(u.email) LIKE ? OR rfold(c.site) LIKE ? OR c.phone LIKE ?)")
            args += [like, like, like, f"%{f.q}%"]
    if f.verified == "yes":
        clauses.append("c.verified_at IS NOT NULL")
    elif f.verified == "no":
        clauses.append("c.verified_at IS NULL")
    if f.state == "blocked":
        clauses.append("c.blocked_at IS NOT NULL")
    elif f.state == "active":
        clauses.append("c.blocked_at IS NULL")
    if f.period in ("7d", "30d"):
        days = 7 if f.period == "7d" else 30
        clauses.append("c.created_at >= ?")
        args.append((now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"))
    return " AND ".join(clauses), args


_SELECT = (
    "SELECT c.*, u.email AS owner_email, u.name AS owner_name, u.status AS owner_status,"
    " (SELECT COUNT(*) FROM vacancies v WHERE v.company_id = c.id AND v.kind = 'vacancy') AS vacancies_total,"
    " (SELECT COUNT(*) FROM vacancies v WHERE v.company_id = c.id AND v.moderation_status = 'pending') AS vacancies_pending,"
    " (SELECT COUNT(*) FROM complaints k JOIN vacancies v ON v.id = k.vacancy_id"
    "   WHERE v.company_id = c.id AND k.status = 'new') AS complaints_open"
    " FROM companies c LEFT JOIN users u ON u.id = c.owner_user_id"
)


def _fold_function(conn: sqlite3.Connection) -> None:
    conn.create_function("rfold", 1, lambda s: stoplist.fold(s) if isinstance(s, str) else s, deterministic=True)


def listing(conn: sqlite3.Connection, f: Filters, now: datetime, per_page: int = PER_PAGE) -> Page:
    _fold_function(conn)
    where, args = _where(f, now)
    total = int(conn.execute(
        f"SELECT COUNT(*) FROM companies c LEFT JOIN users u ON u.id = c.owner_user_id WHERE {where}", args).fetchone()[0])
    p = clamp_page(f.page, total, per_page)
    f.page = p
    column = SORTS[f.sort][1]
    direction = "ASC" if f.dir == "asc" else "DESC"
    rows = conn.execute(
        f"{_SELECT} WHERE {where} ORDER BY {column} {direction}, c.id {direction} LIMIT ? OFFSET ?",
        [*args, per_page, (p - 1) * per_page],
    ).fetchall()
    return Page([dict(r) for r in rows], total, p, per_page)


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    def one(where: str) -> int:
        return int(conn.execute(f"SELECT COUNT(*) FROM companies c WHERE c.merged_into IS NULL AND {where}").fetchone()[0])

    return {
        "all": one("1"), "verified": one("c.verified_at IS NOT NULL"), "unverified": one("c.verified_at IS NULL"),
        "blocked": one("c.blocked_at IS NOT NULL"),
        "pending": int(conn.execute(
            "SELECT COUNT(DISTINCT company_id) FROM vacancies WHERE company_id IS NOT NULL AND moderation_status = 'pending'"
        ).fetchone()[0]),
    }


def get(conn: sqlite3.Connection, company_id: int) -> dict[str, Any] | None:
    _fold_function(conn)
    row = conn.execute(f"{_SELECT} WHERE c.id = ?", (company_id,)).fetchone()
    return dict(row) if row else None


def card(conn: sqlite3.Connection, company_id: int, now: datetime) -> dict[str, Any] | None:
    """Всё для карточки: компания, вакансии, показатели, жалобы, журнал действий."""
    company = get(conn, company_id)
    if company is None:
        return None
    vacancies = [dict(r) for r in conn.execute(
        "SELECT id, profession, status, moderation_status, moderation_note, created_at, pay_min, pay_max, pay_period,"
        " automod_flags FROM vacancies WHERE company_id = ? AND kind = 'vacancy' ORDER BY id DESC LIMIT 50",
        (company_id,),
    )]
    for v in vacancies:
        v["flags"] = automod.flag_labels(v.get("automod_flags"))
    complaints = [dict(r) for r in conn.execute(
        "SELECT k.id, k.vacancy_id, k.reason, k.status, k.created_at FROM complaints k JOIN vacancies v ON v.id = k.vacancy_id"
        " WHERE v.company_id = ? ORDER BY k.id DESC LIMIT 20", (company_id,))]
    journal = [dict(r) for r in conn.execute(
        "SELECT * FROM audit_log WHERE target_type = 'company' AND target_id = ? ORDER BY id DESC LIMIT 30", (str(company_id),))]
    for row in journal:
        row["label"] = ACTION_LABELS.get(row["action"], row["action"])
    stats = metrics.employer_stats(conn, company_id, now)
    return {"company": company, "vacancies": vacancies, "complaints": complaints, "journal": journal, "stats": stats}


# ------------------------------------------------------------------ действия

def _need(conn: sqlite3.Connection, company_id: int) -> dict[str, Any]:
    company = get(conn, company_id)
    if company is None:
        raise LookupError("Компания не найдена.")
    return company


def set_verified(
    conn: sqlite3.Connection, company_id: int, actor: dict[str, Any] | None, note: str, ip: str, now: datetime
) -> bool:
    """Отметить проверенным. False, если уже отмечена. Заблокированную компанию проверять нельзя."""
    company = _need(conn, company_id)
    if company["blocked_at"]:
        raise ValueError("Компания заблокирована: сначала снимите блокировку.")
    if company["verified_at"]:
        return False
    note = clean_line(note, 300)
    conn.execute(
        "UPDATE companies SET verified = 1, verified_at = ?, verified_by = ?, verify_note = ? WHERE id = ?",
        (iso(now), (actor or {}).get("id"), note or None, company_id),
    )
    conn.commit()
    audit.log(conn, actor, "employer.verify", "company", company_id, {"note": note}, ip=ip, now=now)
    return True


def clear_verified(
    conn: sqlite3.Connection, company_id: int, actor: dict[str, Any] | None, note: str, ip: str, now: datetime
) -> bool:
    company = _need(conn, company_id)
    if not company["verified_at"]:
        return False
    note = clean_line(note, 300)
    conn.execute(
        "UPDATE companies SET verified = 0, verified_at = NULL, verified_by = NULL, verify_note = ? WHERE id = ?",
        (note or None, company_id),
    )
    conn.commit()
    audit.log(conn, actor, "employer.unverify", "company", company_id, {"note": note}, ip=ip, now=now)
    return True


def block(
    conn: sqlite3.Connection, company_id: int, actor: dict[str, Any] | None, reason: str, ip: str, now: datetime
) -> bool:
    """Заблокировать: причина обязательна, аккаунт закрыт, вакансии скрыты с пометкой, проверка снята."""
    company = _need(conn, company_id)
    reason = clean_line(reason, 300)
    if not reason:
        raise ValueError("Укажите причину блокировки: она сохранится в журнале.")
    if company["blocked_at"]:
        return False
    note = f"{BLOCK_NOTE}: {reason}"[:300]
    conn.execute(
        "UPDATE companies SET blocked_at = ?, blocked_note = ?, verified = 0, verified_at = NULL, verified_by = NULL"
        " WHERE id = ?",
        (iso(now), reason, company_id),
    )
    hidden = conn.execute(
        "UPDATE vacancies SET status = 'hidden', moderation_note = ? WHERE company_id = ? AND status = 'active'",
        (note, company_id),
    ).rowcount
    conn.commit()
    if company.get("owner_user_id"):
        service.set_blocked(conn, int(company["owner_user_id"]), True)
    audit.log(conn, actor, "employer.block", "company", company_id, {"reason": reason, "hidden": hidden}, ip=ip, now=now)
    return True


def unblock(conn: sqlite3.Connection, company_id: int, actor: dict[str, Any] | None, ip: str, now: datetime) -> bool:
    company = _need(conn, company_id)
    if not company["blocked_at"]:
        return False
    conn.execute("UPDATE companies SET blocked_at = NULL, blocked_note = NULL WHERE id = ?", (company_id,))
    restored = conn.execute(
        "UPDATE vacancies SET status = 'active', moderation_note = NULL WHERE company_id = ? AND status = 'hidden'"
        " AND moderation_status = 'approved' AND moderation_note LIKE ?",
        (company_id, BLOCK_NOTE + "%"),
    ).rowcount
    conn.commit()
    if company.get("owner_user_id"):
        service.set_blocked(conn, int(company["owner_user_id"]), False)
    audit.log(conn, actor, "employer.unblock", "company", company_id, {"restored": restored}, ip=ip, now=now)
    return True


def details_json(value: str) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
