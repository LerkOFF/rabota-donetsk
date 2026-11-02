"""Логика резюме без веба: создание, лимит, согласие, правка, скрытие, поиск, контакты, приглашения, удаление."""
from datetime import timedelta

import pytest

from rabota.resumes import rules, service
from rabota.resumes.service import NotFound, ResumeError
from tests.resumes.conftest import NOW, add_user, add_vacancy, clean_of


def hidden_search(conn, employer, **filters):
    f = service.parse_filters(filters)
    return service.search(conn, employer["id"], f, NOW)["items"]


# ------------------------------------------------------------------ свои резюме

def test_create_saves_everything(conn, seeker):
    rid = service.create(conn, seeker["id"], clean_of(), NOW)
    r = service.get_own(conn, rid, seeker["id"])
    assert r["title"] == "Бариста" and r["profession"] == "бариста" and r["pay_min"] == 30000
    assert r["skills"] == ["кофе", "касса", "сервис"] and r["work_formats"] == ["точка"]
    assert r["places"][0]["place"] == "Кофейня Зерно" and r["experience_years"] == 3
    assert r["visibility"] == "public" and r["created_at"] == r["updated_at"]


def test_limit_of_three_resumes(conn, seeker):
    for _ in range(rules.MAX_RESUMES):
        service.create(conn, seeker["id"], clean_of(), NOW)
    with pytest.raises(ResumeError) as err:
        service.create(conn, seeker["id"], clean_of(), NOW)
    assert err.value.code == "limit"
    assert service.count_own(conn, seeker["id"]) == rules.MAX_RESUMES


def test_limit_is_per_user(conn, seeker):
    other = add_user(conn, "o@example.test")
    for _ in range(rules.MAX_RESUMES):
        service.create(conn, seeker["id"], clean_of(), NOW)
    assert service.create(conn, other["id"], clean_of(), NOW)


def test_consent_is_recorded_with_time_and_version(conn, seeker, resume_id):
    r = service.get_own(conn, resume_id, seeker["id"])
    assert r["consent_at"] and r["consent_version"] == rules.consent_version() and r["consent_withdrawn_at"] is None


def test_hidden_resume_has_no_consent(conn, seeker):
    rid = service.create(conn, seeker["id"], clean_of(visibility="hidden", consent=None), NOW)
    r = service.get_own(conn, rid, seeker["id"])
    assert r["consent_at"] is None and r["visibility"] == "hidden"


def test_consent_kept_on_edit_and_renewed_when_text_changes(conn, seeker, resume_id, monkeypatch):
    first = service.get_own(conn, resume_id, seeker["id"])["consent_at"]
    service.update(conn, resume_id, seeker["id"], clean_of(pay_min="35000"), NOW + timedelta(hours=1))
    r = service.get_own(conn, resume_id, seeker["id"])
    assert r["consent_at"] == first and r["pay_min"] == 35000 and r["updated_at"] != first
    monkeypatch.setenv("LEGAL_UPDATED_AT", "01.01.2031")
    service.update(conn, resume_id, seeker["id"], clean_of(), NOW + timedelta(hours=2))
    r = service.get_own(conn, resume_id, seeker["id"])
    assert r["consent_at"] != first and r["consent_version"] == "resume 01.01.2031"


def test_hiding_withdraws_consent_and_showing_needs_a_new_one(conn, seeker, resume_id):
    service.hide(conn, resume_id, seeker["id"], NOW + timedelta(hours=1))
    r = service.get_own(conn, resume_id, seeker["id"])
    assert r["visibility"] == "hidden" and r["consent_withdrawn_at"]
    service.update(conn, resume_id, seeker["id"], clean_of(), NOW + timedelta(hours=2))
    r = service.get_own(conn, resume_id, seeker["id"])
    assert r["consent_withdrawn_at"] is None and r["consent_at"].startswith("2026-10-29T14")


def test_editing_to_hidden_withdraws_consent(conn, seeker, resume_id):
    service.update(conn, resume_id, seeker["id"], clean_of(visibility="hidden", consent=None), NOW)
    assert service.get_own(conn, resume_id, seeker["id"])["consent_withdrawn_at"]


