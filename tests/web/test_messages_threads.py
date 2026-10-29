"""Переписка по отклику на сайте."""
import re

import pytest

from tests.accounts.harness import build_env, env_impl
from tests.messaging.webkit import apply_as_seeker, make_world, stranger, submit, thread_of

env = pytest.fixture(env_impl)


@pytest.fixture
def chat(env):
    w = make_world(env)
    rid = apply_as_seeker(w)
    return w, rid, thread_of(env, rid)


def count_messages(env):
    return env.query("SELECT COUNT(*) AS n FROM messages")[0]["n"]


def test_thread_page_for_seeker(chat):
    w, rid, tid = chat
    r = w.seeker.client.get(f"/messages/{tid}")
    assert r.status_code == 200
    for needle in (f'action="/messages/{tid}"', 'name="body"', 'name="csrf_token"', "Здравствуйте, хочу работать",
                   "Кофейня Зёрна", "Бариста", f'href="/responses/{rid}"', "Не переводите деньги"):
        assert needle in r.text, needle
    assert 'name="robots" content="noindex' in r.text and r.headers["cache-control"] == "no-store"
    assert "maxlength=\"2000\"" in r.text


def test_employer_replies_and_seeker_sees_it_unread_then_read(chat):
    w, _rid, tid = chat
    r = submit(w.boss, f"/messages/{tid}", {"body": "Добрый день! Приходите завтра в 10:00"})
    assert r.status_code == 303 and r.headers["location"] == f"/messages/{tid}"
    inbox = w.seeker.client.get("/messages")
    assert "Приходите завтра" in inbox.text and 'data-unread-count="1"' in inbox.text
    page = w.seeker.client.get(f"/messages/{tid}")
    assert "Приходите завтра в 10:00" in page.text
    assert 'data-unread-count="0"' in w.seeker.client.get("/messages").text
    assert "Прочитано" in w.boss.client.get(f"/messages/{tid}").text


def test_opening_the_thread_marks_only_incoming_as_read(chat, env):
    w, _rid, tid = chat
    submit(w.boss, f"/messages/{tid}", {"body": "Ответ работодателя"})
    w.boss.client.get(f"/messages/{tid}")
    rows = {r["sender_user_id"]: r["read_at"] for r in env.query("SELECT sender_user_id, read_at FROM messages")}
    assert rows[w.seeker_id]  # сопроводительное письмо прочитал работодатель
    assert rows[w.boss_id] is None  # его собственное сообщение соискатель ещё не открывал


def test_csrf_and_origin_are_required(chat, env):
    w, _rid, tid = chat
    assert w.boss.client.post(f"/messages/{tid}", data={"body": "Привет"}).status_code == 403
    assert w.boss.client.post(f"/messages/{tid}", data={"body": "Привет", "csrf_token": "x"}).status_code == 403
    assert submit(w.boss, f"/messages/{tid}", {"body": "Привет"}, headers={"origin": "https://evil.test"}).status_code == 403
    assert count_messages(env) == 1
    for action in ("block", "unblock", "report"):
        assert w.boss.client.post(f"/messages/{tid}/{action}", data={}).status_code == 403


def test_stranger_gets_404_everywhere(chat, env):
    w, _rid, tid = chat
    for visitor in (stranger(env, "s9@example.test"), stranger(env, "e9@example.test", "employer")):
        assert visitor.client.get(f"/messages/{tid}").status_code == 404
        assert submit(visitor, f"/messages/{tid}", {"body": "Я тут"}).status_code == 404
        assert submit(visitor, f"/messages/{tid}/block").status_code == 404
        assert submit(visitor, f"/messages/{tid}/report", {"message_id": "1", "reason": "Другое", "comment": "ну да"}).status_code == 404
    assert w.boss.client.get("/messages/99999").status_code == 404
    assert w.boss.client.get("/messages/abc").status_code == 404
    assert count_messages(env) == 1
    assert env.query("SELECT COUNT(*) AS n FROM user_blocks")[0]["n"] == 0


def test_empty_message_is_refused_with_error(chat, env):
    w, _rid, tid = chat
    r = submit(w.boss, f"/messages/{tid}", {"body": "   "})
    assert r.status_code == 422 and 'role="alert"' in r.text and "Напишите сообщение" in r.text
    assert count_messages(env) == 1


def test_too_long_message_keeps_text(chat, env):
    w, _rid, tid = chat
    r = submit(w.boss, f"/messages/{tid}", {"body": "ы" * 2001})
    assert r.status_code == 422 and "слишком длинное" in r.text.lower() and "ы" * 100 in r.text
    assert count_messages(env) == 1


