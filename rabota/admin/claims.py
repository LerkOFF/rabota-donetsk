"""Раздел админки «Заявки на карточки»: список заявок «Это моя компания», карточка заявки.

Решения (подтвердить, отклонить, запросить пояснения) и их правила: rabota.employers_auto.claims. Слияние дублей и
компании без вакансий: rabota.employers_auto.merge. Каждое действие пишется в журнал (`audit_log`, цель `claim`).
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from rabota.admin.common import Page, clamp_page, clean_line
from rabota.employers_auto import cards, claims
from rabota.employers_auto.claims import STATUSES

PER_PAGE = 25
VIEWS = {"open": "Ждут решения", "dispute": "Споры", "confirmed": "Подтверждены", "rejected": "Отклонены", "all": "Все"}
METHODS = claims.METHODS
PERIODS = {"all": "Всё время", "7d": "За 7 дней", "30d": "За 30 дней"}
SORTS = {"created": ("Подана", "k.created_at"), "company": ("Компания", "rfold(c.name)"), "updated": ("Обновлена", "k.updated_at")}

FLASH = {
    "claim_approved": "Заявка подтверждена: компания закреплена за заявителем, письмо отправлено.",
    "claim_rejected": "Заявка отклонена, заявителю отправлено письмо.",
    "claim_docs": "Пояснения запрошены, заявителю отправлено письмо.",
    "merged": "Карточки объединены: вакансии перенесены в главную.",
    "empty_deleted": "Пустые автокарточки удалены.",
    "nothing": "Ничего не изменилось.",
}
ACTION_LABELS = {
    "claim.submit": ("Заявка подана", "handshake"),
    "claim.approve": ("Заявка подтверждена", "seal-check"),
    "claim.reject": ("Заявка отклонена", "x-circle"),
    "claim.docs": ("Запрошены пояснения", "info"),
    "company.merge": ("Карточки объединены", "copy"),
    "company.delete_empty": ("Удалены пустые автокарточки", "trash"),
}


@dataclass
class Filters:
    q: str = ""
    view: str = "open"
    method: str = ""
    period: str = "all"
    sort: str = "created"
    dir: str = "desc"
    page: int = 1

    @property
    def active(self) -> bool:
        return bool(self.q or self.method or self.period != "all")

    def params(self, **override: Any) -> dict[str, str]:
        base = {
            "q": self.q, "view": self.view if self.view != "open" else "", "method": self.method,
            "period": self.period if self.period != "all" else "", "sort": self.sort if self.sort != "created" else "",
            "dir": self.dir if self.dir != "desc" else "", "page": str(self.page) if self.page > 1 else "",
        }
        base.update({k: str(v) for k, v in override.items()})
        return {k: v for k, v in base.items() if v}

    def query(self, **override: Any) -> str:
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
        q=clean_line(get("q"), 80), view=get("view") if get("view") in VIEWS else "open",
        method=get("method") if get("method") in METHODS else "", period=get("period") if get("period") in PERIODS else "all",
        sort=get("sort") if get("sort") in SORTS else "created", dir="asc" if get("dir") == "asc" else "desc", page=page,
    )


def _fold(conn: sqlite3.Connection) -> None:
    from rabota.admin import stoplist

    conn.create_function("rfold", 1, lambda s: stoplist.fold(s) if isinstance(s, str) else s, deterministic=True)


def _where(f: Filters, now: datetime) -> tuple[str, list[Any]]:
    clauses = ["1"]
    args: list[Any] = []
    if f.view == "open":
        clauses.append("k.status IN ('sent','review')")
    elif f.view == "dispute":
        clauses.append("k.dispute = 1 AND k.status IN ('sent','review')")
    elif f.view in ("confirmed", "rejected"):
        clauses.append("k.status = ?")
        args.append(f.view)
    if f.q:
        if f.q.isdigit():
            clauses.append("(k.id = ? OR c.id = ?)")
            args += [int(f.q), int(f.q)]
        else:
            from rabota.admin import stoplist

            like = f"%{stoplist.fold(f.q)}%"
            clauses.append("(rfold(c.name) LIKE ? OR rfold(u.email) LIKE ? OR rfold(k.contact) LIKE ?)")
            args += [like, like, like]
    if f.method:
        clauses.append("k.method = ?")
        args.append(f.method)
    if f.period in ("7d", "30d"):
        clauses.append("k.created_at >= ?")
        args.append((now - timedelta(days=7 if f.period == "7d" else 30)).replace(microsecond=0).isoformat())
    return " AND ".join(clauses), args


_SELECT = (
    "SELECT k.*, c.name AS company_name, c.city AS company_city, c.owner_user_id AS company_owner, u.email AS user_email, "
    "u.name AS user_name, (SELECT COUNT(*) FROM vacancies v WHERE v.company_id = c.id) AS vacancies_total "
    "FROM company_claims k JOIN companies c ON c.id = k.company_id JOIN users u ON u.id = k.user_id"
)


def listing(conn: sqlite3.Connection, f: Filters, now: datetime, per_page: int = PER_PAGE) -> Page:
    _fold(conn)
    where, args = _where(f, now)
    total = int(conn.execute(
        f"SELECT COUNT(*) FROM company_claims k JOIN companies c ON c.id = k.company_id JOIN users u ON u.id = k.user_id WHERE {where}",
        args).fetchone()[0])
    p = clamp_page(f.page, total, per_page)
    f.page = p
    direction = "ASC" if f.dir == "asc" else "DESC"
    rows = conn.execute(
        f"{_SELECT} WHERE {where} ORDER BY {SORTS[f.sort][1]} {direction}, k.id {direction} LIMIT ? OFFSET ?",
        [*args, per_page, (p - 1) * per_page]).fetchall()
    return Page([dict(r) for r in rows], total, p, per_page)


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    def one(where: str) -> int:
        return int(conn.execute(f"SELECT COUNT(*) FROM company_claims k WHERE {where}").fetchone()[0])

    return {
        "open": one("k.status IN ('sent','review')"), "review": one("k.status = 'review'"), "sent": one("k.status = 'sent'"),
        "dispute": one("k.dispute = 1 AND k.status IN ('sent','review')"), "confirmed": one("k.status = 'confirmed'"),
        "rejected": one("k.status = 'rejected'"), "all": one("1"),
    }


def nav_count(conn: sqlite3.Connection) -> int:
    """Заявки, которые ждут решения администратора (код введён или проверка по телефону)."""
    try:
        return int(conn.execute("SELECT COUNT(*) FROM company_claims WHERE status = 'review'").fetchone()[0])
    except sqlite3.Error:
        return 0


def get(conn: sqlite3.Connection, claim_id: int) -> dict[str, Any] | None:
    _fold(conn)
    row = conn.execute(f"{_SELECT} WHERE k.id = ?", (claim_id,)).fetchone()
    return dict(row) if row else None


def card(conn: sqlite3.Connection, claim_id: int) -> dict[str, Any] | None:
    """Заявка со всем, что нужно для решения: компания, заявитель, соперники, домены и телефоны, вакансии, журнал."""
    claim = get(conn, claim_id)
    if claim is None:
        return None
    company = cards.get(conn, int(claim["company_id"])) or {}
    user = conn.execute("SELECT id, email, name, phone, role, created_at, email_verified_at FROM users WHERE id = ?",
                        (claim["user_id"],)).fetchone()
    owner = None
    if company.get("owner_user_id"):
        row = conn.execute("SELECT id, email, name FROM users WHERE id = ?", (company["owner_user_id"],)).fetchone()
        owner = dict(row) if row else None
    others = [dict(r) for r in conn.execute(
        "SELECT k.id, k.status, k.method, k.contact, k.created_at, u.email AS user_email FROM company_claims k "
        "JOIN users u ON u.id = k.user_id WHERE k.company_id = ? AND k.id != ? AND k.status IN ('sent','review') ORDER BY k.id",
        (claim["company_id"], claim_id))]
    own_company = conn.execute(
        "SELECT id, name FROM companies WHERE owner_user_id = ? AND id != ? AND merged_into IS NULL ORDER BY id LIMIT 1",
        (claim["user_id"], claim["company_id"])).fetchone()
    vacancies = [dict(r) for r in conn.execute(
        "SELECT id, profession, status, source_type, published_at FROM vacancies WHERE company_id = ? AND kind = 'vacancy' "
        "ORDER BY id DESC LIMIT 12", (claim["company_id"],))]
    journal = [dict(r) for r in conn.execute(
        "SELECT * FROM audit_log WHERE target_type = 'claim' AND target_id = ? ORDER BY id DESC LIMIT 30", (str(claim_id),))]
    for row in journal:
        label, icon = ACTION_LABELS.get(row["action"], (row["action"], "info"))
        row["label"], row["icon"] = label, icon
    return {
        "claim": claim, "company": company, "user": dict(user) if user else {}, "owner": owner, "others": others,
        "own_company": dict(own_company) if own_company else None, "vacancies": vacancies, "journal": journal,
        "counts": cards.counts(conn, int(claim["company_id"])) if company else {},
        "domains": sorted(claims.company_domains(conn, company)) if company else [],
        "phones": sorted(claims.company_phones(conn, company)) if company else [],
    }


def details_json(value: str) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


__all__ = ["STATUSES", "Filters", "card", "counts", "listing", "parse"]
