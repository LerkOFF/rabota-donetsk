"""Отклики и переписка без привязки к вебу. Все функции получают соединение SQLite (строки как словари,
внешние ключи включены) и время; чужие и несуществующие объекты дают NotFound, нарушение правил - MessagingError.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from typing import Any

from rabota.accounts.tokens import iso
from rabota.messaging import rules
from rabota.messaging.rules import MessagingError, NotFound
from rabota.web.queries import VISIBLE_SQL

__all__ = [
    "MessagingError", "NotFound", "apply", "apply_state", "block", "get_conversation", "get_prefs", "get_response",
    "list_conversations", "list_messages", "list_responses", "mark_read", "mark_viewed", "new_responses_count",
    "report_message", "response_counts", "send_message", "set_prefs", "set_status", "unblock", "unread_total",
    "unsubscribe", "withdraw",
]

ALL_STATUSES = (*rules.APP_STATUSES, "withdrawn")
EFFECTIVE_STATUS = "CASE WHEN a.withdrawn_at IS NOT NULL THEN 'withdrawn' ELSE a.status END"
UNREAD_SQL = (
    "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id AND m.sender_user_id <> :me AND m.read_at IS NULL)"
)


def _rows(conn: sqlite3.Connection, sql: str, args: Any = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _page(total: int, page: int, per_page: int) -> dict[str, Any]:
    pages = max(1, math.ceil(total / per_page))
    page = min(max(1, page), pages)
    return {"total": total, "page": page, "pages": pages, "has_prev": page > 1, "has_next": page < pages,
            "offset": (page - 1) * per_page, "per_page": per_page}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


# ------------------------------------------------------------------ отклик

def _responsive_vacancy(conn: sqlite3.Connection, vacancy_id: int) -> dict[str, Any] | None:
    """Вакансия, на которую можно откликнуться: размещена работодателем через сайт, одобрена и не истекла."""
    rows = _rows(
        conn,
        f"SELECT * FROM vacancies WHERE id = ? AND origin = 'direct' AND owner_user_id IS NOT NULL AND {VISIBLE_SQL}",
        (vacancy_id,),
    )
    return rows[0] if rows else None


def blocked_pair(conn: sqlite3.Connection, user_id: int, other_id: int) -> tuple[bool, bool]:
    """(user заблокировал other, other заблокировал user)."""
    rows = conn.execute(
        "SELECT blocker_user_id FROM user_blocks WHERE (blocker_user_id = ? AND blocked_user_id = ?)"
        " OR (blocker_user_id = ? AND blocked_user_id = ?)",
        (user_id, other_id, other_id, user_id),
    ).fetchall()
    who = {r[0] for r in rows}
    return user_id in who, other_id in who


def apply(
    conn: sqlite3.Connection, *, vacancy_id: int, user: dict[str, Any], cover: str, share_phone: bool, now: datetime
) -> dict[str, int]:
    """Отклик соискателя: запись отклика, переписка с ним и сопроводительное сообщение (если оно есть)."""
    if user.get("role") != "job_seeker" or user.get("status", "active") != "active":
        raise MessagingError("forbidden_role", "Откликаться могут только соискатели.")
    vacancy = _responsive_vacancy(conn, vacancy_id)
    if vacancy is None:
        raise NotFound("Вакансия не найдена")
    owner_id = int(vacancy["owner_user_id"])
    if blocked_pair(conn, user["id"], owner_id)[1]:
        raise MessagingError("unavailable", "Не удалось отправить отклик на эту вакансию.")
    if conn.execute("SELECT 1 FROM applications WHERE vacancy_id = ? AND user_id = ?", (vacancy_id, user["id"])).fetchone():
        raise MessagingError("duplicate", "Вы уже откликались на эту вакансию.")
    text = rules.check_text(cover, rules.MAX_COVER, required=False, what="Сопроводительное сообщение")
    stamp = iso(now)
    phone = (user.get("phone") or None) if share_phone else None
    try:
        cur = conn.execute(
            "INSERT INTO applications(vacancy_id, user_id, cover_letter, status, created_at, contact_name, contact_phone)"
            " VALUES (?,?,?,?,?,?,?)",
            (vacancy_id, user["id"], text, "new", stamp, user.get("name") or "", phone),
        )
        application_id = int(cur.lastrowid or 0)
        conv = conn.execute(
            "INSERT INTO conversations(employer_user_id, seeker_user_id, vacancy_id, application_id, started_by,"
            " created_at, last_message_at, title) VALUES (?,?,?,?,?,?,?,?)",
            (owner_id, user["id"], vacancy_id, application_id, user["id"], stamp, stamp if text else None,
             (vacancy["profession"] or "")[:120]),
        )
        conversation_id = int(conv.lastrowid or 0)
        if text:
            conn.execute(
                "INSERT INTO messages(conversation_id, sender_user_id, body, created_at) VALUES (?,?,?,?)",
                (conversation_id, user["id"], text, stamp),
            )
        conn.commit()
    except sqlite3.IntegrityError:  # две отправки подряд: второй отклик на ту же вакансию
        conn.rollback()
        raise MessagingError("duplicate", "Вы уже откликались на эту вакансию.") from None
    return {"application_id": application_id, "conversation_id": conversation_id}


def apply_state(conn: sqlite3.Connection, vacancy_id: int, user_id: int) -> dict[str, Any] | None:
    """Отклик этого соискателя на вакансию (для страницы вакансии) или None."""
    rows = _rows(
        conn,
        f"SELECT a.id AS application_id, {EFFECTIVE_STATUS} AS status,"
        " (SELECT id FROM conversations WHERE application_id = a.id) AS conversation_id"
        " FROM applications a WHERE a.vacancy_id = ? AND a.user_id = ?",
        (vacancy_id, user_id),
    )
    return rows[0] if rows else None


# ------------------------------------------------------------------ отклики: списки и карточка

def _response_select() -> str:
    return (
        f"SELECT a.id, a.vacancy_id, a.user_id AS seeker_id, a.cover_letter, a.created_at, a.withdrawn_at, a.status AS raw_status,"
        f" {EFFECTIVE_STATUS} AS status, a.status_changed_at, a.contact_phone,"
        " COALESCE(NULLIF(a.contact_name, ''), su.name) AS seeker_name,"
        " COALESCE(v.profession, c.title, 'Вакансия') AS vacancy_title, v.city AS vacancy_city,"
        " COALESCE(NULLIF(v.employer, ''), eu.name) AS employer_name, v.owner_user_id AS employer_id,"
        " c.id AS conversation_id,"
        f" CASE WHEN c.id IS NULL THEN 0 ELSE {UNREAD_SQL} END AS unread"
        " FROM applications a"
        " JOIN users su ON su.id = a.user_id"
        " LEFT JOIN vacancies v ON v.id = a.vacancy_id"
        " LEFT JOIN users eu ON eu.id = v.owner_user_id"
        " LEFT JOIN conversations c ON c.application_id = a.id"
    )


def _own_responses(user: dict[str, Any]) -> str:
    return "v.owner_user_id = :me" if user["role"] == "employer" else "a.user_id = :me"


def list_responses(
    conn: sqlite3.Connection, user: dict[str, Any], *, status: str | None = None, vacancy_id: int | None = None,
    page: int = 1, per_page: int = rules.LIST_PAGE,
) -> dict[str, Any]:
    where = [_own_responses(user)]
    args: dict[str, Any] = {"me": user["id"]}
    if status == "withdrawn":
        where.append("a.withdrawn_at IS NOT NULL")
    elif status in rules.APP_STATUSES:
        where.append("a.status = :status AND a.withdrawn_at IS NULL")
        args["status"] = status
    if vacancy_id:
        where.append("a.vacancy_id = :vid")
        args["vid"] = vacancy_id
    clause = " AND ".join(where)
    base = "FROM applications a LEFT JOIN vacancies v ON v.id = a.vacancy_id"
    total = int(conn.execute(f"SELECT COUNT(*) {base} WHERE {clause}", args).fetchone()[0])
    meta = _page(total, page, per_page)
    items = _rows(
        conn,
        f"{_response_select()} WHERE {clause} ORDER BY COALESCE(c.last_message_at, a.created_at) DESC, a.id DESC"
        " LIMIT :limit OFFSET :offset",
        {**args, "limit": per_page, "offset": meta["offset"]},
    )
    return {**meta, "items": items}


def response_counts(conn: sqlite3.Connection, user: dict[str, Any]) -> dict[str, int]:
    counts = dict.fromkeys(("all", *ALL_STATUSES), 0)
    rows = conn.execute(
        f"SELECT {EFFECTIVE_STATUS} AS st, COUNT(*) FROM applications a LEFT JOIN vacancies v ON v.id = a.vacancy_id"
        f" WHERE {_own_responses(user)} GROUP BY st",
        {"me": user["id"]},
    ).fetchall()
    for st, n in rows:
        counts[st] = int(n)
        counts["all"] += int(n)
    return counts


def get_response(conn: sqlite3.Connection, application_id: int, user: dict[str, Any]) -> dict[str, Any] | None:
    """Отклик для участника (соискателя или работодателя вакансии). Чужим и несуществующим - None."""
    rows = _rows(
        conn,
        f"{_response_select()} WHERE a.id = :id AND {_own_responses(user)}",
        {"id": application_id, "me": user["id"]},
    )
    if not rows:
        return None
    row = rows[0]
    row["i_am"] = "employer" if user["role"] == "employer" else "job_seeker"
    row["shared_phone"] = bool(row["contact_phone"])
    row["vacancy_alive"] = row["vacancy_id"] is not None and row["employer_id"] is not None
    return row


def mark_viewed(conn: sqlite3.Connection, application_id: int, employer_id: int, now: datetime) -> bool:
    cur = conn.execute(
        "UPDATE applications SET status = 'viewed', status_changed_at = ? WHERE id = ? AND status = 'new'"
        " AND withdrawn_at IS NULL AND vacancy_id IN (SELECT id FROM vacancies WHERE owner_user_id = ?)",
        (iso(now), application_id, employer_id),
    )
    conn.commit()
    return cur.rowcount > 0


def _owned_application(conn: sqlite3.Connection, application_id: int, employer_id: int) -> dict[str, Any]:
    rows = _rows(
        conn,
        "SELECT a.* FROM applications a JOIN vacancies v ON v.id = a.vacancy_id"
        " WHERE a.id = ? AND v.owner_user_id = ?",
        (application_id, employer_id),
    )
    if not rows:
        raise NotFound("Отклик не найден")
    return rows[0]


def set_status(conn: sqlite3.Connection, application_id: int, employer_id: int, status: str, now: datetime) -> None:
    app = _owned_application(conn, application_id, employer_id)
    if status not in rules.EMPLOYER_SET_STATUSES:
        raise MessagingError("bad_status", "Выберите статус из списка.")
    if app["withdrawn_at"]:
        raise MessagingError("closed", "Соискатель отозвал отклик, статус изменить нельзя.")
    conn.execute("UPDATE applications SET status = ?, status_changed_at = ? WHERE id = ?", (status, iso(now), application_id))
    conn.commit()


def withdraw(conn: sqlite3.Connection, application_id: int, seeker_id: int, now: datetime) -> None:
    row = conn.execute("SELECT withdrawn_at FROM applications WHERE id = ? AND user_id = ?", (application_id, seeker_id)).fetchone()
    if row is None:
        raise NotFound("Отклик не найден")
    if row[0]:
        return
    conn.execute("UPDATE applications SET withdrawn_at = ?, status_changed_at = ? WHERE id = ?", (iso(now), iso(now), application_id))
    conn.commit()


def new_responses_count(conn: sqlite3.Connection, employer_id: int) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM applications a JOIN vacancies v ON v.id = a.vacancy_id"
        " WHERE v.owner_user_id = ? AND a.status = 'new' AND a.withdrawn_at IS NULL",
        (employer_id,),
    ).fetchone()[0])


# ------------------------------------------------------------------ переписка

_CONV_SELECT = (
    "SELECT c.id, c.employer_user_id, c.seeker_user_id, c.vacancy_id, c.application_id, c.created_at, c.last_message_at,"
    " COALESCE(NULLIF(c.title, ''), v.profession, 'Вакансия') AS title,"
    " v.employer AS vacancy_employer, a.status AS raw_app_status, a.withdrawn_at,"
    " eu.name AS employer_name, eu.status AS employer_status, su.name AS seeker_name, su.status AS seeker_status"
    " FROM conversations c"
    " JOIN users eu ON eu.id = c.employer_user_id JOIN users su ON su.id = c.seeker_user_id"
    " LEFT JOIN vacancies v ON v.id = c.vacancy_id LEFT JOIN applications a ON a.id = c.application_id"
)


def get_conversation(conn: sqlite3.Connection, conversation_id: int, user_id: int) -> dict[str, Any] | None:
    """Тред с точки зрения участника: собеседник, состояние отклика, блокировки и можно ли писать. Чужой - None."""
    rows = _rows(
        conn,
        f"{_CONV_SELECT} WHERE c.id = ? AND (c.employer_user_id = ? OR c.seeker_user_id = ?)",
        (conversation_id, user_id, user_id),
    )
    if not rows:
        return None
    c = rows[0]
    as_employer = c["employer_user_id"] == user_id
    c["i_am"] = "employer" if as_employer else "job_seeker"
    c["counterpart_id"] = c["seeker_user_id"] if as_employer else c["employer_user_id"]
    c["counterpart_name"] = c["seeker_name"] if as_employer else (c["vacancy_employer"] or c["employer_name"])
    counterpart_status = c["seeker_status"] if as_employer else c["employer_status"]
    c["app_status"] = "withdrawn" if c["withdrawn_at"] else c["raw_app_status"]
    c["i_blocked"], c["blocked_me"] = blocked_pair(conn, user_id, c["counterpart_id"])
    code, reason = "", ""
    if c["app_status"] == "withdrawn":
        code, reason = "closed", "Отклик отозван, переписка закрыта."
    elif c["app_status"] == "rejected" and not as_employer:
        code, reason = "closed", "Работодатель завершил рассмотрение отклика, писать в этот чат больше нельзя."
    elif c["i_blocked"]:
        code, reason = "blocked", "Вы заблокировали этого пользователя. Чтобы писать ему, разблокируйте."
    elif c["blocked_me"]:
        code, reason = "blocked", "Пользователь не принимает сообщения."
    elif counterpart_status != "active":
        code, reason = "closed", "Аккаунт собеседника недоступен."
    c["can_send"], c["closed_code"], c["closed_reason"] = not code, code, reason
    return c


def _participant(conn: sqlite3.Connection, conversation_id: int, user_id: int) -> dict[str, Any]:
    conv = get_conversation(conn, conversation_id, user_id)
    if conv is None:
        raise NotFound("Переписка не найдена")
    return conv


def list_messages(
    conn: sqlite3.Connection, conversation_id: int, *, before: int | None = None, limit: int = rules.THREAD_PAGE
) -> dict[str, Any]:
    """Последние limit сообщений (или предыдущие перед before) в порядке отправки. Доступ проверяет вызывающий."""
    args: list[Any] = [conversation_id]
    where = "conversation_id = ?"
    if before:
        where += " AND id < ?"
        args.append(before)
    rows = _rows(conn, f"SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT ?", (*args, limit + 1))
    has_older = len(rows) > limit
    items = list(reversed(rows[:limit]))
    return {"items": items, "has_older": has_older, "oldest_id": items[0]["id"] if items else None}


def send_message(conn: sqlite3.Connection, conversation_id: int, user_id: int, body: str, now: datetime) -> int:
    conv = _participant(conn, conversation_id, user_id)
    if not conv["can_send"]:
        raise MessagingError(conv["closed_code"], conv["closed_reason"])
    text = rules.check_text(body, rules.MAX_MESSAGE)
    last = conn.execute(
        "SELECT body, created_at FROM messages WHERE conversation_id = ? AND sender_user_id = ? ORDER BY id DESC LIMIT 1",
        (conversation_id, user_id),
    ).fetchone()
    if last and last["body"] == text and (now - _parse(last["created_at"])).total_seconds() < rules.DUPLICATE_WINDOW_SECONDS:
        raise MessagingError("duplicate_message", "Это сообщение уже отправлено.")
    unanswered = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND sender_user_id = ? AND id >"
        " COALESCE((SELECT MAX(id) FROM messages WHERE conversation_id = ? AND sender_user_id <> ?), 0)",
        (conversation_id, user_id, conversation_id, user_id),
    ).fetchone()[0]
    if unanswered >= rules.MAX_UNANSWERED:
        raise MessagingError(
            "wait_reply", f"Дождитесь ответа: вы уже отправили {rules.MAX_UNANSWERED} сообщения подряд."
        )
    stamp = iso(now)
    cur = conn.execute(
        "INSERT INTO messages(conversation_id, sender_user_id, body, created_at) VALUES (?,?,?,?)",
        (conversation_id, user_id, text, stamp),
    )
    conn.execute("UPDATE conversations SET last_message_at = ? WHERE id = ?", (stamp, conversation_id))
    conn.commit()
    return int(cur.lastrowid or 0)


def mark_read(conn: sqlite3.Connection, conversation_id: int, user_id: int, now: datetime) -> int:
    """Отметить прочитанными входящие сообщения треда. Возвращает, сколько отмечено."""
    _participant(conn, conversation_id, user_id)
    cur = conn.execute(
        "UPDATE messages SET read_at = ? WHERE conversation_id = ? AND sender_user_id <> ? AND read_at IS NULL",
        (iso(now), conversation_id, user_id),
    )
    conn.commit()
    return cur.rowcount


def unread_total(conn: sqlite3.Connection, user_id: int) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM messages m JOIN conversations c ON c.id = m.conversation_id"
        " WHERE (c.employer_user_id = :me OR c.seeker_user_id = :me) AND m.sender_user_id <> :me AND m.read_at IS NULL",
        {"me": user_id},
    ).fetchone()[0])


def list_conversations(
    conn: sqlite3.Connection, user_id: int, *, unread_only: bool = False, page: int = 1, per_page: int = rules.LIST_PAGE
) -> dict[str, Any]:
    where = "(c.employer_user_id = :me OR c.seeker_user_id = :me)"
    if unread_only:
        where += f" AND {UNREAD_SQL} > 0"
    args = {"me": user_id}
    total = int(conn.execute(f"SELECT COUNT(*) FROM conversations c WHERE {where}", args).fetchone()[0])
    meta = _page(total, page, per_page)
    items = _rows(
        conn,
        "SELECT c.id, c.application_id, c.last_message_at, c.created_at,"
        " COALESCE(NULLIF(c.title, ''), v.profession, 'Вакансия') AS title,"
        " CASE WHEN c.employer_user_id = :me THEN su.name ELSE COALESCE(NULLIF(v.employer, ''), eu.name) END AS counterpart_name,"
        f" {UNREAD_SQL} AS unread,"
        " (SELECT body FROM messages m WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_body,"
        " (SELECT sender_user_id FROM messages m WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_sender,"
        " CASE WHEN a.withdrawn_at IS NOT NULL THEN 'withdrawn' ELSE a.status END AS app_status"
        " FROM conversations c"
        " JOIN users eu ON eu.id = c.employer_user_id JOIN users su ON su.id = c.seeker_user_id"
        " LEFT JOIN vacancies v ON v.id = c.vacancy_id LEFT JOIN applications a ON a.id = c.application_id"
        f" WHERE {where} ORDER BY COALESCE(c.last_message_at, c.created_at) DESC, c.id DESC LIMIT :limit OFFSET :offset",
        {**args, "limit": per_page, "offset": meta["offset"]},
    )
    return {**meta, "items": items}


# ------------------------------------------------------------------ блокировка и жалобы

def block(conn: sqlite3.Connection, conversation_id: int, user_id: int, now: datetime) -> None:
    conv = _participant(conn, conversation_id, user_id)
    conn.execute(
        "INSERT OR IGNORE INTO user_blocks(blocker_user_id, blocked_user_id, created_at) VALUES (?,?,?)",
        (user_id, conv["counterpart_id"], iso(now)),
    )
    conn.commit()


def unblock(conn: sqlite3.Connection, conversation_id: int, user_id: int) -> None:
    conv = _participant(conn, conversation_id, user_id)
    conn.execute(
        "DELETE FROM user_blocks WHERE blocker_user_id = ? AND blocked_user_id = ?", (user_id, conv["counterpart_id"])
    )
    conn.commit()


def report_message(
    conn: sqlite3.Connection, conversation_id: int, message_id: int, reporter_id: int, reason: str, comment: str,
    now: datetime,
) -> int:
    """Жалоба на сообщение собеседника. Запись попадает в reports (target_type='message'); в snapshot лежит копия
    сообщения, чтобы модерация видела текст, даже если переписку потом удалят. Повторная жалоба возвращает прежнюю."""
    _participant(conn, conversation_id, reporter_id)
    msg = conn.execute(
        "SELECT * FROM messages WHERE id = ? AND conversation_id = ?", (message_id, conversation_id)
    ).fetchone()
    if msg is None:
        raise NotFound("Сообщение не найдено")
    if msg["sender_user_id"] == reporter_id:
        raise MessagingError("own_message", "Это ваше сообщение: пожаловаться можно на сообщения собеседника.")
    if reason not in rules.REPORT_REASONS:
        raise MessagingError("bad_reason", "Выберите причину из списка.")
    note = rules.clean_text(comment)
    if len(note) > rules.MAX_REPORT_COMMENT:
        raise MessagingError("too_long", f"Комментарий слишком длинный: не больше {rules.MAX_REPORT_COMMENT} знаков.")
    if reason == "Другое" and len(note) < 5:
        raise MessagingError("empty", "Опишите, что не так, хотя бы в паре слов.")
    again = conn.execute(
        "SELECT id FROM reports WHERE reporter_user_id = ? AND target_type = 'message' AND target_id = ?",
        (reporter_id, message_id),
    ).fetchone()
    if again:
        return int(again[0])
    snapshot = {
        "conversation_id": conversation_id, "message_id": message_id, "sender_user_id": msg["sender_user_id"],
        "body": msg["body"], "sent_at": msg["created_at"], "comment": note,
    }
    cur = conn.execute(
        "INSERT INTO reports(reporter_user_id, target_type, target_id, reason, status, created_at, snapshot)"
        " VALUES (?, 'message', ?, ?, 'new', ?, ?)",
        (reporter_id, message_id, reason + (f": {note}" if note else ""), iso(now), json.dumps(snapshot, ensure_ascii=False)),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


# ------------------------------------------------------------------ настройки писем

def get_prefs(conn: sqlite3.Connection, user_id: int) -> dict[str, bool]:
    row = conn.execute("SELECT notify_responses, notify_messages FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return {"responses": True, "messages": True}
    return {"responses": bool(row[0]), "messages": bool(row[1])}


def set_prefs(conn: sqlite3.Connection, user_id: int, *, responses: bool, messages: bool) -> None:
    conn.execute(
        "UPDATE users SET notify_responses = ?, notify_messages = ? WHERE id = ?", (int(responses), int(messages), user_id)
    )
    conn.commit()


def unsubscribe(conn: sqlite3.Connection, user_id: int, kind: str) -> None:
    prefs = get_prefs(conn, user_id)
    if kind in ("responses", "all"):
        prefs["responses"] = False
    if kind in ("messages", "all"):
        prefs["messages"] = False
    set_prefs(conn, user_id, responses=prefs["responses"], messages=prefs["messages"])
