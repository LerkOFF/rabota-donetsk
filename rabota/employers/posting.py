"""Размещение вакансии гостем: черновик, код из письма, автоматический аккаунт работодателя.

Порядок:
1. Гость заполняет одну форму (вакансия, почта, согласие на обработку данных). Вакансия проверяется и сохраняется
   как черновик (`post_drafts`), на почту уходит шестизначный код. Аккаунт и вакансия ещё не создаются,
   очередь модерации спамом не засоряется.
2. Гость вводит код (15 минут, 5 попыток, новый код не чаще раза в минуту и не более 5 раз на черновик).
3. После верного кода: у новой почты создаётся аккаунт работодателя (почта подтверждена кодом, согласие записано,
   пароль случайный: задать свой можно через "Забыли пароль"), у существующего работодателя вакансия
   привязывается к его аккаунту. Вакансия получает автомодерацию (rabota.employers.automod), посетитель входит.

Почта соискателя или администратора не превращается в работодателя: вакансия остаётся без владельца, как раньше.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from rabota import cabinet
from rabota.accounts import codes, passwords, service
from rabota.accounts.mailer import SUBJECTS, Mail, render_mail
from rabota.accounts.tokens import hash_token, iso
from rabota.employers import automod
from rabota.web import queries as q
from rabota.web.postform import validate_post

CODE_TTL = timedelta(minutes=15)
DRAFT_KEEP = timedelta(days=1)
MAX_ATTEMPTS = codes.MAX_ATTEMPTS
RESEND_COOLDOWN = codes.RESEND_COOLDOWN
MAX_SENDS = 5

SUBJECTS.setdefault("post_code", "Код {code} для публикации вакансии на сайте Работа Донецк")


def new_token() -> str:
    return secrets.token_urlsafe(24)


def _code_hash(secret: bytes, draft_id: int, code: str) -> str:
    return codes.hash_code(secret, draft_id, "post", code)


def post_mail(site_url: str, email: str, name: str, code: str) -> Mail:
    return render_mail("post_code", email, name=name or "коллега", code=code, site_url=site_url,
                       ttl_minutes=int(CODE_TTL.total_seconds() // 60))


def create_draft(
    conn: sqlite3.Connection, secret: bytes, *, email: str, form: dict[str, str], consent_at: datetime, ip: str,
    now: datetime,
) -> tuple[str, str]:
    """Сохранить черновик и выпустить код. Возвращает (токен для адреса страницы, код для письма)."""
    token = new_token()
    code = codes.new_code()
    cur = conn.execute(
        "INSERT INTO post_drafts(token_hash, email, payload, code_hash, expires_at, created_at, last_sent_at, consent_at, ip)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (hash_token(token), email, json.dumps(form, ensure_ascii=False), "", iso(now + CODE_TTL), iso(now), iso(now),
         iso(consent_at), ip[:64]),
    )
    draft_id = int(cur.lastrowid or 0)
    conn.execute("UPDATE post_drafts SET code_hash = ? WHERE id = ?", (_code_hash(secret, draft_id, code), draft_id))
    conn.commit()
    return token, code


def get_draft(conn: sqlite3.Connection, token: str, now: datetime) -> dict[str, Any] | None:
    """Черновик по токену из адреса. Использованный и старше суток не отдаётся."""
    if not token or len(token) > 80:
        return None
    row = conn.execute("SELECT * FROM post_drafts WHERE token_hash = ?", (hash_token(token),)).fetchone()
    if row is None or row["used_at"] or row["created_at"] < iso(now - DRAFT_KEEP):
        return None
    draft = dict(row)
    draft["form"] = json.loads(draft["payload"])
    draft["expired"] = draft["expires_at"] <= iso(now)
    return draft


def resend_code(conn: sqlite3.Connection, secret: bytes, token: str, now: datetime) -> tuple[str, str | None]:
    """Новый код. Статус: ok, none (нет черновика), cooldown (не прошла минута), limit (много писем). Код только при ok."""
    draft = get_draft(conn, token, now)
    if draft is None:
        return "none", None
    if draft["last_sent_at"] > iso(now - RESEND_COOLDOWN):
        return "cooldown", None
    if draft["sent_count"] >= MAX_SENDS:
        return "limit", None
    code = codes.new_code()
    conn.execute(
        "UPDATE post_drafts SET code_hash = ?, attempts = 0, sent_count = sent_count + 1, last_sent_at = ?, expires_at = ?"
        " WHERE id = ?",
        (_code_hash(secret, draft["id"], code), iso(now), iso(now + CODE_TTL), draft["id"]),
    )
    conn.commit()
    return "ok", code


def check_code(
    conn: sqlite3.Connection, secret: bytes, token: str, code: str, now: datetime
) -> tuple[str, dict[str, Any] | None]:
    """Проверить код. Статус: ok, none, expired, locked (попытки кончились), bad. Верный код расходуется."""
    import hmac

    draft = get_draft(conn, token, now)
    if draft is None:
        return "none", None
    if draft["expired"]:
        return "expired", None
    spent = conn.execute(
        "UPDATE post_drafts SET attempts = attempts + 1 WHERE id = ? AND used_at IS NULL AND attempts < ?",
        (draft["id"], MAX_ATTEMPTS),
    )
    conn.commit()
    if spent.rowcount != 1:
        return "locked", None
    attempts = draft["attempts"] + 1
    if hmac.compare_digest(draft["code_hash"], _code_hash(secret, draft["id"], code)):
        done = conn.execute("UPDATE post_drafts SET used_at = ? WHERE id = ? AND used_at IS NULL", (iso(now), draft["id"]))
        conn.commit()
        return ("ok", draft) if done.rowcount == 1 else ("none", None)
    return ("locked" if attempts >= MAX_ATTEMPTS else "bad"), None


def purge(conn: sqlite3.Connection, now: datetime) -> int:
    cur = conn.execute("DELETE FROM post_drafts WHERE created_at < ?", (iso(now - DRAFT_KEEP),))
    conn.commit()
    return cur.rowcount


# ------------------------------------------------------------------ после верного кода

@dataclass
class Published:
    kind: str  # employer, anonymous, blocked, invalid
    vacancy_id: int | None = None
    user: dict[str, Any] | None = None
    new_account: bool = False
    session: str = ""
    session_expires: datetime | None = None
    decision: str = ""


def _owner_for(conn: sqlite3.Connection, email: str, name: str, phone: str, consent_at: str, now: datetime
               ) -> tuple[str, dict[str, Any] | None, bool]:
    """Аккаунт для вакансии: (вид, пользователь, создан ли новый). Вид: employer, anonymous, blocked."""
    row = service.get_user_by_email(conn, email)
    random_hash = passwords.hash_password(secrets.token_urlsafe(24))
    if row is None:
        try:
            cur = conn.execute(
                "INSERT INTO users(email, password_hash, role, name, phone, consent_at, email_verified_at, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (email, random_hash, "employer", name, phone or None, consent_at, iso(now), iso(now)),
            )
            conn.commit()
        except sqlite3.IntegrityError:  # гонка двух подтверждений с одной почтой
            conn.rollback()
            return _owner_for(conn, email, name, phone, consent_at, now)
        return "employer", service.get_user(conn, int(cur.lastrowid or 0)), True
    if row["status"] == "blocked":
        return "blocked", None, False
    if not row["email_verified_at"]:
        # почту занял кто-то, кто её не подтвердил: хозяин только что подтвердил её кодом, прежний пароль и сессии гасим
        conn.execute(
            "UPDATE users SET password_hash = ?, role = 'employer', name = COALESCE(NULLIF(name, ''), ?), consent_at = ?,"
            " email_verified_at = ? WHERE id = ? AND email_verified_at IS NULL",
            (random_hash, name, consent_at, iso(now), row["id"]),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        conn.commit()
        return "employer", service.get_user(conn, int(row["id"])), True
    if row["role"] == "employer":
        return "employer", service.get_user(conn, int(row["id"])), False
    return "anonymous", None, False


def publish(
    conn: sqlite3.Connection, draft: dict[str, Any], now: datetime, *, premoderate: bool = True, ip: str = "",
    user_agent: str = "",
) -> Published:
    """Создать аккаунт (или найти), вакансию, применить автомодерацию и открыть сессию."""
    vacancy, cleaned, errors = validate_post(draft["form"])
    if errors or vacancy is None:
        return Published("invalid")
    email = draft["email"]
    name = service.clean_name(cleaned.get("employer") or "") or email.split("@")[0]
    kind, user, new_account = _owner_for(conn, email, name, cleaned.get("phone", ""), draft["consent_at"], now)
    if kind == "blocked":
        return Published("blocked")
    if kind == "anonymous" or user is None:
        vacancy_id = q.add_site_vacancy(conn, vacancy, premoderate=premoderate)
        result = automod.moderate_new(conn, vacancy_id, now, premoderate=premoderate)
        return Published("anonymous", vacancy_id, decision=result.decision)
    company_id = cabinet.ensure_company(conn, user, vacancy.employer or "", vacancy.city or "")
    company = cabinet.get_company(conn, int(user["id"])) or {}
    vacancy.employer = vacancy.employer or company.get("name") or None
    vacancy_id = q.add_site_vacancy(
        conn, vacancy, premoderate=premoderate, owner_user_id=int(user["id"]), company_id=company_id)
    result = automod.moderate_new(conn, vacancy_id, now, premoderate=premoderate)
    raw = service.create_session(conn, int(user["id"]), now, ip, user_agent)
    return Published("employer", vacancy_id, user, new_account, raw, now + service.SESSION_TTL, result.decision)


def email_hint(email: str) -> str:
    """Почта для показа на странице кода: первая буква и домен (письмо ушло на этот адрес)."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else email


__all__ = ["Published", "check_code", "create_draft", "get_draft", "post_mail", "publish", "purge", "resend_code"]
