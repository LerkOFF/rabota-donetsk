"""Письма о новых откликах и сообщениях. В письме только факт и ссылка: ни имён, ни контактов, ни текста сообщений.
Отписка: подписанная ссылка без входа в аккаунт (GET показывает вопрос, отключает только POST с CSRF-токеном)."""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from rabota.accounts.mailer import EMAIL_TEMPLATES_DIR, SITE_NAME, Mail

SUBJECTS = {
    "new_response": "Новый отклик на вакансию «{title}»",
    "new_message": "Новое сообщение по вакансии «{title}»",
}
KINDS = ("responses", "messages", "all")
MAX_TOKEN = 120

_env = Environment(
    loader=FileSystemLoader(str(Path(EMAIL_TEMPLATES_DIR))), autoescape=False, undefined=StrictUndefined,
    keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True,
)


def _sign(user_id: int, kind: str, secret: bytes) -> str:
    return hmac.new(secret, f"unsub:{user_id}:{kind}".encode(), hashlib.sha256).hexdigest()[:32]


def unsubscribe_token(user_id: int, kind: str, secret: bytes) -> str:
    return f"{user_id}.{kind}.{_sign(user_id, kind, secret)}"


def parse_unsubscribe_token(token: str, secret: bytes) -> tuple[int, str] | None:
    if not token or len(token) > MAX_TOKEN:
        return None
    parts = token.split(".")
    if len(parts) != 3 or not parts[0].isdigit() or parts[1] not in KINDS:
        return None
    user_id = int(parts[0])
    if not hmac.compare_digest(_sign(user_id, parts[1], secret), parts[2]):
        return None
    return user_id, parts[1]


def _title(value: str | None) -> str:
    return " ".join((value or "Вакансия").split())[:80] or "Вакансия"


def _render(kind: str, recipient: sqlite3.Row, mail_kind: str, title: str, link: str, site_url: str, secret: bytes) -> Mail:
    body = _env.get_template(f"{kind}.txt").render(
        site_name=SITE_NAME, site_url=site_url, title=title, link=link,
        settings_url=f"{site_url}/notifications",
        unsubscribe_url=f"{site_url}/notifications/unsubscribe/{unsubscribe_token(recipient['id'], mail_kind, secret)}",
    )
    return Mail(to=recipient["email"], subject=SUBJECTS[kind].format(title=title), body=body)


def _recipient(conn: sqlite3.Connection, user_id: int, pref: str) -> sqlite3.Row | None:
    row = conn.execute(
        f"SELECT id, email, status, email_verified_at, {pref} AS wants FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None or row["status"] != "active" or not row["email_verified_at"] or not row["wants"]:
        return None
    return row


def response_mail(conn: sqlite3.Connection, application_id: int, *, site_url: str, secret: bytes) -> Mail | None:
    """Письмо работодателю о новом отклике или None (отключено, аккаунт закрыт, отклика нет)."""
    row = conn.execute(
        "SELECT v.owner_user_id AS employer_id, v.profession AS title FROM applications a"
        " JOIN vacancies v ON v.id = a.vacancy_id WHERE a.id = ?",
        (application_id,),
    ).fetchone()
    if row is None or row["employer_id"] is None:
        return None
    recipient = _recipient(conn, row["employer_id"], "notify_responses")
    if recipient is None:
        return None
    return _render(
        "new_response", recipient, "responses", _title(row["title"]), f"{site_url}/responses/{application_id}",
        site_url, secret,
    )


def message_mail(
    conn: sqlite3.Connection, conversation_id: int, message_id: int, *, site_url: str, secret: bytes
) -> Mail | None:
    """Письмо получателю нового сообщения. Шлётся только за первое непрочитанное сообщение: пока собеседник не
    открыл переписку, следующие сообщения новых писем не вызывают."""
    row = conn.execute(
        "SELECT m.sender_user_id, c.employer_user_id, c.seeker_user_id,"
        " COALESCE(NULLIF(c.title, ''), v.profession, 'Вакансия') AS title"
        " FROM messages m JOIN conversations c ON c.id = m.conversation_id"
        " LEFT JOIN vacancies v ON v.id = c.vacancy_id WHERE m.id = ? AND c.id = ?",
        (message_id, conversation_id),
    ).fetchone()
    if row is None:
        return None
    from_employer = row["sender_user_id"] == row["employer_user_id"]
    recipient_id = row["seeker_user_id"] if from_employer else row["employer_user_id"]
    recipient = _recipient(conn, recipient_id, "notify_messages")
    if recipient is None:
        return None
    unread = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND sender_user_id <> ? AND read_at IS NULL",
        (conversation_id, recipient_id),
    ).fetchone()[0]
    if unread != 1:
        return None
    body = _env.get_template("new_message.txt").render(
        site_name=SITE_NAME, site_url=site_url, title=_title(row["title"]), link=f"{site_url}/messages/{conversation_id}",
        sender="работодатель" if from_employer else "соискатель", settings_url=f"{site_url}/notifications",
        unsubscribe_url=f"{site_url}/notifications/unsubscribe/{unsubscribe_token(recipient['id'], 'messages', secret)}",
    )
    return Mail(to=recipient["email"], subject=SUBJECTS["new_message"].format(title=_title(row["title"])), body=body)
