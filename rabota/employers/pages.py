"""Данные страницы компании: контакты из открытых вакансий, разметка Organization, подписи дат."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from rabota.lifecycle import parse_iso
from rabota.web import format as fmt

_MONTHS = ["январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе", "сентябре", "октябре", "ноябре", "декабре"]
_MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


AUTO_ORIGINS = ("auto", "parsed", "collected", "import")


def is_auto(row: dict[str, Any] | None) -> bool:
    """Карточка создана автоматически из собранных вакансий и пока без владельца.

    Признаки задаёт автосоздание карточек: пустой владелец (owner_user_id или claimed_user_id), отметка
    auto_created или origin/source из списка автоматических. Пока их нет в таблице, карточка считается обычной."""
    if not row:
        return False
    if row.get("auto_created"):
        return True
    origin = str(row.get("origin") or row.get("source") or "").lower()
    if origin in AUTO_ORIGINS:
        return not (row.get("owner_user_id") or row.get("claimed_user_id"))
    return False


def contact_phones(vacancies: list[dict[str, Any]], limit: int = 3) -> list[str]:
    """Телефоны из открытых вакансий компании (они уже опубликованы работодателем), без повторов."""
    out: list[str] = []
    for v in vacancies:
        for phone in v.get("phones") or []:
            if phone and phone not in out:
                out.append(phone)
    return out[:limit]


def contact_telegram(vacancies: list[dict[str, Any]]) -> str:
    for v in vacancies:
        if v.get("telegram"):
            return str(v["telegram"])
    return ""


def registered_label(value: str | None) -> str:
    """"На сайте с октября 2026". Пусто, если даты нет."""
    dt = parse_iso(value)
    if dt is None:
        return ""
    return f"с {_MONTHS_GEN[dt.month - 1]} {dt.year}"


def registered_month(value: str | None) -> str:
    dt = parse_iso(value)
    return "" if dt is None else f"в {_MONTHS[dt.month - 1]} {dt.year}"


def verified_label(value: str | None, now: datetime | None = None) -> str:
    return fmt.date_label(value, now) if value else ""


def company_jsonld(site_url: str, company: dict[str, Any], *, logo_url: str = "", phones: list[str] | None = None) -> dict[str, Any]:
    from rabota.web import seo as seolib

    ld = seolib.company_jsonld(site_url, company)
    ld["@id"] = f"{site_url}/company/{company['id']}#organization"
    if logo_url:
        ld["logo"] = logo_url if logo_url.startswith("http") else site_url + logo_url
    if phones:
        ld["contactPoint"] = {
            "@type": "ContactPoint", "telephone": phones[0], "contactType": "hiring", "areaServed": "RU",
            "availableLanguage": "ru",
        }
        ld["telephone"] = phones[0]
    return ld
