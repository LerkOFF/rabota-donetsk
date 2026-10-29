"""Кабинет работодателя без веба: состояния вакансий, закрытие, поднятие, правка, удаление, компания, сводка."""
from __future__ import annotations

from datetime import timedelta

import pytest

from rabota import cabinet
from rabota.cabinet import CabinetError
from rabota.web.postform import validate_post
from tests.cabinet.conftest import NOW, make_row

FORM = {
    "profession": "Бариста", "category": "общепит", "city": "Донецк", "summary": "Готовить кофе и общаться с гостями.",
    "phone": "+7 949 000-00-00", "pay_min": "30000", "pay_max": "40000", "pay_period": "месяц",
    "employer": "Кофейня Зёрна", "schedule": "2/2",
}


def vacancy(**over):
    v, _, errors = validate_post({**FORM, **over})
    assert v is not None, errors
    return v


def row(conn, vid):
    return dict(conn.execute("SELECT * FROM vacancies WHERE id=?", (vid,)).fetchone())


# ------------------------------------------------------------------ состояния

@pytest.mark.parametrize("kw,key", [
    ({}, "published"),
    ({"status": "hidden", "moderation": "pending"}, "pending"),
    ({"status": "hidden", "moderation": "rejected", "note": "Нет контакта"}, "rejected"),
    ({"status": "closed"}, "closed"),
    ({"status": "expired"}, "expired"),
    ({"seen": NOW - timedelta(days=22)}, "expired"),
    ({"status": "hidden", "note": "Скрыто администратором"}, "hidden"),
    ({"suspicious": 1}, "hidden"),
])
def test_state_keys(conn, boss, kw, key):
    r = row(conn, make_row(conn, boss["id"], **kw))
    assert cabinet.vacancy_state(r, NOW).key == key


def test_state_texts_are_plain_russian(conn, boss):
    rejected = cabinet.vacancy_state(row(conn, make_row(conn, boss["id"], status="hidden", moderation="rejected", note="Нет контакта")), NOW)
    assert rejected.label == "Отклонена" and rejected.note == "Нет контакта"
    pending = cabinet.vacancy_state(row(conn, make_row(conn, boss["id"], status="hidden", moderation="pending")), NOW)
    assert pending.label == "На проверке"
    live = cabinet.vacancy_state(row(conn, make_row(conn, boss["id"], seen=NOW - timedelta(days=15))), NOW)
    assert live.label == "Опубликована" and live.days_left == 6
    assert "6" in live.note
    last = cabinet.vacancy_state(row(conn, make_row(conn, boss["id"], seen=NOW - timedelta(days=20, hours=2))), NOW)
    assert last.days_left == 1 and last.soon
    assert cabinet.vacancy_state(row(conn, make_row(conn, boss["id"], status="closed")), NOW).label == "Закрыта"
    assert cabinet.vacancy_state(row(conn, make_row(conn, boss["id"], status="expired")), NOW).label == "Срок истёк"


# ------------------------------------------------------------------ список

def test_list_only_own_with_counts_and_states(conn, boss, other, seeker):
    mine = make_row(conn, boss["id"])
    make_row(conn, boss["id"], status="hidden", moderation="pending")
    make_row(conn, other["id"], profession="повар")
    conn.execute("INSERT INTO applications(vacancy_id, user_id, status) VALUES (?,?,'new')", (mine, seeker["id"]))
    conn.commit()
    data = cabinet.list_vacancies(conn, boss["id"], now=NOW)
    assert data["total"] == 2 and data["counts"]["all"] == 2
    assert data["counts"]["published"] == 1 and data["counts"]["pending"] == 1
    by_id = {i["id"]: i for i in data["items"]}
    assert by_id[mine]["responses"] == 1 and by_id[mine]["new_responses"] == 1
    assert all(i["owner_user_id"] == boss["id"] for i in data["items"])


