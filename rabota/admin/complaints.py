"""Жалобы на вакансии (таблица complaints) и на сообщения и пользователей (таблица reports).

Причины на форме сайта, автоскрытие при жалобах разных людей, очередь и действия: скрыть, стоп-лист, отклонить, ответить.
"""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
from datetime import datetime
from typing import Any

from rabota import config
from rabota.accounts.mailer import SITE_NAME, Mail
from rabota.admin import audit, moderation, stoplist
from rabota.admin.common import Page, clamp_page, clean_line, clean_text, iso, now_utc

# код причины, текст на форме сайта. Первые пять совпадают с причинами из ТЗ, остальные нужны форме.
PUBLIC_REASONS: tuple[tuple[str, str], ...] = (
    ("closed", "Вакансия не актуальна"),
    ("fraud", "Мошенничество или просьба заплатить за работу"),
    ("ad", "Это не вакансия: реклама или услуги"),
    ("wrong_data", "Ошибка в данных: оплата, телефон, адрес"),
    ("duplicate", "Дубль: такая вакансия уже есть"),
    ("foreign_data", "Здесь указаны мои данные без моего согласия"),
    ("other", "Другое"),
)
REASON_LABELS = {
    "fraud": "Мошенничество", "wrong_data": "Неверные данные", "closed": "Закрыта", "duplicate": "Дубль",
    "foreign_data": "Чужие данные", "ad": "Не вакансия", "other": "Другое",
}
RESOLUTION_LABELS = {"hidden": "Вакансия скрыта", "stoplisted": "Скрыта и в стоп-листе", "dismissed": "Отклонена"}
VIEWS = {"open": "Открытые", "closed": "Закрытые", "all": "Все"}
REPORT_ACTIONS = {"dismiss": "dismissed", "resolve": "resolved", "block": "resolved"}


def code_for_text(text: str) -> str:
    head = (text or "").split(":")[0].strip()
    for code, label in PUBLIC_REASONS:
        if head == label or (text or "").startswith(label):
            return code
    return "other"


def reporter_key(secret: bytes, ip: str) -> str:
    """Отпечаток заявителя для подсчёта жалоб от разных людей: адрес в базу не попадает."""
    return hmac.new(secret, b"complaint:" + ip.encode(), hashlib.sha256).hexdigest()[:24]


def autohide_threshold() -> int:
    try:
        return max(0, int(config.get("COMPLAINT_AUTOHIDE_THRESHOLD", "3")))
    except ValueError:
        return 3


