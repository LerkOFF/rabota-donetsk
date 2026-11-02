"""Бот и аккаунт: /start link_<код> привязывает Telegram, /start login_<код> подтверждает вход. Сеть подменена."""
from datetime import timedelta

from rabota.accounts import telegram as tg
from rabota.alerts import service
from rabota.telegram_bot import bot
from tests.alerts.helpers import NOW, SITE, user

SECRET = b"bot-test-secret-" + b"x" * 16


def msg(text, chat=10, sender=None, kind="private"):
    message = {"message_id": 5, "chat": {"id": chat, "type": kind}, "text": text}
    if sender is not None:
        message["from"] = sender
    return {"update_id": 1, "message": message}


def plan(conn, update, now=NOW):
    return bot.plan(conn, update, site_url=SITE, now=now)


def test_link_command_binds_telegram_user_not_chat_text(conn):
    u = user(conn)
    code = tg.issue_link_code(conn, u["id"], NOW)
    (reply,) = plan(conn, msg(f"/start link_{code}", chat=10, sender={"id": 10, "username": "anna_tg"}))
    assert "Готово" in reply.text and "Анна" in reply.text and "Войти через Telegram" in reply.text
    row = conn.execute("SELECT tg_user_id, tg_username FROM users").fetchone()
    assert (row["tg_user_id"], row["tg_username"]) == (10, "anna_tg")
    assert chr(0x2014) not in reply.text


def test_link_with_dead_code_explains_what_to_do(conn):
    (reply,) = plan(conn, msg("/start link_" + "A" * 22, sender={"id": 10}))
    assert "уже не действует" in reply.text and f"{SITE}/account" in reply.text


def test_link_expired_code(conn):
    u = user(conn)
    code = tg.issue_link_code(conn, u["id"], NOW)
    (reply,) = plan(conn, msg(f"/start link_{code}", sender={"id": 10}), now=NOW + timedelta(minutes=16))
    assert "уже не действует" in reply.text
    assert conn.execute("SELECT tg_user_id FROM users").fetchone()[0] is None


def test_link_refused_when_telegram_belongs_to_another_account(conn):
    a, b = user(conn, "a@example.test"), user(conn, "b@example.test")
    tg.bind_by_code(conn, tg.issue_link_code(conn, a["id"], NOW), 10, "", NOW)
    code = tg.issue_link_code(conn, b["id"], NOW)
    (reply,) = plan(conn, msg(f"/start link_{code}", sender={"id": 10}))
    assert "уже привязан к другому аккаунту" in reply.text and "Готово" not in reply.text
    assert conn.execute("SELECT tg_user_id FROM users WHERE id=?", (b["id"],)).fetchone()[0] is None


def test_link_refused_when_account_has_other_telegram(conn):
    a = user(conn)
    tg.bind_by_code(conn, tg.issue_link_code(conn, a["id"], NOW), 10, "", NOW)
    code = tg.issue_link_code(conn, a["id"], NOW)
    (reply,) = plan(conn, msg(f"/start link_{code}", chat=20, sender={"id": 20}))
    assert "уже привязан другой Telegram" in reply.text


def test_login_command_confirms_for_linked_account(conn):
    u = user(conn)
    tg.bind_by_code(conn, tg.issue_link_code(conn, u["id"], NOW), 10, "", NOW)
    nonce, code = tg.start_login(conn, SECRET, "1.1.1.1", NOW)
    (reply,) = plan(conn, msg(f"/start login_{code}", sender={"id": 10}))
    assert "Вход подтверждён" in reply.text and "Вернитесь на сайт" in reply.text
    assert tg.login_state(conn, SECRET, nonce, NOW) == "confirmed"
    (again,) = plan(conn, msg(f"/start login_{code}", sender={"id": 10}))
    assert "уже подтверждён" in again.text


def test_login_command_for_unlinked_telegram_asks_to_link_first(conn):
    user(conn)
    nonce, code = tg.start_login(conn, SECRET, "1.1.1.1", NOW)
    (reply,) = plan(conn, msg(f"/start login_{code}", chat=99, sender={"id": 99}))
    assert "Сначала привяжите Telegram в профиле на сайте" in reply.text and f"{SITE}/account" in reply.text
    assert tg.login_state(conn, SECRET, nonce, NOW) == "unlinked"
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1  # новый аккаунт не появился


def test_login_command_for_blocked_account(conn):
    from rabota.accounts import service as accounts

    u = user(conn)
    tg.bind_by_code(conn, tg.issue_link_code(conn, u["id"], NOW), 10, "", NOW)
    accounts.set_blocked(conn, u["id"], True)
    nonce, code = tg.start_login(conn, SECRET, "1.1.1.1", NOW)
    (reply,) = plan(conn, msg(f"/start login_{code}", sender={"id": 10}))
    assert "заблокирован" in reply.text
    assert tg.login_state(conn, SECRET, nonce, NOW) == "blocked"


def test_login_command_with_stale_code(conn):
    _nonce, code = tg.start_login(conn, SECRET, "1.1.1.1", NOW)
    (reply,) = plan(conn, msg(f"/start login_{code}", sender={"id": 10}), now=NOW + timedelta(minutes=6))
    assert "уже не действует" in reply.text and "Войти через Telegram" in reply.text
    (junk,) = plan(conn, msg("/start login_%%%", sender={"id": 10}))
    assert "уже не действует" in junk.text


def test_login_code_is_not_logged(conn, caplog):
    u = user(conn)
    tg.bind_by_code(conn, tg.issue_link_code(conn, u["id"], NOW), 10, "", NOW)
    _nonce, code = tg.start_login(conn, SECRET, "1.1.1.1", NOW)
    with caplog.at_level("DEBUG"):
        plan(conn, msg(f"/start login_{code}", sender={"id": 10}))
    assert code not in caplog.text


def test_group_chats_are_ignored_for_account_commands(conn):
    nonce, code = tg.start_login(conn, SECRET, "1.1.1.1", NOW)
    assert plan(conn, msg(f"/start login_{code}", chat=-5, sender={"id": 10}, kind="group")) == []
    assert tg.login_state(conn, SECRET, nonce, NOW) == "waiting"


def test_subscription_codes_still_work(conn):
    row, code = service.subscribe_telegram(conn, user(conn), {"q": "бариста", "city": "Донецк"}, frequency="instant", now=NOW)
    (reply,) = plan(conn, msg(f"/start {code}", chat=77, sender={"id": 77}))
    assert "бариста в Донецке" in reply.text
    assert service.get(conn, row["id"])["tg_chat_id"] == 77
