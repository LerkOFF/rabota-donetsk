"""Страницы подписок: подписка гостя и вошедшего, подтверждение, список, отписка по ссылке, кнопка в выдаче, CSRF и лимиты."""
import re
from datetime import timedelta
from urllib.parse import quote

import pytest

from rabota.alerts import service
from tests.accounts.harness import SITE, build_env, csrf_of, login, make_user, post
from tests.alerts.helpers import vac

QS = "q=" + quote("бариста") + "&city=" + quote("Донецк")
FIELDS = {"q": "бариста", "city": "Донецк"}


@pytest.fixture
def env(tmp_path):
    e = build_env(tmp_path)
    with e.client:
        yield e


def subs(env):
    return env.query("SELECT * FROM alert_subscriptions ORDER BY id")


def code_from(mail) -> str:
    m = re.search(r"^(\d{6})$", mail.body, re.MULTILINE)
    assert m, mail.body
    return m.group(1)


def guest_subscribe(env, email="ivan@example.test", frequency="daily", **extra):
    data = {**FIELDS, "email": email, "channel": "email", "frequency": frequency, "consent": "1", **extra}
    return post(env, "/alerts/new", data, page="/alerts/new?" + QS)


def logged_in(env, email="anna@example.test"):
    uid = make_user(env, email)
    assert login(env, email).status_code == 303
    return uid


# ------------------------------------------------------------------ гость

def test_new_page_shows_the_search_and_is_closed_from_indexing(env):
    r = env.client.get("/alerts/new?" + QS)
    assert r.status_code == 200
    assert "бариста в донецке" in r.text.lower() and 'name="email"' in r.text and 'name="consent"' in r.text
    assert 'name="q" value="бариста"' in r.text
    assert "noindex" in r.headers["x-robots-tag"] and 'content="noindex' in r.text
    assert r.text.count("<h1") == 1


def test_new_page_without_search_asks_to_set_one(env):
    r = env.client.get("/alerts/new?sort=pay")
    assert r.status_code == 200 and "Сначала задайте поиск" in r.text and "<form class=\"auth__form\"" not in r.text
    r = guest_subscribe_empty(env)
    assert r.status_code == 422 and not subs(env)


def guest_subscribe_empty(env):
    return post(env, "/alerts/new", {"email": "ivan@example.test", "channel": "email", "frequency": "daily", "consent": "1"},
                page="/alerts/new?" + QS)


def test_guest_subscribes_with_code_from_the_mail(env):
    r = guest_subscribe(env, frequency="instant")
    assert r.status_code == 303 and r.headers["location"].startswith("/alerts/code/")
    row = subs(env)[0]
    assert row["status"] == "pending" and row["email"] == "ivan@example.test" and row["frequency"] == "instant"
    mail = env.mailer.last("ivan@example.test")
    assert mail and "Код" in mail.subject and "/alerts/confirm/" in mail.body
    assert "List-Unsubscribe" not in dict(mail.headers)  # это письмо по запросу человека, не рассылка
    page = env.client.get(r.headers["location"])
    assert page.status_code == 200 and "Введите код" in page.text
    done = post(env, r.headers["location"], {"code": code_from(mail)}, page=r.headers["location"])
    assert done.status_code == 200 and "Подписка включена" in done.text
    assert subs(env)[0]["status"] == "active"


def test_wrong_code_is_refused_and_attempts_are_limited(env):
    r = guest_subscribe(env)
    where = r.headers["location"]
    wrong = "000000" if code_from(env.mailer.last()) != "000000" else "111111"
    bad = post(env, where, {"code": wrong}, page=where)
    assert bad.status_code == 422 and "Код не подошёл" in bad.text and subs(env)[0]["status"] == "pending"
    for _ in range(service.CODE_ATTEMPTS):
        last = post(env, where, {"code": wrong}, page=where)
    assert last.status_code == 422 and subs(env)[0]["status"] == "pending"
    good = post(env, where, {"code": code_from(env.mailer.last())}, page=where)
    assert good.status_code == 422 and subs(env)[0]["status"] == "pending"  # код сгорел после блокировки


def test_resend_code_respects_cooldown(env):
    where = guest_subscribe(env).headers["location"]
    assert len(env.mailer.outbox) == 1
    post(env, where + "/resend", {}, page=where)
    assert len(env.mailer.outbox) == 1  # прошло меньше минуты
    env.clock.advance(seconds=61)
    r = post(env, where + "/resend", {}, page=where)
    assert r.status_code == 303 and len(env.mailer.outbox) == 2
    assert post(env, where, {"code": code_from(env.mailer.last())}, page=where).status_code == 200


