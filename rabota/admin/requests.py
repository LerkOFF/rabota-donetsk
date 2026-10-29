"""Запросы по персональным данным и обращения: журнал, сроки, удаление данных по контакту.

Сроки взяты из порядка обработки обращений (docs/legal/requests.md). Рабочие дни считаются без праздников,
то есть с запасом: срок получается не позже настоящего.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from rabota.admin import audit, stoplist
from rabota.admin.common import Page, clamp_page, clean_line, clean_text, iso, now_utc
from rabota.lifecycle import parse_iso

# код: (подпись, срок, в рабочих днях ли)
TYPES: dict[str, tuple[str, int, bool]] = {
    "access": ("Доступ к своим данным", 10, True),
    "rectify": ("Уточнение данных", 7, True),
    "block": ("Блокирование данных", 10, True),
    "delete": ("Удаление данных", 10, True),
    "withdraw": ("Отзыв согласия, удаление аккаунта", 30, False),
    "object_ads": ("Возражение против рекламы", 10, True),
    "vacancy_owner": ("Убрать мою вакансию", 3, True),
    "stop_resume": ("Прекратить распространение резюме", 3, True),
    "authority": ("Обращение правообладателя или органа", 10, True),
    "other": ("Другое", 10, True),
}
PUBLIC_KINDS = ("access", "rectify", "block", "delete", "withdraw", "object_ads", "vacancy_owner", "stop_resume", "other")
STATUSES = {"new": "Новое", "in_progress": "В работе", "waiting": "Ждём уточнение", "done": "Завершено"}
OPEN_STATUSES = ("new", "in_progress", "waiting")
RUNNING = ("new", "in_progress")  # на этих статусах срок идёт, на waiting стоит
DECISIONS = {"": "Не выбрано", "satisfied": "Выполнено", "partial": "Выполнено частично", "rejected": "Отказано"}
CHANNELS = {"web": "Форма на сайте", "email": "Почта", "post": "Бумажное письмо", "other": "Другое"}
VIEWS = {"open": "Открытые", "overdue": "Просрочены", "done": "Завершены", "all": "Все"}


def add_working_days(start: datetime, days: int) -> datetime:
    cur, left = start, days
    while left > 0:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            left -= 1
    return cur


def due_at(kind: str, created: datetime) -> datetime:
    _, n, working = TYPES[kind]
    return add_working_days(created, n) if working else created + timedelta(days=n)


def _decorate(row: sqlite3.Row | dict[str, Any], now: datetime) -> dict[str, Any]:
    d = dict(row)
    due = parse_iso(d["due_at"])
    running = d["status"] in RUNNING
    d["kind_label"] = TYPES.get(d["kind"], (d["kind"], 0, True))[0]
    d["status_label"] = STATUSES.get(d["status"], d["status"])
    d["channel_label"] = CHANNELS.get(d["channel"], d["channel"])
    d["decision_label"] = DECISIONS.get(d["decision_code"] or "", "")
    d["overdue"] = bool(running and due and due < now)
    d["due_soon"] = bool(running and due and now <= due <= now + timedelta(days=1))
    d["days_left"] = None if due is None else (due - now).days
    return d


def create(
    conn: sqlite3.Connection, kind: str, *, now: datetime | None = None, name: str = "", contact: str = "",
    subject: str = "", body: str = "", channel: str = "web", actor: dict[str, Any] | None = None, requester_key: str = "",
) -> int:
    if kind not in TYPES:
        raise ValueError("Выберите тип обращения.")
    name, contact = clean_line(name, 120), clean_line(contact, 200)
    body, subject = clean_text(body, 3000), clean_line(subject, 300)
    if not contact:
        raise ValueError("Укажите, как с вами связаться.")
    if not body:
        raise ValueError("Опишите, что нужно сделать.")
    now = now or now_utc()
    cur = conn.execute(
        "INSERT INTO legal_requests(created_at, channel, requester_name, requester_contact, kind, subject, body, due_at, requester_key)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (iso(now), channel if channel in CHANNELS else "other", name, contact, kind, subject, body,
         iso(due_at(kind, now)), requester_key),
    )
    conn.commit()
    rid = int(cur.lastrowid or 0)
    audit.log(conn, actor, "request.create", "request", rid, {"kind": kind, "channel": channel}, now=now)
    return rid


def get(conn: sqlite3.Connection, request_id: int, now: datetime | None = None) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM legal_requests WHERE id=?", (request_id,)).fetchone()
    return _decorate(row, now or now_utc()) if row else None


def update(
    conn: sqlite3.Connection, request_id: int, actor: dict[str, Any] | None, now: datetime | None = None, *,
    status: str | None = None, decision: str | None = None, decision_code: str | None = None,
    admin_note: str | None = None,
) -> None:
    now = now or now_utc()
    row = conn.execute("SELECT * FROM legal_requests WHERE id=?", (request_id,)).fetchone()
    if row is None:
        raise ValueError("Обращение не найдено.")
    new_status = status if status is not None else row["status"]
    if new_status not in STATUSES:
        raise ValueError("Неизвестный статус.")
    new_decision = clean_text(decision, 1500) if decision is not None else row["decision"]
    code = decision_code if decision_code is not None else row["decision_code"]
    if code not in DECISIONS:
        raise ValueError("Неизвестное решение.")
    if new_status == "done" and not new_decision:
        raise ValueError("Чтобы завершить обращение, запишите решение и что ответили.")
    answered = row["answered_at"]
    if new_status == "done" and not answered:
        answered = iso(now)
    note = clean_text(admin_note, 1500) if admin_note is not None else row["admin_note"]
    conn.execute(
        "UPDATE legal_requests SET status=?, decision=?, decision_code=?, answered_at=?, admin_note=? WHERE id=?",
        (new_status, new_decision, code, answered, note, request_id),
    )
    conn.commit()
    audit.log(conn, actor, "request.update", "request", request_id,
              {"status": new_status, "decision_code": code}, now=now)


def counts(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, int]:
    now = now or now_utc()
    q = lambda sql, *a: int(conn.execute(sql, a).fetchone()[0])
    return {
        "open": q("SELECT COUNT(*) FROM legal_requests WHERE status != 'done'"),
        "overdue": q("SELECT COUNT(*) FROM legal_requests WHERE status IN ('new','in_progress') AND due_at < ?", iso(now)),
        "done": q("SELECT COUNT(*) FROM legal_requests WHERE status = 'done'"),
    }


def queue(conn: sqlite3.Connection, view: str = "open", page: int = 1, now: datetime | None = None, per_page: int = 25) -> Page:
    now = now or now_utc()
    stamp = iso(now)
    where = {
        "open": "WHERE status != 'done'",
        "overdue": "WHERE status IN ('new','in_progress') AND due_at < ?",
        "done": "WHERE status = 'done'",
    }.get(view, "")
    args: list[Any] = [stamp] if view == "overdue" else []
    total = int(conn.execute(f"SELECT COUNT(*) FROM legal_requests {where}", args).fetchone()[0])
    p = clamp_page(page, total, per_page)
    order = "id DESC" if view in ("done", "all") else "(status IN ('new','in_progress') AND due_at < ?) DESC, due_at ASC, id ASC"
    order_args = [] if view in ("done", "all") else [stamp]
    rows = conn.execute(
        f"SELECT * FROM legal_requests {where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*args, *order_args, per_page, (p - 1) * per_page],
    ).fetchall()
    return Page([_decorate(r, now) for r in rows], total, p, per_page)


# ------------------------------------------------------------------ удаление данных по контакту

@dataclass
class EraseResult:
    vacancies: int
    raw_posts: int
    entry_id: int


def _scrub_contacts(contacts: dict[str, Any], kind: str, normalized: str) -> dict[str, Any]:
    out = dict(contacts)
    if kind == "phone":
        out["phones"] = [p for p in contacts.get("phones") or [] if stoplist.normalize("phone", str(p)) != normalized]
    else:
        for key in ("telegram", "max"):
            if stoplist.normalize("nick", str(contacts.get(key) or "")) == normalized:
                out.pop(key, None)
    return out


def erase_contact(
    conn: sqlite3.Connection, kind: str, value: str, actor: dict[str, Any] | None, now: datetime | None = None,
    request_id: int | None = None,
) -> EraseResult:
    """Скрыть все вакансии с этим телефоном или ником, убрать контакт из них и из исходных постов,
    занести контакт в стоп-лист (хешем), чтобы сборщики не вернули его."""
    if kind not in ("phone", "nick"):
        raise ValueError("Удалять можно по телефону или нику.")
    normalized = stoplist.normalize(kind, value)
    if normalized is None:
        raise ValueError("Телефон: 10 или 11 цифр. Ник: латиница, цифры, подчёркивание.")
    now = now or now_utc()
    entry_id, _ = stoplist.add(conn, kind, value, actor=actor, note="запрос по персональным данным",
                               source="erasure", hashed=True, now=now)
    hit_vac = hit_raw = 0
    rows = conn.execute("SELECT id, contacts, source, external_id FROM vacancies WHERE contacts != '{}'").fetchall()
    for r in rows:
        try:
            contacts = json.loads(r["contacts"] or "{}")
        except ValueError:
            continue
        check = {"contacts": contacts}
        found = normalized in (stoplist.phones_of(check) if kind == "phone" else stoplist.nicks_of(check))
        if not found:
            continue
        conn.execute(
            "UPDATE vacancies SET status='hidden', contacts=?, moderation_note=? WHERE id=?",
            (json.dumps(_scrub_contacts(contacts, kind, normalized), ensure_ascii=False),
             "Скрыто по запросу о персональных данных", r["id"]),
        )
        hit_vac += 1
        hit_raw += conn.execute(
            "DELETE FROM raw_posts WHERE source=? AND external_id=?", (r["source"], r["external_id"])
        ).rowcount
    if request_id:
        conn.execute("UPDATE legal_requests SET executed=1, executed_at=? WHERE id=?", (iso(now), request_id))
    conn.commit()
    masked = conn.execute("SELECT value FROM stoplist WHERE id=?", (entry_id,)).fetchone()[0]
    audit.log(conn, actor, "request.erase", "request" if request_id else "contact", request_id or masked,
              {"kind": kind, "contact": masked, "vacancies": hit_vac, "raw_posts": hit_raw}, now=now)
    return EraseResult(hit_vac, hit_raw, entry_id)


# ------------------------------------------------------------------ выгрузка журнала

def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def export_csv(conn: sqlite3.Connection, now: datetime | None = None) -> str:
    """Журнал обращений для проверяющих органов: дата, от кого, тип, суть, срок, ответ, решение, исполнено."""
    now = now or now_utc()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Номер", "Дата", "От кого", "Контакт", "Тип", "Суть", "Срок ответа", "Дата ответа", "Решение",
                "Исполнено", "Статус"])
    for row in conn.execute("SELECT * FROM legal_requests ORDER BY id"):
        d = _decorate(row, now)
        w.writerow([_cell(x) for x in (
            d["id"], d["created_at"], d["requester_name"], d["requester_contact"], d["kind_label"], d["body"],
            d["due_at"], d["answered_at"] or "", (d["decision_label"] + ". " + d["decision"]).strip(". "),
            "да" if d["executed"] else "нет", d["status_label"])])
    return out.getvalue()
