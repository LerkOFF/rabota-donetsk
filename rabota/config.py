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