def test_confirm_by_link_needs_a_click_not_just_a_visit(env):
    guest_subscribe(env)
    path = re.search(r"https://example\.test(/alerts/confirm/\S+)", env.mailer.last().body).group(1)
    page = env.client.get(path)
    assert page.status_code == 200 and "Подтвердить подписку" in page.text
    assert subs(env)[0]["status"] == "pending"  # переход по ссылке сам ничего не меняет (почтовые сканеры)
    done = post(env, path, {}, page=path)
    assert done.status_code == 200 and subs(env)[0]["status"] == "active"
    assert env.client.get("/alerts/confirm/1.deadbeef").status_code == 400


def test_consent_and_email_are_required(env):
    r = guest_subscribe(env, consent="")
    assert r.status_code == 422 and "согласие" in r.text.lower() and not subs(env) and not env.mailer.outbox
    r = guest_subscribe(env, email="not-an-email")
    assert r.status_code == 422 and "адрес почты" in r.text and not subs(env)


def test_unknown_channel_or_frequency_is_refused(env):
    assert guest_subscribe(env, channel="sms").status_code == 422
    assert guest_subscribe(env, frequency="hourly").status_code == 422
    assert not subs(env)


def test_guest_subscription_to_known_email_looks_the_same(env):
    first = guest_subscribe(env)
    post(env, first.headers["location"], {"code": code_from(env.mailer.last())}, page=first.headers["location"])
    sent = len(env.mailer.outbox)
    again = guest_subscribe(env)
    assert again.status_code == 303 and again.headers["location"].startswith("/alerts/code/")
    assert len(env.mailer.outbox) == sent and len(subs(env)) == 1  # письма нет, дубля нет, ответ тот же


def test_mail_sending_failure_does_not_break_the_page(tmp_path):
    from rabota.accounts.mailer import MemoryMailer

    e = build_env(tmp_path, mailer=MemoryMailer(fail=True))
    with e.client:
        r = guest_subscribe(e)
        assert r.status_code == 303 and subs(e)[0]["status"] == "pending"


def test_per_email_and_per_ip_limits(tmp_path):
    e = build_env(tmp_path, rate_limits={"alert_new_email": (2, 3600), "alert_new_ip": (5, 3600)})
    with e.client:
        for _ in range(2):
            assert guest_subscribe(e, q="бариста").status_code == 303
        assert guest_subscribe(e).status_code == 429  # третья заявка на ту же почту за час
        assert guest_subscribe(e, email="other@example.test").status_code == 303
        assert guest_subscribe(e, email="third@example.test").status_code == 303
        assert guest_subscribe(e, email="fourth@example.test").status_code == 429  # лимит по адресу посетителя


def test_post_without_csrf_is_forbidden(env):
    r = env.client.post("/alerts/new", data={**FIELDS, "email": "ivan@example.test", "channel": "email", "consent": "1"})
    assert r.status_code == 403 and not subs(env)
    r = env.client.post("/alerts/new", data={"csrf_token": csrf_of(env, "/alerts/new?" + QS), **FIELDS}, headers={"origin": "https://evil.test"})
    assert r.status_code == 403


# ------------------------------------------------------------------ вошедший

def test_logged_in_user_subscribes_at_once(env):
    logged_in(env)
    r = post(env, "/alerts/new", {**FIELDS, "channel": "email", "frequency": "daily"}, page="/alerts/new?" + QS)
    assert r.status_code == 303 and r.headers["location"] == "/alerts?m=added"
    assert subs(env)[0]["status"] == "active" and subs(env)[0]["user_id"] and not env.mailer.outbox
    page = env.client.get("/alerts?m=added")
    assert "Бариста в Донецке" in page.text and "Подписка включена" in page.text and "раз в день" in page.text
    again = post(env, "/alerts/new", {**FIELDS, "channel": "email", "frequency": "instant"}, page="/alerts/new?" + QS)
    assert again.headers["location"] == "/alerts?m=exists" and len(subs(env)) == 1