def test_message_text_is_escaped_and_newlines_kept(chat, env):
    w, _rid, tid = chat
    submit(w.boss, f"/messages/{tid}", {"body": "<script>alert(1)</script>\n\nвторой абзац"})
    html = w.seeker.client.get(f"/messages/{tid}").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html and "второй абзац" in html


def test_pagination_of_long_thread(chat, env):
    w, _rid, tid = chat
    conn = env.db()
    for i in range(45):
        conn.execute("INSERT INTO messages(conversation_id, sender_user_id, body, created_at, read_at) VALUES (?,?,?,?,?)",
                     (tid, w.boss_id, f"сообщение номер {i}", f"2026-10-29T12:{i:02d}:30", "2026-10-29T13:00:00"))
    conn.commit()
    conn.close()
    page = w.seeker.client.get(f"/messages/{tid}")
    assert "сообщение номер 44" in page.text and "сообщение номер 5<" not in page.text
    assert "Показать более ранние" in page.text
    link = re.search(r'href="(/messages/\d+\?before=\d+)"', page.text).group(1)
    older = w.seeker.client.get(link)
    assert older.status_code == 200 and "сообщение номер 5" in older.text and "сообщение номер 44" not in older.text
    assert w.seeker.client.get(f"/messages/{tid}?before=abc").status_code == 200


def test_wait_for_reply_limit_shows_message(chat, env):
    w, _rid, tid = chat
    assert submit(w.seeker, f"/messages/{tid}", {"body": "Второе"}).status_code == 303
    assert submit(w.seeker, f"/messages/{tid}", {"body": "Третье"}).status_code == 303
    r = submit(w.seeker, f"/messages/{tid}", {"body": "Четвёртое"})
    assert r.status_code == 422 and "Дождитесь ответа" in r.text and "Четвёртое" in r.text
    assert count_messages(env) == 3


def test_send_rate_limit_is_429_and_keeps_text(tmp_path):
    env = build_env(tmp_path, rate_limits={"message_user": (2, 600)})
    with env.client:
        w = make_world(env)
        tid = thread_of(env, apply_as_seeker(w))
        assert submit(w.boss, f"/messages/{tid}", {"body": "Первое"}).status_code == 303
        assert submit(w.boss, f"/messages/{tid}", {"body": "Второе"}).status_code == 303
        r = submit(w.boss, f"/messages/{tid}", {"body": "Третье мое"})
        assert r.status_code == 429 and "слишком часто" in r.text.lower() and "Третье мое" in r.text
        assert count_messages(env) == 3


def test_block_and_unblock_in_thread(chat, env):
    w, _rid, tid = chat
    r = submit(w.boss, f"/messages/{tid}/block")
    assert r.status_code == 303 and r.headers["location"] == f"/messages/{tid}"
    boss_page = w.boss.client.get(f"/messages/{tid}")
    assert "Вы заблокировали этого пользователя" in boss_page.text and f'action="/messages/{tid}/unblock"' in boss_page.text
    assert 'name="body"' not in boss_page.text
    seeker_page = w.seeker.client.get(f"/messages/{tid}")
    assert "не принимает сообщения" in seeker_page.text
    assert "Вы заблокировали" not in seeker_page.text and "заблокировал вас" not in seeker_page.text.lower()
    assert submit(w.seeker, f"/messages/{tid}", {"body": "Ответьте"}).status_code == 422
    assert count_messages(env) == 1
    submit(w.boss, f"/messages/{tid}/unblock")
    assert 'name="body"' in w.boss.client.get(f"/messages/{tid}").text
    assert submit(w.seeker, f"/messages/{tid}", {"body": "Теперь можно"}).status_code == 303


def test_report_message(chat, env):
    w, _rid, tid = chat
    mid = env.query("SELECT id FROM messages")[0]["id"]
    page = w.boss.client.get(f"/messages/{tid}")
    assert f'action="/messages/{tid}/report"' in page.text and f'name="message_id" value="{mid}"' in page.text
    r = submit(w.boss, f"/messages/{tid}/report", {"message_id": str(mid), "reason": "Мошенничество или просьба заплатить", "comment": "Просит деньги"})
    assert r.status_code == 303 and r.headers["location"] == f"/messages/{tid}?reported=1"
    assert "Жалоба принята" in w.boss.client.get(f"/messages/{tid}?reported=1").text
    row = env.query("SELECT * FROM reports")[0]
    assert (row["target_type"], row["target_id"], row["status"]) == ("message", mid, "new")
    assert "Просит деньги" in row["snapshot"] and "Здравствуйте, хочу работать" in row["snapshot"]


