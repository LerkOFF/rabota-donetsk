"""Список откликов, доступ, смена статуса, отзыв."""
from datetime import timedelta

import pytest

from rabota.messaging import rules
from rabota.messaging import service as msg
from tests.messaging.conftest import NOW, add_user, add_vacancy


def apply(conn, vid, user, cover="Хочу работать", when=NOW):
    return msg.apply(conn, vacancy_id=vid, user=user, cover=cover, share_phone=True, now=when)


@pytest.fixture
def world(conn, employer):
    """Работодатель с двумя вакансиями, трое соискателей, чужой работодатель с вакансией."""
    v1 = add_vacancy(conn, employer["id"], profession="бариста")
    v2 = add_vacancy(conn, employer["id"], profession="кассир")
    s = [add_user(conn, f"s{i}@example.test", "job_seeker", f"Соискатель {i}", f"+7949000000{i}") for i in range(3)]
    stranger = add_user(conn, "other@example.test", "employer", "Чужой")
    v_other = add_vacancy(conn, stranger["id"], profession="повар")
    r = [apply(conn, v1, s[0], when=NOW), apply(conn, v1, s[1], when=NOW + timedelta(minutes=1)),
         apply(conn, v2, s[2], when=NOW + timedelta(minutes=2))]
    apply(conn, v_other, s[0], when=NOW + timedelta(minutes=3))
    return {"v1": v1, "v2": v2, "s": s, "r": r, "stranger": stranger, "v_other": v_other, "emp": employer}


def test_employer_sees_only_responses_to_own_vacancies_newest_first(conn, world):
    page = msg.list_responses(conn, world["emp"])
    assert page["total"] == 3
    assert [i["seeker_name"] for i in page["items"]] == ["Соискатель 2", "Соискатель 1", "Соискатель 0"]
    assert page["items"][0]["vacancy_title"] == "кассир"
    assert page["items"][0]["contact_phone"] == "+79490000002"


def test_seeker_sees_only_own_responses(conn, world):
    page = msg.list_responses(conn, world["s"][0])
    assert page["total"] == 2
    assert {i["vacancy_title"] for i in page["items"]} == {"бариста", "повар"}


def test_filters_by_status_and_vacancy(conn, world):
    rid = world["r"][0]["application_id"]
    msg.set_status(conn, rid, world["emp"]["id"], "invited", NOW)
    assert msg.list_responses(conn, world["emp"], status="invited")["total"] == 1
    assert msg.list_responses(conn, world["emp"], status="new")["total"] == 2
    assert msg.list_responses(conn, world["emp"], vacancy_id=world["v2"])["total"] == 1
    assert msg.list_responses(conn, world["emp"], status="rejected")["total"] == 0
    counts = msg.response_counts(conn, world["emp"])
    assert counts == {"all": 3, "new": 2, "viewed": 0, "invited": 1, "rejected": 0, "withdrawn": 0}


def test_unknown_status_filter_is_ignored(conn, world):
    assert msg.list_responses(conn, world["emp"], status="hacked'--")["total"] == 3


def test_pagination(conn, employer):
    vid = add_vacancy(conn, employer["id"])
    for i in range(7):
        u = add_user(conn, f"p{i}@example.test", "job_seeker", f"П{i}")
        apply(conn, vid, u, when=NOW + timedelta(minutes=i))
    p1 = msg.list_responses(conn, employer, page=1, per_page=3)
    p3 = msg.list_responses(conn, employer, page=3, per_page=3)
    assert (p1["total"], p1["pages"], p1["has_next"], p1["has_prev"], len(p1["items"])) == (7, 3, True, False, 3)
    assert (p3["has_next"], p3["has_prev"], len(p3["items"])) == (False, True, 1)
    assert msg.list_responses(conn, employer, page=99, per_page=3)["page"] == 3


def test_get_response_only_for_participants(conn, world):
    rid = world["r"][0]["application_id"]
    assert msg.get_response(conn, rid, world["emp"])["seeker_name"] == "Соискатель 0"
    assert msg.get_response(conn, rid, world["s"][0])["vacancy_title"] == "бариста"
    assert msg.get_response(conn, rid, world["s"][1]) is None  # чужой соискатель
    assert msg.get_response(conn, rid, world["stranger"]) is None  # чужой работодатель
    assert msg.get_response(conn, 9999, world["emp"]) is None


