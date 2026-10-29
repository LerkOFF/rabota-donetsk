"""Почта: интерфейс Mailer и три реализации.

FileMailer пишет письма в data/outbox/ как .eml (по умолчанию, ссылки подтверждения читаются локально),
SmtpMailer отправляет по SMTP, MemoryMailer хранит письма в списке для тестов. Выбор по MAIL_BACKEND (file|smtp).
Тексты писем лежат в templates/email/*.txt, по-русски, обычным текстом.
"""
from __future__ import annotations

import logging
import re
import secrets
import smtplib
import ssl
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from rabota import config

log = logging.getLogger(__name__)

EMAIL_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "web" / "templates" / "email"
SITE_NAME = "Работа Донецк"

SUBJECTS = {
    "verify": "Подтвердите почту на сайте Работа Донецк",
    "already_registered": "Вы уже зарегистрированы на сайте Работа Донецк",
    "reset": "Сброс пароля на сайте Работа Донецк",
    "change_email": "Подтвердите новый адрес почты на сайте Работа Донецк",
    "email_changed": "Адрес почты в вашем аккаунте изменён",
    "password_changed": "Пароль в вашем аккаунте изменён",
}


class MailError(Exception):
    """Письмо не отправлено. Текст ошибки не содержит ссылок и токенов."""


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    body: str


class Mailer:
    def send(self, mail: Mail) -> None:  # pragma: no cover - интерфейс
        raise NotImplementedError


class MemoryMailer(Mailer):
    """Для тестов: письма копятся в outbox. fail=True имитирует сбой почтового сервера."""

    def __init__(self, fail: bool = False):
        self.outbox: list[Mail] = []
        self.fail = fail

    def send(self, mail: Mail) -> None:
        if self.fail:
            raise MailError("имитация сбоя отправки")
        self.outbox.append(mail)

    def last(self, to: str | None = None) -> Mail | None:
        for mail in reversed(self.outbox):
            if to is None or mail.to == to:
                return mail
        return None


class FileMailer(Mailer):
    """Сохраняет письма как .eml в папку (по умолчанию data/outbox/): удобно читать ссылки при разработке."""

    def __init__(self, directory: Path | str | None = None, sender: str = ""):
        self.directory = Path(directory) if directory else config.DB_PATH.parent / "outbox"
        self.sender = sender or default_sender()
        self._lock = threading.Lock()

    def send(self, mail: Mail) -> None:
        msg = build_message(mail, self.sender, readable=True)
        safe_to = re.sub(r"[^a-z0-9@._-]", "_", mail.to.lower())[:60]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        name = f"{stamp}-{secrets.token_hex(3)}-{safe_to}.eml"
        try:
            with self._lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                (self.directory / name).write_bytes(bytes(msg))
        except OSError as exc:
            raise MailError(f"не удалось записать письмо: {type(exc).__name__}") from exc


class SmtpMailer(Mailer):
    """SMTP: security = starttls (порт 587), ssl (порт 465) или none."""

    def __init__(
        self, host: str, port: int = 587, user: str = "", password: str = "", sender: str = "",
        security: str = "starttls", timeout: float = 15.0,
    ):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.sender = sender or user or default_sender()
        self.security = security.lower()
        self.timeout = timeout

    def send(self, mail: Mail) -> None:
        msg = build_message(mail, self.sender)
        try:
            if self.security == "ssl":
                client: smtplib.SMTP = smtplib.SMTP_SSL(
                    self.host, self.port, timeout=self.timeout, context=ssl.create_default_context()
                )
            else:
                client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            with client:
                if self.security == "starttls":
                    client.starttls(context=ssl.create_default_context())
                if self.user:
                    client.login(self.user, self.password)
                client.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"SMTP: {type(exc).__name__}") from exc


def default_sender() -> str:
    return config.get("SMTP_FROM") or "noreply@localhost"


def build_message(mail: Mail, sender: str, readable: bool = False) -> EmailMessage:
    """readable=True: тело письма как есть в UTF-8 (8bit), файл .eml читается в любом редакторе."""
    if any(ch in mail.to for ch in "\r\n"):
        raise MailError("недопустимый адрес получателя")
    msg = EmailMessage()
    msg["Subject"] = mail.subject
    msg["From"] = sender if "<" in sender else formataddr((SITE_NAME, sender))
    msg["To"] = mail.to
    msg["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg["Message-ID"] = make_msgid(domain="rabota.local")
    msg.set_content(mail.body, charset="utf-8", cte="8bit" if readable else "base64")
    return msg


def make_mailer(env: Mapping[str, str] | None = None, outbox_dir: Path | str | None = None) -> Mailer:
    """MAIL_BACKEND=file (по умолчанию) или smtp. Настройки SMTP из SMTP_HOST, SMTP_PORT, SMTP_USER и т.д."""

    def get(name: str, default: str = "") -> str:
        return env.get(name, default) if env is not None else config.get(name, default)

    backend = get("MAIL_BACKEND", "file").lower()
    if backend == "smtp":
        host = get("SMTP_HOST")
        if not host:
            raise ValueError("MAIL_BACKEND=smtp требует SMTP_HOST")
        security = get("SMTP_SECURITY", "starttls").lower()
        if security not in ("starttls", "ssl", "none"):
            raise ValueError("SMTP_SECURITY: starttls, ssl или none")
        default_port = "465" if security == "ssl" else "587"
        return SmtpMailer(
            host=host, port=int(get("SMTP_PORT", default_port)), user=get("SMTP_USER"),
            password=get("SMTP_PASSWORD"), sender=get("SMTP_FROM"), security=security,
        )
    if backend != "file":
        raise ValueError("MAIL_BACKEND: file или smtp")
    return FileMailer(outbox_dir, sender=get("SMTP_FROM"))


_env = Environment(
    loader=FileSystemLoader(str(EMAIL_TEMPLATES_DIR)), autoescape=False, undefined=StrictUndefined,
    keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True,
)


def render_mail(kind: str, to: str, **context: object) -> Mail:
    """Собрать письмо по шаблону templates/email/<kind>.txt. В context нужен site_url."""
    body = _env.get_template(f"{kind}.txt").render(site_name=SITE_NAME, **context)
    return Mail(to=to, subject=SUBJECTS[kind], body=body)


def send_safely(mailer: Mailer, mail: Mail) -> bool:
    """Отправить, не роняя запрос. В журнал идёт только тип ошибки: в письме ссылки с токенами."""
    try:
        mailer.send(mail)
    except MailError as exc:
        log.error("письмо не отправлено: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 - отправка не должна ронять запрос
        log.error("письмо не отправлено: %s", type(exc).__name__)
        return False
    return True