def test_report_own_message_or_bad_reason_is_refused(chat, env):
    w, _rid, tid = chat
    mid = env.query("SELECT id FROM messages")[0]["id"]
    assert submit(w.seeker, f"/messages/{tid}/report", {"message_id": str(mid), "reason": "Другое", "comment": "тест тест"}).status_code == 422
    assert submit(w.boss, f"/messages/{tid}/report", {"message_id": str(mid), "reason": "Выдумка", "comment": ""}).status_code == 422
    assert submit(w.boss, f"/messages/{tid}/report", {"message_id": "abc", "reason": "Другое", "comment": "тест тест"}).status_code == 404
    assert env.query("SELECT COUNT(*) AS n FROM reports")[0]["n"] == 0


def test_report_rate_limit(tmp_path):
    env = build_env(tmp_path, rate_limits={"report_user": (1, 3600)})
    with env.client:
        w = make_world(env)
        tid = thread_of(env, apply_as_seeker(w))
        mid = env.query("SELECT id FROM messages")[0]["id"]
        data = {"message_id": str(mid), "reason": "Другое", "comment": "первая жалоба"}
        assert submit(w.boss, f"/messages/{tid}/report", data).status_code == 303
        assert submit(w.boss, f"/messages/{tid}/report", data).status_code == 429


def test_closed_thread_after_withdrawal_and_rejection(chat, env):
    w, rid, tid = chat
    submit(w.boss, f"/responses/{rid}/status", {"status": "rejected"})
    page = w.seeker.client.get(f"/messages/{tid}")
    assert 'name="body"' not in page.text and "рассмотрение" in page.text
    assert submit(w.seeker, f"/messages/{tid}", {"body": "Подумайте ещё"}).status_code == 409
    assert 'name="body"' in w.boss.client.get(f"/messages/{tid}").text
    submit(w.seeker, f"/responses/{rid}/withdraw")
    assert "Отклик отозван" in w.boss.client.get(f"/messages/{tid}").text
    assert 'name="body"' not in w.boss.client.get(f"/messages/{tid}").text


def test_inbox_states(env):
    w = make_world(env)
    assert "Сообщений пока нет" in w.seeker.client.get("/messages").text
    rid = apply_as_seeker(w)
    tid = thread_of(env, rid)
    submit(w.boss, f"/messages/{tid}", {"body": "Привет"})
    w.seeker.client.get(f"/messages/{tid}")
    unread = w.seeker.client.get("/messages?unread=1")
    assert "Нет непрочитанных сообщений" in unread.text and f'href="/messages/{tid}"' not in unread.text
    all_ = w.seeker.client.get("/messages")
    assert f'href="/messages/{tid}"' in all_.text and "Кофейня Зёрна" in all_.text


def test_message_email_arrives_once_per_unread_burst_without_text(chat, env):
    w, _rid, tid = chat
    before = len(env.mailer.outbox)
    submit(w.boss, f"/messages/{tid}", {"body": "Секретная подробность 8 900 000-00-00"})
    submit(w.boss, f"/messages/{tid}", {"body": "Ещё одна подробность"})
    mails = [m for m in env.mailer.outbox[before:] if m.to == "seeker@example.test"]
    assert len(mails) == 1
    assert f"/messages/{tid}" in mails[0].body and "Секретная" not in mails[0].body and "900" not in mails[0].body
    w.seeker.client.get(f"/messages/{tid}")
    submit(w.boss, f"/messages/{tid}", {"body": "Новое после прочтения"})
    assert len([m for m in env.mailer.outbox[before:] if m.to == "seeker@example.test"]) == 2


def test_message_email_respects_the_switch(chat, env):
    w, _rid, tid = chat
    submit(w.seeker, "/notifications", {"messages": ""})
    before = len(env.mailer.outbox)
    submit(w.boss, f"/messages/{tid}", {"body": "Привет"})
    assert [m for m in env.mailer.outbox[before:] if m.to == "seeker@example.test"] == []


def test_mail_failure_does_not_block_sending(tmp_path):
    from rabota.accounts.mailer import MemoryMailer

    env = build_env(tmp_path, mailer=MemoryMailer())
    with env.client:
        w = make_world(env)
        tid = thread_of(env, apply_as_seeker(w))
        env.mailer.fail = True
        assert submit(w.boss, f"/messages/{tid}", {"body": "Привет"}).status_code == 303
        assert count_messages(env) == 2