def test_foreign_resume_is_not_found_everywhere(conn, seeker, resume_id):
    stranger = add_user(conn, "s@example.test")
    assert service.get_own(conn, resume_id, stranger["id"]) is None
    for action in (lambda: service.hide(conn, resume_id, stranger["id"], NOW),
                   lambda: service.refresh(conn, resume_id, stranger["id"], NOW),
                   lambda: service.delete(conn, resume_id, stranger["id"]),
                   lambda: service.update(conn, resume_id, stranger["id"], clean_of(), NOW)):
        with pytest.raises(NotFound):
            action()
    assert service.get_own(conn, resume_id, seeker["id"]) is not None


def test_refresh_moves_update_time(conn, seeker, resume_id):
    service.refresh(conn, resume_id, seeker["id"], NOW + timedelta(days=5))
    assert service.get_own(conn, resume_id, seeker["id"])["updated_at"].startswith("2026-11-03")


def test_delete_removes_resume_views_and_application_link(conn, seeker, employer, resume_id):
    vid = add_vacancy(conn, employer["id"])
    conn.execute("INSERT INTO applications(vacancy_id, user_id, resume_id) VALUES (?,?,?)", (vid, seeker["id"], resume_id))
    service.record_view(conn, resume_id, employer["id"], NOW)
    service.delete(conn, resume_id, seeker["id"])
    assert service.get_own(conn, resume_id, seeker["id"]) is None
    assert conn.execute("SELECT COUNT(*) FROM resume_views").fetchone()[0] == 0
    assert conn.execute("SELECT resume_id FROM applications").fetchone()[0] is None


def test_list_own_has_percent_tips_and_stats(conn, seeker, employer, resume_id):
    service.record_view(conn, resume_id, employer["id"], NOW)
    service.open_contact(conn, resume_id, employer["id"], NOW)
    item = service.list_own(conn, seeker["id"], NOW)[0]
    assert item["percent"] == 100 and item["views"] == 1 and item["contacts"] == 1 and item["live"]
    assert item["pay_text"].startswith("от 30") and item["experience_text"].startswith("Стаж 3")


def test_stale_resume_is_marked(conn, seeker, resume_id):
    later = NOW + timedelta(days=rules.STALE_DAYS + 1)
    assert service.list_own(conn, seeker["id"], later)[0]["stale"] is True
    assert service.list_own(conn, seeker["id"], NOW)[0]["stale"] is False


# ------------------------------------------------------------------ работодатель: поиск

def test_search_shows_only_public_resumes(conn, seeker, employer):
    service.create(conn, seeker["id"], clean_of(profession="Открытое"), NOW)
    service.create(conn, seeker["id"], clean_of(profession="Поотклику", visibility="open"), NOW)
    service.create(conn, seeker["id"], clean_of(profession="Скрытое", visibility="hidden", consent=None), NOW)
    titles = [r["title"] for r in hidden_search(conn, employer)]
    assert titles == ["Открытое"]


def test_search_hides_admin_hidden_blocked_and_stale(conn, seeker, employer, resume_id):
    assert len(hidden_search(conn, employer)) == 1
    conn.execute("UPDATE resumes SET admin_hidden = 1")
    assert hidden_search(conn, employer) == []
    conn.execute("UPDATE resumes SET admin_hidden = 0")
    conn.execute("INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES (?,?)", (seeker["id"], employer["id"]))
    assert hidden_search(conn, employer) == []
    conn.execute("DELETE FROM user_blocks")
    conn.execute("UPDATE users SET status = 'blocked' WHERE id = ?", (seeker["id"],))
    assert hidden_search(conn, employer) == []
    conn.execute("UPDATE users SET status = 'active'")
    f = service.parse_filters({})
    assert service.search(conn, employer["id"], f, NOW + timedelta(days=rules.STALE_DAYS + 1))["items"] == []


def test_search_withdrawn_consent_hides_resume(conn, seeker, employer, resume_id):
    conn.execute("UPDATE resumes SET consent_withdrawn_at = ?", (NOW.isoformat(),))
    assert hidden_search(conn, employer) == []


def test_search_never_returns_contacts_or_surname(conn, seeker, employer, resume_id):
    item = hidden_search(conn, employer)[0]
    assert item["display_name"] == "Мария П."
    assert "owner_name" in item and item["display_name"] != item["owner_name"]  # полное имя не выводится в шаблонах поиска


