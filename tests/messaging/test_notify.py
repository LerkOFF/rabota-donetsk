"""Письма о новых откликах и сообщениях: тексты, настройки, отписка."""
from datetime import timedelta

import pytest

from rabota.messaging import notify
from rabota.messaging import service as msg
from tests.messaging.conftest import NOW

SITE = "https://example.test"
SECRET = b"s" * 32
PHONE = "+79491112233"


@pytest.fixture
def thread(conn, seeker, employer, vacancy):
    res = msg.apply(conn, vacancy_id=vacancy, user=seeker, cover="Мой телефон 8 949 111-22-33, пишите на seeker@example.test",
                    share_phone=True, now=NOW)
    return {"id": res["conversation_id"], "app": res["application_id"], "s": seeker, "e": employer}


def test_response_mail_goes_to_employer_without_personal_data(conn, thread):
    mail = notify.response_mail(conn, thread["app"], site_url=SITE, secret=SECRET)
    assert mail is not None and mail.to == "boss@example.test"
    assert "бариста" in mail.subject
    assert f"{SITE}/responses/{thread['app']}" in mail.body
    assert "/notifications/unsubscribe/" in mail.body and f"{SITE}/notifications" in mail.body
    for private in ("Мария", "Петрова", PHONE, "949", "seeker@example.test", "Мой телефон"):
        assert private not in mail.subject + mail.body


def test_message_mail_has_link_but_not_text(conn, thread):
    mid = msg.send_message(conn, thread["id"], thread["e"]["id"], "Секретный текст с номером 8 900 000-00-00", NOW + timedelta(minutes=1))
    mail = notify.message_mail(conn, thread["id"], mid, site_url=SITE, secret=SECRET)
    assert mail is not None and mail.to == "seeker@example.test"
    assert f"{SITE}/messages/{thread['id']}" in mail.body
    assert "работодател" in mail.body.lower()
    for private in ("Секретный", "8 900", "Олег", "Босс", "boss@example.test"):
        assert private not in mail.subject + mail.body
    assert "бариста" in mail.subject


def test_message_mail_from_seeker_says_seeker(conn, thread):
    msg.send_message(conn, thread["id"], thread["e"]["id"], "Приходите", NOW + timedelta(minutes=1))
    msg.mark_read(conn, thread["id"], thread["e"]["id"], NOW + timedelta(minutes=1))  # работодатель открыл переписку
    mid = msg.send_message(conn, thread["id"], thread["s"]["id"], "Хорошо", NOW + timedelta(minutes=2))
    mail = notify.message_mail(conn, thread["id"], mid, site_url=SITE, secret=SECRET)
    assert mail.to == "boss@example.test" and "соискател" in mail.body.lower()
    assert "Мария" not in mail.body


def test_only_first_unread_message_triggers_mail(conn, thread):
    m1 = msg.send_message(conn, thread["id"], thread["e"]["id"], "Раз", NOW + timedelta(minutes=1))
    assert notify.message_mail(conn, thread["id"], m1, site_url=SITE, secret=SECRET) is not None
    m2 = msg.send_message(conn, thread["id"], thread["e"]["id"], "Два", NOW + timedelta(minutes=2))
    assert notify.message_mail(conn, thread["id"], m2, site_url=SITE, secret=SECRET) is None
    msg.mark_read(conn, thread["id"], thread["s"]["id"], NOW + timedelta(minutes=3))
    m3 = msg.send_message(conn, thread["id"], thread["e"]["id"], "Три", NOW + timedelta(minutes=4))
    assert notify.message_mail(conn, thread["id"], m3, site_url=SITE, secret=SECRET) is not None


def test_switched_off_notifications_send_nothing(conn, thread):
    msg.set_prefs(conn, thread["e"]["id"], responses=False, messages=True)
    assert notify.response_mail(conn, thread["app"], site_url=SITE, secret=SECRET) is None
    msg.set_prefs(conn, thread["s"]["id"], responses=True, messages=False)
    mid = msg.send_message(conn, thread["id"], thread["e"]["id"], "Привет", NOW + timedelta(minutes=1))
    assert notify.message_mail(conn, thread["id"], mid, site_url=SITE, secret=SECRET) is None


def test_no_mail_to_blocked_or_unverified_recipient(conn, thread):
    conn.execute("UPDATE users SET status='blocked' WHERE id=?", (thread["e"]["id"],))
    conn.commit()
    assert notify.response_mail(conn, thread["app"], site_url=SITE, secret=SECRET) is None


def test_prefs_roundtrip_defaults_on(conn, employer):
    assert msg.get_prefs(conn, employer["id"]) == {"responses": True, "messages": True}
    msg.set_prefs(conn, employer["id"], responses=False, messages=True)
    assert msg.get_prefs(conn, employer["id"]) == {"responses": False, "messages": True}


def test_unsubscribe_token_roundtrip_and_tamper():
    token = notify.unsubscribe_token(42, "messages", SECRET)
    assert notify.parse_unsubscribe_token(token, SECRET) == (42, "messages")
    assert notify.parse_unsubscribe_token(token, b"other" * 8) is None
    uid, kind, sig = token.split(".")
    assert notify.parse_unsubscribe_token(f"43.{kind}.{sig}", SECRET) is None
    assert notify.parse_unsubscribe_token(f"{uid}.responses.{sig}", SECRET) is None
    assert notify.parse_unsubscribe_token(f"{uid}.{kind}.{sig[:-1]}x", SECRET) is None
    for junk in ("", "a.b", "1.messages", "x.messages.abc", "1.hack.abc", "." * 3, "a" * 500):
        assert notify.parse_unsubscribe_token(junk, SECRET) is None


def test_unsubscribe_switches_off_exactly_that_kind(conn, employer):
    msg.unsubscribe(conn, employer["id"], "messages")
    assert msg.get_prefs(conn, employer["id"]) == {"responses": True, "messages": False}
    msg.unsubscribe(conn, employer["id"], "all")
    assert msg.get_prefs(conn, employer["id"]) == {"responses": False, "messages": False}