def test_list_filter_and_paging(conn, boss):
    for i in range(23):
        make_row(conn, boss["id"], profession=f"курьер {i}")
    make_row(conn, boss["id"], status="closed")
    page1 = cabinet.list_vacancies(conn, boss["id"], now=NOW)
    assert page1["total"] == 24 and page1["pages"] == 2 and len(page1["items"]) == 20 and page1["has_next"]
    page2 = cabinet.list_vacancies(conn, boss["id"], now=NOW, page=2)
    assert len(page2["items"]) == 4 and page2["has_prev"] and not page2["has_next"]
    assert cabinet.list_vacancies(conn, boss["id"], now=NOW, page=99)["page"] == 2
    closed = cabinet.list_vacancies(conn, boss["id"], now=NOW, state="closed")
    assert closed["total"] == 1
    assert cabinet.list_vacancies(conn, boss["id"], now=NOW, state="rejected")["total"] == 0
    assert cabinet.list_vacancies(conn, boss["id"], now=NOW, state="nonsense")["total"] == 24


def test_list_empty(conn, boss):
    data = cabinet.list_vacancies(conn, boss["id"], now=NOW)
    assert data["total"] == 0 and data["items"] == [] and data["counts"]["all"] == 0


# ------------------------------------------------------------------ закрыть

def test_close_published(conn, boss):
    vid = make_row(conn, boss["id"])
    cabinet.close_vacancy(conn, boss["id"], vid, NOW)
    assert row(conn, vid)["status"] == "closed"


def test_close_foreign_or_missing_is_not_found(conn, boss, other):
    vid = make_row(conn, other["id"])
    with pytest.raises(CabinetError) as e:
        cabinet.close_vacancy(conn, boss["id"], vid, NOW)
    assert e.value.code == "not_found"
    assert row(conn, vid)["status"] == "active"
    with pytest.raises(CabinetError):
        cabinet.close_vacancy(conn, boss["id"], 9999, NOW)


def test_close_unpublished_is_refused(conn, boss):
    vid = make_row(conn, boss["id"], status="hidden", moderation="pending")
    with pytest.raises(CabinetError) as e:
        cabinet.close_vacancy(conn, boss["id"], vid, NOW)
    assert e.value.code == "not_allowed"


# ------------------------------------------------------------------ продлить, поднять

def test_raise_resets_term_and_moves_up(conn, boss):
    vid = make_row(conn, boss["id"], seen=NOW - timedelta(days=5))
    cabinet.raise_vacancy(conn, boss["id"], vid, NOW)
    r = row(conn, vid)
    assert r["last_seen_at"] == NOW.replace(microsecond=0).isoformat() and r["published_at"] == r["last_seen_at"]
    assert r["status"] == "active"


def test_raise_not_more_than_once_a_day(conn, boss):
    vid = make_row(conn, boss["id"], seen=NOW - timedelta(days=5))
    cabinet.raise_vacancy(conn, boss["id"], vid, NOW)
    with pytest.raises(CabinetError) as e:
        cabinet.raise_vacancy(conn, boss["id"], vid, NOW + timedelta(hours=23))
    assert e.value.code == "too_soon"
    cabinet.raise_vacancy(conn, boss["id"], vid, NOW + timedelta(hours=24, minutes=1))
    assert row(conn, vid)["last_seen_at"] == (NOW + timedelta(hours=24, minutes=1)).replace(microsecond=0).isoformat()


def test_raise_wait_is_reported(conn, boss):
    vid = make_row(conn, boss["id"], seen=NOW - timedelta(hours=20))
    wait = cabinet.raise_wait(row(conn, vid), NOW)
    assert wait is not None and timedelta(hours=3, minutes=59) < wait <= timedelta(hours=4)
    old = make_row(conn, boss["id"], seen=NOW - timedelta(days=3))
    assert cabinet.raise_wait(row(conn, old), NOW) is None


def test_raise_expired_brings_it_back(conn, boss):
    vid = make_row(conn, boss["id"], status="expired", seen=NOW - timedelta(days=25))
    cabinet.raise_vacancy(conn, boss["id"], vid, NOW)
    r = row(conn, vid)
    assert r["status"] == "active" and cabinet.vacancy_state(r, NOW).key == "published"


def test_reopen_closed_inside_term_keeps_dates(conn, boss):
    seen = NOW - timedelta(hours=2)
    vid = make_row(conn, boss["id"], status="closed", seen=seen)
    cabinet.raise_vacancy(conn, boss["id"], vid, NOW)
    r = row(conn, vid)
    assert r["status"] == "active" and r["last_seen_at"] == seen.replace(microsecond=0).isoformat()


