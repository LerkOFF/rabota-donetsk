"""Кабинет работодателя без веба: состояния вакансий, закрытие, поднятие, правка, удаление, карточка компании, сводка.

Все функции работают с вакансиями одного владельца (`owner_user_id`). Чужая и несуществующая вакансия даёт
`CabinetError` с кодом `not_found`: снаружи это всегда 404. Время передаётся явно, поэтому логику легко проверять.

Правила (подробно в docs/WEB.md):

- состояние вакансии для человека выводится из `moderation_status`, `status`, пометок и срока жизни;
- поднять (повторная публикация обнуляет срок и ставит вакансию выше в ленте) можно не чаще раза в сутки;
- правка опубликованной вакансии отправляет её на повторную проверку, только если изменились оплата, контакты
  или текст (профессия, организация, описание, требования).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from rabota import config, lifecycle
from rabota.accounts.service import valid_email
from rabota.messaging.rules import clean_text
from rabota.models import Vacancy
from rabota.web import format as fmt

PER_PAGE = 20
RAISE_INTERVAL = timedelta(days=1)
SOON_DAYS = 3  # за сколько дней до конца срока вакансия попадает в "скоро исчезнет"
MAX_LIST = 1000  # больше вакансий одного владельца в списке не разбирается

STATE_ORDER = ("published", "pending", "rejected", "closed", "expired", "hidden")
STATE_LABELS = {
    "published": "Опубликована",
    "pending": "На проверке",
    "rejected": "Отклонена",
    "closed": "Закрыта",
    "expired": "Срок истёк",
    "hidden": "Скрыта",
}
FILTER_LABELS = {
    "": "Все",
    "published": "Опубликованы",
    "pending": "На проверке",
    "rejected": "Отклонены",
    "closed": "Закрыты",
    "expired": "Срок истёк",
    "hidden": "Скрыты",
}

# какие поля вакансии относятся к группам повторной проверки
PAY_FIELDS = ("pay_min", "pay_max", "pay_period", "pay_daily")
TEXT_FIELDS = ("profession", "employer", "summary", "requirements")

COMPANY_LIMITS = {"name": 120, "city": 60, "site": 200, "description": 1500}


class CabinetError(Exception):
    """Ожидаемая ошибка с текстом для человека. code: not_found, not_allowed, too_soon."""

    def __init__(self, code: str, text: str):
        super().__init__(text)
        self.code = code
        self.text = text


@dataclass(frozen=True)
class State:
    key: str
    label: str
    note: str = ""
    days_left: int | None = None
    soon: bool = False
    expires_at: datetime | None = None


# ------------------------------------------------------------------ время

def _iso(now: datetime) -> str:
    return now.astimezone(UTC).replace(microsecond=0).isoformat()


def _seen(row: dict[str, Any]) -> datetime | None:
    return lifecycle.parse_iso(row.get("last_seen_at") or row.get("published_at"))


def _plain_days(n: int) -> str:
    return f"{n} {fmt.plural(n, 'день', 'дня', 'дней')}"


# ------------------------------------------------------------------ состояние

def vacancy_state(row: dict[str, Any], now: datetime) -> State:
    """Состояние вакансии простым языком. Порядок важен: проверка главнее статуса, срок главнее скрытия."""
    moderation = row.get("moderation_status") or "approved"
    status = row.get("status") or "active"
    note = (row.get("moderation_note") or "").strip()
    if moderation == "pending":
        return State("pending", STATE_LABELS["pending"], "Мы проверяем вакансию, обычно это занимает несколько часов.")
    if moderation == "rejected":
        return State("rejected", STATE_LABELS["rejected"], note or "Причина не указана. Исправьте вакансию и отправьте снова.")
    if status == "closed":
        return State("closed", STATE_LABELS["closed"], "Вакансия закрыта и не видна соискателям.")
    stale = status == "active" and lifecycle.is_stale(row.get("last_seen_at") or row.get("published_at"), now=now)
    if status == "expired" or stale:
        return State("expired", STATE_LABELS["expired"],
                     f"Вакансии больше {config.VACANCY_TTL_DAYS} дней, поэтому её скрыли. Опубликуйте её снова.")
    if status == "hidden" or row.get("suspicious") or row.get("duplicate_of"):
        return State("hidden", STATE_LABELS["hidden"], note or "Вакансию скрыл модератор. Если это ошибка, напишите нам.")
    seen = _seen(row)
    if seen is None:
        return State("published", STATE_LABELS["published"])
    expires = seen + timedelta(days=config.VACANCY_TTL_DAYS)
    left = max(0, math.ceil((expires - now).total_seconds() / 86400))
    if left <= 1:
        text = "Исчезнет с сайта сегодня или завтра. Поднимите вакансию, чтобы продлить срок."
    else:
        text = f"Будет на сайте ещё {_plain_days(left)}."
    return State("published", STATE_LABELS["published"], text, left, left <= SOON_DAYS, expires)


def raise_wait(row: dict[str, Any], now: datetime) -> timedelta | None:
    """Сколько ждать до следующего поднятия. None: можно сейчас."""
    seen = _seen(row)
    if seen is None:
        return None
    left = seen + RAISE_INTERVAL - now
    return left if left > timedelta(0) else None


def wait_text(wait: timedelta) -> str:
    minutes = max(1, math.ceil(wait.total_seconds() / 60))
    if minutes < 60:
        return f"{minutes} {fmt.plural(minutes, 'минуту', 'минуты', 'минут')}"
    hours = math.ceil(minutes / 60)
    return f"{hours} {fmt.plural(hours, 'час', 'часа', 'часов')}"


# ------------------------------------------------------------------ доступ

def _owned(conn: sqlite3.Connection, owner_id: int, vacancy_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM vacancies WHERE id = ? AND owner_user_id = ? AND kind = 'vacancy'", (vacancy_id, owner_id)
    ).fetchone()
    if row is None:
        raise CabinetError("not_found", "Вакансия не найдена.")
    return dict(row)


def get_owned(conn: sqlite3.Connection, owner_id: int, vacancy_id: int) -> dict[str, Any]:
    return _owned(conn, owner_id, vacancy_id)


# ------------------------------------------------------------------ список

def _states(conn: sqlite3.Connection, owner_id: int, now: datetime) -> list[tuple[dict[str, Any], State]]:
    rows = conn.execute(
        "SELECT * FROM vacancies WHERE owner_user_id = ? AND kind = 'vacancy' ORDER BY created_at DESC, id DESC LIMIT ?",
        (owner_id, MAX_LIST),
    ).fetchall()
    return [(dict(r), vacancy_state(dict(r), now)) for r in rows]


def _counts(pairs: list[tuple[dict[str, Any], State]]) -> dict[str, int]:
    counts = dict.fromkeys(("all", *STATE_ORDER), 0)
    for _, st in pairs:
        counts["all"] += 1
        counts[st.key] += 1
    return counts


def state_counts(conn: sqlite3.Connection, owner_id: int, now: datetime) -> dict[str, int]:
    return _counts(_states(conn, owner_id, now))


def _response_counts(conn: sqlite3.Connection, ids: list[int]) -> dict[int, tuple[int, int]]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT vacancy_id, COUNT(*) AS total,"
        f" SUM(CASE WHEN status = 'new' AND withdrawn_at IS NULL THEN 1 ELSE 0 END) AS fresh"
        f" FROM applications WHERE vacancy_id IN ({marks}) GROUP BY vacancy_id",
        ids,
    ).fetchall()
    return {int(r["vacancy_id"]): (int(r["total"]), int(r["fresh"] or 0)) for r in rows}


def decorate(row: dict[str, Any], st: State, now: datetime, responses: tuple[int, int] = (0, 0)) -> dict[str, Any]:
    """Запись списка для шаблона: подписи, даты, число откликов и доступные действия."""
    item = dict(row)
    item["title"] = (row["profession"] or "").strip().capitalize()
    item["pay"] = fmt.pay_parts(row["pay_min"], row["pay_max"], row["pay_period"])
    item["location"] = fmt.location_label(row["city"], row["district"])
    item["state"] = st
    item["responses"], item["new_responses"] = responses
    item["created_label"] = fmt.date_label(row.get("created_at"), now)
    seen = _seen(row)
    item["published_label"] = fmt.date_label(seen, now) if seen and st.key in ("published", "closed", "expired") else ""
    item["until_label"] = fmt.date_label(st.expires_at, now) if st.expires_at else ""
    live = st.key == "published"
    wait = raise_wait(row, now) if st.key in ("published", "expired") else None
    item["can_edit"] = st.key != "hidden"
    item["can_close"] = live
    item["can_raise"] = st.key in ("published", "expired", "closed")
    item["raise_label"] = "Поднять" if live else ("Открыть снова" if st.key == "closed" else "Опубликовать")
    item["raise_wait"] = wait_text(wait) if wait else ""
    item["view_url"] = f"/vacancy/{row['id']}" if st.key in ("published", "pending") else ""
    item["responses_url"] = f"/responses?vacancy={row['id']}" if item["responses"] else ""
    return item


def list_vacancies(
    conn: sqlite3.Connection, owner_id: int, *, now: datetime, state: str = "", page: int = 1, per_page: int = PER_PAGE
) -> dict[str, Any]:
    pairs = _states(conn, owner_id, now)
    counts = _counts(pairs)
    state = state if state in STATE_ORDER else ""
    chosen = [p for p in pairs if not state or p[1].key == state]
    total = len(chosen)
    pages = max(1, math.ceil(total / per_page))
    page = min(max(1, page), pages)
    part = chosen[(page - 1) * per_page: page * per_page]
    resp = _response_counts(conn, [int(r["id"]) for r, _ in part])
    items = [decorate(r, st, now, resp.get(int(r["id"]), (0, 0))) for r, st in part]
    return {
        "items": items, "total": total, "page": page, "pages": pages, "has_prev": page > 1, "has_next": page < pages,
        "counts": counts, "state": state,
    }


def get_item(conn: sqlite3.Connection, owner_id: int, vacancy_id: int, now: datetime) -> dict[str, Any]:
    row = _owned(conn, owner_id, vacancy_id)
    resp = _response_counts(conn, [vacancy_id]).get(vacancy_id, (0, 0))
    return decorate(row, vacancy_state(row, now), now, resp)


# ------------------------------------------------------------------ действия

def close_vacancy(conn: sqlite3.Connection, owner_id: int, vacancy_id: int, now: datetime) -> None:
    row = _owned(conn, owner_id, vacancy_id)
    st = vacancy_state(row, now)
    if st.key == "closed":
        return
    if st.key != "published":
        raise CabinetError("not_allowed", "Закрыть можно только опубликованную вакансию.")
    conn.execute("UPDATE vacancies SET status = 'closed' WHERE id = ? AND owner_user_id = ?", (vacancy_id, owner_id))
    conn.commit()


def raise_vacancy(conn: sqlite3.Connection, owner_id: int, vacancy_id: int, now: datetime) -> str:
    """Поднять опубликованную или опубликовать снова истёкшую или закрытую. Возвращает "raised" или "reopened".

    Закрытая вакансия, у которой срок ещё идёт, просто открывается: даты не меняются и ограничения нет.
    Остальные случаи обнуляют срок и ставят вакансию наверх ленты, не чаще раза в сутки."""
    row = _owned(conn, owner_id, vacancy_id)
    st = vacancy_state(row, now)
    if st.key == "pending":
        raise CabinetError("not_allowed", "Вакансия ещё на проверке: после одобрения она опубликуется сама.")
    if st.key == "rejected":
        raise CabinetError("not_allowed", "Вакансию отклонили. Исправьте её и отправьте на проверку снова.")
    if st.key == "hidden":
        raise CabinetError("not_allowed", "Вакансию скрыл модератор, поднять её нельзя.")
    if st.key == "closed" and not lifecycle.is_stale(row.get("last_seen_at") or row.get("published_at"), now=now):
        conn.execute("UPDATE vacancies SET status = 'active' WHERE id = ? AND owner_user_id = ?", (vacancy_id, owner_id))
        conn.commit()
        return "reopened"
    wait = raise_wait(row, now)
    if wait:
        raise CabinetError("too_soon", f"Поднимать вакансию можно раз в сутки. Попробуйте через {wait_text(wait)}.")
    stamp = _iso(now)
    conn.execute(
        "UPDATE vacancies SET status = 'active', published_at = ?, last_seen_at = ? WHERE id = ? AND owner_user_id = ?",
        (stamp, stamp, vacancy_id, owner_id),
    )
    conn.commit()
    return "raised"


def delete_vacancy(conn: sqlite3.Connection, owner_id: int, vacancy_id: int) -> int:
    """Удалить вакансию владельца. Отклики на неё удаляются, переписки остаются с сохранённым названием.
    Возвращает число удалённых откликов."""
    _owned(conn, owner_id, vacancy_id)
    count = int(conn.execute("SELECT COUNT(*) FROM applications WHERE vacancy_id = ?", (vacancy_id,)).fetchone()[0])
    conn.execute(
        "UPDATE conversations SET vacancy_id = NULL, application_id = NULL WHERE vacancy_id = ?"
        " OR application_id IN (SELECT id FROM applications WHERE vacancy_id = ?)",
        (vacancy_id, vacancy_id),
    )
    conn.execute("DELETE FROM applications WHERE vacancy_id = ?", (vacancy_id,))
    conn.execute("DELETE FROM complaints WHERE vacancy_id = ?", (vacancy_id,))
    conn.execute("DELETE FROM favorites WHERE vacancy_id = ?", (vacancy_id,))
    conn.execute("UPDATE vacancies SET duplicate_of = NULL WHERE duplicate_of = ?", (vacancy_id,))
    conn.execute("DELETE FROM vacancies WHERE id = ? AND owner_user_id = ?", (vacancy_id, owner_id))
    conn.commit()
    return count


# ------------------------------------------------------------------ правка

def _contacts_of(value: Any) -> tuple[tuple[str, ...], str, str]:
    data = value
    if isinstance(value, str):
        try:
            data = json.loads(value or "{}")
        except ValueError:
            data = {}
    data = data if isinstance(data, dict) else {}
    phones = tuple(sorted(str(p) for p in (data.get("phones") or [])))
    return phones, str(data.get("telegram") or "").lower(), str(data.get("email") or "").lower()


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def review_reasons(old: dict[str, Any], new: Vacancy) -> list[str]:
    """Что изменилось из того, что требует новой проверки: "оплата", "контакты", "описание"."""
    reasons: list[str] = []
    fresh = new.to_dict()
    if any((old.get(f) or 0) != (fresh.get(f) or 0) if f in ("pay_min", "pay_max", "pay_daily")
           else _text(old.get(f)) != _text(fresh.get(f)) for f in PAY_FIELDS):
        reasons.append("оплата")
    if _contacts_of(old.get("contacts")) != _contacts_of(fresh.get("contacts")):
        reasons.append("контакты")
    if any(_text(old.get(f)).lower() != _text(fresh.get(f)).lower() if f in ("profession",) else _text(old.get(f)) != _text(fresh.get(f))
           for f in TEXT_FIELDS):
        reasons.append("описание")
    return reasons


def form_values(row: dict[str, Any]) -> dict[str, str]:
    """Значения полей формы вакансии по записи базы (те же имена, что у формы размещения)."""
    contacts: Any = row.get("contacts")
    if isinstance(contacts, str):
        try:
            contacts = json.loads(contacts or "{}")
        except ValueError:
            contacts = {}
    contacts = contacts if isinstance(contacts, dict) else {}
    phones = [str(p) for p in (contacts.get("phones") or []) if p]
    first_phone = phones[0] if phones else ""
    raw_telegram = str(contacts.get("telegram") or "")
    raw_email = str(contacts.get("email") or "")

    def s(name: str) -> str:
        value = row.get(name)
        return "" if value is None else str(value)

    experience = (row.get("experience") or "").lower()
    return {
        "profession": s("title_raw") or (row.get("profession") or "").capitalize(),
        "category": s("category"), "work_format": s("work_format"), "employment": s("employment"),
        "schedule": s("schedule"), "pay_min": s("pay_min"), "pay_max": s("pay_max"), "pay_period": s("pay_period"),
        "pay_daily": "1" if row.get("pay_daily") else "",
        "no_experience": "1" if experience.startswith(("не нужен", "без опыт")) else "",
        "city": s("city") or "Донецк", "district": s("district"), "age_min": s("age_min"),
        "requirements": s("requirements"), "employer": s("employer"),
        "phone": first_phone, "telegram": raw_telegram, "email": raw_email, "summary": s("summary"),
    }


def update_vacancy(
    conn: sqlite3.Connection, owner_id: int, vacancy_id: int, new: Vacancy, now: datetime, *, premoderate: bool = True
) -> str:
    """Сохранить правку. Возвращает "saved" (правка принята), "review" (опубликованная вакансия ушла на повторную
    проверку) или "resubmitted" (отклонённая вакансия отправлена на проверку снова)."""
    row = _owned(conn, owner_id, vacancy_id)
    st = vacancy_state(row, now)
    if st.key == "hidden":
        raise CabinetError("not_allowed", "Вакансию скрыл модератор, править её нельзя.")
    reasons = review_reasons(row, new)
    d = new.to_dict()
    phones = (d["contacts"] or {}).get("phones") or [""]
    dedupe = hashlib.sha1(f"{d['profession'].lower()}|{phones[0]}|{d['city'] or ''}".encode()).hexdigest()[:16]
    values: dict[str, Any] = {
        "profession": d["profession"], "title_raw": d["title_raw"], "category": d["category"],
        "work_format": d["work_format"], "employment": d["employment"], "schedule": d["schedule"],
        "pay_min": d["pay_min"], "pay_max": d["pay_max"], "pay_period": d["pay_period"], "pay_daily": int(bool(d["pay_daily"])),
        "city": d["city"], "district": d["district"], "experience": d["experience"], "age_min": d["age_min"],
        "requirements": d["requirements"], "employer": d["employer"],
        "contacts": json.dumps(d["contacts"], ensure_ascii=False), "summary": d["summary"], "dedupe_key": dedupe,
    }
    outcome = "saved"
    if premoderate:
        if st.key == "rejected":
            values.update(moderation_status="pending", status="hidden", moderation_note=None)
            outcome = "resubmitted"
        elif st.key != "pending" and reasons:
            values.update(moderation_status="pending", status="hidden", moderation_note=None)
            outcome = "review"
    elif st.key == "rejected":
        values.update(moderation_status="approved", status="active", moderation_note=None, last_seen_at=_iso(now),
                      published_at=_iso(now))
    sets = ", ".join(f"{k} = ?" for k in values)
    conn.execute(f"UPDATE vacancies SET {sets} WHERE id = ? AND owner_user_id = ?", [*values.values(), vacancy_id, owner_id])
    conn.commit()
    return outcome


# ------------------------------------------------------------------ компания

def get_company(conn: sqlite3.Connection, owner_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM companies WHERE owner_user_id = ? ORDER BY id LIMIT 1", (owner_id,)).fetchone()
    return dict(row) if row else None


def _line(value: Any, limit: int) -> str:
    return " ".join(clean_text(str(value or "")).split())[:limit]


def _site(raw: str) -> str | None:
    """Адрес сайта с https://, если схема не указана. None: это не адрес сайта."""
    if not raw:
        return ""
    candidate = raw if re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.IGNORECASE) else "https://" + raw
    parts = urlsplit(candidate)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or "." not in host or " " in raw or len(host) < 4:
        return None
    return candidate


