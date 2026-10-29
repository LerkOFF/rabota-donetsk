"""Удаление аккаунта удаляет переписку и отклики, жалобы обезличиваются."""
import json

import pytest

from rabota.accounts import service as accounts
from rabota.messaging import rules
from rabota.messaging import service as msg
from tests.messaging.conftest import NOW, add_user, add_vacancy


@pytest.fixture
def thread(conn, seeker, employer, vacancy):
    res = msg.apply(conn, vacancy_id=vacancy, user=seeker, cover="Здравствуйте!", share_phone=True, now=NOW)
    msg.send_message(conn, res["conversation_id"], employer["id"], "Приходите", NOW)
    return {"id": res["conversation_id"], "app": res["application_id"], "s": seeker, "e": employer}


def counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("applications", "conversations", "messages", "user_blocks", "reports")}


def test_deleting_seeker_removes_responses_threads_messages_blocks(conn, thread):
    msg.block(conn, thread["id"], thread["s"]["id"], NOW)
    accounts.delete_account(conn, thread["s"]["id"])
    assert counts(conn) == {"applications": 0, "conversations": 0, "messages": 0, "user_blocks": 0, "reports": 0}
    assert accounts.get_user(conn, thread["e"]["id"]) is not None


def test_deleting_employer_removes_vacancies_and_threads(conn, thread):
    accounts.delete_account(conn, thread["e"]["id"])
    assert counts(conn)["conversations"] == 0 and counts(conn)["messages"] == 0
    assert counts(conn)["applications"] == 0
    assert conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 0


def test_other_peoples_threads_stay(conn, thread, employer):
    other = add_user(conn, "o@example.test", "job_seeker", "Другой")
    v2 = add_vacancy(conn, employer["id"], profession="повар")
    msg.apply(conn, vacancy_id=v2, user=other, cover="Привет", share_phone=False, now=NOW)
    accounts.delete_account(conn, thread["s"]["id"])
    assert counts(conn)["conversations"] == 1 and counts(conn)["messages"] == 1


def test_report_about_deleted_senders_message_is_anonymised_and_kept(conn, thread):
    mid = conn.execute("SELECT id FROM messages WHERE sender_user_id=?", (thread["s"]["id"],)).fetchone()[0]
    msg.report_message(conn, thread["id"], mid, thread["e"]["id"], rules.REPORT_REASONS[0], "Просит деньги", NOW)
    accounts.delete_account(conn, thread["s"]["id"])
    row = conn.execute("SELECT * FROM reports").fetchone()
    snap = json.loads(row["snapshot"])
    assert snap["sender_user_id"] is None and snap["body"] == "Здравствуйте!"
    assert row["reporter_user_id"] == thread["e"]["id"]


def test_reporters_free_text_is_scrubbed_when_reporter_is_deleted(conn, thread):
    mid = conn.execute("SELECT id FROM messages WHERE sender_user_id=?", (thread["s"]["id"],)).fetchone()[0]
    msg.report_message(conn, thread["id"], mid, thread["e"]["id"], "Другое", "Он мне звонил домой", NOW)
    accounts.delete_account(conn, thread["e"]["id"])
    row = conn.execute("SELECT * FROM reports").fetchone()
    assert row["reporter_user_id"] is None
    snap = json.loads(row["snapshot"])
    assert snap["comment"] == "" and "звонил" not in row["reason"]
    assert row["reason"] == "Другое"
