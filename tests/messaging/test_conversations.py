"""Переписка по отклику: отправка, непрочитанное, страницы, защита от спама, закрытые треды."""
from datetime import timedelta

import pytest

from rabota.messaging import rules
from rabota.messaging import service as msg
from tests.messaging.conftest import NOW, add_user, add_vacancy


@pytest.fixture
def thread(conn, seeker, employer, vacancy):
    res = msg.apply(conn, vacancy_id=vacancy, user=seeker, cover="Здравствуйте!", share_phone=True, now=NOW)
    return {"id": res["conversation_id"], "app": res["application_id"], "s": seeker, "e": employer}


def send(conn, thread, who, body, minutes=1):
    return msg.send_message(conn, thread["id"], thread[who]["id"], body, NOW + timedelta(minutes=minutes))


def test_both_sides_write_and_messages_keep_order(conn, thread):
    send(conn, thread, "e", "Добрый день, приходите на собеседование", 1)
    send(conn, thread, "s", "Спасибо, во сколько?", 2)
    page = msg.list_messages(conn, thread["id"])
    assert [m["body"] for m in page["items"]] == [
        "Здравствуйте!", "Добрый день, приходите на собеседование", "Спасибо, во сколько?"]
    assert [m["sender_user_id"] for m in page["items"]] == [thread["s"]["id"], thread["e"]["id"], thread["s"]["id"]]
    row = conn.execute("SELECT last_message_at FROM conversations WHERE id=?", (thread["id"],)).fetchone()
    assert row[0] == page["items"][-1]["created_at"]


def test_text_is_cleaned_and_validated(conn, thread):
    send(conn, thread, "e", "  привет\x00 \n\n\n\nмир  ")
    assert msg.list_messages(conn, thread["id"])["items"][-1]["body"] == "привет\n\nмир"
    for bad, code in (("   ", "empty"), ("", "empty"), ("а" * (rules.MAX_MESSAGE + 1), "too_long")):
        with pytest.raises(rules.MessagingError) as e:
            send(conn, thread, "e", bad)
        assert e.value.code == code


def test_html_is_stored_as_plain_text(conn, thread):
    send(conn, thread, "e", "<img src=x onerror=alert(1)>")
    assert msg.list_messages(conn, thread["id"])["items"][-1]["body"] == "<img src=x onerror=alert(1)>"


def test_unread_counters_and_mark_read(conn, thread):
    send(conn, thread, "e", "Первое", 1)
    send(conn, thread, "e", "Второе", 2)
    assert msg.unread_total(conn, thread["s"]["id"]) == 2
    assert msg.unread_total(conn, thread["e"]["id"]) == 1  # сопроводительное письмо
    convs = msg.list_conversations(conn, thread["s"]["id"])["items"]
    assert convs[0]["unread"] == 2 and convs[0]["last_body"] == "Второе"
    # читает работодатель: чужие непрочитанные соискателя не трогаются
    assert msg.mark_read(conn, thread["id"], thread["e"]["id"], NOW + timedelta(minutes=5)) == 1
    assert msg.unread_total(conn, thread["s"]["id"]) == 2
    assert msg.mark_read(conn, thread["id"], thread["s"]["id"], NOW + timedelta(minutes=6)) == 2
    assert msg.unread_total(conn, thread["s"]["id"]) == 0
    assert msg.mark_read(conn, thread["id"], thread["s"]["id"], NOW + timedelta(minutes=7)) == 0


def test_read_marks_are_visible_to_sender(conn, thread):
    send(conn, thread, "e", "Привет", 1)
    msg.mark_read(conn, thread["id"], thread["s"]["id"], NOW + timedelta(minutes=9))
    mine = [m for m in msg.list_messages(conn, thread["id"])["items"] if m["sender_user_id"] == thread["e"]["id"]]
    assert mine[0]["read_at"]


def test_messages_are_paged_from_the_newest(conn, thread):
    for i in range(1, 8):
        send(conn, thread, "e" if i % 2 else "s", f"сообщение {i}", i)
    first = msg.list_messages(conn, thread["id"], limit=3)
    assert [m["body"] for m in first["items"]] == ["сообщение 5", "сообщение 6", "сообщение 7"]
    assert first["has_older"] is True
    older = msg.list_messages(conn, thread["id"], before=first["items"][0]["id"], limit=3)
    assert [m["body"] for m in older["items"]] == ["сообщение 2", "сообщение 3", "сообщение 4"]
    oldest = msg.list_messages(conn, thread["id"], before=older["items"][0]["id"], limit=3)
    assert [m["body"] for m in oldest["items"]] == ["Здравствуйте!", "сообщение 1"]
    assert oldest["has_older"] is False


def test_only_participants_get_the_thread(conn, thread):
    stranger = add_user(conn, "x@example.test", "job_seeker", "Чужой")
    other_boss = add_user(conn, "boss2@example.test", "employer", "Чужой босс")
    admin = add_user(conn, "adm@example.test", "admin", "Админ")
    for user in (stranger, other_boss, admin):
        assert msg.get_conversation(conn, thread["id"], user["id"]) is None
        with pytest.raises(msg.NotFound):
            msg.send_message(conn, thread["id"], user["id"], "я тут", NOW)
        with pytest.raises(msg.NotFound):
            msg.mark_read(conn, thread["id"], user["id"], NOW)
    assert msg.get_conversation(conn, 99999, thread["s"]["id"]) is None
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_inbox_lists_only_own_threads(conn, thread, employer):
    other_seeker = add_user(conn, "o@example.test", "job_seeker", "Другой")
    v2 = add_vacancy(conn, employer["id"], profession="повар")
    msg.apply(conn, vacancy_id=v2, user=other_seeker, cover="", share_phone=False, now=NOW)
    assert msg.list_conversations(conn, thread["s"]["id"])["total"] == 1
    assert msg.list_conversations(conn, employer["id"])["total"] == 2
    assert msg.list_conversations(conn, other_seeker["id"])["total"] == 1


