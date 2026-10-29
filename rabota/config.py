"""Настройки из .env. Секреты только оттуда, в git их нет."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


DB_PATH = Path(get("DB_PATH", str(ROOT / "data" / "rabota.db")))
USER_AGENT = get("HTTP_USER_AGENT", "Mozilla/5.0 (compatible; RabotaDonetskBot/0.1)")

# Сколько дней вакансия живёт на сайте с последней публикации. Повторная публикация обнуляет отсчёт.
VACANCY_TTL_DAYS = int(get("VACANCY_TTL_DAYS", "21"))
# Сколько дней истёкшая вакансия лежит скрытой в базе до физического удаления.
EXPIRED_KEEP_DAYS = int(get("EXPIRED_KEEP_DAYS", "30"))


# ---------------------------------------------------------------------------------------------
# Режим работы и параметры сервера. Читаются из окружения при каждом вызове, чтобы тесты могли их менять.

TRUTHY = {"1", "true", "yes", "on", "да"}


def flag(name: str, default: bool = False) -> bool:
    raw = get(name).strip().lower()
    if not raw:
        return default
    return raw in TRUTHY


def environment() -> str:
    """production, staging или development (по умолчанию). Задаётся переменной ENVIRONMENT."""
    return (get("ENVIRONMENT") or "development").strip().lower()


def is_production() -> bool:
    return environment() == "production"


def debug() -> bool:
    """Режим отладки. На проде должен быть выключен, проверка в rabota.ops.preflight."""
    return flag("DEBUG")


def site_url(default: str = "https://работа-донецк.рф") -> str:
    return (get("SITE_URL") or default).strip().rstrip("/")


def telegram_proxy() -> str:
    """Прокси для Telegram: TELEGRAM_PROXY (socks5://host:port), для совместимости TG_PROXY. Пусто: напрямую.

    Один адрес используется и слушателем Telethon, и сборщиком публичных страниц t.me/s.
    """
    return (get("TELEGRAM_PROXY") or get("TG_PROXY")).strip()


def metrika_id() -> str:
    """Номер счётчика Яндекс Метрики (только цифры) или пусто: тогда ничего не подключается."""
    value = get("METRIKA_ID").strip()
    return value if value.isdigit() else ""
