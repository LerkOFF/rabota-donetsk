"""Настройки, которые владелец меняет из админки без правки кода и перезапуска.

Схема лежит в коде (SCHEMA): группа, подпись, тип, умолчание, границы. Значения хранятся в таблице `settings`
только если отличаются от умолчания; пока строки нет, действует значение из .env или умолчание из кода.
Каждое изменение пишется в `settings_log` (кто, когда, было, стало) и в общий журнал действий.

Читать значения в коде сайта нужно через `effective(conn, ключ)`: сначала таблица настроек, иначе .env и умолчание.
Сбой чтения (таблицы ещё нет, база занята) не поднимается дальше: возвращается значение из .env или умолчание.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rabota import config
from rabota.admin import audit
from rabota.admin.common import actor_id, clean_text, iso, now_utc

KIND_INT = "int"
KIND_BOOL = "bool"
KIND_TEXT = "text"

GROUPS: dict[str, tuple[str, str]] = {
    "vacancies": ("Сроки жизни вакансий", "Когда вакансия скрывается и когда удаляется. Работает при ежедневной уборке воркера."),
    "moderation": ("Жалобы", "Сколько жалоб скрывают вакансию автоматически."),
    "access": ("Доступ", "Регистрация новых пользователей."),
    "texts": ("Тексты", "Короткие тексты, которые видят посетители."),
}


@dataclass(frozen=True)
class Setting:
    key: str
    group: str
    label: str
    kind: str                       # int, bool, text
    default: Callable[[], Any]      # умолчание считается при каждом чтении: .env читается на лету
    minimum: int = 0
    maximum: int = 0                # для text: наибольшая длина
    hint: str = ""
    unit: str = ""

    def default_value(self) -> Any:
        return self.default()


def _env_int(name: str, fallback: int) -> Callable[[], int]:
    def read() -> int:
        try:
            return int(config.get(name, str(fallback)))
        except ValueError:
            return fallback

    return read


SCHEMA: tuple[Setting, ...] = (
    Setting("vacancy_ttl_days", "vacancies", "Срок жизни вакансии", KIND_INT, lambda: config.VACANCY_TTL_DAYS, 3, 90,
            "Через столько дней после последней публикации вакансия скрывается. Показ на сайте ограничен значением VACANCY_TTL_DAYS из .env: "
            "дольше него без перезапуска вакансию не показать.", "дней"),
    Setting("expired_keep_days", "vacancies", "Сколько хранить скрытые", KIND_INT, lambda: config.EXPIRED_KEEP_DAYS, 1, 365,
            "Сколько дней скрытая по сроку вакансия лежит в базе до удаления.", "дней"),
    Setting("complaint_autohide_threshold", "moderation", "Порог автоскрытия по жалобам", KIND_INT,
            _env_int("COMPLAINT_AUTOHIDE_THRESHOLD", 3), 0, 50,
            "Сколько разных людей должны пожаловаться, чтобы вакансия скрылась сама. 0: не скрывать.", "жалоб"),
    Setting("registration_open", "access", "Регистрация открыта", KIND_BOOL, lambda: config.flag("REGISTRATION_OPEN", True), 0, 1,
            "Если выключить, страница регистрации покажет, что она откроется позже. Вход для уже созданных аккаунтов работает."),
    Setting("registration_closed_note", "texts", "Пояснение при закрытой регистрации", KIND_TEXT, lambda: "", 0, 300,
            "Дополнительная строка на странице «Регистрация откроется позже». Пусто: без пояснения."),
)
BY_KEY: dict[str, Setting] = {s.key: s for s in SCHEMA}


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS settings ("
        " key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL DEFAULT '');"
        "CREATE TABLE IF NOT EXISTS settings_log ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor_user_id INTEGER, actor_email TEXT NOT NULL DEFAULT '',"
        " key TEXT NOT NULL, old TEXT NOT NULL DEFAULT '', new TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '');"
        "CREATE INDEX IF NOT EXISTS idx_settings_log_id ON settings_log(id DESC);"
    )
    conn.commit()


# ------------------------------------------------------------------ чтение

def parse(setting: Setting, raw: str) -> Any:
    """Хранимая строка -> значение. Испорченное значение даёт умолчание, а не ошибку."""
    try:
        if setting.kind == KIND_INT:
            value = int(raw)
            return value if setting.minimum <= value <= setting.maximum else setting.default_value()
        if setting.kind == KIND_BOOL:
            return raw == "1"
        return raw[: setting.maximum]
    except (TypeError, ValueError):
        return setting.default_value()


def stored(conn: sqlite3.Connection) -> dict[str, str]:
    """Значения, которые изменил владелец (ключ -> строка). Без таблицы или при сбое: пусто."""
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT key, value FROM settings")}
    except sqlite3.Error:
        return {}


def effective(conn: sqlite3.Connection | None, key: str) -> Any:
    """Значение настройки: таблица, иначе .env и умолчание. Неизвестный ключ - ошибка программиста."""
    setting = BY_KEY[key]
    if conn is not None:
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            return parse(setting, row[0])
    return setting.default_value()


def snapshot(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Все настройки для страницы: текущее значение, умолчание, изменено ли."""
    overrides = stored(conn)
    out: dict[str, dict[str, Any]] = {}
    for s in SCHEMA:
        default = s.default_value()
        changed = s.key in overrides
        out[s.key] = {"value": parse(s, overrides[s.key]) if changed else default, "default": default, "changed": changed}
    return out


