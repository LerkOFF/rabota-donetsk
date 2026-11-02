"""Настройки подписок и Telegram из .env и из админки.

Токен бота и идентификатор канала берутся только из окружения. Без них доставка в Telegram, бот и автопостинг
выключены и ничего не ломают. Тумблеры и лимиты, которые меняет владелец, читаются из таблицы настроек админки.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from rabota import config
from rabota.admin import settings as admin_settings

DEFAULT_BOT_USERNAME = "rabota_dnr_donetsk_bot"
DEFAULT_CHANNEL_USERNAME = "dnr_donetsk_rabota"


def _get(env: Mapping[str, str] | None, name: str, default: str = "") -> str:
    value = env.get(name) if env is not None else config.get(name)
    return (value or default).strip()


@dataclass(frozen=True)
class TelegramConfig:
    token: str = ""
    bot_username: str = DEFAULT_BOT_USERNAME
    channel_id: str = ""
    channel_username: str = DEFAULT_CHANNEL_USERNAME
    proxy: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TelegramConfig:
        return cls(
            token=_get(env, "TG_BOT_TOKEN"),
            bot_username=_get(env, "TG_BOT_USERNAME", DEFAULT_BOT_USERNAME).lstrip("@"),
            channel_id=_get(env, "TG_CHANNEL_ID"),
            channel_username=_get(env, "TG_CHANNEL_USERNAME", DEFAULT_CHANNEL_USERNAME).lstrip("@"),
            proxy=(_get(env, "TELEGRAM_PROXY") or _get(env, "TG_PROXY")),
        )

    @property
    def bot_ready(self) -> bool:
        """Есть токен и имя бота: можно принимать команды и слать подписчикам."""
        return bool(self.token and self.bot_username)

    @property
    def channel_ready(self) -> bool:
        return bool(self.token and self.channel_id)

    @property
    def bot_url(self) -> str:
        return f"https://t.me/{self.bot_username}" if self.bot_username else ""

    @property
    def channel_url(self) -> str:
        return f"https://t.me/{self.channel_username}" if self.channel_username else ""

    def start_link(self, code: str) -> str:
        return f"{self.bot_url}?start={code}"


def mail_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Можно ли рассылать письма. SMTP не настроен или на проде стоит файловый режим: письма никуда не уйдут,
    а вакансии были бы помечены отправленными, поэтому рассылка не запускается."""
    backend = _get(env, "MAIL_BACKEND", "file").lower()
    if backend == "smtp":
        return bool(_get(env, "SMTP_HOST"))
    return backend == "file" and not config.is_production()


def setting(conn: sqlite3.Connection | None, key: str) -> int | bool:
    return admin_settings.effective(conn, key)  # type: ignore[no-any-return]