def add(
    conn: sqlite3.Connection, vacancy_id: int, reason: str, *, code: str = "", reporter_email: str | None = None,
    reporter_key: str | None = None, now: datetime | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO complaints(vacancy_id, reason, reason_code, reporter_email, reporter_key, created_at) VALUES (?,?,?,?,?,?)",
        (vacancy_id, reason, code or code_for_text(reason), reporter_email or None, reporter_key or None,
         iso(now or now_utc())),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def autohide_if_needed(
    conn: sqlite3.Connection, vacancy_id: int, threshold: int | None = None, now: datetime | None = None
) -> bool:
    """Скрыть опубликованную вакансию, если открытых жалоб от разных людей набралось не меньше порога (0: выключено)."""
    limit = autohide_threshold() if threshold is None else threshold
    if limit <= 0:
        return False
    people = conn.execute(
        "SELECT COUNT(DISTINCT reporter_key) FROM complaints WHERE vacancy_id=? AND status='new' AND reporter_key IS NOT NULL",
        (vacancy_id,),
    ).fetchone()[0]
    if people < limit:
        return False
    cur = conn.execute(
        "UPDATE vacancies SET status='hidden', moderation_note=? WHERE id=? AND status='active' AND moderation_status='approved'",
        (f"Скрыто автоматически: жалобы от {people} разных людей. Проверьте и верните или оставьте скрытой.", vacancy_id),
    )
    conn.commit()
    if not cur.rowcount:
        return False
    audit.log(conn, None, "vacancy.autohide", "vacancy", vacancy_id, {"people": people, "threshold": limit}, now=now)
    return True


# ------------------------------------------------------------------ очередь

_SELECT = """
SELECT c.*, v.profession, v.employer, v.status AS vacancy_status, v.moderation_status AS vacancy_moderation,
  (SELECT COUNT(*) FROM complaints o WHERE o.vacancy_id = c.vacancy_id AND o.status = 'new') AS same_vacancy_open
FROM complaints c LEFT JOIN vacancies v ON v.id = c.vacancy_id
"""


def _where(view: str) -> str:
    return {"open": "WHERE c.status = 'new'", "closed": "WHERE c.status != 'new'"}.get(view, "")


def _decorate(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["code"] = d.get("reason_code") or code_for_text(d["reason"])
    d["code_label"] = REASON_LABELS.get(d["code"], "Другое")
    d["resolution_label"] = RESOLUTION_LABELS.get(d.get("resolution") or "", "")
    return d


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    one = lambda sql: int(conn.execute(sql).fetchone()[0])
    return {
        "open": one("SELECT COUNT(*) FROM complaints WHERE status='new'"),
        "closed": one("SELECT COUNT(*) FROM complaints WHERE status!='new'"),
        "reports_open": one("SELECT COUNT(*) FROM reports WHERE status='new'"),
    }


def queue(conn: sqlite3.Connection, view: str = "open", page: int = 1, per_page: int = 25) -> Page:
    where = _where(view)
    total = int(conn.execute(f"SELECT COUNT(*) FROM complaints c {where}").fetchone()[0])
    p = clamp_page(page, total, per_page)
    order = "(c.reason_code = 'fraud') DESC, c.created_at ASC, c.id ASC" if view == "open" else "c.id DESC"
    rows = conn.execute(f"{_SELECT} {where} ORDER BY {order} LIMIT ? OFFSET ?", (per_page, (p - 1) * per_page)).fetchall()
    return Page([_decorate(r) for r in rows], total, p, per_page)


def get(conn: sqlite3.Connection, complaint_id: int) -> dict[str, Any] | None:
    row = conn.execute(f"{_SELECT} WHERE c.id = ?", (complaint_id,)).fetchone()
    return _decorate(row) if row else None


def for_vacancy(conn: sqlite3.Connection, vacancy_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(f"{_SELECT} WHERE c.vacancy_id = ? ORDER BY c.id DESC", (vacancy_id,)).fetchall()
    return [_decorate(r) for r in rows]


def stop_candidates(conn: sqlite3.Connection, vacancy_id: int) -> list[dict[str, str]]:
    """Что можно занести в стоп-лист из вакансии: телефоны, ники, работодатель."""
    v = moderation.get(conn, vacancy_id)
    if v is None:
        return []
    out: list[dict[str, str]] = []
    contacts = v["contacts"] or {}
    for p in contacts.get("phones") or []:
        if stoplist.normalize("phone", str(p)):
            out.append({"kind": "phone", "value": str(p)})
    for key in ("telegram", "max"):
        if contacts.get(key) and stoplist.normalize("nick", str(contacts[key])):
            out.append({"kind": "nick", "value": str(contacts[key])})
    if v.get("employer") and stoplist.normalize("employer", v["employer"]):
        out.append({"kind": "employer", "value": v["employer"]})
    return out


# ------------------------------------------------------------------ действия

def _close_open(conn: sqlite3.Connection, vacancy_id: int, resolution: str, actor: dict[str, Any] | None, note: str, now: datetime) -> None:
    conn.execute(
        "UPDATE complaints SET status='resolved', resolution=?, resolved_at=?, resolved_by=?, admin_note=? "
        "WHERE vacancy_id=? AND status='new'",
        (resolution, iso(now), (actor or {}).get("id"), note, vacancy_id),
    )
    conn.commit()


def resolve(
    conn: sqlite3.Connection, complaint_id: int, action: str, actor: dict[str, Any] | None, *, now: datetime | None = None,
    note: str = "", stop: list[tuple[str, str]] | None = None, restore: bool = False,
) -> None:
    """hide: скрыть вакансию и закрыть все открытые жалобы на неё; stoplist: то же плюс записи в стоп-лист;
    dismiss: закрыть только эту жалобу. ValueError с понятным текстом, если действие не удалось."""
    now = now or now_utc()
    c = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
    if c is None:
        raise ValueError("Жалоба не найдена.")
    if action not in ("hide", "stoplist", "dismiss"):
        raise ValueError("Неизвестное действие.")
    note = clean_line(note, 500)
    vid = int(c["vacancy_id"])
    if action == "dismiss":
        conn.execute(
            "UPDATE complaints SET status='dismissed', resolution='dismissed', resolved_at=?, resolved_by=?, admin_note=? WHERE id=?",
            (iso(now), (actor or {}).get("id"), note, complaint_id),
        )
        conn.commit()
        if restore:
            moderation.restore(conn, vid, actor, now)
    elif action == "hide":
        moderation.hide(conn, vid, actor, note or "Скрыто по жалобе", now)
        _close_open(conn, vid, "hidden", actor, note, now)
    else:
        entries = stop or []
        for kind, value in entries:  # сначала проверка всех записей, потом изменения
            if stoplist.normalize(kind, value) is None:
                raise ValueError(f"Запись «{value}» не подходит для стоп-листа ({stoplist.KINDS.get(kind, kind)}).")
        if not entries:
            raise ValueError("Выберите хотя бы одну запись для стоп-листа.")
        for kind, value in entries:
            entry_id, _ = stoplist.add(conn, kind, value, actor=actor, note=f"по жалобе {complaint_id}", now=now)
            stoplist.apply_to_existing(conn, actor, entry_id, now)
        moderation.hide(conn, vid, actor, note or "Скрыто по жалобе, контакт в стоп-листе", now)
        _close_open(conn, vid, "stoplisted", actor, note, now)
    audit.log(conn, actor, f"complaint.{action}", "complaint", complaint_id, {"vacancy": vid, "note": note}, now=now)


# ------------------------------------------------------------------ ответ заявителю

def build_reply(conn: sqlite3.Connection, complaint_id: int, text: str, site_url: str) -> Mail:
    row = conn.execute("SELECT reporter_email, vacancy_id FROM complaints WHERE id=?", (complaint_id,)).fetchone()
    if row is None:
        raise ValueError("Жалоба не найдена.")
    if not row["reporter_email"]:
        raise ValueError("Заявитель не оставил адрес почты.")
    text = clean_text(text, 3000)
    if not text:
        raise ValueError("Напишите текст ответа.")
    site_url = site_url.rstrip("/")
    body = (
        f"Здравствуйте!\n\n{text}\n\n"
        f"Ваша жалоба была на вакансию: {site_url}/vacancy/{row['vacancy_id']}\n\n{SITE_NAME}\n{site_url}\n"
    )
    return Mail(to=row["reporter_email"], subject=f"Ответ на вашу жалобу: {SITE_NAME}", body=body)


def mark_replied(conn: sqlite3.Connection, complaint_id: int, actor: dict[str, Any] | None, now: datetime | None = None) -> None:
    now = now or now_utc()
    conn.execute("UPDATE complaints SET reply_sent_at=? WHERE id=?", (iso(now), complaint_id))
    conn.commit()
    audit.log(conn, actor, "complaint.reply", "complaint", complaint_id, now=now)


# ------------------------------------------------------------------ жалобы на сообщения и пользователей

REPORT_TYPES = {"vacancy": "Вакансия", "user": "Пользователь", "message": "Сообщение", "resume": "Резюме"}


def _report_target(conn: sqlite3.Connection, r: dict[str, Any]) -> None:
    ttype, tid = r["target_type"], r["target_id"]
    r["target_text"], r["target_user_id"] = "Объект удалён", None
    if ttype == "message":
        m = conn.execute("SELECT body, sender_user_id FROM messages WHERE id=?", (tid,)).fetchone()
        if m:
            r["target_text"], r["target_user_id"] = m["body"], m["sender_user_id"]
    elif ttype == "user":
        u = conn.execute("SELECT id, email, name FROM users WHERE id=?", (tid,)).fetchone()
        if u:
            r["target_text"], r["target_user_id"] = f"{u['name'] or 'Без имени'}, {u['email']}", u["id"]
    elif ttype == "resume":
        s = conn.execute("SELECT user_id, title, profession FROM resumes WHERE id=?", (tid,)).fetchone()
        if s:
            r["target_text"], r["target_user_id"] = s["title"] or s["profession"] or "Резюме", s["user_id"]
    elif ttype == "vacancy":
        v = conn.execute("SELECT profession FROM vacancies WHERE id=?", (tid,)).fetchone()
        if v:
            r["target_text"] = v["profession"]


def reports_queue(conn: sqlite3.Connection, view: str = "open", page: int = 1, per_page: int = 25) -> Page:
    where = {"open": "WHERE r.status = 'new'", "closed": "WHERE r.status != 'new'"}.get(view, "")
    total = int(conn.execute(f"SELECT COUNT(*) FROM reports r {where}").fetchone()[0])
    p = clamp_page(page, total, per_page)
    rows = conn.execute(
        f"SELECT r.*, u.email AS reporter_email FROM reports r LEFT JOIN users u ON u.id = r.reporter_user_id {where} "
        "ORDER BY r.id DESC LIMIT ? OFFSET ?", (per_page, (p - 1) * per_page),
    ).fetchall()
    items = []
    for row in rows:
        d = dict(row)
        _report_target(conn, d)
        d["type_label"] = REPORT_TYPES.get(d["target_type"], d["target_type"])
        items.append(d)
    return Page(items, total, p, per_page)


def report_resolve(
    conn: sqlite3.Connection, report_id: int, action: str, actor: dict[str, Any] | None, now: datetime | None = None,
    note: str = "",
) -> None:
    if action not in REPORT_ACTIONS:
        raise ValueError("Неизвестное действие.")
    now = now or now_utc()
    row = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    if row is None:
        raise ValueError("Жалоба не найдена.")
    info = dict(row)
    _report_target(conn, info)
    if action == "block":
        uid = info["target_user_id"]
        if uid is None:
            raise ValueError("Не удалось определить пользователя.")
        target = conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
        if target is None or target["role"] == "admin":
            raise ValueError("Администратора заблокировать нельзя.")
        from rabota.accounts import service

        service.set_blocked(conn, int(uid), True)
        audit.log(conn, actor, "user.block", "user", uid, {"report": report_id}, now=now)
    status = REPORT_ACTIONS[action]
    conn.execute(
        "UPDATE reports SET status=?, resolution=?, resolved_at=?, resolved_by=?, admin_note=? WHERE id=?",
        (status, "blocked" if action == "block" else status, iso(now), (actor or {}).get("id"), clean_line(note, 500), report_id),
    )
    conn.commit()
    audit.log(conn, actor, f"report.{action}", "report", report_id, {"note": note}, now=now)

