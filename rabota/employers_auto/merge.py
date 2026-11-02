"""Дубли карточек: поиск похожих и слияние. Главная карточка остаётся, вторая получает `merged_into` и
открывается редиректом на главную; все вакансии, заявки и данные переносятся."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

from rabota.admin import audit
from rabota.admin.common import Page, clamp_page, iso, now_utc
from rabota.employers_auto import cards, names

FILL = ("description", "site", "phone", "contact_email", "city")
BUCKET_LIMIT = 150


class MergeError(Exception):
    def __init__(self, code: str, text: str):
        super().__init__(text)
        self.code = code
        self.text = text


def merge_companies(
    conn: sqlite3.Connection, main_id: int, dup_id: int, actor: dict[str, Any] | None, ip: str = "",
    now: datetime | None = None, *, commit: bool = True,
) -> dict[str, Any]:
    """Влить dup в main. Возвращает {"moved": число перенесённых вакансий}."""
    if main_id == dup_id:
        raise MergeError("same", "Нельзя слить карточку саму с собой.")
    main = conn.execute("SELECT * FROM companies WHERE id = ?", (main_id,)).fetchone()
    dup = conn.execute("SELECT * FROM companies WHERE id = ?", (dup_id,)).fetchone()
    if main is None or dup is None:
        raise MergeError("none", "Одной из карточек нет.")
    main, dup = dict(main), dict(dup)
    if main.get("merged_into") or dup.get("merged_into"):
        raise MergeError("merged", "Карточка уже слита с другой.")
    if main.get("owner_user_id") and dup.get("owner_user_id") and main["owner_user_id"] != dup["owner_user_id"]:
        raise MergeError("owners", "У обеих карточек разные владельцы: сначала разберите заявки и спор.")
    moment = iso(now or now_utc())
    owner = main.get("owner_user_id") or dup.get("owner_user_id")
    moved = conn.execute("UPDATE vacancies SET company_id = ? WHERE company_id = ?", (main_id, dup_id)).rowcount
    if owner:
        conn.execute(
            "UPDATE vacancies SET owner_user_id = COALESCE(owner_user_id, ?) WHERE company_id = ? AND origin != 'direct'", (owner, main_id))
    sets, args = [], []
    for col in FILL:
        if not (main.get(col) or "").strip() and (dup.get(col) or "").strip():
            sets.append(f"{col} = ?")
            args.append(dup[col])
    if not main.get("owner_user_id") and dup.get("owner_user_id"):
        sets += ["owner_user_id = ?", "claimed_at = COALESCE(?, claimed_at)", "verified = ?", "verified_at = COALESCE(verified_at, ?)",
                 "verified_by = COALESCE(verified_by, ?)", "verify_note = COALESCE(verify_note, ?)"]
        args += [dup["owner_user_id"], dup.get("claimed_at"), dup.get("verified") or 0, dup.get("verified_at"), dup.get("verified_by"),
                 dup.get("verify_note")]
    if not main.get("source_ref") and dup.get("source_ref") and main.get("origin") == "auto":
        pass  # ссылку у источника остаётся искать через слитую карточку (см. cards._follow)
    if sets:
        conn.execute(f"UPDATE companies SET {', '.join(sets)} WHERE id = ?", [*args, main_id])
    conn.execute("UPDATE company_claims SET company_id = ? WHERE company_id = ?", (main_id, dup_id))
    conn.execute(
        "UPDATE companies SET merged_into = ?, owner_user_id = NULL, verified = 0, verified_at = NULL, verified_by = NULL "
        "WHERE id = ?", (main_id, dup_id))
    cards.refresh_dates(conn, main_id)
    if commit:
        conn.commit()
    audit.log(conn, actor, "company.merge", "company", main_id, {"merged": dup_id, "moved": moved, "at": moment}, ip, now)
    return {"moved": moved}


def delete_empty(conn: sqlite3.Connection, ids: list[int], actor: dict[str, Any] | None, ip: str = "", now: datetime | None = None) -> int:
    """Удалить автокарточки, у которых нет ни вакансий (даже в архиве), ни владельца, ни заявок. Возвращает число удалённых."""
    removed = 0
    for cid in ids:
        row = conn.execute(
            "SELECT id FROM companies c WHERE id = ? AND origin = 'auto' AND owner_user_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM vacancies v WHERE v.company_id = c.id) "
            "AND NOT EXISTS (SELECT 1 FROM company_claims k WHERE k.company_id = c.id) "
            "AND NOT EXISTS (SELECT 1 FROM companies m WHERE m.merged_into = c.id)", (cid,)).fetchone()
        if row is None:
            continue
        conn.execute("DELETE FROM companies WHERE id = ?", (cid,))
        removed += 1
    conn.commit()
    if removed:
        audit.log(conn, actor, "company.delete_empty", "company", ",".join(map(str, ids))[:200], {"removed": removed}, ip, now)
    return removed


# ------------------------------------------------------------------ поиск дублей

def _loose(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


def find_duplicates(conn: sqlite3.Connection, limit: int = 60) -> list[dict[str, Any]]:
    """Пары карточек, похожих друг на друга: одинаковое название в разных городах, автокарточка и карточка работодателя,
    почти одинаковые названия. Слитые и заблокированные не участвуют."""
    rows = conn.execute(
        "SELECT c.id, c.name, c.norm_name, c.city, c.origin, c.owner_user_id, c.verified_at, "
        "(SELECT COUNT(*) FROM vacancies v WHERE v.company_id = c.id) AS vacancies "
        "FROM companies c WHERE c.merged_into IS NULL AND c.blocked_at IS NULL ORDER BY c.id"
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["key"] = _loose(d["norm_name"] or names.parse_name(d["name"]).norm)
        if len(d["key"]) >= 3:
            items.append(d)
    buckets: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        buckets.setdefault(it["key"][:4], []).append(it)
    pairs: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for group in buckets.values():
        if len(group) < 2 or len(group) > BUCKET_LIMIT:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                pair = (a["id"], b["id"])
                if pair in seen:
                    continue
                if a["key"] == b["key"]:
                    reason, score = "Одинаковое название", 1.0
                else:
                    score = SequenceMatcher(None, a["key"], b["key"]).ratio()
                    if score < 0.9:
                        continue
                    reason = "Похожее название"
                seen.add(pair)
                main, dup = (a, b) if _rank(a) <= _rank(b) else (b, a)
                pairs.append({"main": main, "dup": dup, "reason": reason, "score": round(score, 2),
                              "blocked": bool(main["owner_user_id"] and dup["owner_user_id"] and main["owner_user_id"] != dup["owner_user_id"])})
    pairs.sort(key=lambda p: (-p["score"], p["main"]["id"]))
    return pairs[:limit]


def _rank(c: dict[str, Any]) -> tuple[int, int, int]:
    """Какая карточка главная: с владельцем, проверенная, с большим числом вакансий, старшая."""
    return (0 if c["owner_user_id"] else 1, 0 if c["verified_at"] else 1, -int(c["vacancies"]) * 1000 + int(c["id"]))


# ------------------------------------------------------------------ компании без вакансий

EMPTY_VIEWS = {"none": "Нет ни одной вакансии", "nolive": "Нет открытых вакансий"}


def companies_without_vacancies(conn: sqlite3.Connection, view: str = "none", page: int = 1, per_page: int = 25) -> Page:
    from rabota.web import queries as q

    if view not in EMPTY_VIEWS:
        view = "none"
    live = f"(SELECT COUNT(*) FROM vacancies WHERE company_id = c.id AND {q.VISIBLE_SQL})"
    cond = ("NOT EXISTS (SELECT 1 FROM vacancies v WHERE v.company_id = c.id)" if view == "none" else f"{live} = 0")
    where = f"c.merged_into IS NULL AND {cond}"
    total = int(conn.execute(f"SELECT COUNT(*) FROM companies c WHERE {where}").fetchone()[0])
    page = clamp_page(page, total, per_page)
    rows = conn.execute(
        f"SELECT c.*, (SELECT COUNT(*) FROM vacancies WHERE company_id = c.id) AS vacancies_total, "
        f"(SELECT COUNT(*) FROM vacancies WHERE company_id = c.id AND status = 'archived') AS vacancies_archived "
        f"FROM companies c WHERE {where} ORDER BY c.id DESC LIMIT ? OFFSET ?", (per_page, (page - 1) * per_page)).fetchall()
    return Page([dict(r) for r in rows], total, page, per_page)
