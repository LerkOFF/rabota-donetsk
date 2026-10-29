import email as email_lib
import smtplib
from email import policy
from typing import ClassVar

import pytest
from jinja2.exceptions import UndefinedError

from rabota.accounts import mailer
from rabota.accounts.mailer import (
    FileMailer,
    Mail,
    MailError,
    MemoryMailer,
    SmtpMailer,
    make_mailer,
    render_mail,
    send_safely,
)

MAIL = Mail(to="ivan@example.test", subject="Тема письма", body="Привет, мир!\nhttps://example.test/verify/abc\n")


def test_memory_mailer_collects_and_finds_last():
    m = MemoryMailer()
    m.send(MAIL)
    m.send(Mail("other@example.test", "s", "b"))
    assert len(m.outbox) == 2
    assert m.last("ivan@example.test") == MAIL
    assert m.last().to == "other@example.test"
    assert m.last("nobody@example.test") is None


def test_memory_mailer_can_fail():
    with pytest.raises(MailError):
        MemoryMailer(fail=True).send(MAIL)


def test_file_mailer_writes_readable_eml(tmp_path):
    fm = FileMailer(tmp_path / "outbox", sender="noreply@example.test")
    fm.send(MAIL)
    files = list((tmp_path / "outbox").glob("*.eml"))
    assert len(files) == 1 and "ivan@example.test" in files[0].name
    msg = email_lib.message_from_bytes(files[0].read_bytes(), policy=policy.default)
    assert msg["To"] == "ivan@example.test"
    assert msg["Subject"] == "Тема письма"
    assert "noreply@example.test" in msg["From"]
    assert "https://example.test/verify/abc" in msg.get_content()


def test_file_mailer_does_not_overwrite(tmp_path):
    fm = FileMailer(tmp_path)
    for _ in range(3):
        fm.send(MAIL)
    assert len(list(tmp_path.glob("*.eml"))) == 3


def test_file_mailer_unwritable_dir_raises_mailerror(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(MailError):
        FileMailer(blocker / "sub").send(MAIL)


def test_header_injection_is_refused(tmp_path):
    with pytest.raises(MailError):
        FileMailer(tmp_path).send(Mail("a@example.test\r\nBcc: x@example.test", "s", "b"))
    assert not list(tmp_path.glob("*.eml"))


class FakeSMTP:
    instances: ClassVar[list["FakeSMTP"]] = []
    fail_on: str = ""

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls = []
        FakeSMTP.instances.append(self)
        if FakeSMTP.fail_on == "connect":
            raise OSError("no route")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.calls.append("quit")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))
        if FakeSMTP.fail_on == "login":
            raise smtplib.SMTPAuthenticationError(535, b"bad")

    def send_message(self, msg):
        self.calls.append(("send", msg["To"]))
        if FakeSMTP.fail_on == "send":
            raise smtplib.SMTPRecipientsRefused({})


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_on = ""
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_smtp_starttls_flow(fake_smtp):
    SmtpMailer("smtp.example.test", 587, "user", "pw", "noreply@example.test").send(MAIL)
    inst = fake_smtp.instances[0]
    assert (inst.host, inst.port) == ("smtp.example.test", 587)
    assert inst.calls[:3] == ["starttls", ("login", "user"), ("send", "ivan@example.test")]


def test_smtp_ssl_flow_has_no_starttls(fake_smtp):
    SmtpMailer("smtp.example.test", 465, "user", "pw", security="ssl").send(MAIL)
    inst = fake_smtp.instances[0]
    assert inst.context is not None and "starttls" not in inst.calls


def test_smtp_none_security_without_login(fake_smtp):
    SmtpMailer("localhost", 25, security="none").send(MAIL)
    assert fake_smtp.instances[0].calls[0] == ("send", "ivan@example.test")


@pytest.mark.parametrize("where", ["connect", "login", "send"])
def test_smtp_failures_become_mailerror_without_secrets(fake_smtp, where):
    fake_smtp.fail_on = where
    with pytest.raises(MailError) as exc:
        SmtpMailer("smtp.example.test", 587, "user", "super-secret-pw").send(MAIL)
    assert "super-secret-pw" not in str(exc.value)


