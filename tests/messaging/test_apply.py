"""Отклик соискателя на вакансию работодателя."""
import pytest

from rabota.messaging import rules
from rabota.messaging import service as msg
from tests.messaging.conftest import NOW, add_user, add_vacancy


def apply(conn, vacancy_id, user, cover="Здравствуйте, хочу работать", share_phone=True):
    return msg.apply(conn, vacancy_id=vacancy_id, user=user, cover=cover, share_phone=share_phone, now=NOW)


def test_apply_creates_response_thread_and_cover_message(conn, seeker, employer, vacancy):
    res = apply(conn, vacancy, seeker)
    app = conn.execute("SELECT * FROM applications WHERE id=?", (res["application_id"],)).fetchone()
    assert (app["vacancy_id"], app["user_id"], app["status"]) == (vacancy, seeker["id"], "new")
    assert app["cover_letter"] == "Здравствуйте, хочу работать"
    assert (app["contact_name"], app["contact_phone"]) == ("Мария Петрова", "+79491112233")
    conv = conn.execute("SELECT * FROM conversations WHERE id=?", (res["conversation_id"],)).fetchone()
    assert (conv["employer_user_id"], conv["seeker_user_id"], conv["vacancy_id"]) == (employer["id"], seeker["id"], vacancy)
    assert conv["application_id"] == app["id"] and conv["started_by"] == seeker["id"] and conv["title"] == "бариста"
    first = conn.execute("SELECT * FROM messages WHERE conversation_id=?", (conv["id"],)).fetchall()
    assert [(m["sender_user_id"], m["body"], m["read_at"]) for m in first] == [(seeker["id"], "Здравствуйте, хочу работать", None)]
    assert conv["last_message_at"] == first[0]["created_at"]


def test_cover_letter_is_optional_and_makes_no_message(conn, seeker, vacancy):
    res = apply(conn, vacancy, seeker, cover="  ")
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert conn.execute("SELECT last_message_at FROM conversations WHERE id=?", (res["conversation_id"],)).fetchone()[0] is None


def test_phone_is_shared_only_on_request_and_if_set(conn, seeker, vacancy, employer):
    res = apply(conn, vacancy, seeker, share_phone=False)
    assert conn.execute("SELECT contact_phone FROM applications").fetchone()[0] is None
    nophone = add_user(conn, "nophone@example.test", "job_seeker", "Без телефона")
    other = add_vacancy(conn, employer["id"], profession="кассир")
    apply(conn, other, nophone, share_phone=True)
    assert conn.execute("SELECT contact_phone FROM applications WHERE user_id=?", (nophone["id"],)).fetchone()[0] is None
    assert res["application_id"]


def test_second_response_to_same_vacancy_is_forbidden(conn, seeker, vacancy):
    apply(conn, vacancy, seeker)
    with pytest.raises(rules.MessagingError) as e:
        apply(conn, vacancy, seeker)
    assert e.value.code == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1


def test_response_after_withdrawal_is_still_forbidden(conn, seeker, vacancy):
    res = apply(conn, vacancy, seeker)
    msg.withdraw(conn, res["application_id"], seeker["id"], NOW)
    with pytest.raises(rules.MessagingError) as e:
        apply(conn, vacancy, seeker)
    assert e.value.code == "duplicate"


@pytest.mark.parametrize("kw", [
    {"origin": "parsed"},
    {"status": "expired"},
    {"status": "hidden"},
    {"moderation": "pending"},
    {"moderation": "rejected"},
])
def test_only_visible_vacancies_from_employers_take_responses(conn, seeker, employer, kw):
    vid = add_vacancy(conn, employer["id"], **kw)
    with pytest.raises(msg.NotFound):
        apply(conn, vid, seeker)


def test_parsed_vacancy_without_owner_takes_no_responses(conn, seeker):
    vid = add_vacancy(conn, None, origin="direct")
    with pytest.raises(msg.NotFound):
        apply(conn, vid, seeker)


def test_unknown_vacancy(conn, seeker):
    with pytest.raises(msg.NotFound):
        apply(conn, 9999, seeker)


@pytest.mark.parametrize("role", ["employer", "admin"])
def test_only_job_seekers_respond(conn, vacancy, role):
    user = add_user(conn, f"{role}@example.test", role, "Кто-то")
    with pytest.raises(rules.MessagingError) as e:
        apply(conn, vacancy, user)
    assert e.value.code == "forbidden_role"


def test_blocked_account_cannot_respond(conn, vacancy):
    user = add_user(conn, "bad@example.test", "job_seeker", "Плохой", status="blocked")
    with pytest.raises(rules.MessagingError) as e:
        apply(conn, vacancy, user)
    assert e.value.code == "forbidden_role"


def test_employer_who_blocked_the_seeker_gets_no_response_and_no_hint(conn, seeker, employer, vacancy):
    conn.execute("INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES (?,?)", (employer["id"], seeker["id"]))
    conn.commit()
    with pytest.raises(rules.MessagingError) as e:
        apply(conn, vacancy, seeker)
    assert e.value.code == "unavailable"
    assert "заблок" not in e.value.text.lower()


def test_cover_letter_is_cleaned_and_limited(conn, seeker, vacancy):
    res = apply(conn, vacancy, seeker, cover="  привет\x00 \r\n\r\n\r\n\r\nмир ")
    assert conn.execute("SELECT cover_letter FROM applications WHERE id=?", (res["application_id"],)).fetchone()[0] == "привет\n\nмир"
    other = add_vacancy(conn, conn.execute("SELECT owner_user_id FROM vacancies WHERE id=?", (vacancy,)).fetchone()[0])
    with pytest.raises(rules.MessagingError) as e:
        apply(conn, other, seeker, cover="а" * (rules.MAX_COVER + 1))
    assert e.value.code == "too_long"


def test_apply_state(conn, seeker, vacancy):
    assert msg.apply_state(conn, vacancy, seeker["id"]) is None
    res = apply(conn, vacancy, seeker)
    state = msg.apply_state(conn, vacancy, seeker["id"])
    assert state["application_id"] == res["application_id"] and state["status"] == "new"
    msg.withdraw(conn, res["application_id"], seeker["id"], NOW)
    assert msg.apply_state(conn, vacancy, seeker["id"])["status"] == "withdrawn"
