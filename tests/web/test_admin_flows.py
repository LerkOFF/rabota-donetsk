"""Сценарии админки через сайт: размещение, модерация, жалобы, стоп-лист, запросы, журнал."""
import re

import pytest

from tests.accounts.harness import csrf_of
from tests.web.admin_harness import add_vacancy, apost, login_admin, make_env, one, rows

POST_FORM = {
    "profession": "Бариста", "category": "общепит", "city": "Донецк", "summary": "Готовить кофе и общаться с гостями.",
    "phone": "+7 949 123-45-67", "email": "boss@example.test", "employer": "Кофейня Уют",
}


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e


@pytest.fixture
def admin(env):
    return login_admin(env)


def submit_vacancy(env, **over):
    r = env.client.post("/razmestit", data={**POST_FORM, **over}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return one(env, "SELECT MAX(id) AS id FROM vacancies")["id"]


# ------------------------------------------------------------------ модерация

def test_submitted_vacancy_waits_in_queue_then_goes_live(env, admin):
    vid = submit_vacancy(env)
    assert env.client.get(f"/vacancy/{vid}").status_code in (404,)  # скрыта до проверки
    assert f"/vacancy/{vid}" not in env.client.get("/vacancies").text
    page = env.client.get("/admin/moderation")
    assert "Бариста" in page.text
    assert f"/admin/vacancies/{vid}" in page.text
    assert "Кофейня Уют" in env.client.get(f"/admin/vacancies/{vid}").text  # просмотр карточки
    r = apost(env, f"/admin/vacancies/{vid}/approve")
    assert r.status_code == 303
    assert env.client.get(f"/vacancy/{vid}").status_code == 200
    assert "бариста" in env.client.get("/vacancies").text.lower()
    assert "Кофейня Уют" in env.client.get("/admin/audit").text or "vacancy.approve" in env.client.get("/admin/audit").text


def test_pending_not_in_listing_or_sitemap(env, admin):
    vid = submit_vacancy(env)
    assert f"/vacancy/{vid}" not in env.client.get("/sitemap.xml").text
    assert "Кофейня Уют" not in env.client.get("/vacancies").text


def test_reject_with_template_mails_author(env, admin):
    vid = submit_vacancy(env)
    r = apost(env, f"/admin/vacancies/{vid}/reject", {"reason_code": "paid", "reason": ""})
    assert r.status_code == 303
    v = one(env, "SELECT moderation_status, status, moderation_note FROM vacancies WHERE id=?", vid)
    assert (v["moderation_status"], v["status"]) == ("rejected", "hidden") and "заплатить" in v["moderation_note"]
    mail = env.mailer.last("boss@example.test")
    assert mail and "заплатить" in mail.body and "Бариста" in mail.body


def test_reject_without_reason_shows_error(env, admin):
    vid = submit_vacancy(env)
    r = apost(env, f"/admin/vacancies/{vid}/reject", {"reason_code": "", "reason": ""}, page=f"/admin/vacancies/{vid}")
    assert r.status_code == 422 and "причин" in r.text.lower()
    assert one(env, "SELECT moderation_status FROM vacancies WHERE id=?", vid)["moderation_status"] == "pending"


def test_hide_and_restore(env, admin):
    vid = add_vacancy(env)
    assert env.client.get(f"/vacancy/{vid}").status_code == 200
    apost(env, f"/admin/vacancies/{vid}/hide", {"reason": "проверка"}, page=f"/admin/vacancies/{vid}")
    assert env.client.get(f"/vacancy/{vid}").status_code == 404
    apost(env, f"/admin/vacancies/{vid}/restore", page=f"/admin/vacancies/{vid}")
    assert env.client.get(f"/vacancy/{vid}").status_code == 200


def test_edit_fields(env, admin):
    vid = submit_vacancy(env)
    data = {**POST_FORM, "profession": "Старший бариста", "pay_min": "5000", "pay_period": "смена"}
    r = apost(env, f"/admin/vacancies/{vid}/edit", data, page=f"/admin/vacancies/{vid}")
    assert r.status_code == 303
    v = one(env, "SELECT profession, pay_min, pay_period, status FROM vacancies WHERE id=?", vid)
    assert (v["profession"], v["pay_min"], v["pay_period"], v["status"]) == ("старший бариста", 5000, "смена", "hidden")


def test_edit_validation_error_keeps_data(env, admin):
    vid = submit_vacancy(env)
    r = apost(env, f"/admin/vacancies/{vid}/edit", {**POST_FORM, "profession": "", "phone": ""}, page=f"/admin/vacancies/{vid}")
    assert r.status_code == 422 and "error" in r.text
    assert one(env, "SELECT profession FROM vacancies WHERE id=?", vid)["profession"] == "бариста"


def test_edit_keeps_other_contacts(env, admin):
    vid = add_vacancy(env, contacts={"phones": ["+79490000000"], "max": "boss_max"}, status="hidden")
    apost(env, f"/admin/vacancies/{vid}/edit",
          {"profession": "бариста", "category": "общепит", "summary": "Готовить кофе и общаться с гостями.",
           "phone": "+7 949 111-22-33", "city": "Донецк"}, page=f"/admin/vacancies/{vid}")
    contacts = one(env, "SELECT contacts FROM vacancies WHERE id=?", vid)["contacts"]
    assert "79491112233" in contacts and "boss_max" in contacts and "79490000000" not in contacts


def test_unknown_vacancy_404(env, admin):
    assert env.client.get("/admin/vacancies/99999").status_code == 404
    assert apost(env, "/admin/vacancies/99999/approve").status_code == 404


def test_queue_empty_state_and_bad_params(env, admin):
    r = env.client.get("/admin/moderation?view=%27%3Bdrop&page=abc&q=" + "я" * 500)
    assert r.status_code == 200 and "Очередь пуста" in env.client.get("/admin/moderation").text


def test_xss_is_escaped(env, admin):
    vid = add_vacancy(env, profession="<script>alert(1)</script>", employer='"><img src=x onerror=1>',
                      status="hidden", moderation_status="pending")
    for path in ("/admin/moderation", f"/admin/vacancies/{vid}"):
        html = env.client.get(path).text
        assert "<script>alert(1)" not in html and "<img src=x" not in html


def test_stoplist_warning_on_card(env, admin):
    vid = submit_vacancy(env)
    apost(env, "/admin/stoplist/add", {"kind": "phone", "value": "+7 949 123-45-67", "apply_existing": ""})
    assert "стоп-лист" in env.client.get(f"/admin/vacancies/{vid}").text.lower()


# ------------------------------------------------------------------ жалобы

def complain(env, vid, reason="Мошенничество или просьба заплатить за работу", email=""):
    r = env.client.post(f"/vacancy/{vid}/complaint", data={"reason": reason, "comment": "тест", "email": email},
                        headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303, r.text


def test_complaint_flow_hide(env, admin):
    vid = add_vacancy(env)
    complain(env, vid, email="user@example.test")
    c = one(env, "SELECT * FROM complaints")
    assert c["reporter_email"] == "user@example.test" and c["reason_code"] == "fraud" and c["reporter_key"]
    page = env.client.get("/admin/complaints")
    assert "Мошенничество" in page.text and f"/admin/complaints/{c['id']}" in page.text
    detail = env.client.get(f"/admin/complaints/{c['id']}")
    assert detail.status_code == 200 and "user@example.test" in detail.text
    r = apost(env, f"/admin/complaints/{c['id']}/hide", {"note": "подтвердилось"}, page=f"/admin/complaints/{c['id']}")
    assert r.status_code == 303
    assert env.client.get(f"/vacancy/{vid}").status_code == 404
    assert one(env, "SELECT status, resolution FROM complaints")["resolution"] == "hidden"


def test_complaint_bad_email_rejected(env):
    vid = add_vacancy(env)
    r = env.client.post(f"/vacancy/{vid}/complaint", data={"reason": "Другое", "comment": "что-то не так", "email": "не почта"},
                        headers={"origin": "http://testserver"})
    assert r.status_code == 422 and rows(env, "SELECT * FROM complaints") == []


def test_complaint_reasons_cover_duplicate_and_foreign_data(env):
    vid = add_vacancy(env)
    html = env.client.get(f"/vacancy/{vid}").text
    assert "Дубль" in html and "мои данные" in html


def test_autohide_through_public_form(env, monkeypatch):
    monkeypatch.setenv("COMPLAINT_AUTOHIDE_THRESHOLD", "1")
    vid = add_vacancy(env)
    complain(env, vid)
    assert one(env, "SELECT status FROM vacancies WHERE id=?", vid)["status"] == "hidden"
    assert env.client.get(f"/vacancy/{vid}").status_code == 404


def test_same_person_does_not_trigger_autohide(env, monkeypatch):
    monkeypatch.setenv("COMPLAINT_AUTOHIDE_THRESHOLD", "2")
    vid = add_vacancy(env)
    complain(env, vid)
    complain(env, vid)
    assert one(env, "SELECT status FROM vacancies WHERE id=?", vid)["status"] == "active"


def test_complaint_dismiss_and_reply(env, admin):
    vid = add_vacancy(env)
    complain(env, vid, email="user@example.test")
    cid = one(env, "SELECT id FROM complaints")["id"]
    r = apost(env, f"/admin/complaints/{cid}/reply", {"text": "Проверили, всё в порядке."}, page=f"/admin/complaints/{cid}")
    assert r.status_code == 303
    mail = env.mailer.last("user@example.test")
    assert mail and "всё в порядке" in mail.body
    assert one(env, "SELECT reply_sent_at FROM complaints")["reply_sent_at"]
    apost(env, f"/admin/complaints/{cid}/dismiss", {"note": "нет оснований"}, page=f"/admin/complaints/{cid}")
    assert one(env, "SELECT status FROM complaints")["status"] == "dismissed"
    assert env.client.get(f"/vacancy/{vid}").status_code == 200


def test_reply_without_address_is_error(env, admin):
    vid = add_vacancy(env)
    complain(env, vid)
    cid = one(env, "SELECT id FROM complaints")["id"]
    r = apost(env, f"/admin/complaints/{cid}/reply", {"text": "привет"}, page=f"/admin/complaints/{cid}")
    assert r.status_code == 422 and not env.mailer.outbox


def test_complaint_stoplist_action(env, admin):
    vid = add_vacancy(env, contacts={"phones": ["+79490000000"]}, employer="Тёмные Дела")
    other = add_vacancy(env, profession="повар", contacts={"phones": ["+79490000000"]})
    complain(env, vid)
    cid = one(env, "SELECT id FROM complaints")["id"]
    detail = env.client.get(f"/admin/complaints/{cid}").text
    assert "+79490000000" in detail
    r = apost(env, f"/admin/complaints/{cid}/stoplist",
              {"stop": ["phone|+79490000000", "employer|Тёмные Дела"], "extra_kind": "keyword", "extra_value": "предоплата",
               "note": "мошенники"}, page=f"/admin/complaints/{cid}")
    assert r.status_code == 303
    kinds = {e["kind"] for e in rows(env, "SELECT kind FROM stoplist")}
    assert kinds == {"phone", "employer", "keyword"}
    for v in (vid, other):
        assert env.client.get(f"/vacancy/{v}").status_code == 404


def test_stoplist_action_without_selection_is_error(env, admin):
    vid = add_vacancy(env)
    complain(env, vid)
    cid = one(env, "SELECT id FROM complaints")["id"]
    r = apost(env, f"/admin/complaints/{cid}/stoplist", {"note": "x"}, page=f"/admin/complaints/{cid}")
    assert r.status_code == 422 and rows(env, "SELECT * FROM stoplist") == []


def test_reports_queue_and_block(env, admin):
    conn = env.db()
    conn.execute("INSERT INTO users(id,email,password_hash,role) VALUES (50,'bad@example.test','h','employer')")
    conn.execute("INSERT INTO reports(reporter_user_id, target_type, target_id, reason) VALUES (NULL,'user',50,'Спам')")
    conn.commit()
    conn.close()
    assert "bad@example.test" in env.client.get("/admin/reports").text
    r = apost(env, "/admin/reports/1/block", {"note": "спам"}, page="/admin/reports")
    assert r.status_code == 303
    assert one(env, "SELECT status FROM users WHERE id=50")["status"] == "blocked"
    assert one(env, "SELECT status FROM reports")["status"] == "resolved"


# ------------------------------------------------------------------ стоп-лист

def test_stoplist_add_apply_and_delete(env, admin):
    vid = add_vacancy(env, contacts={"phones": ["+79491112233"]})
    r = apost(env, "/admin/stoplist/add", {"kind": "phone", "value": "8 949 111 22 33", "note": "спам", "apply_existing": "1"})
    assert r.status_code == 303
    assert env.client.get(f"/vacancy/{vid}").status_code == 404
    page = env.client.get("/admin/stoplist").text
    assert "9491112233" in page.replace("+7", "").replace(" ", "")
    eid = one(env, "SELECT id FROM stoplist")["id"]
    assert apost(env, f"/admin/stoplist/{eid}/delete", page="/admin/stoplist").status_code == 303
    assert rows(env, "SELECT * FROM stoplist") == []


def test_stoplist_add_invalid(env, admin):
    r = apost(env, "/admin/stoplist/add", {"kind": "phone", "value": "12"}, page="/admin/stoplist")
    assert r.status_code == 422 and "Телефон" in r.text
    assert apost(env, "/admin/stoplist/add", {"kind": "nope", "value": "xxx"}, page="/admin/stoplist").status_code == 422


# ------------------------------------------------------------------ запросы по ПДн

LEGAL = {"kind": "vacancy_owner", "name": "Иван", "contact": "ivan@example.test",
         "subject": "https://работа-донецк.рф/vacancy/5", "body": "Это моя вакансия, уберите её, пожалуйста."}


def public_request(env, **over):
    html = env.client.get("/legal/request").text
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    cap = re.search(r'name="captcha_token" value="([^"]+)"', html).group(1)
    q = re.search(r"Сколько будет (\d+) \+ (\d+)\?", html)
    data = {"csrf_token": token, "captcha_token": cap, "captcha_answer": str(int(q.group(1)) + int(q.group(2))), **LEGAL, **over}
    return env.client.post("/legal/request", data=data, follow_redirects=False)


def test_request_flow_from_public_form_to_decision(env, admin):
    r = public_request(env)
    assert r.status_code == 303 and "/legal/request?sent=" in r.headers["location"]
    row = one(env, "SELECT * FROM legal_requests")
    assert row["kind"] == "vacancy_owner" and row["channel"] == "web" and row["status"] == "new"
    page = env.client.get("/admin/requests")
    assert "ivan@example.test" in page.text and "Убрать мою вакансию" in page.text
    detail = env.client.get(f"/admin/requests/{row['id']}").text
    assert "уберите её" in detail
    r = apost(env, f"/admin/requests/{row['id']}/update",
              {"status": "done", "decision_code": "satisfied", "decision": "Вакансия скрыта, ответили письмом"},
              page=f"/admin/requests/{row['id']}")
    assert r.status_code == 303
    done = one(env, "SELECT * FROM legal_requests")
    assert done["status"] == "done" and done["answered_at"]
    assert "Завершено" in env.client.get("/admin/requests?view=done").text


def test_request_done_without_decision_is_error(env, admin):
    public_request(env)
    rid = one(env, "SELECT id FROM legal_requests")["id"]
    r = apost(env, f"/admin/requests/{rid}/update", {"status": "done", "decision": ""}, page=f"/admin/requests/{rid}")
    assert r.status_code == 422 and one(env, "SELECT status FROM legal_requests")["status"] == "new"


def test_overdue_requests_are_highlighted(env, admin):
    public_request(env)
    conn = env.db()
    conn.execute("UPDATE legal_requests SET due_at='2020-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()
    page = env.client.get("/admin/requests").text
    assert "Просрочено" in page and "is-overdue" in page
    assert "Просрочено" in env.client.get("/admin").text


def test_erase_by_phone_via_admin(env, admin):
    vid = add_vacancy(env, contacts={"phones": ["+79490000000"]})
    other = add_vacancy(env, profession="повар", contacts={"phones": ["+79491112233"]})
    public_request(env, kind="delete", subject="+7 949 000-00-00")
    rid = one(env, "SELECT id FROM legal_requests")["id"]
    r = apost(env, f"/admin/requests/{rid}/erase", {"kind": "phone", "value": "+7 949 000-00-00"}, page=f"/admin/requests/{rid}")
    assert r.status_code == 303
    assert env.client.get(f"/vacancy/{vid}").status_code == 404
    assert env.client.get(f"/vacancy/{other}").status_code == 200
    assert one(env, "SELECT executed FROM legal_requests")["executed"] == 1
    assert one(env, "SELECT source FROM stoplist")["source"] == "erasure"


def test_erase_invalid_value(env, admin):
    public_request(env)
    rid = one(env, "SELECT id FROM legal_requests")["id"]
    r = apost(env, f"/admin/requests/{rid}/erase", {"kind": "phone", "value": "1"}, page=f"/admin/requests/{rid}")
    assert r.status_code == 422


def test_manual_request_entry_with_receipt_date(env, admin):
    r = apost(env, "/admin/requests/new", {"kind": "access", "name": "Пётр", "contact": "petr@example.test",
                                           "body": "Пришло письмо", "channel": "email", "received_on": "2026-10-01"},
              page="/admin/requests")
    assert r.status_code == 303
    row = one(env, "SELECT * FROM legal_requests")
    assert row["channel"] == "email" and row["created_at"].startswith("2026-10-01") and row["due_at"].startswith("2026-10-15")


def test_manual_request_bad_date(env, admin):
    r = apost(env, "/admin/requests/new", {"kind": "access", "contact": "a@b.ru", "body": "x", "received_on": "вчера"}, page="/admin/requests")
    assert r.status_code == 422


def test_csv_export(env, admin):
    public_request(env)
    r = env.client.get("/admin/requests.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"] and "ivan@example.test" in r.text


# ------------------------------------------------------------------ журнал и сводка

def test_audit_log_records_actor_and_actions(env, admin):
    vid = submit_vacancy(env)
    apost(env, f"/admin/vacancies/{vid}/approve")
    apost(env, "/admin/stoplist/add", {"kind": "keyword", "value": "закладки"})
    html = env.client.get("/admin/audit").text
    assert "admin@example.test" in html and "vacancy.approve" in html and "stoplist.add" in html
    assert "действие" in html.lower()


def test_audit_filter_and_pagination(env, admin):
    for i in range(3):
        apost(env, "/admin/stoplist/add", {"kind": "keyword", "value": f"слово{i}"})
    html = env.client.get("/admin/audit?action=stoplist.&page=99").text
    assert "stoplist.add" in html


def test_dashboard_counters(env, admin):
    submit_vacancy(env)
    vid = add_vacancy(env)
    complain(env, vid)
    public_request(env)
    html = env.client.get("/admin").text
    for label in ("Активных вакансий", "На проверке", "Жалобы", "Запросы", "Источники"):
        assert label in html
    data = re.findall(r'data-counter="(\w+)">(\d+)<', html)
    counters = dict(data)
    assert counters["pending"] == "1" and counters["complaints"] == "1" and counters["requests"] == "1"
    assert counters["active"] == "1"


def test_dashboard_survives_health_failure(env, admin):
    def boom(conn):
        raise RuntimeError("нет настроек")

    env.app.state.admin.health = boom
    r = env.client.get("/admin")
    assert r.status_code == 200 and "нет настроек" in r.text


def test_dashboard_lists_source_problems_and_errors(env, admin):
    from types import SimpleNamespace

    src = SimpleNamespace(key="vk:x", label="VK x", status="stale", age_seconds=9999.0, last_error="timeout", problem=True)
    env.app.state.admin.health = lambda conn: SimpleNamespace(sources=[src], worker_status="ok", worker_age_seconds=5.0, ok=False)
    conn = env.db()
    conn.execute("INSERT INTO ingest_state(source, updated_at, last_error) VALUES ('vk:x','2026-10-29T10:00:00+00:00','HTTP 500')")
    conn.commit()
    conn.close()
    html = env.client.get("/admin").text
    assert "VK x" in html and "HTTP 500" in html


def test_csrf_token_page_has_logout_form(env, admin):
    assert csrf_of(env, "/admin")
