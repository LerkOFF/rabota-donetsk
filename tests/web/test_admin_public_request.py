"""Публичная форма обращения /legal/request: капча, ловушка, лимит, проверка полей."""
import re

import pytest

from rabota.web import admin_routes
from tests.web.admin_harness import make_env, one, rows

BASE = {"kind": "access", "name": "Иван", "contact": "ivan@example.test", "subject": "",
        "body": "Хочу получить копию своих данных."}


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e


def form_tokens(env):
    html = env.client.get("/legal/request").text
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    cap = re.search(r'name="captcha_token" value="([^"]+)"', html).group(1)
    q = re.search(r"Сколько будет (\d+) \+ (\d+)\?", html)
    return token, cap, int(q.group(1)) + int(q.group(2))


def send(env, answer=None, **over):
    token, cap, total = form_tokens(env)
    data = {"csrf_token": token, "captcha_token": cap, "captcha_answer": str(total if answer is None else answer), **BASE, **over}
    return env.client.post("/legal/request", data=data, follow_redirects=False)


def test_form_renders_all_states(env):
    html = env.client.get("/legal/request").text
    assert "Запрос по персональным данным" in html
    assert 'name="website"' in html and 'name="captcha_answer"' in html
    for kind_label in ("Доступ к своим данным", "Удаление данных", "Убрать мою вакансию"):
        assert kind_label in html
    assert 'rel="canonical" href="https://example.test/legal/request"' in html
    assert "<h1" in html and "срок" in html.lower()


def test_sent_page(env):
    r = send(env)
    assert r.status_code == 303
    page = env.client.get(r.headers["location"])
    assert page.status_code == 200 and "принято" in page.text.lower()
    row = one(env, "SELECT * FROM legal_requests")
    assert str(row["id"]) in page.text and row["requester_key"] and "10." not in row["requester_key"]


def test_wrong_captcha(env):
    r = send(env, answer=999)
    assert r.status_code == 422 and ("сумм" in r.text.lower() or "ответ" in r.text.lower())
    assert rows(env, "SELECT * FROM legal_requests") == []
    assert "Иван" in r.text  # введённое не теряется


def test_tampered_or_expired_captcha(env):
    token, cap, total = form_tokens(env)
    a, b, ts, sig = cap.split(".")
    forged = f"{int(a) + 1}.{b}.{ts}.{sig}"
    r = env.client.post("/legal/request", data={"csrf_token": token, "captcha_token": forged, "captcha_answer": str(total + 1), **BASE})
    assert r.status_code == 422
    from datetime import timedelta

    base_clock = env.app.state.admin.clock
    env.app.state.admin.clock = lambda: base_clock() + timedelta(hours=3)
    r = env.client.post("/legal/request", data={"csrf_token": token, "captcha_token": cap, "captcha_answer": str(total), **BASE})
    assert r.status_code == 422 and rows(env, "SELECT * FROM legal_requests") == []


def test_honeypot_silently_accepted_but_not_saved(env):
    r = send(env, website="http://spam.example")
    assert r.status_code == 303 and rows(env, "SELECT * FROM legal_requests") == []


def test_validation_errors(env):
    r = send(env, contact="", body="", kind="zzz")
    assert r.status_code == 422
    assert r.text.count("field--invalid") >= 2 and rows(env, "SELECT * FROM legal_requests") == []


def test_contact_must_look_like_email_or_phone(env):
    assert send(env, contact="просто текст").status_code == 422
    assert send(env, contact="+7 949 123-45-67").status_code == 303


def test_long_text_is_cut_not_crash(env):
    assert send(env, body="а" * 10_000).status_code in (303, 413, 422)


def test_rate_limit_per_ip(env):
    env.app.state.admin.limiter.limits["legal_request"] = (2, 3600)
    codes = [send(env).status_code for _ in range(4)]
    assert codes == [303, 303, 429, 429]


def test_csrf_required(env):
    _, cap, total = form_tokens(env)
    r = env.client.post("/legal/request", data={"captcha_token": cap, "captcha_answer": str(total), **BASE})
    assert r.status_code == 403


def test_captcha_helpers_roundtrip():
    secret = b"s" * 32
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    q = admin_routes.make_captcha(secret, now)
    assert admin_routes.check_captcha(secret, q["token"], str(q["a"] + q["b"]), now + timedelta(minutes=5))
    assert not admin_routes.check_captcha(secret, q["token"], str(q["a"] + q["b"] + 1), now)
    assert not admin_routes.check_captcha(secret, q["token"], str(q["a"] + q["b"]), now + timedelta(hours=2))
    assert not admin_routes.check_captcha(b"x" * 32, q["token"], str(q["a"] + q["b"]), now)
    assert not admin_routes.check_captcha(secret, "мусор", "5", now)
    assert not admin_routes.check_captcha(secret, q["token"], "abc", now)
