"""Модерация вакансий: очередь на проверку, одобрить, отклонить с причиной, скрыть, вернуть, править поля.

Состояния вакансии: `moderation_status` (pending, approved, rejected) и `status` (active, hidden, expired, closed).
В выдаче видна только пара approved + active (см. rabota.web.queries.VISIBLE_SQL).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from rabota.admin import audit, stoplist
from rabota.admin.common import Page, clamp_page, clean_line, iso, now_utc
from rabota.db import vacancy_row_to_dict

REJECT_TEMPLATES = {
    "paid": "Просьба заплатить за трудоустройство, обучение или оформление запрещена правилами для работодателей.",
    "contacts": "Нет рабочего контакта: оставьте телефон, почту или ник Telegram, по которым можно связаться.",
    "unclear": "Условия не ясны: укажите профессию, оплату с периодом и график работы.",
    "not_job": "Это не вакансия: реклама, услуги и набор в сетевые компании не размещаются.",
    "military": "Набор на военную службу размещается только по решению администрации сайта.",
    "duplicate": "Такая вакансия уже есть на сайте.",
}
REJECT_LABELS = {
    "paid": "Просят деньги", "contacts": "Нет контакта", "unclear": "Условия не ясны", "not_job": "Не вакансия",
    "military": "Набор на службу", "duplicate": "Дубль", "other": "Другая причина",
}

VIEWS = {
    "pending": ("На проверке", "moderation_status = 'pending'"),
    "rejected": ("Отклонены", "moderation_status = 'rejected'"),
    "hidden": ("Скрыты", "status = 'hidden' AND moderation_status = 'approved'"),
    "all": ("Все", "kind = 'vacancy'"),
}

# поля, которые администратор может править; остальное (статус, источник, дедупликация) через них не меняется
EDITABLE = (
    "profession", "title_raw", "category", "work_format", "employment", "schedule", "pay_min", "pay_max", "pay_period",
    "pay_daily", "city", "district", "experience", "age_min", "requirements", "employer", "summary", "contacts",
)


def reason_text(code: str, manual: str) -> str:
    """Причина отказа: свой текст главнее шаблона."""
    manual = clean_line(manual, 500)
    if manual:
        return manual
    return REJECT_TEMPLATES.get(code, "")


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        name: int(conn.execute(f"SELECT COUNT(*) FROM vacancies WHERE {where}").fetchone()[0])
        for name, (_, where) in VIEWS.items()
    }


def queue(conn: sqlite3.Connection, view: str = "pending", page: int = 1, q: str = "", per_page: int = 25) -> Page:
    where = VIEWS.get(view, VIEWS["pending"])[1]
    args: list[Any] = []
    q = clean_line(q, 80)
    conn.create_function("rfold", 1, lambda s: stoplist.fold(s) if isinstance(s, str) else s, deterministic=True)
    if q:
        if q.isdigit():
            where += " AND id = ?"
            args.append(int(q))
        else:
            like = f"%{stoplist.fold(q)}%"
            where += " AND (rfold(profession) LIKE ? OR rfold(title_raw) LIKE ? OR rfold(employer) LIKE ? OR contacts LIKE ?)"
            args += [like, like, like, f"%{q}%"]
    total = int(conn.execute(f"SELECT COUNT(*) FROM vacancies WHERE {where}", args).fetchone()[0])
    p = clamp_page(page, total, per_page)
    rows = conn.execute(
        f"SELECT * FROM vacancies WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?", [*args, per_page, (p - 1) * per_page]
    ).fetchall()
    return Page([vacancy_row_to_dict(r) for r in rows], total, p, per_page)


def get(conn: sqlite3.Connection, vacancy_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM vacancies WHERE id=?", (vacancy_id,)).fetchone()
    return vacancy_row_to_dict(row) if row else None


def approve(conn: sqlite3.Connection, vacancy_id: int, actor: dict[str, Any] | None, now: datetime | None = None) -> bool:
    """Одобрить: вакансия появляется в выдаче, срок жизни считается с этого момента."""
    now = now or now_utc()
    row = conn.execute("SELECT status FROM vacancies WHERE id=?", (vacancy_id,)).fetchone()
    if row is None:
        return False
    status = "active" if row["status"] == "hidden" else row["status"]
    conn.execute(
        "UPDATE vacancies SET moderation_status='approved', moderation_note=NULL, status=?, last_seen_at=? WHERE id=?",
        (status, iso(now), vacancy_id),
    )
    conn.commit()
    audit.log(conn, actor, "vacancy.approve", "vacancy", vacancy_id, now=now)
    return True


def reject(conn: sqlite3.Connection, vacancy_id: int, actor: dict[str, Any] | None, reason: str, now: datetime | None = None) -> bool:
    reason = clean_line(reason, 500)
    if not reason:
        raise ValueError("Укажите причину отказа: автор увидит её в письме.")
    cur = conn.execute(
        "UPDATE vacancies SET moderation_status='rejected', status='hidden', moderation_note=? WHERE id=?",
        (reason, vacancy_id),
    )
    conn.commit()
    if not cur.rowcount:
        return False
    audit.log(conn, actor, "vacancy.reject", "vacancy", vacancy_id, {"reason": reason}, now=now)
    return True


def hide(conn: sqlite3.Connection, vacancy_id: int, actor: dict[str, Any] | None, reason: str = "", now: datetime | None = None) -> bool:
    reason = clean_line(reason, 500)
    cur = conn.execute("UPDATE vacancies SET status='hidden', moderation_note=? WHERE id=?", (reason or "Скрыто администратором", vacancy_id))
    conn.commit()
    if not cur.rowcount:
        return False
    audit.log(conn, actor, "vacancy.hide", "vacancy", vacancy_id, {"reason": reason}, now=now)
    return True


def restore(conn: sqlite3.Connection, vacancy_id: int, actor: dict[str, Any] | None, now: datetime | None = None) -> bool:
    """Вернуть скрытую вакансию в выдачу. Непроверенную и отклонённую сначала нужно одобрить."""
    cur = conn.execute(
        "UPDATE vacancies SET status='active', moderation_note=NULL WHERE id=? AND status='hidden' AND moderation_status='approved'",
        (vacancy_id,),
    )
    conn.commit()
    if not cur.rowcount:
        return False
    audit.log(conn, actor, "vacancy.restore", "vacancy", vacancy_id, now=now)
    return True


def update_fields(
    conn: sqlite3.Connection, vacancy_id: int, actor: dict[str, Any] | None, values: dict[str, Any],
    now: datetime | None = None,
) -> list[str]:
    """Правка полей. Меняются только поля из EDITABLE и только отличающиеся значения. Возвращает названия изменённых."""
    row = conn.execute("SELECT * FROM vacancies WHERE id=?", (vacancy_id,)).fetchone()
    if row is None:
        return []
    changes: dict[str, tuple[Any, Any]] = {}
    for name, new in values.items():
        if name not in EDITABLE:
            continue
        old = row[name]
        if name == "contacts":
            new_db = json.dumps(new, ensure_ascii=False) if isinstance(new, dict) else str(new)
        elif name == "pay_daily":
            new_db = int(bool(new))
        else:
            new_db = new
        if new_db != old:
            changes[name] = (old, new_db)
    if not changes:
        return []
    sets = ", ".join(f"{name} = ?" for name in changes)
    conn.execute(f"UPDATE vacancies SET {sets} WHERE id = ?", [new for _, new in changes.values()] + [vacancy_id])
    conn.commit()
    audit.log(conn, actor, "vacancy.edit", "vacancy", vacancy_id,
              {name: {"было": str(old)[:80], "стало": str(new)[:80]} for name, (old, new) in changes.items()}, now=now)
    return list(changes)


def notify_address(conn: sqlite3.Connection, vacancy_id: int) -> str:
    """Куда писать автору: почта аккаунта-владельца, иначе почта из контактов вакансии."""
    row = conn.execute(
        "SELECT v.contacts, u.email FROM vacancies v LEFT JOIN users u ON u.id = v.owner_user_id WHERE v.id=?",
        (vacancy_id,),
    ).fetchone()
    if row is None:
        return ""
    if row["email"]:
        return str(row["email"])
    try:
        return str((json.loads(row["contacts"] or "{}")).get("email") or "")
    except ValueError:
        return ""


def stoplist_match(conn: sqlite3.Connection, vacancy_id: int) -> stoplist.Match | None:
    v = get(conn, vacancy_id)
    return stoplist.Stoplist.load(conn).match(v) if v else None
