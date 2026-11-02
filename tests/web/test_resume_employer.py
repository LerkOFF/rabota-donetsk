"""Поиск резюме для работодателя: доступ, фильтры, видимость, карточка, контакты по кнопке, приглашение."""
import re

import pytest

from tests.accounts.harness import env_impl, login_as
from tests.messaging.webkit import make_world, new_vacancy, second_visitor, submit
from tests.web.resume_kit import create_id, employer_visitor

env = pytest.fixture(env_impl)


@pytest.fixture
def world(env):
    """Соискатель с открытым резюме (env) и работодатель с опубликованной вакансией."""
    w = make_world(env)
    rid = create_id(w.seeker)
    return w, rid


def test_search_is_closed_for_guests_and_seekers(env):
    r = env.client.get("/employers/resumes", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login") and "noindex" in r.headers["x-robots-tag"]
    assert env.client.get("/employers/resumes/1", follow_redirects=False).status_code == 303
    login_as(env, "anna@example.test", role="job_seeker")
    for path in ("/employers/resumes", "/employers/resumes/1"):
        assert env.client.get(path).status_code == 403, path
    assert submit(env, "/employers/resumes/1/contact").status_code == 403
    assert submit(env, "/employers/resumes/1/invite", {"vacancy_id": "1", "text": "x"}).status_code == 403


def test_no_public_resume_pages_and_robots_closed(env):
    assert "Disallow: /employers/resumes" in env.client.get("/robots.txt").text
    sitemap = env.client.get("/sitemap.xml").text
    assert "/employers/resumes" not in sitemap and "/cabinet/resumes" not in sitemap
    for path in ("/resume/1", "/resumes", "/resumes/1", "/cabinet/resumes/1"):
        assert env.client.get(path, follow_redirects=False).status_code in (303, 404, 405), path


def test_search_page_for_employer(world):
    w, rid = world
    r = w.boss.client.get("/employers/resumes")
    assert r.status_code == 200
    for needle in ("Поиск резюме", "Бариста", "Мария П.", "от 30", "Стаж 3", 'name="q"', 'name="district"', 'name="pay_to"',
                   'name="exp"', 'name="employment"', 'name="format"', f'href="/employers/resumes/{rid}"', "Найдено: 1"):
        assert needle in r.text, needle
    assert "Петрова" not in r.text and "949" not in r.text and "maria_p" not in r.text  # ни фамилии, ни контактов
    assert re.search(r'<meta name="robots" content="noindex', r.text) and "noindex" in r.headers["x-robots-tag"]
    assert r.headers["cache-control"] == "no-store" and r.text.count("<h1") == 1


def test_search_empty_states(env):
    boss, _ = employer_visitor(env)
    assert "Открытых резюме пока нет" in boss.client.get("/employers/resumes").text
    seeker = second_visitor(env)
    login_as(seeker, "s@example.test")
    create_id(seeker)
    page = boss.client.get("/employers/resumes?q=космонавт").text
    assert "Подходящих резюме нет" in page and "Сбросить фильтры" in page and "Найдено: 0" in page


def test_search_filters_work_through_the_url(world):
    w, _rid = world
    get = lambda q: w.boss.client.get("/employers/resumes?" + q).text
    assert "Бариста" in get("q=бариста") and "Найдено: 0" in get("q=кассир")
    assert "Найдено: 1" in get("district=Ворошиловский") and "Найдено: 0" in get("district=Калининский")
    assert "Найдено: 1" in get("pay_to=40000&period=месяц") and "Найдено: 0" in get("pay_to=20000&period=месяц")
    assert "Найдено: 1" in get("exp=3") and "Найдено: 0" in get("exp=5")
    assert "Найдено: 1" in get("employment=постоянно") and "Найдено: 0" in get("employment=подработка")
    assert "Найдено: 1" in get("format=точка") and "Найдено: 0" in get("format=вахта")
    assert w.boss.client.get("/employers/resumes?exp=zzz&sort=bad&page=abc").status_code == 200
    assert "Сбросить" in get("q=бариста")


def test_only_public_resumes_are_listed_and_openable(world):
    w, rid = world
    conn = w.seeker.db()
    conn.execute("UPDATE resumes SET visibility='open'")
    conn.commit()
    conn.close()
    assert "Найдено: 0" in w.boss.client.get("/employers/resumes").text
    assert w.boss.client.get(f"/employers/resumes/{rid}").status_code == 404
    submit(w.seeker, f"/cabinet/resumes/{rid}/hide")
    assert w.boss.client.get(f"/employers/resumes/{rid}").status_code == 404


def test_resume_by_application_is_visible_to_that_employer_only(world):
    w, rid = world
    conn = w.seeker.db()
    conn.execute("UPDATE resumes SET visibility='open'")
    conn.commit()
    conn.close()
    r = submit(w.seeker, f"/vacancy/{w.vacancy}/apply", {"cover": "Здравствуйте", "resume_id": str(rid)})
    assert r.status_code == 303
    card = w.boss.client.get(f"/employers/resumes/{rid}")
    assert card.status_code == 200 and "приложил это резюме к отклику" in card.text
    other, _ = employer_visitor(env=w.seeker, email="boss2@example.test", name="Другой")
    assert other.client.get(f"/employers/resumes/{rid}").status_code == 404


def test_admin_hidden_blocked_and_blocking_seeker(world):
    w, rid = world
    conn = w.seeker.db()
    conn.execute("UPDATE resumes SET admin_hidden=1")
    conn.commit()
    assert w.boss.client.get(f"/employers/resumes/{rid}").status_code == 404
    conn.execute("UPDATE resumes SET admin_hidden=0")
    conn.execute("INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES (?,?)", (w.seeker_id, w.boss_id))
    conn.commit()
    conn.close()
    assert w.boss.client.get(f"/employers/resumes/{rid}").status_code == 404
    assert "Найдено: 0" in w.boss.client.get("/employers/resumes").text


def test_card_hides_contacts_until_button(world):
    w, rid = world
    r = w.boss.client.get(f"/employers/resumes/{rid}")
    assert r.status_code == 200
    for needle in ("Бариста", "Мария П.", "Кофейня Зерно", "кофе", "касса", "Показать контакты", f'action="/employers/resumes/{rid}/contact"',
                   "Пригласить", f'action="/employers/resumes/{rid}/invite"', 'name="vacancy_id"', "Работаю быстро"):
        assert needle in r.text, needle
    for secret in ("Петрова", "+7 949 111-22-33", "+79491112233", "maria_p", "seeker@example.test"):
        assert secret not in r.text, secret
    assert re.search(r'<meta name="robots" content="noindex', r.text) and "noindex" in r.headers["x-robots-tag"]
    assert r.text.count("<h1") == 1 and "<title>" in r.text


def test_contact_opens_only_by_button_and_is_counted(world):
    w, rid = world
    w.boss.client.get(f"/employers/resumes/{rid}")
    assert w.seeker.query("SELECT COUNT(*) AS n FROM resume_views WHERE kind='contact'")[0]["n"] == 0
    r = submit(w.boss, f"/employers/resumes/{rid}/contact")
    assert r.status_code == 303 and r.headers["location"] == f"/employers/resumes/{rid}#contacts"
    page = w.boss.client.get(f"/employers/resumes/{rid}").text
    for needle in ("Мария Петрова", "+7 949 111-22-33", 'href="tel:+79491112233"', "@maria_p", "t.me/maria_p", "Контакты открыты вами"):
        assert needle in page, needle
    assert "Показать контакты" not in page and "seeker@example.test" not in page
    submit(w.boss, f"/employers/resumes/{rid}/contact")  # повтор бесплатен и не двоится
    assert w.seeker.query("SELECT COUNT(*) AS n FROM resume_views WHERE kind='contact'")[0]["n"] == 1
    assert w.seeker.query("SELECT COUNT(*) AS n FROM resume_views WHERE kind='view'")[0]["n"] == 1
    mine = w.seeker.client.get("/cabinet/resumes").text
    assert "Просмотров: 1" in mine and "Открыли контакты: 1" in mine


def test_contact_needs_csrf_and_other_employer_sees_no_contacts(world):
    w, rid = world
    r = w.boss.client.post(f"/employers/resumes/{rid}/contact", headers={"origin": "http://testserver"})
    assert r.status_code == 403
    assert w.seeker.query("SELECT COUNT(*) AS n FROM resume_views WHERE kind='contact'")[0]["n"] == 0
    submit(w.boss, f"/employers/resumes/{rid}/contact")
    other, _ = employer_visitor(w.seeker, "boss2@example.test", "Другой")
    assert "+7 949 111-22-33" not in other.client.get(f"/employers/resumes/{rid}").text


def test_resume_without_contacts_says_so(env):
    boss, _ = employer_visitor(env)
    s = second_visitor(env)
    login_as(s, "s@example.test", name="Ольга Смирнова")
    rid = create_id(s, contact_phone="", contact_telegram="")
    submit(boss, f"/employers/resumes/{rid}/contact")
    assert "не оставил телефон и Telegram" in boss.client.get(f"/employers/resumes/{rid}").text


def test_contact_daily_limit_shows_message(env, monkeypatch):
    from rabota.resumes import rules

    monkeypatch.setattr(rules, "MAX_CONTACTS_PER_DAY", 1)
    boss, _ = employer_visitor(env)
    ids = []
    for i in range(2):
        s = second_visitor(env)
        login_as(s, f"s{i}@example.test", name="Ольга Смирнова")
        ids.append(create_id(s))
    assert submit(boss, f"/employers/resumes/{ids[0]}/contact").status_code == 303
    r = submit(boss, f"/employers/resumes/{ids[1]}/contact")
    assert r.status_code == 429 and "не больше 1 раз" in r.text
    assert "Показать контакты" in r.text


# ------------------------------------------------------------------ приглашение

def test_invite_creates_a_conversation_and_notifies(world):
    w, rid = world
    page = w.boss.client.get(f"/employers/resumes/{rid}").text
    assert "Приглашаем вас обсудить вакансию «Бариста»" in page
    text = "Здравствуйте, Мария! Приглашаем вас на собеседование завтра в 10:00."
    r = submit(w.boss, f"/employers/resumes/{rid}/invite", {"vacancy_id": str(w.vacancy), "text": text})
    assert r.status_code == 303 and r.headers["location"].startswith("/messages/")
    conv = w.seeker.query("SELECT * FROM conversations")[0]
    assert (conv["employer_user_id"], conv["seeker_user_id"], conv["application_id"], conv["vacancy_id"]) == (w.boss_id, w.seeker_id, None, w.vacancy)
    thread = w.seeker.client.get(f"/messages/{conv['id']}")
    assert thread.status_code == 200 and "собеседование" in thread.text
    inbox = w.seeker.client.get("/messages")
    assert "Бариста" in inbox.text
    mails = [m for m in w.seeker.mailer.outbox if m.to == "seeker@example.test"]
    assert mails and "собеседование" not in mails[-1].body and "seeker@example.test" not in mails[-1].body
    assert w.seeker.client.post(f"/messages/{conv['id']}", data={}).status_code in (403, 404)


def test_seeker_can_answer_an_invitation(world):
    w, rid = world
    submit(w.boss, f"/employers/resumes/{rid}/invite", {"vacancy_id": str(w.vacancy), "text": "Приглашаем на собеседование."})
    conv = w.seeker.query("SELECT id FROM conversations")[0]["id"]
    r = submit(w.seeker, f"/messages/{conv}", {"body": "Спасибо, приду"})
    assert r.status_code == 303
    assert w.seeker.query("SELECT COUNT(*) AS n FROM messages")[0]["n"] == 2


def test_second_invite_goes_to_existing_thread(world):
    w, rid = world
    submit(w.boss, f"/employers/resumes/{rid}/invite", {"vacancy_id": str(w.vacancy), "text": "Приглашаем на собеседование."})
    r = submit(w.boss, f"/employers/resumes/{rid}/invite", {"vacancy_id": str(w.vacancy), "text": "Ещё раз приглашаем."})
    assert r.status_code == 303 and r.headers["location"].startswith("/messages/")
    assert w.seeker.query("SELECT COUNT(*) AS n FROM conversations")[0]["n"] == 1
    assert w.seeker.query("SELECT COUNT(*) AS n FROM messages")[0]["n"] == 1


def test_invite_validation_keeps_text(world):
    w, rid = world
    r = submit(w.boss, f"/employers/resumes/{rid}/invite", {"vacancy_id": str(w.vacancy), "text": "   "})
    assert r.status_code == 422 and 'role="alert"' in r.text and "Напишите сообщение" in r.text
    r = submit(w.boss, f"/employers/resumes/{rid}/invite", {"vacancy_id": "999", "text": "Приглашаем"})
    assert r.status_code == 422 and "своих опубликованных вакансий" in r.text and "Приглашаем" in r.text
    assert w.seeker.query("SELECT COUNT(*) AS n FROM conversations")[0]["n"] == 0


def test_invite_only_on_own_vacancy(world):
    w, rid = world
    _other, other_id = employer_visitor(w.seeker, "boss2@example.test", "Другой")
    foreign = new_vacancy(w.seeker, other_id)
    r = submit(w.boss, f"/employers/resumes/{rid}/invite", {"vacancy_id": str(foreign), "text": "Приглашаем"})
    assert r.status_code == 422
    assert w.seeker.query("SELECT COUNT(*) AS n FROM conversations")[0]["n"] == 0


def test_invite_without_vacancies_points_to_posting(env):
    boss, _ = employer_visitor(env)
    s = second_visitor(env)
    login_as(s, "s@example.test", name="Ольга Смирнова")
    rid = create_id(s)
    page = boss.client.get(f"/employers/resumes/{rid}").text
    assert "Приглашать можно на свою опубликованную вакансию" in page and 'href="/razmestit"' in page
    assert f'action="/employers/resumes/{rid}/invite"' not in page


def test_invite_to_hidden_resume_and_blocked_are_404(world):
    w, rid = world
    submit(w.seeker, f"/cabinet/resumes/{rid}/hide")
    r = submit(w.boss, f"/employers/resumes/{rid}/invite", {"vacancy_id": str(w.vacancy), "text": "Приглашаем"})
    assert r.status_code == 404


def test_search_rate_limit(tmp_path):
    from tests.accounts.harness import build_env

    e = build_env(tmp_path, rate_limits={"resume_search": (2, 3600)})
    with e.client:
        login_as(e, "b@example.test", role="employer")
        assert e.client.get("/employers/resumes").status_code == 200
        assert e.client.get("/employers/resumes").status_code == 200
        assert e.client.get("/employers/resumes").status_code == 429
