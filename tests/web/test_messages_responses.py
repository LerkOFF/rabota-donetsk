"""Страницы откликов: список, карточка, статусы, отзыв, кабинет."""
import pytest

from tests.accounts.harness import build_env, env_impl
from tests.messaging.webkit import apply_as_seeker, make_world, new_vacancy, second_visitor, stranger, submit

env = pytest.fixture(env_impl)


def test_pages_need_login_and_verified_role(env):
    for path in ("/responses", "/responses/1", "/messages", "/messages/1"):
        r = env.client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"/login?next={path}", path


def test_empty_states(env):
    w = make_world(env)
    seeker_page = w.seeker.client.get("/responses")
    assert seeker_page.status_code == 200 and "Вы пока не откликались" in seeker_page.text
    assert "/vacancies?direct=1" in seeker_page.text
    boss_page = w.boss.client.get("/responses")
    assert "Откликов пока нет" in boss_page.text


def test_seeker_list_and_card(env):
    w = make_world(env)
    rid = apply_as_seeker(w)
    page = w.seeker.client.get("/responses")
    assert f'data-response-id="{rid}"' in page.text and "Бариста" in page.text and "Отправлен" in page.text
    assert 'name="robots" content="noindex' in page.text and page.headers["cache-control"] == "no-store"
    card = w.seeker.client.get(f"/responses/{rid}")
    assert card.status_code == 200
    for needle in ("Здравствуйте, хочу работать", "Отозвать отклик", f'action="/responses/{rid}/withdraw"', "Кофейня Зёрна"):
        assert needle in card.text, needle
    assert 'action="/responses/' + str(rid) + '/status"' not in card.text  # статус меняет только работодатель


def test_employer_list_card_and_auto_viewed(env):
    w = make_world(env)
    rid = apply_as_seeker(w)
    page = w.boss.client.get("/responses")
    assert "Мария Петрова" in page.text and "Новый" in page.text
    assert w.boss.client.get(f"/responses/{rid}").status_code == 200
    card = w.boss.client.get(f"/responses/{rid}")
    for needle in ("Мария Петрова", "+7 949 111-22-33", "tel:+79491112233", "Здравствуйте, хочу работать",
                   f'action="/responses/{rid}/status"', 'value="invited"', 'value="rejected"', "Написать"):
        assert needle in card.text, needle
    assert "seeker@example.test" not in card.text  # почта соискателя работодателю не показывается
    assert env.query("SELECT status FROM applications")[0]["status"] == "viewed"


def test_stranger_gets_404_not_403(env):
    w = make_world(env)
    rid = apply_as_seeker(w)
    for visitor in (stranger(env, "s2@example.test"), stranger(env, "e2@example.test", "employer")):
        assert visitor.client.get(f"/responses/{rid}").status_code == 404
        assert submit(visitor, f"/responses/{rid}/status", {"status": "invited"}).status_code == 404
        assert submit(visitor, f"/responses/{rid}/withdraw").status_code == 404
    assert env.query("SELECT status, withdrawn_at FROM applications")[0]["status"] == "new"
    assert w.seeker.client.get("/responses/99999").status_code == 404


def test_other_employers_see_only_their_own_responses(env):
    w = make_world(env)
    apply_as_seeker(w)
    other_boss = stranger(env, "boss2@example.test", "employer")
    page = other_boss.client.get("/responses")
    assert "Мария Петрова" not in page.text and "Откликов пока нет" in page.text


def test_status_change_by_employer_with_csrf(env):
    w = make_world(env)
    rid = apply_as_seeker(w)
    r = submit(w.boss, f"/responses/{rid}/status", {"status": "invited"})
    assert r.status_code == 303 and r.headers["location"] == f"/responses/{rid}"
    assert env.query("SELECT status FROM applications")[0]["status"] == "invited"
    assert "Приглашён" in w.seeker.client.get(f"/responses/{rid}").text
    no_csrf = w.boss.client.post(f"/responses/{rid}/status", data={"status": "rejected"})
    assert no_csrf.status_code == 403
    assert env.query("SELECT status FROM applications")[0]["status"] == "invited"


def test_seeker_cannot_change_status(env):
    w = make_world(env)
    rid = apply_as_seeker(w)
    assert submit(w.seeker, f"/responses/{rid}/status", {"status": "invited"}).status_code in (403, 404)
    assert env.query("SELECT status FROM applications")[0]["status"] == "new"


def test_bad_status_is_rejected(env):
    w = make_world(env)
    rid = apply_as_seeker(w)
    r = submit(w.boss, f"/responses/{rid}/status", {"status": "new"})
    assert r.status_code == 422
    assert env.query("SELECT status FROM applications")[0]["status"] == "new"


