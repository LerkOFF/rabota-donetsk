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
