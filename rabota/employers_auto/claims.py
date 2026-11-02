"""Заявка «Это моя компания»: работодатель забирает автокарточку.

Путь заявки:
1. Вошедший работодатель указывает рабочую почту на домене компании или телефон из вакансий компании.
2. Почта: на неё уходит шестизначный код (заявка `sent`), после верного кода заявка уходит на проверку (`review`).
   Телефон: заявка сразу `review`, администратор звонит или пишет по номеру.
3. Администратор подтверждает (`confirmed`), отклоняет (`rejected`) или просит пояснения (заявка остаётся `review`).
4. После подтверждения: у компании появляется владелец, `claimed_at` и отметка проверенного работодателя, все её
   вакансии получают владельца и управляются в кабинете. Происхождение карточки (`origin = auto`) не меняется.

Одна карточка - один владелец. Заявка на карточку, у которой уже есть владелец или другая открытая заявка, помечается
спором (`dispute`) и решается только вручную. Лимиты: заявок в сутки и открытых одновременно на аккаунт (настройки админки),
паузы и число попыток кода, повторная заявка на ту же компанию после отказа не раньше чем через неделю.

Функции не отправляют письма: письмо собирается и возвращается в результате, отправляет вызывающий.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from rabota.accounts.mailer import SITE_NAME, Mail
from rabota.accounts.tokens import iso
from rabota.admin import audit, settings
from rabota.admin.common import clean_line, clean_text

STATUSES = {"sent": "Отправлена", "review": "На проверке", "confirmed": "Подтверждена", "rejected": "Отклонена"}
OPEN = ("sent", "review")
METHODS = {"email": "Рабочая почта", "phone": "Телефон"}

CODE_LENGTH = 6
CODE_TTL = timedelta(minutes=15)
MAX_ATTEMPTS = 5
RESEND_COOLDOWN = timedelta(seconds=60)
MAX_CODES_PER_CLAIM = 5
REJECT_COOLDOWN = timedelta(days=7)
MESSAGE_LIMIT = 500
NOTE_LIMIT = 500

# бесплатные почтовые сервисы: адрес на них не доказывает связь с компанией
FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "mail.ru", "inbox.ru", "list.ru", "bk.ru", "internet.ru", "yandex.ru", "yandex.com",
    "ya.ru", "yandex.ua", "yandex.kz", "yandex.by", "rambler.ru", "lenta.ru", "autorambler.ru", "ro.ru", "outlook.com",
    "hotmail.com", "live.com", "icloud.com", "me.com", "yahoo.com", "ymail.com", "proton.me", "protonmail.com", "tutanota.com",
    "ukr.net", "i.ua", "meta.ua", "bigmir.net", "tut.by", "mail.com", "gmx.com", "aol.com", "zoho.com", "qip.ru",
    "donpac.ru", "dnr.ru",
})
_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+)$")


class ClaimError(Exception):
    """Заявку нельзя принять или обработать. `code` для тестов и логики, `text` показывается человеку."""

    def __init__(self, code: str, text: str):
        super().__init__(text)
        self.code = code
        self.text = text


@dataclass
class Outcome:
    claim_id: int
    status: str
    mail: Mail | None = None
    mails: list[Mail] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ данные компании

def normalize_phone(raw: str | None) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return "+7" + digits


def host_of(site: str | None) -> str:
    text = (site or "").strip()
    if not text:
        return ""
    host = urlsplit(text if "//" in text else "//" + text).hostname or ""
    host = host.lower().strip(".")
    return host.removeprefix("www.")


def is_free_mail(domain: str) -> bool:
    d = domain.lower().strip(".")
    return d in FREE_MAIL


def company_domains(conn: sqlite3.Connection, company: dict[str, Any]) -> set[str]:
    """Домены компании: сайт карточки и домены почт из её вакансий (бесплатные почтовики не считаются)."""
    out: set[str] = set()
    site = host_of(company.get("site"))
    if site and not is_free_mail(site):
        out.add(site)
    rows = conn.execute("SELECT contacts FROM vacancies WHERE company_id = ? AND contacts LIKE '%@%' LIMIT 300", (company["id"],))
    for r in rows:
        try:
            email = (json.loads(r[0] or "{}") or {}).get("email") or ""
        except (TypeError, ValueError):
            continue
        m = _EMAIL.match(str(email).strip())
        if m and not is_free_mail(m.group(1)):
            out.add(m.group(1).lower())
    return out


def company_phones(conn: sqlite3.Connection, company: dict[str, Any]) -> set[str]:
    """Телефоны компании: из карточки и из её вакансий."""
    out: set[str] = set()
    own = normalize_phone(company.get("phone"))
    if own:
        out.add(own)
    rows = conn.execute("SELECT contacts FROM vacancies WHERE company_id = ? AND contacts LIKE '%phones%' LIMIT 300", (company["id"],))
    for r in rows:
        try:
            phones = (json.loads(r[0] or "{}") or {}).get("phones") or []
        except (TypeError, ValueError):
            continue
        for p in phones:
            n = normalize_phone(str(p))
            if n:
                out.add(n)
    return out


def domain_matches(domain: str, known: set[str]) -> bool:
    d = domain.lower()
    return any(d == k or d.endswith("." + k) or k.endswith("." + d) for k in known)


def mask_phone(phone: str) -> str:
    return phone[:5] + "***" + phone[-2:] if len(phone) >= 9 else phone


# ------------------------------------------------------------------ чтение

def get(conn: sqlite3.Connection, claim_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM company_claims WHERE id = ?", (claim_id,)).fetchone()
    return dict(row) if row else None


def latest_for(conn: sqlite3.Connection, company_id: int, user_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM company_claims WHERE company_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1", (company_id, user_id)
    ).fetchone()
    return dict(row) if row else None


def for_user(conn: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT k.*, c.name AS company_name FROM company_claims k JOIN companies c ON c.id = k.company_id "
        "WHERE k.user_id = ? ORDER BY k.id DESC LIMIT 50", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def open_for_company(conn: sqlite3.Connection, company_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM company_claims WHERE company_id = ? AND status IN ('sent', 'review') ORDER BY id", (company_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def can_claim(company: dict[str, Any] | None) -> bool:
    """Карточку можно забрать: она есть, не заблокирована и не слита."""
    return bool(company) and not company.get("blocked_at") and not company.get("merged_into")  # type: ignore[union-attr]


# ------------------------------------------------------------------ код

def new_code() -> str:
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def clean_code(raw: str) -> str | None:
    value = re.sub(r"[\s\-.]", "", raw or "")
    return value if re.fullmatch(rf"[0-9]{{{CODE_LENGTH}}}", value) else None


def hash_code(secret: bytes, claim_id: int, user_id: int, code: str) -> str:
    return hmac.new(secret, f"claim:{claim_id}:{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


def code_mail(to: str, company_name: str, code: str, site_url: str) -> Mail:
    body = (
        f"Здравствуйте!\n\nКод для заявки на карточку «{company_name}» на сайте {SITE_NAME}: {code}\n\n"
        "Код действует 15 минут. Введите его на странице заявки.\n"
        "Если вы не отправляли заявку, просто проигнорируйте письмо: без кода она не будет рассмотрена.\n\n"
        f"{site_url}\n"
    )
    return Mail(to=to, subject=f"Код {code} для заявки на карточку компании", body=body)


def _issue_code(conn: sqlite3.Connection, secret: bytes, claim: dict[str, Any], now: datetime) -> str:
    code = new_code()
    conn.execute(
        "UPDATE company_claims SET code_hash = ?, code_expires_at = ?, code_attempts = 0, code_sent_at = ?, "
        "code_sent_count = code_sent_count + 1, updated_at = ? WHERE id = ?",
        (hash_code(secret, claim["id"], claim["user_id"], code), iso(now + CODE_TTL), iso(now), iso(now), claim["id"]),
    )
    return code


def check_code(conn: sqlite3.Connection, secret: bytes, claim_id: int, user_id: int, raw: str, now: datetime) -> str:
    """ok, format, none (кода нет или заявка не ждёт кода), expired, locked, bad. Верный код переводит заявку на проверку."""
    claim = get(conn, claim_id)
    if claim is None or claim["user_id"] != user_id or claim["status"] != "sent" or claim["method"] != "email":
        return "none"
    code = clean_code(raw)
    if code is None:
        return "format"
    if not claim["code_hash"]:
        return "none"
    if claim["code_expires_at"] and claim["code_expires_at"] <= iso(now):
        return "expired"
    spent = conn.execute(
        "UPDATE company_claims SET code_attempts = code_attempts + 1 WHERE id = ? AND code_attempts < ?", (claim_id, MAX_ATTEMPTS))
    conn.commit()
    if spent.rowcount != 1:
        return "locked"
    if hmac.compare_digest(claim["code_hash"], hash_code(secret, claim_id, user_id, code)):
        conn.execute(
            "UPDATE company_claims SET status = 'review', code_ok_at = ?, code_hash = NULL, updated_at = ? "
            "WHERE id = ? AND status = 'sent'", (iso(now), iso(now), claim_id))
        conn.commit()
        return "ok"
    if claim["code_attempts"] + 1 >= MAX_ATTEMPTS:
        return "locked"
    return "bad"


def resend_code(conn: sqlite3.Connection, secret: bytes, claim_id: int, user_id: int, site_url: str, now: datetime) -> Outcome:
    claim = get(conn, claim_id)
    if claim is None or claim["user_id"] != user_id or claim["status"] != "sent" or claim["method"] != "email":
        raise ClaimError("none", "Для этой заявки код не нужен.")
    if claim["code_sent_at"] and claim["code_sent_at"] > iso(now - RESEND_COOLDOWN):
        raise ClaimError("cooldown", "Новый код можно запросить через минуту.")
    if claim["code_sent_count"] >= MAX_CODES_PER_CLAIM:
        raise ClaimError("limit", "Кодов для этой заявки больше нельзя: отправьте заявку заново через сутки или выберите телефон.")
    company = conn.execute("SELECT name FROM companies WHERE id = ?", (claim["company_id"],)).fetchone()
    code = _issue_code(conn, secret, claim, now)
    conn.commit()
    return Outcome(claim_id, "sent", code_mail(claim["contact"], company["name"] if company else "", code, site_url))


# ------------------------------------------------------------------ подача

def _limit(conn: sqlite3.Connection, key: str) -> int:
    return int(settings.effective(conn, key))


def validate_contact(conn: sqlite3.Connection, company: dict[str, Any], method: str, raw: str) -> tuple[str, str, int, int]:
    """(контакт, домен почты, домен совпал, телефон совпал). Ошибка: ClaimError с текстом для формы."""
    value = clean_line(raw, 120)
    if method == "email":
        m = _EMAIL.match(value)
        if not m:
            raise ClaimError("email", "Проверьте адрес почты: он должен выглядеть как name@company.ru.")
        domain = m.group(1).lower()
        if is_free_mail(domain):
            raise ClaimError("free_mail", "Нужна рабочая почта на домене компании, а не mail.ru, gmail.com и подобные. "
                                          "Если рабочей почты нет, выберите телефон.")
        known = company_domains(conn, company)
        if known and not domain_matches(domain, known):
            raise ClaimError("domain", "Домен почты не совпадает с сайтом и почтой компании из вакансий ("
                             + ", ".join(sorted(known)[:3]) + "). Укажите почту на этом домене или выберите телефон.")
        return value.lower(), domain, 1 if known else 0, 0
    if method == "phone":
        phone = normalize_phone(value)
        if not phone:
            raise ClaimError("phone", "Телефон в формате +7 949 123-45-67.")
        known_phones = company_phones(conn, company)
        if known_phones and phone not in known_phones:
            raise ClaimError("phone_unknown", "Этого номера нет в вакансиях компании. Укажите номер из вакансии "
                             "или выберите рабочую почту.")
        return phone, "", 0, 1 if known_phones else 0
    raise ClaimError("method", "Выберите способ подтверждения.")


def submit(
    conn: sqlite3.Connection, secret: bytes, company: dict[str, Any], user: dict[str, Any], *, method: str, contact: str,
    message: str, ip: str, site_url: str, now: datetime,
) -> Outcome:
    """Принять заявку. Проверяет права, лимиты и контакт. Для почты возвращает письмо с кодом."""
    if not can_claim(company):
        raise ClaimError("gone", "Эту карточку забрать нельзя.")
    if user.get("role") != "employer":
        raise ClaimError("role", "Заявку подаёт работодатель: войдите в аккаунт работодателя или зарегистрируйте новый.")
    uid = int(user["id"])
    if company.get("owner_user_id") == uid:
        raise ClaimError("own", "Эта карточка уже ваша.")
    prior = latest_for(conn, int(company["id"]), uid)
    if prior and prior["status"] in OPEN:
        raise ClaimError("open", "Заявка на эту компанию уже отправлена и ждёт решения.")
    if prior and prior["status"] == "rejected" and prior["decided_at"] and prior["decided_at"] > iso(now - REJECT_COOLDOWN):
        raise ClaimError("cooldown", "Предыдущую заявку отклонили. Новую на эту компанию можно подать через неделю "
                                     "или написать нам: приложите подтверждение.")
    open_now = int(conn.execute(
        "SELECT COUNT(*) FROM company_claims WHERE user_id = ? AND status IN ('sent','review')", (uid,)).fetchone()[0])
    if open_now >= _limit(conn, "claims_open_max"):
        raise ClaimError("open_max", f"Одновременно можно ждать решения по {_limit(conn, 'claims_open_max')} заявкам. "
                                     "Дождитесь ответа по текущим.")
    today = int(conn.execute(
        "SELECT COUNT(*) FROM company_claims WHERE user_id = ? AND created_at > ?", (uid, iso(now - timedelta(days=1)))).fetchone()[0])
    if today >= _limit(conn, "claims_per_day"):
        raise ClaimError("per_day", "Достигнут суточный лимит заявок. Попробуйте завтра.")
    value, domain, domain_match, phone_match = validate_contact(conn, company, method, contact)
    text = clean_text(message, MESSAGE_LIMIT)
    others = open_for_company(conn, int(company["id"]))
    dispute = 1 if (company.get("owner_user_id") or any(o["user_id"] != uid for o in others)) else 0
    status = "sent" if method == "email" else "review"
    cur = conn.execute(
        "INSERT INTO company_claims(company_id, user_id, status, method, contact, email_domain, domain_match, phone_match, "
        "dispute, message, ip, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (company["id"], uid, status, method, value, domain, domain_match, phone_match, dispute, text, ip[:64], iso(now), iso(now)),
    )
    claim_id = int(cur.lastrowid or 0)
    if dispute:
        conn.execute("UPDATE company_claims SET dispute = 1 WHERE company_id = ? AND status IN ('sent','review')", (company["id"],))
    mail = None
    if method == "email":
        claim = get(conn, claim_id) or {}
        code = _issue_code(conn, secret, claim, now)
        mail = code_mail(value, company["name"], code, site_url)
    conn.commit()
    audit.log(conn, user, "claim.submit", "claim", claim_id, {"company": company["id"], "method": method, "dispute": dispute}, ip, now)
    return Outcome(claim_id, status, mail, detail={"dispute": bool(dispute)})


def add_note(conn: sqlite3.Connection, claim_id: int, user_id: int, text: str, now: datetime) -> None:
    claim = get(conn, claim_id)
    if claim is None or claim["user_id"] != user_id or claim["status"] not in OPEN:
        raise ClaimError("none", "Заявка закрыта: дополнить её нельзя.")
    extra = clean_text(text, MESSAGE_LIMIT)
    if not extra:
        raise ClaimError("empty", "Напишите, чем подтвердить, что компания ваша.")
    joined = (claim["message"] + "\n\n" if claim["message"] else "") + extra
    conn.execute("UPDATE company_claims SET message = ?, updated_at = ? WHERE id = ?", (joined[-2000:], iso(now), claim_id))
    conn.commit()


def withdraw(conn: sqlite3.Connection, claim_id: int, user_id: int, now: datetime) -> bool:
    cur = conn.execute(
        "UPDATE company_claims SET status = 'rejected', admin_note = 'Отозвана заявителем', decided_at = ?, updated_at = ?, "
        "code_hash = NULL WHERE id = ? AND user_id = ? AND status IN ('sent','review')", (iso(now), iso(now), claim_id, user_id))
    conn.commit()
    return cur.rowcount > 0


# ------------------------------------------------------------------ решения администратора

def _decision_mail(user_email: str, subject: str, lines: list[str], site_url: str) -> Mail:
    body = "Здравствуйте!\n\n" + "\n".join(lines) + f"\n\n{site_url}\n"
    return Mail(to=user_email, subject=subject, body=body)


def _user(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT id, email, name, role FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def approve(
    conn: sqlite3.Connection, claim_id: int, actor: dict[str, Any], note: str, ip: str, site_url: str, now: datetime,
    *, transfer: bool = False,
) -> Outcome:
    """Подтвердить заявку: компания (или карточка пользователя, если она у него уже есть) получает владельца."""
    from rabota.employers_auto import merge as merging

    claim = get(conn, claim_id)
    if claim is None:
        raise ClaimError("none", "Заявка не найдена.")
    if claim["status"] != "review":
        raise ClaimError("state", "Подтвердить можно только заявку «на проверке»: заявитель ещё не ввёл код из письма." if claim["status"] == "sent"
                         else "Заявка уже закрыта.")
    company = conn.execute("SELECT * FROM companies WHERE id = ?", (claim["company_id"],)).fetchone()
    user = _user(conn, claim["user_id"])
    if company is None or user is None or not can_claim(dict(company)):
        raise ClaimError("gone", "Компании или заявителя больше нет, или карточка заблокирована.")
    company = dict(company)
    prev_owner = company.get("owner_user_id")
    if prev_owner and prev_owner != user["id"] and not transfer:
        raise ClaimError("owned", "У карточки уже есть владелец. Чтобы передать её заявителю, отметьте передачу.")
    stamp = iso(now)
    clean_note = clean_line(note, NOTE_LIMIT)
    target = int(company["id"])
    mine = conn.execute(
        "SELECT id FROM companies WHERE owner_user_id = ? AND id != ? AND merged_into IS NULL ORDER BY id LIMIT 1",
        (user["id"], target)).fetchone()
    merged_into = None
    if mine is not None and not prev_owner:
        # у заявителя уже есть своя карточка: собранная вливается в неё, чтобы у владельца была одна компания
        merging.merge_companies(conn, int(mine["id"]), target, actor, ip, now, commit=False)
        merged_into = int(mine["id"])
        target = merged_into
        conn.execute("UPDATE company_claims SET company_id = ? WHERE company_id = ? AND id != ?", (target, company["id"], claim_id))
        conn.execute("UPDATE company_claims SET company_id = ? WHERE id = ?", (target, claim_id))
    elif mine is not None:
        raise ClaimError("has_other", "У заявителя уже есть другая компания: передайте карточку после слияния вручную.")
    conn.execute(
        "UPDATE companies SET owner_user_id = ?, claimed_at = ?, verified = 1, verified_at = ?, verified_by = ?, verify_note = ? "
        "WHERE id = ?",
        (user["id"], stamp, stamp, actor.get("id"), f"Заявка №{claim_id} подтверждена", target),
    )
    conn.execute(
        "UPDATE vacancies SET owner_user_id = ? WHERE company_id = ? AND origin != 'direct' AND kind = 'vacancy'",
        (user["id"], target))
    conn.execute(
        "UPDATE company_claims SET status = 'confirmed', admin_note = ?, decided_by = ?, decided_at = ?, updated_at = ?, "
        "code_hash = NULL WHERE id = ?", (clean_note, actor.get("id"), stamp, stamp, claim_id))
    mails: list[Mail] = [_decision_mail(user["email"], f"Карточка компании «{company['name']}» теперь ваша", [
        f"Заявку на карточку «{company['name']}» подтвердили.",
        "Компания отмечена как проверенный работодатель, её вакансии появились в разделе «Мои вакансии».",
        "Добавьте логотип и описание в разделе «Компания».",
    ], site_url)]
    losers = conn.execute(
        "SELECT id, user_id FROM company_claims WHERE company_id = ? AND id != ? AND status IN ('sent','review')",
        (target, claim_id)).fetchall()
    for r in losers:
        conn.execute(
            "UPDATE company_claims SET status = 'rejected', admin_note = 'Карточку забрал другой заявитель', decided_by = ?, "
            "decided_at = ?, updated_at = ?, code_hash = NULL WHERE id = ?", (actor.get("id"), stamp, stamp, r["id"]))
        other = _user(conn, r["user_id"])
        if other:
            mails.append(_decision_mail(other["email"], "Заявка на карточку компании отклонена", [
                f"Заявку на карточку «{company['name']}» отклонили: карточку подтвердил другой заявитель.",
                "Если вы считаете, что это ошибка, подайте заявку заново и опишите, как подтвердить связь с компанией."], site_url))
    conn.commit()
    audit.log(conn, actor, "claim.approve", "claim", claim_id,
              {"company": target, "user": user["id"], "transfer": bool(prev_owner), "merged_into": merged_into, "note": clean_note}, ip, now)
    return Outcome(claim_id, "confirmed", mails[0], mails, {"company_id": target, "merged_into": merged_into})


def reject(conn: sqlite3.Connection, claim_id: int, actor: dict[str, Any], reason: str, ip: str, site_url: str, now: datetime) -> Outcome:
    claim = get(conn, claim_id)
    if claim is None:
        raise ClaimError("none", "Заявка не найдена.")
    if claim["status"] not in OPEN:
        raise ClaimError("state", "Заявка уже закрыта.")
    text = clean_line(reason, NOTE_LIMIT)
    if not text:
        raise ClaimError("reason", "Напишите причину: заявитель увидит её в письме и в профиле.")
    stamp = iso(now)
    conn.execute(
        "UPDATE company_claims SET status = 'rejected', admin_note = ?, decided_by = ?, decided_at = ?, updated_at = ?, "
        "code_hash = NULL WHERE id = ?", (text, actor.get("id"), stamp, stamp, claim_id))
    conn.commit()
    audit.log(conn, actor, "claim.reject", "claim", claim_id, {"reason": text}, ip, now)
    user = _user(conn, claim["user_id"])
    company = conn.execute("SELECT name FROM companies WHERE id = ?", (claim["company_id"],)).fetchone()
    mail = _decision_mail(user["email"], "Заявка на карточку компании отклонена", [
        f"Заявку на карточку «{company['name'] if company else ''}» отклонили.", f"Причина: {text}",
        "Вы можете подать заявку заново через неделю или раньше, если опишете, как подтвердить связь с компанией.",
    ], site_url) if user else None
    return Outcome(claim_id, "rejected", mail)


def request_docs(conn: sqlite3.Connection, claim_id: int, actor: dict[str, Any], text: str, ip: str, site_url: str, now: datetime) -> Outcome:
    claim = get(conn, claim_id)
    if claim is None:
        raise ClaimError("none", "Заявка не найдена.")
    if claim["status"] not in OPEN:
        raise ClaimError("state", "Заявка уже закрыта.")
    note = clean_line(text, NOTE_LIMIT)
    if not note:
        raise ClaimError("reason", "Напишите, что нужно прислать.")
    stamp = iso(now)
    conn.execute(
        "UPDATE company_claims SET docs_requested_at = ?, docs_note = ?, updated_at = ? WHERE id = ?", (stamp, note, stamp, claim_id))
    conn.commit()
    audit.log(conn, actor, "claim.docs", "claim", claim_id, {"note": note}, ip, now)
    user = _user(conn, claim["user_id"])
    mail = _decision_mail(user["email"], "Нужны пояснения по заявке на карточку компании", [
        "Чтобы рассмотреть заявку, нужны пояснения:", note,
        "Откройте страницу заявки на сайте и допишите ответ в поле «Дополнить заявку».",
    ], site_url) if user else None
    return Outcome(claim_id, claim["status"], mail)


# ------------------------------------------------------------------ удаление аккаунта

def release_owner(conn: sqlite3.Connection, user_id: int) -> int:
    """Аккаунт владельца удаляется: автокарточки и собранные вакансии остаются без владельца, а не удаляются с ним.
    Возвращает число освобождённых карточек. Не коммитит."""
    ids = [r[0] for r in conn.execute("SELECT id FROM companies WHERE owner_user_id = ? AND origin = 'auto'", (user_id,))]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    conn.execute(f"UPDATE vacancies SET owner_user_id = NULL WHERE owner_user_id = ? AND origin != 'direct' AND company_id IN ({marks})",
                 [user_id, *ids])
    conn.execute(
        f"UPDATE companies SET owner_user_id = NULL, claimed_at = NULL, verified = 0, verified_at = NULL, verified_by = NULL, "
        f"verify_note = NULL WHERE id IN ({marks})", ids)
    return len(ids)
