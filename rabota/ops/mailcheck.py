"""Проверка почты на сервере: python -m rabota mail-check [--send-to адрес].

Без аргументов ничего не отправляется: собирается письмо с кодом (как при регистрации, код пробный), печатаются его
заголовки и размеры частей, и проверяется соединение с SMTP-сервером (подключение, EHLO, STARTTLS, вход), без
отправки. С --send-to уходит одно проверочное письмо на указанный адрес. Пароли и тексты кодов не печатаются.
"""
from __future__ import annotations

import argparse
import smtplib
import ssl
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from email.message import EmailMessage

from rabota import config
from rabota.accounts import codes
from rabota.accounts.mailer import (
    Mailer,
    MailError,
    SmtpMailer,
    ascii_address,
    build_message,
    default_sender,
    make_mailer,
    render_mail,
)

SAMPLE_CODE = "000000"


def describe(msg: EmailMessage) -> list[str]:
    """Заголовки и устройство письма для глаз администратора (без текста и кода)."""
    lines = []
    for name in ("From", "To", "Subject", "Date", "Message-ID", "Auto-Submitted"):
        value = str(msg[name] or "")
        if name == "Subject":
            value = value.replace(SAMPLE_CODE, "XXXXXX")
        lines.append(f"{name}: {value}")
    for part in msg.walk():
        if not part.is_multipart():
            payload = part.get_payload(decode=True) or b""
            lines.append(f"часть {part.get_content_type()}: {len(payload)} байт, {part.get_content_charset()}")
    return lines


def probe_smtp(env: Mapping[str, str] | None = None, timeout: float = 10.0) -> str:
    """Подключиться к SMTP, дойти до входа и выйти, ничего не отправляя. Возвращает описание шагов."""

    def get(name: str, default: str = "") -> str:
        return env.get(name, default) if env is not None else config.get(name, default)

    host = get("SMTP_HOST")
    security = get("SMTP_SECURITY", "starttls").lower()
    port = int(get("SMTP_PORT", "465" if security == "ssl" else "587"))
    user, password = get("SMTP_USER"), get("SMTP_PASSWORD")
    steps = [f"{host}:{port} ({security})"]
    try:
        if security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
        with client:
            code, _ = client.ehlo()
            steps.append(f"EHLO {code}")
            if security == "starttls":
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
                steps.append("STARTTLS ok")
            if user:
                client.login(user, password)
                steps.append("вход ok")
            else:
                steps.append("без входа")
            client.noop()
    except (smtplib.SMTPException, OSError) as exc:
        steps.append(f"ОШИБКА {type(exc).__name__}")
        raise MailError("; ".join(steps)) from exc
    return "; ".join(steps)


def run(send_to: str | None = None, mailer: Mailer | None = None, out=None) -> int:
    out = out or sys.stdout
    site_url = (config.get("SITE_URL") or "").rstrip("/")
    sender = default_sender()
    print(f"режим: {config.get('MAIL_BACKEND') or 'file'}; отправитель: {ascii_address(sender)}", file=out)
    sample = render_mail(
        "verify_code", "check@example.invalid", name="Проверка", code=SAMPLE_CODE, link=f"{site_url}/verify/sample",
        site_url=site_url, ttl_minutes=int(codes.CODE_TTL["verify"].total_seconds() // 60),
    )
    msg = build_message(sample, sender)
    print("письмо с кодом собрано:", file=out)
    for line in describe(msg):
        print("  " + line, file=out)
    code = 0
    if (config.get("MAIL_BACKEND") or "file").lower() == "smtp":
        try:
            print("SMTP: " + probe_smtp(), file=out)
        except MailError as exc:
            print(f"SMTP: {exc}", file=out)
            code = 1
    if send_to and code == 0:
        mailer = mailer or make_mailer()
        test = render_mail(
            "mail_test", send_to, site_url=site_url, sent_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        )
        try:
            mailer.send(test)
        except MailError as exc:
            print(f"отправка: ОШИБКА {exc}", file=out)
            return 1
        print(f"отправлено одно проверочное письмо на {send_to}", file=out)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rabota mail-check")
    parser.add_argument("--send-to", help="отправить одно проверочное письмо на этот адрес")
    args = parser.parse_args(argv)
    return run(args.send_to)


__all__ = ["SmtpMailer", "describe", "main", "probe_smtp", "run"]
