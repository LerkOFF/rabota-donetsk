"""Блокировка собеседника и жалобы на сообщения."""
import json
from datetime import timedelta

import pytest

from rabota.messaging import rules
from rabota.messaging import service as msg
from tests.messaging.conftest import NOW, add_user, add_vacancy


@pytest.fixture
def thread(conn, seeker, employer, vacancy):
    res = msg.apply(conn, vacancy_id=vacancy, user=seeker, cover="Здравствуйте!", share_phone=True, now=NOW)
    return {"id": res["conversation_id"], "app": res["application_id"], "s": seeker, "e": employer}


def test_block_stops_the_blocked_side_and_the_blocker(conn, thread):
    msg.block(conn, thread["id"], thread["e"]["id"], NOW)
    assert conn.execute("SELECT COUNT(*) FROM user_blocks").fetchone()[0] == 1
    with pytest.raises(rules.MessagingError) as blocked:
        msg.send_message(conn, thread["id"], thread["s"]["id"], "Ответьте, пожалуйста", NOW)
    assert blocked.value.code == "blocked" and "заблок" not in blocked.value.text.lower()
    with pytest.raises(rules.MessagingError) as mine:
        msg.send_message(conn, thread["id"], thread["e"]["id"], "Передумал", NOW)
    assert mine.value.code == "blocked"
    view_blocker = msg.get_conversation(conn, thread["id"], thread["e"]["id"])
    view_blocked = msg.get_conversation(conn, thread["id"], thread["s"]["id"])
    assert (view_blocker["i_blocked"], view_blocker["can_send"]) == (True, False)
    assert (view_blocked["blocked_me"], view_blocked["can_send"]) == (True, False)
    assert view_blocked["i_blocked"] is False
    assert "заблок" in view_blocker["closed_reason"].lower()
    assert "заблок" not in view_blocked["closed_reason"].lower()


def test_unblock_restores_writing(conn, thread):
    msg.block(conn, thread["id"], thread["e"]["id"], NOW)
    msg.unblock(conn, thread["id"], thread["e"]["id"])
    msg.send_message(conn, thread["id"], thread["s"]["id"], "Снова здравствуйте", NOW + timedelta(minutes=1))
    assert conn.execute("SELECT COUNT(*) FROM user_blocks").fetchone()[0] == 0


def test_block_is_idempotent_and_unblock_of_nothing_is_fine(conn, thread):
    msg.unblock(conn, thread["id"], thread["e"]["id"])
    msg.block(conn, thread["id"], thread["e"]["id"], NOW)
    msg.block(conn, thread["id"], thread["e"]["id"], NOW)
    assert conn.execute("SELECT COUNT(*) FROM user_blocks").fetchone()[0] == 1


def test_block_applies_to_every_thread_of_the_pair(conn, thread, employer, seeker):
    other = add_vacancy(conn, employer["id"], profession="повар")
    res = msg.apply(conn, vacancy_id=other, user=seeker, cover="", share_phone=False, now=NOW)
    msg.block(conn, thread["id"], employer["id"], NOW)
    with pytest.raises(rules.MessagingError):
        msg.send_message(conn, res["conversation_id"], seeker["id"], "И тут тоже", NOW)


def test_only_participants_can_block(conn, thread):
    stranger = add_user(conn, "x@example.test", "job_seeker", "Чужой")
    with pytest.raises(msg.NotFound):
        msg.block(conn, thread["id"], stranger["id"], NOW)
    with pytest.raises(msg.NotFound):
        msg.unblock(conn, thread["id"], stranger["id"])
    assert conn.execute("SELECT COUNT(*) FROM user_blocks").fetchone()[0] == 0


def message_of(conn, thread, who):
    return conn.execute(
        "SELECT id FROM messages WHERE conversation_id=? AND sender_user_id=?", (thread["id"], thread[who]["id"])
    ).fetchone()[0]


def test_report_saves_snapshot_for_moderation(conn, thread):
    mid = message_of(conn, thread, "s")
    rid = msg.report_message(conn, thread["id"], mid, thread["e"]["id"], "Спам или реклама", " Просит деньги ", NOW)
    row = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    assert (row["reporter_user_id"], row["target_type"], row["target_id"], row["status"]) == (
        thread["e"]["id"], "message", mid, "new")
    assert row["reason"] == "Спам или реклама: Просит деньги"
    snap = json.loads(row["snapshot"])
    assert snap == {
        "conversation_id": thread["id"], "message_id": mid, "sender_user_id": thread["s"]["id"],
        "body": "Здравствуйте!", "sent_at": snap["sent_at"], "comment": "Просит деньги",
    }
    assert snap["sent_at"]


def test_report_without_comment(conn, thread):
    mid = message_of(conn, thread, "s")
    rid = msg.report_message(conn, thread["id"], mid, thread["e"]["id"], "Оскорбления или угрозы", "", NOW)
    assert conn.execute("SELECT reason FROM reports WHERE id=?", (rid,)).fetchone()[0] == "Оскорбления или угрозы"


def test_report_validation(conn, thread):
    mid = message_of(conn, thread, "s")
    with pytest.raises(rules.MessagingError) as e:
        msg.report_message(conn, thread["id"], mid, thread["e"]["id"], "Из головы", "", NOW)
    assert e.value.code == "bad_reason"
    with pytest.raises(rules.MessagingError) as e2:
        msg.report_message(conn, thread["id"], mid, thread["e"]["id"], "Другое", "ну", NOW)
    assert e2.value.code == "empty"
    with pytest.raises(rules.MessagingError) as e3:
        msg.report_message(conn, thread["id"], mid, thread["e"]["id"], rules.REPORT_REASONS[0], "а" * 501, NOW)
    assert e3.value.code == "too_long"
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0


def test_cannot_report_own_message_or_foreign_message(conn, thread, employer):
    mine = message_of(conn, thread, "s")
    with pytest.raises(rules.MessagingError) as e:
        msg.report_message(conn, thread["id"], mine, thread["s"]["id"], rules.REPORT_REASONS[0], "", NOW)
    assert e.value.code == "own_message"
    other_seeker = add_user(conn, "o@example.test", "job_seeker", "Другой")
    v2 = add_vacancy(conn, employer["id"], profession="повар")
    other = msg.apply(conn, vacancy_id=v2, user=other_seeker, cover="Чужой текст", share_phone=False, now=NOW)
    foreign = conn.execute("SELECT id FROM messages WHERE conversation_id=?", (other["conversation_id"],)).fetchone()[0]
    with pytest.raises(msg.NotFound):  # сообщение из другого треда через свой тред не достать
        msg.report_message(conn, thread["id"], foreign, thread["e"]["id"], rules.REPORT_REASONS[0], "", NOW)
    stranger = add_user(conn, "x@example.test", "job_seeker", "Чужой")
    with pytest.raises(msg.NotFound):
        msg.report_message(conn, thread["id"], mine, stranger["id"], rules.REPORT_REASONS[0], "", NOW)


def test_repeated_report_is_not_duplicated(conn, thread):
    mid = message_of(conn, thread, "s")
    first = msg.report_message(conn, thread["id"], mid, thread["e"]["id"], rules.REPORT_REASONS[0], "", NOW)
    again = msg.report_message(conn, thread["id"], mid, thread["e"]["id"], rules.REPORT_REASONS[1], "", NOW)
    assert first == again
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1


def test_report_reasons_are_the_known_list():
    assert "Другое" in rules.REPORT_REASONS and len(rules.REPORT_REASONS) >= 4