def test_reopen_closed_after_term_resets(conn, boss):
    vid = make_row(conn, boss["id"], status="closed", seen=NOW - timedelta(days=30))
    cabinet.raise_vacancy(conn, boss["id"], vid, NOW)
    assert row(conn, vid)["last_seen_at"] == NOW.replace(microsecond=0).isoformat()


@pytest.mark.parametrize("kw", [
    {"status": "hidden", "moderation": "pending"},
    {"status": "hidden", "moderation": "rejected", "note": "нет"},
    {"status": "hidden", "note": "Скрыто администратором"},
])
def test_raise_refused_when_not_approved_or_hidden(conn, boss, kw):
    vid = make_row(conn, boss["id"], seen=NOW - timedelta(days=5), **kw)
    with pytest.raises(CabinetError) as e:
        cabinet.raise_vacancy(conn, boss["id"], vid, NOW)
    assert e.value.code == "not_allowed"


def test_raise_foreign(conn, boss, other):
    vid = make_row(conn, other["id"], seen=NOW - timedelta(days=5))
    with pytest.raises(CabinetError) as e:
        cabinet.raise_vacancy(conn, boss["id"], vid, NOW)
    assert e.value.code == "not_found"


# ------------------------------------------------------------------ правка

def test_review_groups():
    old = {"pay_min": 30000, "pay_max": 40000, "pay_period": "месяц", "pay_daily": 0, "contacts": '{"phones": ["+79490000000"]}',
           "profession": "бариста", "employer": "Кофейня Зёрна", "summary": "Готовить кофе и общаться с гостями.", "requirements": None}
    assert cabinet.review_reasons(old, vacancy()) == []
    assert cabinet.review_reasons(old, vacancy(pay_max="50000")) == ["оплата"]
    assert cabinet.review_reasons(old, vacancy(phone="+7 949 111-22-33")) == ["контакты"]
    assert cabinet.review_reasons(old, vacancy(telegram="@zerno_bar")) == ["контакты"]
    assert cabinet.review_reasons(old, vacancy(summary="Готовить кофе и встречать гостей по утрам.")) == ["описание"]
    assert cabinet.review_reasons(old, vacancy(requirements="Опыт от года")) == ["описание"]
    assert cabinet.review_reasons(old, vacancy(pay_daily="1", phone="+79491112233")) == ["оплата", "контакты"]
    # график, район, сфера и прочее проверки не требуют
    assert cabinet.review_reasons(old, vacancy(schedule="5/2", district="Ворошиловский", age_min="18")) == []


def test_edit_non_sensitive_keeps_published(conn, boss):
    vid = make_row(conn, boss["id"])
    out = cabinet.update_vacancy(conn, boss["id"], vid, vacancy(schedule="5/2", district="Ворошиловский"), NOW)
    r = row(conn, vid)
    assert out == "saved" and r["status"] == "active" and r["moderation_status"] == "approved"
    assert r["schedule"] == "5/2" and r["district"] == "Ворошиловский"


def test_edit_pay_sends_published_back_to_review(conn, boss):
    vid = make_row(conn, boss["id"])
    out = cabinet.update_vacancy(conn, boss["id"], vid, vacancy(pay_max="55000"), NOW)
    r = row(conn, vid)
    assert out == "review" and r["moderation_status"] == "pending" and r["status"] == "hidden" and r["pay_max"] == 55000
    assert cabinet.vacancy_state(r, NOW).key == "pending"


def test_edit_contacts_and_text_send_back_to_review(conn, boss):
    for kw in ({"phone": "+7 949 123-45-67"}, {"summary": "Совсем другое описание работы в кофейне."}):
        vid = make_row(conn, boss["id"])
        assert cabinet.update_vacancy(conn, boss["id"], vid, vacancy(**kw), NOW) == "review"
        assert row(conn, vid)["moderation_status"] == "pending"


def test_edit_without_premoderation_never_hides(conn, boss):
    vid = make_row(conn, boss["id"])
    out = cabinet.update_vacancy(conn, boss["id"], vid, vacancy(pay_max="55000"), NOW, premoderate=False)
    assert out == "saved" and row(conn, vid)["status"] == "active"


