"""python -m rabota mail-check: письмо с кодом собирается, SMTP проверяется, лишнего не отправляется."""
import io
import smtplib
from typing import ClassVar

import pytest

from rabota import __main__ as cli
from rabota.accounts.mailer import MemoryMailer
from rabota.ops import mailcheck


class FakeSMTP:
    instances: ClassVar[list] = []
    fail = False

    def __init__(self, host, port, timeout=None, context=None):
        if FakeSMTP.fail:
            raise OSError("down")
        self.calls = [("connect", host, port)]
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def ehlo(self):
        self.calls.append("ehlo")
        return 250, b"ok"

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))

    def noop(self):
        self.calls.append("noop")

    def send_message(self, msg):  # проверка не должна ничего отправлять
        self.calls.append("SEND")


@pytest.fixture
def smtp_env(monkeypatch):
    FakeSMTP.instances, FakeSMTP.fail = [], False
    monkeypatch.setattr(mailcheck.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailcheck.smtplib, "SMTP_SSL", FakeSMTP)
    for k, v in {"MAIL_BACKEND": "smtp", "SMTP_HOST": "127.0.0.1", "SMTP_PORT": "25", "SMTP_SECURITY": "none",
                 "SMTP_FROM": "noreply@работа-донецк.рф", "SITE_URL": "https://example.test",
                 "SMTP_USER": "", "SMTP_PASSWORD": "s3cret-pass"}.items():
        monkeypatch.setenv(k, v)


def test_check_builds_code_mail_and_probes_without_sending(smtp_env):
    out = io.StringIO()
    assert mailcheck.run(out=out) == 0
    text = out.getvalue()
    assert "noreply@xn----7sbadrg9awfdp1a2d.xn--p1ai" in text
    assert "Subject: Код XXXXXX для регистрации" in text and "000000" not in text
    assert "text/plain" in text and "text/html" in text and "utf-8" in text
    assert "SMTP: 127.0.0.1:25 (none)" in text and "без входа" in text
    assert "s3cret-pass" not in text
    assert all("SEND" not in inst.calls for inst in FakeSMTP.instances)


def test_check_reports_smtp_failure(smtp_env):
    FakeSMTP.fail = True
    out = io.StringIO()
    assert mailcheck.run(out=out) == 1
    assert "ОШИБКА OSError" in out.getvalue()


def test_check_sends_exactly_one_test_mail_when_asked(smtp_env):
    out, mailer = io.StringIO(), MemoryMailer()
    assert mailcheck.run("owner@example.test", mailer=mailer, out=out) == 0
    assert [m.to for m in mailer.outbox] == ["owner@example.test"]
    assert mailer.outbox[0].subject == "Проверка почты сайта Работа Донецк"
    assert "проверочное письмо" in mailer.outbox[0].body


def test_check_does_not_send_when_smtp_is_down(smtp_env):
    FakeSMTP.fail = True
    mailer = MemoryMailer()
    assert mailcheck.run("owner@example.test", mailer=mailer, out=io.StringIO()) == 1
    assert mailer.outbox == []


def test_probe_with_login_and_starttls(smtp_env):
    env = {"SMTP_HOST": "smtp.example.test", "SMTP_SECURITY": "starttls", "SMTP_USER": "u", "SMTP_PASSWORD": "p"}
    summary = mailcheck.probe_smtp(env)
    assert "STARTTLS ok" in summary and "вход ok" in summary
    assert ("login", "u") in FakeSMTP.instances[-1].calls


def test_probe_smtp_error_is_mailerror(monkeypatch):
    class Bad(FakeSMTP):
        def noop(self):
            raise smtplib.SMTPServerDisconnected("x")

    monkeypatch.setattr(mailcheck.smtplib, "SMTP", Bad)
    with pytest.raises(mailcheck.MailError):
        mailcheck.probe_smtp({"SMTP_HOST": "h", "SMTP_SECURITY": "none"})


def test_cli_command_is_wired(smtp_env, capsys):
    assert cli.main(["mail-check"]) == 0
    assert "письмо с кодом собрано" in capsys.readouterr().out