def validate_company(form: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Очистка и проверка карточки компании. Возвращает очищенные значения и ошибки по полям."""
    errors: dict[str, str] = {}
    raw_name = _line(form.get("name"), 400)
    cleaned = {
        "name": _line(form.get("name"), COMPANY_LIMITS["name"]),
        "city": _line(form.get("city"), COMPANY_LIMITS["city"]) or "Донецк",
        "site": _line(form.get("site"), 400),
        "description": _line(form.get("description"), 4000),
        "phone": _line(form.get("phone"), 40),
        "contact_email": _line(form.get("contact_email"), 300).lower(),
    }
    if len(cleaned["name"]) < 2:
        errors["name"] = "Укажите название компании: хотя бы два знака."
    elif len(raw_name) > COMPANY_LIMITS["name"]:
        errors["name"] = f"Название длиннее {COMPANY_LIMITS['name']} знаков."
    if len(cleaned["site"]) > COMPANY_LIMITS["site"]:
        errors["site"] = "Адрес сайта слишком длинный."
    else:
        site = _site(cleaned["site"])
        if site is None:
            errors["site"] = "Адрес сайта в формате https://example.ru."
        else:
            cleaned["site"] = site
    if len(cleaned["description"]) > COMPANY_LIMITS["description"]:
        errors["description"] = f"Описание длиннее {COMPANY_LIMITS['description']} знаков: сократите на {len(cleaned['description']) - COMPANY_LIMITS['description']}."
    if cleaned["phone"]:
        norm = fmt.normalize_phone(cleaned["phone"])
        if norm is None:
            errors["phone"] = "Телефон в формате +7 949 123-45-67."
        else:
            cleaned["phone"] = norm
    if cleaned["contact_email"] and not valid_email(cleaned["contact_email"]):
        errors["contact_email"] = "Проверьте адрес почты: он должен выглядеть как name@example.ru."
    cleaned["city"] = cleaned["city"][: COMPANY_LIMITS["city"]]
    return cleaned, errors


def save_company(conn: sqlite3.Connection, owner_id: int, values: dict[str, str]) -> int:
    existing = get_company(conn, owner_id)
    args = (values["name"], values["city"], values["site"] or None, values["description"],
            values["phone"] or None, values["contact_email"] or None)
    if existing:
        conn.execute(
            "UPDATE companies SET name = ?, city = ?, site = ?, description = ?, phone = ?, contact_email = ? WHERE id = ?",
            (*args, existing["id"]),
        )
        conn.commit()
        return int(existing["id"])
    cur = conn.execute(
        "INSERT INTO companies(name, city, site, description, phone, contact_email, owner_user_id) VALUES (?,?,?,?,?,?,?)",
        (*args, owner_id),
    )
    conn.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]


def ensure_company(conn: sqlite3.Connection, user: dict[str, Any], name: str, city: str) -> int:
    """Компания работодателя: уже есть, иначе создаётся с названием из вакансии или именем аккаунта."""
    existing = get_company(conn, int(user["id"]))
    if existing:
        return int(existing["id"])
    title = _line(name, COMPANY_LIMITS["name"]) or _line(user.get("name"), COMPANY_LIMITS["name"]) or "Работодатель"
    cur = conn.execute(
        "INSERT INTO companies(owner_user_id, name, city) VALUES (?,?,?)", (user["id"], title, _line(city, 60) or "Донецк")
    )
    conn.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]


# ------------------------------------------------------------------ сводка

def employer_summary(conn: sqlite3.Connection, owner_id: int, now: datetime) -> dict[str, Any]:
    pairs = _states(conn, owner_id, now)
    counts = _counts(pairs)
    attention: list[dict[str, str]] = []
    for row, st in pairs:
        title = (row["profession"] or "").strip().capitalize()
        if st.key == "rejected" and len(attention) < 5:
            attention.append({
                "kind": "rejected", "icon": "x-circle", "href": f"/cabinet/vacancies/{row['id']}/edit",
                "text": f"«{title}» отклонена: {st.note}", "action": "Исправить",
            })
    for row, st in pairs:
        title = (row["profession"] or "").strip().capitalize()
        if st.key == "published" and st.soon and len(attention) < 8:
            when = "сегодня или завтра" if (st.days_left or 0) <= 1 else f"через {_plain_days(st.days_left or 0)}"
            attention.append({
                "kind": "soon", "icon": "clock", "href": "/cabinet/vacancies?state=published",
                "text": f"«{title}» исчезнет с сайта {when}", "action": "Продлить",
            })
    return {"counts": counts, "attention": attention, "company": get_company(conn, owner_id)}