def test_make_mailer_default_is_file(tmp_path):
    assert isinstance(make_mailer({}, outbox_dir=tmp_path), FileMailer)


def test_make_mailer_smtp_from_env():
    m = make_mailer({
        "MAIL_BACKEND": "smtp", "SMTP_HOST": "smtp.example.test", "SMTP_PORT": "2525", "SMTP_USER": "u",
        "SMTP_PASSWORD": "p", "SMTP_FROM": "noreply@example.test", "SMTP_SECURITY": "ssl",
    })
    assert isinstance(m, SmtpMailer)
    assert (m.host, m.port, m.user, m.security, m.sender) == ("smtp.example.test", 2525, "u", "ssl", "noreply@example.test")


def test_make_mailer_smtp_default_ports():
    assert make_mailer({"MAIL_BACKEND": "smtp", "SMTP_HOST": "h"}).port == 587
    assert make_mailer({"MAIL_BACKEND": "smtp", "SMTP_HOST": "h", "SMTP_SECURITY": "ssl"}).port == 465


@pytest.mark.parametrize("env", [
    {"MAIL_BACKEND": "smtp"},
    {"MAIL_BACKEND": "pigeon"},
    {"MAIL_BACKEND": "smtp", "SMTP_HOST": "h", "SMTP_SECURITY": "weird"},
])
def test_make_mailer_rejects_bad_settings(env):
    with pytest.raises(ValueError):
        make_mailer(env)


def test_send_safely_swallows_failures():
    assert send_safely(MemoryMailer(), MAIL) is True
    assert send_safely(MemoryMailer(fail=True), MAIL) is False

    class Boom(mailer.Mailer):
        def send(self, mail):
            raise RuntimeError("secret https://example.test/verify/TOKEN")

    assert send_safely(Boom(), MAIL) is False


def test_send_safely_log_has_no_links(caplog):
    class Boom(mailer.Mailer):
        def send(self, mail):
            raise RuntimeError("https://example.test/verify/TOKEN123")

    with caplog.at_level("ERROR"):
        send_safely(Boom(), MAIL)
    assert "TOKEN123" not in caplog.text


CONTEXT = {
    "verify": {"name": "Иван", "link": "https://example.test/verify/T1"},
    "already_registered": {},
    "reset": {"name": "Иван", "link": "https://example.test/reset/T2"},
    "change_email": {"name": "Иван", "link": "https://example.test/email/confirm/T3"},
    "email_changed": {"name": "Иван", "new_email": "new@example.test"},
    "password_changed": {"name": "Иван"},
}


@pytest.mark.parametrize("kind", sorted(CONTEXT))
def test_letters_render_in_russian_with_site_link(kind):
    m = render_mail(kind, "ivan@example.test", site_url="https://example.test", **CONTEXT[kind])
    assert m.to == "ivan@example.test"
    assert m.subject and any("а" <= ch <= "я" or "А" <= ch <= "Я" for ch in m.subject)
    assert "https://example.test" in m.body
    dash = chr(0x2014)
    assert dash not in m.body and dash not in m.subject
    for value in CONTEXT[kind].values():
        assert value in m.body


def test_verify_and_reset_letters_state_lifetime():
    v = render_mail("verify", "a@example.test", site_url="https://example.test", **CONTEXT["verify"])
    r = render_mail("reset", "a@example.test", site_url="https://example.test", **CONTEXT["reset"])
    assert "24 часа" in v.body and "1 час" in r.body


def test_missing_template_variable_is_an_error():
    with pytest.raises(UndefinedError):
        render_mail("verify", "a@example.test", site_url="https://example.test")


def test_file_mailer_body_is_human_readable(tmp_path):
    FileMailer(tmp_path).send(MAIL)
    raw = next(tmp_path.glob("*.eml")).read_bytes().decode("utf-8")
    assert "https://example.test/verify/abc" in raw and "Привет, мир!" in raw


def test_smtp_message_body_is_transport_safe(fake_smtp):
    msg = mailer.build_message(MAIL, "noreply@example.test")
    assert msg["Content-Transfer-Encoding"] in ("base64", "quoted-printable")