def test_edit_pending_stays_pending(conn, boss):
    vid = make_row(conn, boss["id"], status="hidden", moderation="pending")
    assert cabinet.update_vacancy(conn, boss["id"], vid, vacancy(pay_max="55000"), NOW) == "saved"
    r = row(conn, vid)
    assert r["moderation_status"] == "pending" and r["status"] == "hidden"


def test_edit_rejected_is_resubmitted_and_reason_cleared(conn, boss):
    vid = make_row(conn, boss["id"], status="hidden", moderation="rejected", note="Нет контакта")
    out = cabinet.update_vacancy(conn, boss["id"], vid, vacancy(), NOW)
    r = row(conn, vid)
    assert out == "resubmitted" and r["moderation_status"] == "pending" and r["moderation_note"] is None


def test_edit_closed_with_sensitive_change_needs_review(conn, boss):
    vid = make_row(conn, boss["id"], status="closed")
    assert cabinet.update_vacancy(conn, boss["id"], vid, vacancy(pay_max="55000"), NOW) == "review"
    r = row(conn, vid)
    assert r["status"] == "hidden" and r["moderation_status"] == "pending"


def test_edit_hidden_by_moderator_is_refused(conn, boss):
    vid = make_row(conn, boss["id"], status="hidden", note="Скрыто администратором")
    with pytest.raises(CabinetError) as e:
        cabinet.update_vacancy(conn, boss["id"], vid, vacancy(), NOW)
    assert e.value.code == "not_allowed"


def test_edit_foreign_is_not_found_and_changes_nothing(conn, boss, other):
    vid = make_row(conn, other["id"])
    with pytest.raises(CabinetError) as e:
        cabinet.update_vacancy(conn, boss["id"], vid, vacancy(schedule="5/2"), NOW)
    assert e.value.code == "not_found" and row(conn, vid)["schedule"] is None


def test_edit_keeps_owner_origin_and_dates(conn, boss):
    seen = NOW - timedelta(days=3)
    vid = make_row(conn, boss["id"], seen=seen)
    cabinet.update_vacancy(conn, boss["id"], vid, vacancy(schedule="5/2"), NOW)
    r = row(conn, vid)
    assert r["owner_user_id"] == boss["id"] and r["origin"] == "direct" and r["last_seen_at"] == seen.replace(microsecond=0).isoformat()


def test_form_values_roundtrip(conn, boss):
    vid = make_row(conn, boss["id"], contacts='{"phones": ["+79490000000"], "telegram": "@zerno_bar", "email": "a@b.ru"}',
                   experience="не нужен", age_min=18, pay_daily=1)
    values = cabinet.form_values(row(conn, vid))
    assert values["profession"] == "Бариста" and values["phone"] == "+79490000000" and values["telegram"] == "@zerno_bar"
    assert values["email"] == "a@b.ru" and values["no_experience"] == "1" and values["pay_daily"] == "1"
    assert values["pay_min"] == "30000" and values["age_min"] == "18"
    # то, что показано в форме, проходит ту же проверку
    v, _, errors = validate_post({**values, "category": "общепит"})
    assert v is not None and not errors


# ------------------------------------------------------------------ удаление

def test_delete_removes_vacancy_responses_and_keeps_threads(conn, boss, other, seeker):
    vid = make_row(conn, boss["id"])
    app_id = conn.execute("INSERT INTO applications(vacancy_id, user_id) VALUES (?,?)", (vid, seeker["id"])).lastrowid
    conv = conn.execute(
        "INSERT INTO conversations(employer_user_id, seeker_user_id, vacancy_id, application_id, started_by, title)"
        " VALUES (?,?,?,?,?,?)", (boss["id"], seeker["id"], vid, app_id, seeker["id"], "бариста")).lastrowid
    conn.execute("INSERT INTO complaints(vacancy_id, reason) VALUES (?, 'x')", (vid,))
    conn.commit()
    assert cabinet.delete_vacancy(conn, boss["id"], vid) == 1
    assert conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM complaints").fetchone()[0] == 0
    thread = conn.execute("SELECT vacancy_id, application_id, title FROM conversations WHERE id=?", (conv,)).fetchone()
    assert thread["vacancy_id"] is None and thread["application_id"] is None and thread["title"] == "бариста"