def test_filters_and_no_results_state(env):
    w = make_world(env)
    rid = apply_as_seeker(w)
    submit(w.boss, f"/responses/{rid}/status", {"status": "invited"})
    assert "Мария Петрова" in w.boss.client.get("/responses?status=invited").text
    empty = w.boss.client.get("/responses?status=rejected")
    assert "Мария Петрова" not in empty.text and "Нет откликов с таким статусом" in empty.text
    assert "Мария Петрова" in w.boss.client.get(f"/responses?vacancy={w.vacancy}").text
    other_vid = new_vacancy(env, w.boss_id, profession="повар")
    assert "Мария Петрова" not in w.boss.client.get(f"/responses?vacancy={other_vid}").text
    assert w.boss.client.get("/responses?status=%27%3Bdrop&page=abc&vacancy=x").status_code == 200


def test_pagination_of_long_lists(tmp_path):
    env = build_env(tmp_path, rate_limits={"apply_user": (100, 3600), "apply_ip": (100, 3600)})
    with env.client:
        w = make_world(env)
        for i in range(23):
            v = new_vacancy(env, w.boss_id, profession=f"профессия{i}")
            assert submit(w.seeker, f"/vacancy/{v}/apply", {"cover": ""}).status_code == 303
        page1 = w.seeker.client.get("/responses")
        assert 'rel="next"' in page1.text and "Страница 1 из 2" in page1.text
        page2 = w.seeker.client.get("/responses?page=2")
        assert "Страница 2 из 2" in page2.text and 'rel="prev"' in page2.text
        assert w.seeker.client.get("/responses?page=99").status_code == 200


def test_withdraw_flow(env):
    w = make_world(env)
    rid = apply_as_seeker(w)
    r = submit(w.seeker, f"/responses/{rid}/withdraw")
    assert r.status_code == 303 and r.headers["location"] == "/responses?withdrawn=1"
    assert "Отклик отозван" in w.seeker.client.get("/responses?withdrawn=1").text
    card = w.boss.client.get(f"/responses/{rid}")
    assert "Отозван" in card.text and f'action="/responses/{rid}/status"' not in card.text
    assert submit(w.boss, f"/responses/{rid}/status", {"status": "invited"}).status_code == 409
    assert w.seeker.client.get(f"/vacancy/{w.vacancy}/apply").status_code == 200
    assert "Вы уже откликались на эту вакансию" in w.seeker.client.get(f"/vacancy/{w.vacancy}/apply").text


def test_card_hides_phone_when_seeker_did_not_share_it(env):
    w = make_world(env)
    r = submit(w.seeker, f"/vacancy/{w.vacancy}/apply", {"cover": "Привет"})
    rid = int(r.headers["location"].split("/responses/")[1].split("?")[0])
    card = w.boss.client.get(f"/responses/{rid}")
    assert "+7 949 111-22-33" not in card.text and "не показал телефон" in card.text.lower()


def test_cover_letter_is_escaped(env):
    w = make_world(env)
    rid = apply_as_seeker(w, "<script>alert(1)</script> & привет")
    html = w.boss.client.get(f"/responses/{rid}").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_legacy_cabinet_addresses_redirect(env):
    w = make_world(env)
    assert w.seeker.client.get("/cabinet/responses", follow_redirects=False).headers["location"] == "/responses"
    assert w.seeker.client.get("/cabinet/messages", follow_redirects=False).headers["location"] == "/messages"


def test_cabinets_show_counters_and_links(env):
    w = make_world(env)
    rid = apply_as_seeker(w)
    boss_cab = w.boss.client.get("/cabinet/employer").text
    assert 'data-new-responses="1"' in boss_cab and 'href="/responses"' in boss_cab and "Мария Петрова" in boss_cab
    seeker_cab = w.seeker.client.get("/cabinet/seeker").text
    assert 'data-my-responses="1"' in seeker_cab and 'href="/messages"' in seeker_cab
    w.boss.client.get(f"/responses/{rid}")
    assert 'data-new-responses="0"' in w.boss.client.get("/cabinet/employer").text
    submit(w.boss, f"/responses/{rid}/status", {"status": "invited"})
    tid = env.query("SELECT id FROM conversations")[0]["id"]
    submit(w.boss, f"/messages/{tid}", {"body": "Приходите завтра"})
    assert 'data-unread="1"' in w.seeker.client.get("/cabinet/seeker").text


def test_admin_has_no_access(env):
    from rabota.accounts import service
    from tests.accounts.harness import PASSWORD, login

    conn = env.db()
    service.create_admin(conn, "admin@example.test", PASSWORD)
    conn.close()
    visitor = second_visitor(env)
    assert login(visitor, "admin@example.test").status_code == 303
    assert visitor.client.get("/responses").status_code == 403