def display(setting: Setting, value: Any) -> str:
    if setting.kind == KIND_BOOL:
        return "да" if value else "нет"
    if setting.kind == KIND_TEXT:
        return str(value) if value else "пусто"
    return f"{value} {setting.unit}".strip()


def grouped() -> list[tuple[str, str, str, list[Setting]]]:
    """Группы в порядке схемы: (ключ, название, пояснение, настройки)."""
    result = []
    for gkey, (title, lead) in GROUPS.items():
        items = [s for s in SCHEMA if s.group == gkey]
        if items:
            result.append((gkey, title, lead, items))
    return result


# ------------------------------------------------------------------ проверка и запись

def validate(form: dict[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
    """Значения формы (ключ -> строка) -> (проверенные значения, ошибки по ключам). Лишние ключи игнорируются."""
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, raw in form.items():
        setting = BY_KEY.get(key)
        if setting is None:
            continue
        if setting.kind == KIND_BOOL:
            if raw not in ("0", "1"):
                errors[key] = "Нужно выбрать да или нет."
            else:
                values[key] = raw == "1"
        elif setting.kind == KIND_INT:
            text = str(raw).strip()
            if not text.lstrip("-").isdigit() or len(text) > 9:
                errors[key] = "Нужно целое число."
                continue
            number = int(text)
            if not setting.minimum <= number <= setting.maximum:
                errors[key] = f"Допустимо от {setting.minimum} до {setting.maximum}."
            else:
                values[key] = number
        else:
            text = clean_text(raw, setting.maximum + 1).replace("\n", " ").strip()
            if len(text) > setting.maximum:
                errors[key] = f"Не больше {setting.maximum} знаков."
            else:
                values[key] = text
    return values, errors


def _store(setting: Setting, value: Any) -> str:
    if setting.kind == KIND_BOOL:
        return "1" if value else "0"
    return str(value)


def save(
    conn: sqlite3.Connection, actor: dict[str, Any] | None, form: dict[str, str], ip: str = "", now: datetime | None = None,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Проверить и записать значения. Возвращает (список изменений, ошибки). При любой ошибке не пишется ничего.

    Значение, равное умолчанию, снимает изменение: настройка снова «как в .env».
    """
    values, errors = validate(form)
    if errors:
        return [], errors
    moment = iso(now or now_utc())
    who = (actor or {}).get("email") or audit.SYSTEM
    current = stored(conn)
    changes: list[dict[str, str]] = []
    for key, value in values.items():
        setting = BY_KEY[key]
        default = setting.default_value()
        old_value = parse(setting, current[key]) if key in current else default
        if value == old_value:
            continue
        if value == default:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO settings(key, value, updated_at, updated_by) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                (key, _store(setting, value), moment, who),
            )
        conn.execute(
            "INSERT INTO settings_log(at, actor_user_id, actor_email, key, old, new, ip) VALUES (?,?,?,?,?,?,?)",
            (moment, actor_id(actor), who, key, display(setting, old_value), display(setting, value), ip[:64]),
        )
        changes.append({"key": key, "old": display(setting, old_value), "new": display(setting, value)})
    conn.commit()
    if changes:
        audit.log(conn, actor, "settings.save", "settings", ",".join(c["key"] for c in changes),
                  {"changes": [f"{c['key']}: {c['old']} -> {c['new']}" for c in changes]}, ip, now)
    return changes, {}


def change_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    try:
        rows = conn.execute("SELECT * FROM settings_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        d = dict(r) if isinstance(r, sqlite3.Row) else dict(zip([c[0] for c in conn.execute("SELECT * FROM settings_log LIMIT 0").description], r, strict=False))
        setting = BY_KEY.get(d["key"])
        d["label"] = setting.label if setting else d["key"]
        out.append(d)
    return out


# ------------------------------------------------------------------ удобные чтения для кода сайта

def ttl_days(conn: sqlite3.Connection | None) -> int:
    return int(effective(conn, "vacancy_ttl_days"))


def keep_days(conn: sqlite3.Connection | None) -> int:
    return int(effective(conn, "expired_keep_days"))


def autohide_threshold(conn: sqlite3.Connection | None) -> int:
    return int(effective(conn, "complaint_autohide_threshold"))


def registration_open(conn: sqlite3.Connection | None) -> bool:
    return bool(effective(conn, "registration_open"))


# ------------------------------------------------------------------ данные для страницы

def page(conn: sqlite3.Connection, submitted: dict[str, str] | None = None, errors: dict[str, str] | None = None) -> dict[str, Any]:
    """Контекст страницы настроек: группы с полями, журнал изменений. submitted: значения формы после ошибки."""
    snap = snapshot(conn)
    submitted = submitted or {}
    errors = errors or {}
    groups = []
    for gkey, title, lead, items in grouped():
        rows = []
        for s in items:
            info = snap[s.key]
            if s.key in submitted:
                shown = submitted[s.key]
            elif s.kind == KIND_BOOL:
                shown = "1" if info["value"] else "0"
            else:
                shown = str(info["value"])
            rows.append({
                "key": s.key, "field": "s_" + s.key, "label": s.label, "kind": s.kind, "hint": s.hint, "unit": s.unit,
                "minimum": s.minimum, "maximum": s.maximum, "value": shown, "checked": shown == "1",
                "changed": info["changed"], "default_text": display(s, info["default"]), "current_text": display(s, info["value"]),
                "error": errors.get(s.key, ""),
            })
        groups.append({"key": gkey, "title": title, "lead": lead, "rows": rows})
    ttl_env = config.VACANCY_TTL_DAYS
    notes = []
    if int(snap["vacancy_ttl_days"]["value"]) > ttl_env:
        notes.append(f"Срок жизни выше VACANCY_TTL_DAYS из .env ({ttl_env}): на сайте вакансии всё равно исчезнут через {ttl_env} дн. "
                     "Чтобы показывать дольше, измените .env и перезапустите сайт.")
    return {
        "groups": groups, "log": change_log(conn), "notes": notes, "changed_count": sum(1 for v in snap.values() if v["changed"]),
        "error": "Настройки не сохранены: исправьте отмеченные поля." if errors else "",
    }
