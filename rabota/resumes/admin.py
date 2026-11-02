"""Раздел «Резюме» админки: числа, список с фильтрами, карточка, скрытие нарушающих и возврат. Каждое действие в журнале."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from rabota.admin import audit
from rabota.admin.common import Page, actor_id, clamp_page, clean_line, iso
from rabota.pipeline.dictionaries import norm
from rabota.resumes import rules, service

PER_PAGE = 25
VIEWS = {
    "all": "Все",
    "public": "Всем работодателям",
    "open": "Только по отклику",
    "hidden": "Скрыты владельцем",
    "admin_hidden": "Скрыты модератором",
    "stale": "Давно не обновлялись",
}
FLASH = {
    "resume_hidden": "Резюме скрыто, владелец увидит причину в разделе «Мои резюме».",
    "resume_restored": "Резюме возвращено владельцу.",
}
MAX_NOTE = 300


def counts(conn: sqlite3.Connection, now: datetime) -> dict[str, int]:
    fresh = iso(now - timedelta(days=rules.STALE_DAYS))
    one = lambda sql, *a: int(conn.execute(sql, a).fetchone()[0])
    return {
        "all": one("SELECT COUNT(*) FROM resumes"),
        "public": one("SELECT COUNT(*) FROM resumes WHERE visibility = 'public' AND admin_hidden = 0"),
        "open": one("SELECT COUNT(*) FROM resumes WHERE visibility = 'open' AND admin_hidden = 0"),
        "hidden": one("SELECT COUNT(*) FROM resumes WHERE visibility = 'hidden'"),
        "admin_hidden": one("SELECT COUNT(*) FROM resumes WHERE admin_hidden = 1"),
        "stale": one("SELECT COUNT(*) FROM resumes WHERE updated_at < ?", fresh),
        "owners": one("SELECT COUNT(DISTINCT user_id) FROM resumes"),
        "contacts": one("SELECT COUNT(*) FROM resume_views WHERE kind = 'contact'"),
    }


def parse(params: Any) -> dict[str, Any]:
    view = str(params.get("view") or "all")
    try:
        page = int(params.get("page") or 1)
    except ValueError:
        page = 1
    return {"view": view if view in VIEWS else "all", "q": clean_line(params.get("q"), 80), "page": max(1, min(page, 100_000))}


def query_string(f: dict[str, Any]) -> str:
    from urllib.parse import urlencode

    return urlencode({k: v for k, v in (("view", f["view"] if f["view"] != "all" else ""), ("q", f["q"])) if v})


def listing(conn: sqlite3.Connection, f: dict[str, Any], now: datetime) -> Page:
    where, args = ["1"], []
    view = f["view"]
    if view in ("public", "open"):
        where.append("r.visibility = ? AND r.admin_hidden = 0")
        args.append(view)
    elif view == "hidden":
        where.append("r.visibility = 'hidden'")
    elif view == "admin_hidden":
        where.append("r.admin_hidden = 1")
    elif view == "stale":
        where.append("r.updated_at < ?")
        args.append(iso(now - timedelta(days=rules.STALE_DAYS)))
    if f["q"]:
        q = f["q"].lstrip("№#")
        if q.isdigit() and len(q) <= 9:
            where.append("(r.id = ? OR r.user_id = ?)")
            args += [int(q), int(q)]
        else:
            like = "%" + norm(q).replace("%", "").replace("_", "") + "%"
            where.append("(r.profession LIKE ? OR r.skills_key LIKE ? OR u.email LIKE ?)")
            args += [like, like, f"%{q}%"]
    clause = " AND ".join(where)
    total = int(conn.execute(
        f"SELECT COUNT(*) FROM resumes r JOIN users u ON u.id = r.user_id WHERE {clause}", args).fetchone()[0])
    page_no = clamp_page(f["page"], total, PER_PAGE)
    rows = conn.execute(
        "SELECT r.*, u.email AS owner_email, u.name AS owner_name, u.status AS owner_status,"
        " (SELECT COUNT(DISTINCT employer_user_id) FROM resume_views v WHERE v.resume_id = r.id AND v.kind = 'view') AS views,"
        " (SELECT COUNT(*) FROM resume_views v WHERE v.resume_id = r.id AND v.kind = 'contact') AS contacts"
        f" FROM resumes r JOIN users u ON u.id = r.user_id WHERE {clause} ORDER BY r.updated_at DESC, r.id DESC LIMIT ? OFFSET ?",
        [*args, PER_PAGE, (page_no - 1) * PER_PAGE],
    ).fetchall()
    items = [service.present(service.decode(dict(r)), now) | {
        "owner_email": r["owner_email"], "owner_name": r["owner_name"], "views": r["views"], "contacts": r["contacts"],
        "owner_status": r["owner_status"],
    } for r in rows]
    return Page(items=items, total=total, page=page_no, per_page=PER_PAGE)


def get(conn: sqlite3.Connection, resume_id: int, now: datetime) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT r.*, u.email AS owner_email, u.name AS owner_name, u.status AS owner_status FROM resumes r"
        " JOIN users u ON u.id = r.user_id WHERE r.id = ?", (resume_id,)).fetchone()
    if row is None:
        return None
    r = service.present(service.decode(dict(row)), now)
    r.update(owner_email=row["owner_email"], owner_name=row["owner_name"], owner_status=row["owner_status"])
    r.update(service.view_stats(conn, [resume_id]).get(resume_id, {"views": 0, "contacts": 0}))
    r["applications"] = int(conn.execute("SELECT COUNT(*) FROM applications WHERE resume_id = ?", (resume_id,)).fetchone()[0])
    return r


def hide(conn: sqlite3.Connection, resume_id: int, note: str, admin: dict[str, Any], ip: str, now: datetime) -> bool:
    text = clean_line(note, MAX_NOTE)
    if not text:
        raise ValueError("Напишите причину: владелец увидит её в разделе «Мои резюме».")
    cur = conn.execute(
        "UPDATE resumes SET admin_hidden = 1, admin_note = ?, admin_hidden_at = ? WHERE id = ?", (text, iso(now), resume_id))
    conn.commit()
    if not cur.rowcount:
        return False
    audit.log(conn, admin, "resume.hide", "resume", resume_id, {"note": text}, ip, now)
    return True


def restore(conn: sqlite3.Connection, resume_id: int, admin: dict[str, Any], ip: str, now: datetime) -> bool:
    cur = conn.execute(
        "UPDATE resumes SET admin_hidden = 0, admin_note = '', admin_hidden_at = NULL WHERE id = ? AND admin_hidden = 1",
        (resume_id,))
    conn.commit()
    if not cur.rowcount:
        return False
    audit.log(conn, admin, "resume.restore", "resume", resume_id, {"by": actor_id(admin)}, ip, now)
    return True
