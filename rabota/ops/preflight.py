"""Проверка настроек перед запуском на сервере. Секреты не печатает, только имена переменных.

    .venv/bin/python scripts/preflight.py [--env-file .env] [--strict] [--migrate]

Код возврата 1, если есть ошибки (или предупреждения при --strict).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from rabota import legal
from rabota.realtime.telegram_listener import parse_proxy

ERROR, WARN = "ошибка", "предупреждение"
VERIFY_NAME = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,78}\.html$"


@dataclass(frozen=True)
class Problem:
    level: str
    name: str
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.name}: {self.message}"


def check(env: Mapping[str, str]) -> list[Problem]:
    import re

    def val(name: str) -> str:
        return (env.get(name) or "").strip()

    out: list[Problem] = []

    def err(name: str, msg: str) -> None:
        out.append(Problem(ERROR, name, msg))

    def warn(name: str, msg: str) -> None:
        out.append(Problem(WARN, name, msg))

    if val("ENVIRONMENT").lower() != "production":
        err("ENVIRONMENT", "для боевого сервера нужно ENVIRONMENT=production")
    if val("DEBUG").lower() in {"1", "true", "yes", "on"}:
        err("DEBUG", "режим отладки должен быть выключен")

    site = urlparse(val("SITE_URL"))
    if site.scheme != "https" or not site.hostname:
        err("SITE_URL", "нужен адрес сайта с https, например https://работа-донецк.рф")
    elif site.hostname in ("localhost", "127.0.0.1") or site.hostname.endswith(("example.test", "example.com", "example.ru")):
        err("SITE_URL", "адрес-заглушка")

    if val("WEB_TRUST_PROXY") != "1":
        err("WEB_TRUST_PROXY", "сайт стоит за nginx: нужно WEB_TRUST_PROXY=1, иначе лимиты видят один адрес (127.0.0.1)")

    secret = val("SECRET_KEY")
    if not secret:
        warn("SECRET_KEY", "не задан: ключ создастся файлом data/.secret_key; лучше задать явно (32+ символа)")
    elif len(secret) < 32:
        err("SECRET_KEY", "короче 32 символов")

    if val("MAIL_BACKEND").lower() != "smtp":
        err("MAIL_BACKEND", "письма подтверждения и сброса пароля не уйдут: нужно MAIL_BACKEND=smtp")
    else:
        if not val("SMTP_HOST"):
            err("SMTP_HOST", "не задан")
        sender = val("SMTP_FROM")
        if not sender or sender.endswith(("@example.ru", "@localhost", "@example.com")):
            err("SMTP_FROM", "не задан или заглушка")
        if not val("SMTP_USER") or not val("SMTP_PASSWORD"):
            warn("SMTP_USER", "логин или пароль SMTP пусты: нужно, если почтовый сервис требует вход")
        if val("SMTP_SECURITY").lower() not in ("", "starttls", "ssl", "none"):
            err("SMTP_SECURITY", "допустимо starttls, ssl или none")

    missing = legal.missing_settings(env)
    if missing:
        warn("OPERATOR_*", "не заданы " + ", ".join(missing) + ": в юридических текстах останутся заглушки, регистрацию открывать нельзя")
    if not val("CONTACT_EMAIL"):
        warn("CONTACT_EMAIL", "не задана почта для подвала сайта")

    if not (val("LLM_API_KEY") or val("LLM_PROVIDER")):
        warn("LLM_API_KEY", "нет ключа модели: новые афиши-картинки не будут распознаваться")
    if not val("VK_ACCESS_TOKEN"):
        warn("VK_ACCESS_TOKEN", "не задан: опрос VK выключен")
    tg_id = val("TG_API_ID")
    if not tg_id or not val("TG_API_HASH"):
        warn("TG_API_ID", "слушатель Telegram выключен (нужны TG_API_ID и TG_API_HASH)")
    elif not tg_id.isdigit():
        err("TG_API_ID", "должен быть числом")
    proxy = val("TELEGRAM_PROXY") or val("TG_PROXY")
    if proxy:
        try:
            parse_proxy(proxy)
        except ValueError as exc:
            err("TELEGRAM_PROXY", str(exc).replace("TG_PROXY", "TELEGRAM_PROXY"))
    else:
        warn("TELEGRAM_PROXY", "не задан: если с сервера Telegram недоступен напрямую, слушатель и сборщик t.me не заработают")

    if val("METRIKA_ID") and not val("METRIKA_ID").isdigit():
        err("METRIKA_ID", "должен состоять из цифр")
    verify = val("WEBMASTER_VERIFY_FILE")
    if verify and not re.match(VERIFY_NAME, verify):
        err("WEBMASTER_VERIFY_FILE", "ожидается имя файла вида yandex_0123abcd.html")

    if bool(val("ALERT_TG_BOT_TOKEN")) != bool(val("ALERT_TG_CHAT_ID")):
        err("ALERT_TG_BOT_TOKEN", "для уведомлений нужны и токен бота, и ALERT_TG_CHAT_ID")
    if not val("ALERT_TG_BOT_TOKEN"):
        warn("ALERT_TG_BOT_TOKEN", "уведомления о сбоях в Telegram выключены")
    if not val("HEALTH_TOKEN"):
        warn("HEALTH_TOKEN", "не задан: подробности /healthz/deep недоступны")
    if val("DATABASE_URL"):
        warn("DATABASE_URL", "не используется: сайт работает на SQLite, путь задаёт DB_PATH")
    db_path = val("DB_PATH")
    if db_path and not Path(db_path).is_absolute():
        warn("DB_PATH", "лучше абсолютный путь")
    return out