def test_logged_in_user_limit(env):
    logged_in(env)
    conn = env.db()
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('alerts_max_per_user', '1', 'x')")
    conn.commit()
    conn.close()
    assert post(env, "/alerts/new", {"q": "а1", "channel": "email"}, page="/alerts/new?q=%D0%B01").status_code == 303
    r = post(env, "/alerts/new", {"q": "б2", "channel": "email"}, page="/alerts/new?q=%D0%B01")
    assert r.status_code == 422 and "не больше 1" in r.text and len(subs(env)) == 1


def test_email_subscriptions_can_be_switched_off_in_admin(env):
    logged_in(env)
    conn = env.db()
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('alerts_email_enabled', '0', 'x')")
    conn.commit()
    conn.close()
    page = env.client.get("/alerts/new?" + QS)
    assert "временно отключены" in page.text
    r = post(env, "/alerts/new", {**FIELDS, "channel": "email"}, page="/alerts/new?" + QS)
    assert r.status_code == 422 and not subs(env)


def test_list_page_states(env):
    guest = env.client.get("/alerts")
    assert guest.status_code == 200 and "Как подписаться" in guest.text and "noindex" in guest.headers["x-robots-tag"]
    logged_in(env)
    empty = env.client.get("/alerts")
    assert "Подписок пока нет" in empty.text and empty.text.count("<h1") == 1


def test_manage_own_subscription(env):
    logged_in(env)
    post(env, "/alerts/new", {**FIELDS, "channel": "email", "frequency": "daily"}, page="/alerts/new?" + QS)
    sid = subs(env)[0]["id"]
    assert post(env, f"/alerts/{sid}/pause", {}, page="/alerts").headers["location"] == "/alerts?m=paused"
    assert subs(env)[0]["status"] == "paused" and "на паузе" in env.client.get("/alerts").text
    assert post(env, f"/alerts/{sid}/resume", {}, page="/alerts").headers["location"] == "/alerts?m=resumed"
    assert subs(env)[0]["status"] == "active"
    assert post(env, f"/alerts/{sid}/frequency", {"frequency": "instant"}, page="/alerts").status_code == 303
    assert subs(env)[0]["frequency"] == "instant"
    assert post(env, f"/alerts/{sid}/frequency", {"frequency": "never"}, page="/alerts").status_code == 422
    assert post(env, f"/alerts/{sid}/delete", {}, page="/alerts").headers["location"] == "/alerts?m=deleted"
    assert subs(env) == []


def test_foreign_subscription_and_garbage_ids_are_404(env, tmp_path):
    other = build_env(tmp_path / "second")
    # подписка другого человека в той же базе
    logged_in(env, "anna@example.test")
    uid2 = make_user(env, "boris@example.test")
    conn = env.db()
    service.subscribe_user(conn, {"id": uid2, "email": "boris@example.test"}, FIELDS, now=env.clock())
    conn.close()
    sid = subs(env)[0]["id"]
    for action in ("pause", "resume", "delete", "frequency", "telegram"):
        r = post(env, f"/alerts/{sid}/{action}", {"frequency": "daily"}, page="/alerts")
        assert r.status_code in (404, 409), action
    assert subs(env)[0]["status"] == "active"
    assert post(env, "/alerts/abc/pause", {}, page="/alerts").status_code == 404
    assert post(env, "/alerts/99999999999999/delete", {}, page="/alerts").status_code == 404
    other.client.close()


def test_management_needs_login_and_csrf(env):
    r = env.client.post("/alerts/1/delete", data={}, follow_redirects=False)
    assert r.status_code in (303, 403)
    logged_in(env)
    post(env, "/alerts/new", {**FIELDS, "channel": "email"}, page="/alerts/new?" + QS)
    sid = subs(env)[0]["id"]
    assert env.client.post(f"/alerts/{sid}/delete", data={"csrf_token": "forged"}).status_code == 403
    assert len(subs(env)) == 1


# ------------------------------------------------------------------ отписка по ссылке

def unsub_path(env, sub_id=None):
    sub_id = sub_id or subs(env)[0]["id"]
    return "/alerts/unsubscribe/" + service.make_token(sub_id, "unsub", env.acc.secret)


def confirmed_guest(env):
    r = guest_subscribe(env)
    post(env, r.headers["location"], {"code": code_from(env.mailer.last())}, page=r.headers["location"])
    return subs(env)[0]["id"]


