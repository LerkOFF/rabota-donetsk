"""Общее для служебной части: время, пагинация, текущий администратор."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

PER_PAGE = 25


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


@dataclass
class Page:
    items: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    page: int = 1
    per_page: int = PER_PAGE

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def clamp_page(page: int, total: int, per_page: int = PER_PAGE) -> int:
    pages = max(1, -(-total // per_page))
    return min(max(int(page or 1), 1), pages)


def actor_id(actor: dict[str, Any] | None) -> int | None:
    return int(actor["id"]) if actor and actor.get("id") else None


def clean_line(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def clean_text(value: Any, limit: int = 2000) -> str:
    """Многострочный текст: управляющие символы убраны, переводы строк сохранены."""
    text = "".join(ch for ch in str(value or "") if ch in "\n\t" or ord(ch) >= 32)
    return text.strip()[:limit]