def test_search_filters(conn, seeker, employer):
    service.create(conn, seeker["id"], clean_of(), NOW)
    other = add_user(conn, "k@example.test", name="Иван Кассиров")
    service.create(conn, other["id"], clean_of(
        profession="Кассир", district="Калининский", pay_min="20000", skills="1С, касса", experience_years="0",
        employments=["подработка"], work_formats=["удалённо"], place_1="", role_1="", period_1=""), NOW)
    names = lambda **f: sorted(r["title"] for r in hidden_search(conn, employer, **f))
    assert names() == ["Бариста", "Кассир"]
    assert names(q="кассир") == ["Кассир"]
    assert names(q="1с") == ["Кассир"]
    assert names(q="БАРИСТА") == ["Бариста"]
    assert names(district="калининск") == ["Кассир"]
    assert names(pay_to="25000") == ["Кассир"]
    assert names(pay_to="25000", period="смена") == []
    assert names(exp="0") == ["Кассир"]
    assert names(exp="3") == ["Бариста"]
    assert names(exp="5") == []
    assert names(employment="подработка") == ["Кассир"]
    assert names(format="удалённо") == ["Кассир"]
    assert names(format="точка") == ["Бариста"]
    assert names(q="кассир", district="ворошилов") == []


def test_search_filters_ignore_garbage(conn, seeker, employer, resume_id):
    f = service.parse_filters({"exp": "99", "employment": "x", "format": "y", "sort": "z", "pay_to": "abc", "period": "год", "q": "a" * 500})
    assert f["exp"] == "" and f["employment"] == "" and f["sort"] == "fresh" and f["pay_to"] is None
    assert f["period"] == "месяц" and len(f["q"]) == 80
    wild = service.parse_filters({"q": "б_р"})  # подчёркивание не подстановка: "бар" не находится по "б_р"
    assert service.search(conn, employer["id"], wild, NOW)["items"] == []


def test_search_pagination(conn, employer):
    for i in range(rules.PER_PAGE + 5):
        u = add_user(conn, f"u{i}@example.test", name=f"Имя{i} Фамилия")
        service.create(conn, u["id"], clean_of(), NOW)
    f = service.parse_filters({})
    one = service.search(conn, employer["id"], f, NOW, 1)
    two = service.search(conn, employer["id"], f, NOW, 2)
    assert one["total"] == rules.PER_PAGE + 5 and len(one["items"]) == rules.PER_PAGE and len(two["items"]) == 5
    assert one["pages"] == 2 and service.search(conn, employer["id"], f, NOW, 99)["page"] == 2


def test_sort_by_experience(conn, seeker, employer):
    service.create(conn, seeker["id"], clean_of(profession="Малоопытный", experience_years="1"), NOW)
    other = add_user(conn, "b@example.test", name="Борис Б")
    service.create(conn, other["id"], clean_of(profession="Опытный", experience_years="9"), NOW)
    assert [r["title"] for r in hidden_search(conn, employer, sort="exp")] == ["Опытный", "Малоопытный"]


# ------------------------------------------------------------------ карточка, просмотры, контакты

def test_card_open_for_public_and_hidden_for_others(conn, seeker, employer, resume_id):
    assert service.get_for_employer(conn, resume_id, employer["id"], NOW)["in_search"] is True
    service.update(conn, resume_id, seeker["id"], clean_of(visibility="open"), NOW)
    assert service.get_for_employer(conn, resume_id, employer["id"], NOW) is None
    service.update(conn, resume_id, seeker["id"], clean_of(visibility="hidden", consent=None), NOW)
    assert service.get_for_employer(conn, resume_id, employer["id"], NOW) is None


def test_card_open_by_application_only_for_that_employer(conn, seeker, employer, resume_id):
    service.update(conn, resume_id, seeker["id"], clean_of(visibility="open"), NOW)
    other_boss = add_user(conn, "boss2@example.test", "employer", "Другой")
    vid = add_vacancy(conn, employer["id"])
    conn.execute("INSERT INTO applications(vacancy_id, user_id, resume_id) VALUES (?,?,?)", (vid, seeker["id"], resume_id))
    card = service.get_for_employer(conn, resume_id, employer["id"], NOW)
    assert card["via_application"] is True and card["in_search"] is False
    assert service.get_for_employer(conn, resume_id, other_boss["id"], NOW) is None
    conn.execute("UPDATE applications SET withdrawn_at = ?", (NOW.isoformat(),))
    assert service.get_for_employer(conn, resume_id, employer["id"], NOW) is None