def test_unsubscribe_link_works_without_login_and_needs_a_click(env):
    confirmed_guest(env)
    path = unsub_path(env)
    page = env.client.get(path)
    assert page.status_code == 200 and "Отписаться" in page.text and "noindex" in page.headers["x-robots-tag"]
    assert subs(env)[0]["status"] == "active"  # GET ничего не отключает
    assert env.client.post(path, data={}).status_code == 403  # без токена формы
    done = post(env, path, {}, page=path)
    assert done.status_code == 200 and "Вы отписались" in done.text and subs(env)[0]["status"] == "unsubscribed"
    again = env.client.post(path, data={"csrf_token": csrf_of(env, "/login")}, follow_redirects=False)
    assert again.status_code == 200
    assert "отписались" in env.client.get(path).text.lower()


def test_one_click_unsubscribe_from_mail_client(env):
    confirmed_guest(env)
    path = unsub_path(env)
    r = env.client.post(path, data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200 and subs(env)[0]["status"] == "unsubscribed"


def test_unsubscribe_rejects_forged_tokens(env):
    sid = confirmed_guest(env)
    for bad in ("1.deadbeef", "x", f"{sid}.", service.make_token(sid, "confirm", env.acc.secret)):
        assert env.client.get("/alerts/unsubscribe/" + bad).status_code == 400, bad
        assert env.client.post("/alerts/unsubscribe/" + bad, data={"List-Unsubscribe": "One-Click"}).status_code == 400
    assert subs(env)[0]["status"] == "active"


def test_unsubscribe_ip_limit(tmp_path):
    e = build_env(tmp_path, rate_limits={"alert_unsub_ip": (3, 3600)})
    with e.client:
        codes = [e.client.get("/alerts/unsubscribe/1.bad").status_code for _ in range(5)]
        assert codes == [400, 400, 400, 429, 429]


# ------------------------------------------------------------------ рассылка до письма и обратно

def test_full_cycle_subscribe_receive_unsubscribe(env):
    from rabota.alerts import dispatch

    sid = confirmed_guest(env)
    conn = env.db()
    conn.execute("UPDATE alert_subscriptions SET frequency='instant' WHERE id=?", (sid,))
    conn.commit()
    vac(conn)
    conn.close()
    from rabota.web.queries import open_conn

    c = open_conn(env.db_path)
    res = dispatch.run_email_cycle(c, env.mailer, env.clock() + timedelta(hours=1), site_url=SITE, secret=env.acc.secret, sleep=lambda s: None)
    c.close()
    assert res["sent"] == 1
    mail = env.mailer.last("ivan@example.test")
    link = re.search(r"https://example\.test(/alerts/unsubscribe/\S+)", mail.body).group(1)
    assert post(env, link, {}, page=link).status_code == 200
    assert subs(env)[0]["status"] == "unsubscribed"


# ------------------------------------------------------------------ кнопка в выдаче, подвал, ссылки

def test_feed_button_for_guest_and_user(env):
    vac(env.db())
    plain = env.client.get("/vacancies")
    assert "Подписаться на такие вакансии" not in plain.text  # без поиска и фильтров подписываться не на что
    guest = env.client.get("/vacancies?" + QS + "&sort=pay")
    assert 'href="/alerts/new?' in guest.text and "Подписаться на такие вакансии" in guest.text and 'rel="nofollow"' in guest.text
    logged_in(env)
    user_page = env.client.get("/vacancies?q=" + quote("бариста") + "&daily=1")
    assert 'form="alert-sub-form"' in user_page.text and 'id="alert-sub-form"' in user_page.text
    assert 'name="q" value="бариста"' in user_page.text and 'name="daily" value="1"' in user_page.text
    token = re.search(r'id="alert-sub-form".*?name="csrf_token" value="([^"]+)"', user_page.text, re.DOTALL).group(1)
    r = env.client.post("/alerts/new", data={"csrf_token": token, "channel": "email", "frequency": "daily", "q": "бариста", "daily": "1"},
                        follow_redirects=False)
    assert r.status_code == 303 and subs(env)[0]["filters_key"] == '{"daily":true,"q":"бариста"}'


def test_footer_links(env):
    html = env.client.get("/about").text
    assert 'href="https://t.me/dnr_donetsk_rabota"' in html and 'href="/alerts"' in html
    assert "t.me/rabota_dnr_donetsk_bot" not in html  # токена нет, бот не работает, ссылка не нужна


def test_footer_has_bot_link_when_bot_is_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "123456:test-token")
    e = build_env(tmp_path)
    with e.client:
        assert 'href="https://t.me/rabota_dnr_donetsk_bot"' in e.client.get("/about").text


