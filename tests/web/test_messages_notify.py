"""Настройки уведомлений, отписка по ссылке из письма, удаление аккаунта с перепиской."""
import re

import pytest

from rabota.messaging import notify
from tests.accounts.harness import PASSWORD, SECRET, env_impl
from tests.messaging.webkit import apply_as_seeker, make_world, second_visitor, stranger, submit, thread_of

env = pytest.fixture(env_impl)


def unsubscribe_path(mail):
    return re.search(r"https://example\.test(/notifications/unsubscribe/\S+)", mail.body).group(1)


def test_settings_page_and_save(env):
    w = make_world(env)
    page = w.boss.client.get("/notifications")
    assert page.status_code == 200 and 'name="responses"' in page.text and 'name="messages"' in page.text
    assert page.text.count("checked") >= 2 and 'name="robots" content="noindex' in page.text
    r = submit(w.boss, "/notifications", {"responses": "1"})
    assert r.status_code == 303 and r.headers["location"] == "/notifications?saved=1"
    row = env.query("SELECT notify_responses, notify_messages FROM users WHERE id=?", w.boss_id)[0]
    assert (row[0], row[1]) == (1, 0)
    assert "Настройки сохранены" in w.boss.client.get("/notifications?saved=1").text


def test_settings_need_login_and_csrf(env):
    assert env.client.get("/notifications", follow_redirects=False).status_code == 303
    w = make_world(env)
    assert w.boss.client.post("/notifications", data={"responses": "1"}).status_code == 403


def test_account_page_links_to_notifications(env):
    w = make_world(env)
    assert 'href="/notifications"' in w.boss.client.get("/account").text


def test_switched_off_responses_means_no_mail(env):
    w = make_world(env)
    submit(w.boss, "/notifications", {"messages": "1"})
    apply_as_seeker(w)
    assert [m for m in env.mailer.outbox if m.to == "boss@example.test"] == []


def test_unsubscribe_link_works_without_login_and_asks_for_confirmation(env):
    w = make_world(env)
    apply_as_seeker(w)
    mail = [m for m in env.mailer.outbox if m.to == "boss@example.test"][-1]
    path = unsubscribe_path(mail)
    guest = second_visitor(env)
    page = guest.client.get(path)
    assert page.status_code == 200 and "Отключить письма" in page.text and f'action="{path}"' in page.text
    assert env.query("SELECT notify_responses FROM users WHERE id=?", w.boss_id)[0][0] == 1  # GET ничего не меняет
    r = guest.client.post(path, data={"csrf_token": re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)},
                          follow_redirects=False)
    assert r.status_code == 200 and "Письма отключены" in r.text
    row = env.query("SELECT notify_responses, notify_messages FROM users WHERE id=?", w.boss_id)[0]
    assert (row[0], row[1]) == (0, 1)


def test_unsubscribe_post_needs_csrf(env):
    w = make_world(env)
    apply_as_seeker(w)
    path = unsubscribe_path([m for m in env.mailer.outbox if m.to == "boss@example.test"][-1])
    guest = second_visitor(env)
    assert guest.client.post(path, data={}).status_code == 403
    assert env.query("SELECT notify_responses FROM users WHERE id=?", w.boss_id)[0][0] == 1


def test_tampered_unsubscribe_link_is_refused(env):
    w = make_world(env)
    guest = second_visitor(env)
    for token in (f"{w.boss_id}.responses.deadbeef", "junk", f"999.messages.{'a' * 32}"):
        assert guest.client.get(f"/notifications/unsubscribe/{token}").status_code == 400
    forged = notify.unsubscribe_token(w.boss_id, "responses", b"wrong" * 8)
    assert guest.client.get(f"/notifications/unsubscribe/{forged}").status_code == 400


def test_unsubscribe_works_with_real_signature(env):
    w = make_world(env)
    token = notify.unsubscribe_token(w.seeker_id, "messages", SECRET.encode())
    guest = second_visitor(env)
    page = guest.client.get(f"/notifications/unsubscribe/{token}")
    assert page.status_code == 200


def test_notification_mails_are_rate_limited_per_recipient(tmp_path):
    from tests.accounts.harness import build_env

    env = build_env(tmp_path, rate_limits={"notify_mail": (1, 3600), "apply_user": (10, 3600)})
    with env.client:
        w = make_world(env)
        from tests.messaging.webkit import new_vacancy

        apply_as_seeker(w)
        other = new_vacancy(env, w.boss_id, profession="повар")
        submit(w.seeker, f"/vacancy/{other}/apply", {"cover": ""})
        assert len([m for m in env.mailer.outbox if m.to == "boss@example.test"]) == 1


def test_deleting_account_over_the_web_wipes_threads_for_both(env):
    w = make_world(env)
    rid = apply_as_seeker(w)
    tid = thread_of(env, rid)
    submit(w.boss, f"/messages/{tid}", {"body": "Привет"})
    r = submit(w.seeker, "/account/delete", {"password": PASSWORD, "confirm": "1"})
    assert r.status_code == 303
    for table in ("applications", "conversations", "messages"):
        assert env.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] == 0
    assert w.boss.client.get(f"/messages/{tid}").status_code == 404
    assert stranger(env).client.get("/responses").status_code == 200