def test_hidden_resume_not_visible_even_via_application(conn, seeker, employer, resume_id):
    vid = add_vacancy(conn, employer["id"])
    conn.execute("INSERT INTO applications(vacancy_id, user_id, resume_id) VALUES (?,?,?)", (vid, seeker["id"], resume_id))
    service.hide(conn, resume_id, seeker["id"], NOW)
    assert service.get_for_employer(conn, resume_id, employer["id"], NOW) is None


def test_views_counted_once_a_day_per_employer(conn, seeker, employer, resume_id):
    service.record_view(conn, resume_id, employer["id"], NOW)
    service.record_view(conn, resume_id, employer["id"], NOW + timedelta(hours=1))
    assert conn.execute("SELECT COUNT(*) FROM resume_views WHERE kind='view'").fetchone()[0] == 1
    service.record_view(conn, resume_id, employer["id"], NOW + timedelta(days=2))
    other = add_user(conn, "b2@example.test", "employer")
    service.record_view(conn, resume_id, other["id"], NOW)
    assert service.view_stats(conn, [resume_id])[resume_id] == {"views": 2, "contacts": 0}


def test_contact_is_opened_once_and_counted(conn, seeker, employer, resume_id):
    assert service.contact_opened(conn, resume_id, employer["id"]) is False
    assert service.open_contact(conn, resume_id, employer["id"], NOW) is True
    assert service.open_contact(conn, resume_id, employer["id"], NOW) is False
    assert service.contact_opened(conn, resume_id, employer["id"]) is True
    assert conn.execute("SELECT COUNT(*) FROM resume_views WHERE kind='contact'").fetchone()[0] == 1


def test_daily_contact_limit(conn, employer, monkeypatch):
    monkeypatch.setattr(rules, "MAX_CONTACTS_PER_DAY", 2)
    ids = []
    for i in range(3):
        u = add_user(conn, f"c{i}@example.test")
        ids.append(service.create(conn, u["id"], clean_of(), NOW))
    service.open_contact(conn, ids[0], employer["id"], NOW)
    service.open_contact(conn, ids[1], employer["id"], NOW)
    with pytest.raises(ResumeError) as err:
        service.open_contact(conn, ids[2], employer["id"], NOW)
    assert err.value.code == "contact_limit"
    assert service.open_contact(conn, ids[2], employer["id"], NOW + timedelta(days=1, minutes=1)) is True


# ------------------------------------------------------------------ приглашение

def invite(conn, employer, resume_id, vid, text="Здравствуйте! Приглашаем вас на собеседование."):
    return service.invite(conn, resume_id=resume_id, employer=employer, vacancy_id=vid, text=text, now=NOW)


def test_invite_creates_conversation_and_message(conn, seeker, employer, resume_id):
    vid = add_vacancy(conn, employer["id"])
    res = invite(conn, employer, resume_id, vid)
    conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (res["conversation_id"],)).fetchone()
    assert (conv["employer_user_id"], conv["seeker_user_id"], conv["vacancy_id"], conv["started_by"]) == (
        employer["id"], seeker["id"], vid, employer["id"])
    assert conv["application_id"] is None and conv["title"] == "бариста"
    msg = conn.execute("SELECT * FROM messages WHERE conversation_id = ?", (conv["id"],)).fetchone()
    assert msg["sender_user_id"] == employer["id"] and "собеседование" in msg["body"]


def test_invite_twice_points_to_existing_thread(conn, seeker, employer, resume_id):
    vid = add_vacancy(conn, employer["id"])
    first = invite(conn, employer, resume_id, vid)
    with pytest.raises(ResumeError) as err:
        invite(conn, employer, resume_id, vid)
    assert err.value.code == "exists" and err.value.extra["conversation_id"] == first["conversation_id"]
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1


@pytest.mark.parametrize("kw,code", [({"parsed": True}, "bad_vacancy"), ({"foreign": True}, "bad_vacancy")])
def test_invite_only_on_own_published_vacancy(conn, seeker, employer, resume_id, kw, code):
    other = add_user(conn, "boss3@example.test", "employer")
    vid = add_vacancy(conn, other["id"]) if kw.get("foreign") else add_vacancy(conn, employer["id"], origin="parsed")
    with pytest.raises(ResumeError) as err:
        invite(conn, employer, resume_id, vid)
    assert err.value.code == code