# ------------------------------------------------------------------ Telegram со стороны сайта

def test_telegram_option_is_hidden_without_a_bot(env):
    page = env.client.get("/alerts/new?" + QS)
    assert 'value="telegram"' not in page.text
    r = post(env, "/alerts/new", {**FIELDS, "channel": "telegram", "frequency": "instant"}, page="/alerts/new?" + QS)
    assert r.status_code == 422 and "Telegram сейчас недоступна" in r.text and not subs(env)


@pytest.fixture
def tg_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TG_BOT_USERNAME", "test_bot")
    e = build_env(tmp_path)
    with e.client:
        yield e


def test_guest_subscribes_via_telegram(tg_env):
    env = tg_env
    assert 'value="telegram"' in env.client.get("/alerts/new?" + QS).text
    r = post(env, "/alerts/new", {**FIELDS, "channel": "telegram", "frequency": "instant"}, page="/alerts/new?" + QS)
    assert r.status_code == 303 and r.headers["location"].startswith("/alerts/telegram/")
    row = subs(env)[0]
    assert row["channel"] == "telegram" and row["status"] == "pending" and not env.mailer.outbox
    page = env.client.get(r.headers["location"])
    code = re.search(r"https://t\.me/test_bot\?start=([A-Za-z0-9_-]+)", page.text).group(1)
    assert page.status_code == 200 and "Открыть Telegram" in page.text and "noindex" in page.headers["x-robots-tag"]
    conn = env.db()
    status, _ = service.bind_telegram(conn, code, 777, env.clock())
    conn.close()
    assert status == "ok"
    assert "Telegram подключён" in env.client.get(r.headers["location"]).text


def test_telegram_page_without_code_offers_a_new_link(tg_env):
    env = tg_env
    r = post(env, "/alerts/new", {**FIELDS, "channel": "telegram"}, page="/alerts/new?" + QS)
    bare = r.headers["location"].split("?")[0]
    page = env.client.get(bare)
    assert "устарела" in page.text and "Получить новую ссылку" in page.text
    renew = post(env, bare, {}, page=bare)
    assert renew.status_code == 303 and "?c=" in renew.headers["location"]
    assert env.client.get("/alerts/telegram/1.bad").status_code == 404


def test_logged_in_user_adds_telegram_to_email_subscription(tg_env):
    env = tg_env
    logged_in(env)
    post(env, "/alerts/new", {**FIELDS, "channel": "email", "frequency": "instant"}, page="/alerts/new?" + QS)
    assert "Ещё и в Telegram" in env.client.get("/alerts").text
    r = post(env, f"/alerts/{subs(env)[0]['id']}/telegram", {}, page="/alerts")
    assert r.status_code == 303 and r.headers["location"].startswith("/alerts/telegram/")
    tg = subs(env)[1]
    assert tg["channel"] == "telegram" and tg["frequency"] == "instant" and tg["user_id"]
    assert "Ждёт подключения" in env.client.get("/alerts").text


def test_guest_telegram_requests_are_limited_by_ip(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "123456:test-token")
    e = build_env(tmp_path, rate_limits={"alert_tg_ip": (2, 3600)})
    with e.client:
        codes = [post(e, "/alerts/new", {"q": f"п{i}", "channel": "telegram"}, page="/alerts/new?q=x").status_code for i in range(4)]
        assert codes == [303, 303, 429, 429]


# ------------------------------------------------------------------ админка

def test_admin_system_page_has_alerts_block_and_settings(tmp_path):
    from tests.web.admin_harness import login_admin, make_env

    e = make_env(tmp_path)
    with e.client:
        login_admin(e)
        conn = e.db()
        service.subscribe_user(conn, {"id": 1, "email": "admin@example.test"}, FIELDS, now=e.clock())
        conn.close()
        html = e.client.get("/admin/system").text
        assert 'data-key="alerts"' in html and 'data-sortable="system-blocks"' in html and "Подписки и рассылка" in html
        assert "Подписок всего" in html
        settings_page = e.client.get("/admin/settings").text
        for key in ("alerts_email_enabled", "alerts_mail_per_hour", "alerts_max_per_user", "tg_bot_enabled", "channel_enabled",
                    "channel_posts_per_hour"):
            assert f"s_{key}" in settings_page, key
        assert 'data-key="alerts"' in settings_page and 'data-key="telegram"' in settings_page