def test_inbox_counterpart_names_and_unread_filter(conn, thread):
    send(conn, thread, "e", "Привет", 1)
    seeker_view = msg.list_conversations(conn, thread["s"]["id"])["items"][0]
    employer_view = msg.list_conversations(conn, thread["e"]["id"])["items"][0]
    assert seeker_view["counterpart_name"] == "Кофейня Зёрна"  # у соискателя название компании
    assert employer_view["counterpart_name"] == "Мария Петрова"
    assert seeker_view["title"] == "бариста"
    assert msg.list_conversations(conn, thread["s"]["id"], unread_only=True)["total"] == 1
    msg.mark_read(conn, thread["id"], thread["s"]["id"], NOW)
    assert msg.list_conversations(conn, thread["s"]["id"], unread_only=True)["total"] == 0


def test_inbox_orders_by_latest_activity(conn, thread, employer):
    s2 = add_user(conn, "s2@example.test", "job_seeker", "Второй")
    v2 = add_vacancy(conn, employer["id"], profession="повар")
    res = msg.apply(conn, vacancy_id=v2, user=s2, cover="Привет", share_phone=False, now=NOW + timedelta(minutes=10))
    items = msg.list_conversations(conn, employer["id"])["items"]
    assert items[0]["id"] == res["conversation_id"]
    send(conn, thread, "e", "Новый ответ", 30)
    assert msg.list_conversations(conn, employer["id"])["items"][0]["id"] == thread["id"]


# ------------------------------------------------------------------ защита от спама

def test_sender_must_wait_for_reply_after_three_messages(conn, thread):
    # сопроводительное письмо уже первое, ещё два можно
    send(conn, thread, "s", "Второе", 1)
    send(conn, thread, "s", "Третье", 2)
    with pytest.raises(rules.MessagingError) as e:
        send(conn, thread, "s", "Четвёртое", 3)
    assert e.value.code == "wait_reply"
    send(conn, thread, "e", "Ответ", 4)
    send(conn, thread, "s", "Теперь можно", 5)


def test_employer_is_limited_the_same_way(conn, thread):
    for i in range(rules.MAX_UNANSWERED):
        send(conn, thread, "e", f"Сообщение {i}", i + 1)
    with pytest.raises(rules.MessagingError) as e:
        send(conn, thread, "e", "Ещё", 10)
    assert e.value.code == "wait_reply"


def test_double_click_duplicate_is_rejected(conn, thread):
    send(conn, thread, "e", "Приходите завтра", 1)
    with pytest.raises(rules.MessagingError) as e:
        send(conn, thread, "e", "Приходите завтра", 1)
    assert e.value.code == "duplicate_message"
    send(conn, thread, "s", "ок", 2)
    send(conn, thread, "e", "Приходите завтра", 3)  # после ответа и паузы повтор уже допустим


def test_too_many_links(conn, thread):
    with pytest.raises(rules.MessagingError) as e:
        send(conn, thread, "e", "http://a.ru http://b.ru http://c.ru")
    assert e.value.code == "too_many_links"


# ------------------------------------------------------------------ закрытые треды

def test_withdrawn_response_closes_the_thread(conn, thread):
    msg.withdraw(conn, thread["app"], thread["s"]["id"], NOW)
    conv = msg.get_conversation(conn, thread["id"], thread["e"]["id"])
    assert conv["can_send"] is False and "отозв" in conv["closed_reason"].lower()
    for who in ("e", "s"):
        with pytest.raises(rules.MessagingError) as e:
            send(conn, thread, who, "Привет")
        assert e.value.code == "closed"


def test_rejected_response_stops_seeker_but_not_employer(conn, thread):
    msg.set_status(conn, thread["app"], thread["e"]["id"], "rejected", NOW)
    with pytest.raises(rules.MessagingError) as e:
        send(conn, thread, "s", "Пожалуйста, подумайте ещё")
    assert e.value.code == "closed"
    send(conn, thread, "e", "К сожалению, место занято", 2)
    assert msg.get_conversation(conn, thread["id"], thread["s"]["id"])["can_send"] is False
    assert msg.get_conversation(conn, thread["id"], thread["e"]["id"])["can_send"] is True


def test_blocked_by_admin_counterpart_cannot_be_written_to(conn, thread):
    conn.execute("UPDATE users SET status='blocked' WHERE id=?", (thread["s"]["id"],))
    conn.commit()
    with pytest.raises(rules.MessagingError) as e:
        send(conn, thread, "e", "Привет")
    assert e.value.code == "closed"


def test_conversation_view_fields(conn, thread):
    conv = msg.get_conversation(conn, thread["id"], thread["s"]["id"])
    assert conv["counterpart_id"] == thread["e"]["id"] and conv["counterpart_name"] == "Кофейня Зёрна"
    assert conv["title"] == "бариста" and conv["can_send"] is True and conv["application_id"] == thread["app"]
    assert conv["app_status"] == "new" and conv["i_am"] == "job_seeker"
    emp = msg.get_conversation(conn, thread["id"], thread["e"]["id"])
    assert emp["counterpart_name"] == "Мария Петрова" and emp["i_am"] == "employer"
