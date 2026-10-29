"""Общие модели данных. Контракт между сборщиками, разбором, базой и сайтом."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SOURCE_TYPES = ("telegram", "vk", "max", "trudvsem", "hh", "mczn", "avito")

CATEGORIES = (
    "общепит", "торговля", "офис", "склад", "производство", "стройка",
    "транспорт", "медицина", "образование", "услуги", "другое",
)
WORK_FORMATS = ("офис", "точка", "объект", "склад", "выезд", "удалённо", "вахта")
EMPLOYMENTS = ("постоянно", "подработка", "на сегодня", "сменная")
PAY_PERIODS = ("месяц", "смена", "день", "час", "задача")
# что это за пост: в ленту идут только vacancy
POST_KINDS = ("vacancy", "service", "military", "ad", "other")


@dataclass
class RawPost:
    """Сырой пост или вакансия из источника, как получили."""

    source_type: str            # одно из SOURCE_TYPES
    source: str                 # канал, паблик, сайт: "rabota_donetskv", "dnr.work", "trudvsem"
    external_id: str            # id внутри источника, уникален вместе с source
    url: str                    # ссылка на оригинал
    text: str                   # текст как есть, без обработки
    published_at: str | None = None   # ISO 8601, UTC
    image_urls: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # структурные поля источника (trudvsem, hh)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Vacancy:
    """Нормализованная вакансия для сайта."""

    profession: str                       # нормализованное: "бариста"
    title_raw: str = ""
    category: str = "другое"              # из CATEGORIES
    work_format: str | None = None        # из WORK_FORMATS
    employment: str | None = None         # из EMPLOYMENTS
    schedule: str | None = None           # "5/2", "2/2", "сутки", "с 9:00 до 18:00"
    pay_min: int | None = None
    pay_max: int | None = None
    pay_period: str | None = None         # из PAY_PERIODS
    pay_daily: bool = False               # ежедневная выплата
    pay_monthly_estimate: int | None = None
    city: str | None = None               # "Донецк"
    district: str | None = None           # "Ворошиловский", "Текстильщик"
    experience: str | None = None         # "не нужен", "от 1 года"
    age_min: int | None = None
    requirements: str | None = None
    employer: str | None = None
    contacts: dict[str, Any] = field(default_factory=dict)  # {"phones": [...], "telegram": "@x", "max": "...", "email": "..."}
    summary: str = ""                     # 1-2 чистых предложения для карточки, без эмодзи и капса
    source_type: str = ""
    source: str = ""
    source_url: str = ""
    external_id: str = ""
    published_at: str | None = None
    last_seen_at: str | None = None       # когда вакансию видели в последний раз (UTC), от него идёт срок жизни
    kind: str = "vacancy"                 # из POST_KINDS
    dedupe_key: str = ""                  # ключ склейки дублей
    duplicate_of: int | None = None
    status: str = "active"                # active | closed | hidden
    suspicious: bool = False              # мнимая вакансия, на ручной просмотр

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
