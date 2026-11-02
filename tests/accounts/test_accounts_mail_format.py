"""Формат писем: тема, текстовая и HTML-части, заголовки, кодировка, кириллические домены."""
import email as email_lib
import re
from email import policy

import pytest

from rabota.accounts import mailer
from rabota.accounts.mailer import Mail, ascii_address, build_message, render_mail

SENDER = "noreply@xn----7sbadrg9awfdp1a2d.xn--p1ai"


def parse(msg):
    return email_lib.message_from_bytes(bytes(msg), policy=policy.default)


def code_mail(kind="verify_code", **over):
    ctx = {"name": "Иван", "code": "042917", "site_url": "https://example.test", "ttl_minutes": 15,
           "link": "https://example.test/verify/abc"}
    ctx.update(over)
    return render_mail(kind, "ivan@example.test", **ctx)


def test_subject_has_code_and_site_name():
    assert code_mail().subject == "Код 042917 для регистрации на сайте Работа Донецк"
    assert code_mail("reset_code").subject == "Код 042917 для сброса пароля на сайте Работа Донецк"


@pytest.mark.parametrize("kind", ["verify_code", "reset_code"])
def test_code_mail_has_text_and_html_with_same_code(kind):
    mail = code_mail(kind)
    assert "042917" in mail.body and "042917" in mail.html
    assert "15 минут" in mail.body and "15 минут" in mail.html
    assert "Никому не сообщайте" in mail.body and "Никому не сообщайте" in mail.html
    assert mail.html.lstrip().lower().startswith("<!doctype html>")
    assert 'lang="ru"' in mail.html and "prefers-color-scheme: dark" in mail.html
    assert 'name="color-scheme"' in mail.html


def test_verify_mail_keeps_fallback_link_reset_mail_has_none():
    assert "https://example.test/verify/abc" in code_mail().body
    assert "https://example.test/verify/abc" in code_mail().html
    assert "http" not in code_mail("reset_code", link="x").body.replace("https://example.test", "")


def test_html_escapes_user_name():
    mail = code_mail(name="<script>alert(1)</script>")
    assert "<script>alert(1)" not in mail.html
    assert "&lt;script&gt;" in mail.html


def test_message_is_multipart_alternative_utf8():
    msg = parse(build_message(code_mail(), SENDER))
    assert msg.get_content_type() == "multipart/alternative"
    kinds = [p.get_content_type() for p in msg.iter_parts()]
    assert kinds == ["text/plain", "text/html"]
    text = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert "042917" in text and "042917" in html and "Здравствуйте, Иван!" in text
    for part in msg.iter_parts():
        assert part.get_content_charset() == "utf-8"
    assert msg["Subject"] == "Код 042917 для регистрации на сайте Работа Донецк"


def test_plain_mail_without_html_is_single_part():
    msg = parse(build_message(Mail("a@example.test", "Тема", "Тело"), SENDER))
    assert msg.get_content_type() == "text/plain"


def test_headers_are_complete_and_transactional():
    msg = parse(build_message(code_mail(), SENDER))
    assert msg["Date"] and re.match(r"^\w{3}, \d{1,2} \w{3} \d{4} \d\d:\d\d:\d\d \+0000$", msg["Date"])
    assert msg["Message-ID"].endswith("@xn----7sbadrg9awfdp1a2d.xn--p1ai>")
    assert "Работа Донецк" in str(msg["From"]) and SENDER in str(msg["From"])
    assert msg["To"] == "ivan@example.test"
    assert msg["Auto-Submitted"] == "auto-generated"
    assert msg["X-Auto-Response-Suppress"] == "All"
    assert msg["List-Unsubscribe"] is None and msg["Reply-To"] is None
    ids = {parse(build_message(code_mail(), SENDER))["Message-ID"] for _ in range(3)}
    assert len(ids) == 3


def test_from_display_name_is_rfc2047_encoded_on_the_wire():
    raw = bytes(build_message(code_mail(), SENDER))
    header = next(line for line in raw.decode("ascii").splitlines() if line.startswith("From:"))
    assert "=?utf-8?" in header.lower()  # на проводе только ASCII


def test_wire_format_is_ascii_headers():
    raw = bytes(build_message(code_mail(), SENDER))
    head = raw.split(b"\n\n", 1)[0]
    head.decode("ascii")  # не должно упасть


def test_cyrillic_domains_become_punycode():
    assert ascii_address("noreply@работа-донецк.рф") == SENDER
    assert ascii_address("иван@пример.рф") == "иван@xn--e1afmkfd.xn--p1ai"  # локальная часть как есть
    assert ascii_address("a@example.test") == "a@example.test"
    assert ascii_address("not-an-address") == "not-an-address"


def test_recipient_with_cyrillic_domain_is_encoded():
    msg = parse(build_message(Mail("ivan@пример.рф", "Тема", "Тело"), SENDER))
    assert msg["To"] == "ivan@xn--e1afmkfd.xn--p1ai"


def test_sender_with_cyrillic_domain_gets_message_id_in_punycode():
    msg = parse(build_message(Mail("a@example.test", "Тема", "Тело"), "noreply@работа-донецк.рф"))
    assert SENDER in str(msg["From"]) and "xn----7sbadrg9awfdp1a2d.xn--p1ai" in msg["Message-ID"]


def test_sender_with_display_name_is_kept():
    msg = parse(build_message(Mail("a@example.test", "Т", "Т"), "Сайт <noreply@example.test>"))
    assert "noreply@example.test" in str(msg["From"]) and msg["Message-ID"].endswith("@example.test>")


def test_header_injection_in_sender_is_refused():
    with pytest.raises(mailer.MailError):
        build_message(Mail("a@example.test", "s", "b"), "noreply@example.test\r\nBcc: x@example.test")


def test_readable_mode_keeps_utf8_body():
    raw = bytes(build_message(code_mail(), SENDER, readable=True)).decode("utf-8")
    assert "Здравствуйте, Иван!" in raw
