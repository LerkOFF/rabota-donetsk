"""Подписи и форматирование для карточек: оплата с периодом, свежесть, склонения."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

NBSP = " "

PERIOD_LABELS = {
    "месяц": "в месяц",
    "смена": "за смену",
    "день": "в день",
    "час": "в час",
    "задача": "за задачу",
}

WORK_FORMAT_LABELS = {
    "офис": "В офисе",
    "точка": "В заведении",
    "объект": "На объекте",
    "склад": "На складе",
    "выезд": "С выездом",
    "удалённо": "Удалённо",
    "вахта": "Вахта",
}

EMPLOYMENT_LABELS = {
    "постоянно": "Постоянная работа",
    "подработка": "Подработка",
    "на сегодня": "На сегодня",
    "сменная": "Сменная работа",
}

SOURCE_LABELS = {
    "telegram": "Telegram",
    "vk": "ВКонтакте",
    "max": "MAX",
    "trudvsem": "Работа России",
    "hh": "hh.ru",
    "mczn": "Центр занятости",
    "avito": "Авито",
    "site": "Разместили на сайте",
}

CATEGORY_ICONS = {
    "общепит": "fork-knife",
    "торговля": "shopping-bag",
    "офис": "desktop",
    "склад": "package",
    "производство": "factory",
    "стройка": "hard-hat",
    "транспорт": "truck",
    "медицина": "first-aid-kit",
    "образование": "graduation-cap",
    "услуги": "wrench",
    "другое": "dots-three-circle",
}

CATEGORY_LABELS = {
    "общепит": "Общепит",
    "торговля": "Торговля",
    "офис": "Офис",
    "склад": "Склад",
    "производство": "Производство",
    "стройка": "Стройка",
    "транспорт": "Транспорт",
    "медицина": "Медицина",
    "образование": "Образование",
    "услуги": "Услуги",
    "другое": "Другое",
}

_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def plural(n: int, one: str, few: str, many: str) -> str:
    """Склонение по числу: 1 вакансия, 2 вакансии, 5 вакансий."""
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def count_label(n: int, one: str, few: str, many: str) -> str:
    return f"{n}{NBSP}{plural(n, one, few, many)}"


def money(value: int) -> str:
    """48500 -> '48 500' (неразрывные пробелы)."""
    return f"{int(value):,}".replace(",", NBSP)


def pay_parts(pay_min: int | None, pay_max: int | None, period: str | None) -> dict[str, Any]:
    """Оплата для показа: крупная часть и период отдельно.

    Примеры: "4 000 ₽" + "за смену"; "от 48 500 до 56 000 ₽" + "в месяц".
    """
    lo = pay_min or None
    hi = pay_max or None
    if lo and hi and lo > hi:
        lo, hi = hi, lo
    period_text = PERIOD_LABELS.get(period or "", "")
    if not lo and not hi:
        return {"known": False, "main": "Оплата по договорённости", "period": "", "text": "Оплата по договорённости"}
    if lo and hi and lo != hi:
        main = f"от{NBSP}{money(lo)} до{NBSP}{money(hi)}{NBSP}₽"
    elif lo and hi:
        main = f"{money(lo)}{NBSP}₽"
    elif lo:
        main = f"от{NBSP}{money(lo)}{NBSP}₽"
    else:
        main = f"до{NBSP}{money(hi or 0)}{NBSP}₽"
    text = f"{main} {period_text}".strip()
    return {"known": True, "main": main, "period": period_text, "text": text}


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def time_ago(value: str | None, now: datetime | None = None) -> str:
    """Свежесть: "2 часа назад", "вчера", "12 октября"."""
    dt = parse_dt(value)
    if dt is None:
        return ""
    now = now or datetime.now(UTC)
    seconds = (now - dt).total_seconds()
    seconds = max(seconds, 0)
    if seconds < 60:
        return "только что"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} {plural(minutes, 'минуту', 'минуты', 'минут')} назад"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours} {plural(hours, 'час', 'часа', 'часов')} назад"
    days = (now.date() - dt.date()).days
    if days == 1:
        return "вчера"
    if days < 7:
        return f"{days} {plural(days, 'день', 'дня', 'дней')} назад"
    month = _MONTHS[dt.month - 1]
    if dt.year == now.year:
        return f"{dt.day} {month}"
    return f"{dt.day} {month} {dt.year}"


def district_label(district: str | None) -> str:
    """"Ворошиловский" -> "Ворошиловский район", "Текстильщик" остаётся как есть."""
    d = (district or "").strip()
    if not d:
        return ""
    low = d.lower()
    if low.endswith(("ский", "цкий", "ной", "ый")) and " " not in d and "район" not in low:
        return f"{d} район"
    return d


def location_label(city: str | None, district: str | None) -> str:
    parts = [p for p in ((city or "").strip(), district_label(district)) if p]
    return ", ".join(parts)


def phone_pretty(phone: str) -> str:
    """+79490000000 -> +7 949 000-00-00."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 11 and digits[0] in "78":
        d = digits[1:]
        return f"+7 {d[:3]} {d[3:6]}-{d[6:8]}-{d[8:]}"
    return phone


def phone_href(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return "+" + digits if digits else phone


def normalize_phone(raw: str) -> str | None:
    """Приводит телефон из формы к виду +7XXXXXXXXXX. None, если это не телефон."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    else:
        return None
    return "+" + digits


def telegram_href(handle: str) -> str:
    h = handle.strip()
    if h.startswith(("http://", "https://")):
        return h
    return "https://t.me/" + h.lstrip("@")