def test_invite_needs_text_and_respects_limit_of_links(conn, seeker, employer, resume_id):
    vid = add_vacancy(conn, employer["id"])
    with pytest.raises(ResumeError) as err:
        invite(conn, employer, resume_id, vid, text="   ")
    assert err.value.code == "bad_text"
    with pytest.raises(ResumeError):
        invite(conn, employer, resume_id, vid, text="a.ru b.ru c.ru")
    with pytest.raises(ResumeError):
        invite(conn, employer, resume_id, vid, text="я" * (rules.MAX_INVITE_TEXT + 1))


def test_invite_to_unavailable_resume_is_not_found(conn, seeker, employer, resume_id):
    vid = add_vacancy(conn, employer["id"])
    service.hide(conn, resume_id, seeker["id"], NOW)
    with pytest.raises(NotFound):
        invite(conn, employer, resume_id, vid)


def test_invite_blocked_by_seeker_looks_like_missing_resume(conn, seeker, employer, resume_id):
    vid = add_vacancy(conn, employer["id"])
    conn.execute("INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES (?,?)", (seeker["id"], employer["id"]))
    with pytest.raises(NotFound):
        invite(conn, employer, resume_id, vid)


def test_daily_invite_limit(conn, employer, monkeypatch):
    monkeypatch.setattr(rules, "MAX_INVITES_PER_DAY", 1)
    vid = add_vacancy(conn, employer["id"])
    ids = [service.create(conn, add_user(conn, f"i{i}@example.test")["id"], clean_of(), NOW) for i in range(2)]
    invite(conn, employer, ids[0], vid)
    with pytest.raises(ResumeError) as err:
        invite(conn, employer, ids[1], vid)
    assert err.value.code == "invite_limit"


def test_default_invite_text_mentions_vacancy(conn, seeker, employer, resume_id):
    vid = add_vacancy(conn, employer["id"])
    card = service.get_for_employer(conn, resume_id, employer["id"], NOW)
    text = service.default_invite_text(card, service.employer_vacancies(conn, employer["id"])[0])
    assert "Бариста" in text and "Мария" in text and vid


# ------------------------------------------------------------------ отклик и удаление аккаунта

def test_apply_options_and_check(conn, seeker, resume_id):
    assert [r["id"] for r in service.apply_options(conn, seeker["id"], NOW)] == [resume_id]
    assert service.check_for_apply(conn, resume_id, seeker["id"]) == resume_id
    stranger = add_user(conn, "s2@example.test")
    with pytest.raises(ResumeError):
        service.check_for_apply(conn, resume_id, stranger["id"])
    service.hide(conn, resume_id, seeker["id"], NOW)
    assert service.apply_options(conn, seeker["id"], NOW) == []
    with pytest.raises(ResumeError):
        service.check_for_apply(conn, resume_id, seeker["id"])


def test_admin_hidden_resume_cannot_be_attached(conn, seeker, resume_id):
    conn.execute("UPDATE resumes SET admin_hidden = 1")
    with pytest.raises(ResumeError):
        service.check_for_apply(conn, resume_id, seeker["id"])


def test_erase_user_data_removes_resumes_and_views(conn, seeker, employer, resume_id):
    service.open_contact(conn, resume_id, employer["id"], NOW)
    service.erase_user_data(conn, seeker["id"])
    assert conn.execute("SELECT COUNT(*) FROM resumes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM resume_views").fetchone()[0] == 0


def test_erase_employer_removes_his_views_only(conn, seeker, employer, resume_id):
    service.open_contact(conn, resume_id, employer["id"], NOW)
    service.erase_user_data(conn, employer["id"])
    assert conn.execute("SELECT COUNT(*) FROM resumes").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM resume_views").fetchone()[0] == 0


def test_delete_account_removes_resumes(conn, seeker, resume_id):
    from rabota.accounts import service as accounts

    accounts.delete_account(conn, seeker["id"])
    assert conn.execute("SELECT COUNT(*) FROM resumes").fetchone()[0] == 0