def test_delete_foreign_is_not_found(conn, boss, other):
    vid = make_row(conn, other["id"])
    with pytest.raises(CabinetError) as e:
        cabinet.delete_vacancy(conn, boss["id"], vid)
    assert e.value.code == "not_found"
    assert conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 1


def test_delete_never_touches_parsed_vacancy_without_owner(conn, boss):
    vid = make_row(conn, None, origin="parsed")
    with pytest.raises(CabinetError):
        cabinet.delete_vacancy(conn, boss["id"], vid)


# ------------------------------------------------------------------ компания

def test_company_validation():
    ok, errors = cabinet.validate_company({"name": " Кофейня  «Зерно» ", "site": "zerno.example", "description": "Сеть\n кофеен",
                                           "city": "", "phone": "8 949 123-45-67", "contact_email": "Hr@Zerno.Example"})
    assert not errors
    assert ok["name"] == "Кофейня «Зерно»" and ok["site"] == "https://zerno.example" and ok["city"] == "Донецк"
    assert ok["description"] == "Сеть кофеен" and ok["phone"] == "+79491234567" and ok["contact_email"] == "hr@zerno.example"


@pytest.mark.parametrize("field,value", [
    ("name", ""), ("name", "а"), ("site", "javascript:alert(1)"), ("site", "not a site"), ("site", "ftp://x.ru"),
    ("phone", "12345"), ("contact_email", "no-at-sign"), ("description", "я" * 1501),
])
def test_company_validation_errors(field, value):
    _, errors = cabinet.validate_company({"name": "Кофейня", field: value})
    assert field in errors


def test_company_strips_invisible_and_limits():
    ok, errors = cabinet.validate_company({"name": "Кофе\u200bйня\x00", "description": "Текст" + chr(0x202E) + " с мусором"})
    assert not errors and ok["name"] == "Кофейня" and chr(0x202E) not in ok["description"]
    ok, errors = cabinet.validate_company({"name": "К" * 200})
    assert "name" in errors


def test_company_save_creates_then_updates(conn, boss):
    assert cabinet.get_company(conn, boss["id"]) is None
    cleaned, _ = cabinet.validate_company({"name": "Кофейня", "city": "Макеевка", "phone": "+79490000000"})
    cid = cabinet.save_company(conn, boss["id"], cleaned)
    cleaned["name"] = "Кофейня Зерно"
    assert cabinet.save_company(conn, boss["id"], cleaned) == cid
    c = cabinet.get_company(conn, boss["id"])
    assert c["name"] == "Кофейня Зерно" and c["city"] == "Макеевка" and c["phone"] == "+79490000000"
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1


def test_ensure_company_reuses_or_creates(conn, boss, other):
    first = cabinet.ensure_company(conn, boss, "Кофейня", "Донецк")
    assert cabinet.ensure_company(conn, boss, "Другое имя", "Донецк") == first
    assert cabinet.get_company(conn, boss["id"])["name"] == "Кофейня"
    second = cabinet.ensure_company(conn, other, "", "Донецк")
    assert second != first and cabinet.get_company(conn, other["id"])["name"] == "Игорь Чужой"


def test_company_does_not_leak_between_owners(conn, boss, other):
    cleaned, _ = cabinet.validate_company({"name": "Моя"})
    cabinet.save_company(conn, boss["id"], cleaned)
    assert cabinet.get_company(conn, other["id"]) is None


# ------------------------------------------------------------------ сводка

def test_employer_summary(conn, boss):
    make_row(conn, boss["id"], seen=NOW - timedelta(days=19))
    make_row(conn, boss["id"], status="hidden", moderation="rejected", note="Нет контакта", profession="повар")
    make_row(conn, boss["id"], status="hidden", moderation="pending", profession="кассир")
    make_row(conn, boss["id"], status="closed", profession="грузчик")
    s = cabinet.employer_summary(conn, boss["id"], NOW)
    assert s["counts"]["published"] == 1 and s["counts"]["rejected"] == 1 and s["counts"]["pending"] == 1
    assert s["counts"]["closed"] == 1 and s["counts"]["all"] == 4
    kinds = {a["kind"] for a in s["attention"]}
    assert kinds == {"rejected", "soon"}
    rejected = next(a for a in s["attention"] if a["kind"] == "rejected")
    assert "Нет контакта" in rejected["text"] and rejected["href"].endswith("/edit")
    assert s["company"] is None
