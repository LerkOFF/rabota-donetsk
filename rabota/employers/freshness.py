"""Метки свежести и доверия на карточках вакансий. Чистые функции: время передаётся явно.

Метки:
- "новая": вакансия впервые появилась на сайте меньше суток назад;
- "обновлено 2 часа назад": дата последней публикации или поднятия заметно позже первой;
- "проверена": вакансию работодателя одобрил модератор или сработала автопроверка для проверенного работодателя.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from rabota.web import format as fmt

NEW_WINDOW = timedelta(hours=24)
UPDATE_GAP = timedelta(hours=1)  # поднятие раньше чем через час после публикации обновлением не считается


@dataclass(frozen=True)
class Chip:
    kind: str  # new, updated, checked
    label: str
    title: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "label": self.label, "title": self.title}


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return fmt.parse_dt(value) if isinstance(value, str) else None


def is_new(first_seen: Any, now: datetime) -> bool:
    """Вакансия новая, если появилась меньше суток назад (и не из будущего)."""
    dt = _dt(first_seen)
    if dt is None:
        return False
    return -timedelta(minutes=5) <= now - dt < NEW_WINDOW  # небольшой разброс часов не мешает


def updated_label(first_seen: Any, last_update: Any, now: datetime) -> str:
    """"обновлено 2 часа назад". Пусто, если обновления не было или оно совпадает с публикацией."""
    first, last = _dt(first_seen), _dt(last_update)
    if last is None:
        return ""
    if first is not None and last - first < UPDATE_GAP:
        return ""
    ago = fmt.time_ago(last.isoformat(), now)
    if not ago:
        return ""
    return f"обновлено {ago}"


def is_checked(row: dict[str, Any]) -> bool:
    """Вакансию работодателя на сайте прошёл проверку: одобрена, не скрыта, не помечена подозрительной."""
    return (
        row.get("origin") == "direct"
        and (row.get("moderation_status") or "approved") == "approved"
        and not row.get("suspicious")
    )


def chips(row: dict[str, Any], now: datetime) -> list[Chip]:
    """Метки для карточки в порядке показа. Спарсенные вакансии "проверена" не получают никогда."""
    first_seen = row.get("published_at") or row.get("created_at")
    out: list[Chip] = []
    if is_new(first_seen, now):
        out.append(Chip("new", "Новая", "Вакансия появилась на сайте меньше суток назад"))
    else:
        label = updated_label(first_seen, row.get("last_seen_at"), now)
        if label:
            out.append(Chip("updated", label, "Работодатель поднял или подтвердил вакансию"))
    if is_checked(row):
        out.append(Chip("checked", "Проверена", "Вакансию проверил модератор сайта: это настоящее предложение о работе"))
    return out