def test_seeker_view_has_no_phone_of_employer_leak_and_employer_view_has_seeker_phone(conn, world):
    rid = world["r"][0]["application_id"]
    assert msg.get_response(conn, rid, world["emp"])["contact_phone"] == "+79490000000"


def test_mark_viewed_moves_new_to_viewed_once(conn, world):
    rid = world["r"][0]["application_id"]
    assert msg.mark_viewed(conn, rid, world["emp"]["id"], NOW) is True
    assert msg.get_response(conn, rid, world["emp"])["status"] == "viewed"
    assert msg.mark_viewed(conn, rid, world["emp"]["id"], NOW) is False
    msg.set_status(conn, rid, world["emp"]["id"], "invited", NOW)
    msg.mark_viewed(conn, rid, world["emp"]["id"], NOW)
    assert msg.get_response(conn, rid, world["emp"])["status"] == "invited"


def test_mark_viewed_ignores_non_owner(conn, world):
    rid = world["r"][0]["application_id"]
    assert msg.mark_viewed(conn, rid, world["stranger"]["id"], NOW) is False
    assert msg.mark_viewed(conn, rid, world["s"][0]["id"], NOW) is False


@pytest.mark.parametrize("status", ["viewed", "invited", "rejected"])
def test_employer_changes_status(conn, world, status):
    rid = world["r"][0]["application_id"]
    msg.set_status(conn, rid, world["emp"]["id"], status, NOW)
    row = conn.execute("SELECT status, status_changed_at FROM applications WHERE id=?", (rid,)).fetchone()
    assert row["status"] == status and row["status_changed_at"]


@pytest.mark.parametrize("status", ["new", "withdrawn", "", "approved"])
def test_bad_status_is_rejected(conn, world, status):
    with pytest.raises(rules.MessagingError) as e:
        msg.set_status(conn, world["r"][0]["application_id"], world["emp"]["id"], status, NOW)
    assert e.value.code == "bad_status"


def test_only_owner_employer_changes_status(conn, world):
    rid = world["r"][0]["application_id"]
    for actor in (world["stranger"], world["s"][0]):
        with pytest.raises(msg.NotFound):
            msg.set_status(conn, rid, actor["id"], "invited", NOW)
    assert conn.execute("SELECT status FROM applications WHERE id=?", (rid,)).fetchone()[0] == "new"


def test_withdraw_by_owner_only_and_closes_response(conn, world):
    rid = world["r"][0]["application_id"]
    with pytest.raises(msg.NotFound):
        msg.withdraw(conn, rid, world["s"][1]["id"], NOW)
    with pytest.raises(msg.NotFound):
        msg.withdraw(conn, rid, world["emp"]["id"], NOW)
    msg.withdraw(conn, rid, world["s"][0]["id"], NOW)
    got = msg.get_response(conn, rid, world["emp"])
    assert got["status"] == "withdrawn" and got["withdrawn_at"]
    with pytest.raises(rules.MessagingError) as e:
        msg.set_status(conn, rid, world["emp"]["id"], "invited", NOW)
    assert e.value.code == "closed"
    msg.withdraw(conn, rid, world["s"][0]["id"], NOW)  # повторный отзыв безвреден
    assert msg.list_responses(conn, world["emp"], status="withdrawn")["total"] == 1
    assert msg.list_responses(conn, world["emp"], status="new")["total"] == 2


def test_new_response_counter(conn, world):
    assert msg.new_responses_count(conn, world["emp"]["id"]) == 3
    msg.mark_viewed(conn, world["r"][0]["application_id"], world["emp"]["id"], NOW)
    assert msg.new_responses_count(conn, world["emp"]["id"]) == 2
    msg.withdraw(conn, world["r"][1]["application_id"], world["s"][1]["id"], NOW)
    assert msg.new_responses_count(conn, world["emp"]["id"]) == 1
    assert msg.new_responses_count(conn, world["stranger"]["id"]) == 1


def test_response_survives_deleted_vacancy_title_in_thread(conn, world):
    """Вакансию удалили по сроку: отклик исчезает вместе с ней, переписка остаётся с запомненным названием."""
    conn.execute("DELETE FROM vacancies WHERE id=?", (world["v1"],))
    conn.commit()
    assert msg.list_responses(conn, world["emp"])["total"] == 1
    threads = msg.list_conversations(conn, world["emp"]["id"])
    titles = {t["title"] for t in threads["items"]}
    assert "бариста" in titles
