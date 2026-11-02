"""Команды бота: /start с кодом и без, /list, /stop с подтверждением, /help. Сети нет: разбираются обновления."""
import asyncio

from rabota.alerts import service
from rabota.telegram_bot import bot
from tests.alerts.helpers import NOW, SITE, FakeBotApi, user, vac


def msg(text, chat=10, kind="private"):
    return {"update_id": 1, "message": {"message_id": 5, "chat": {"id": chat, "type": kind}, "text": text}}


def plan(conn, update):
    return bot.plan(conn, update, site_url=SITE, now=NOW)


def test_start_without_code_explains_how_to_subscribe(conn):
    (reply,) = plan(conn, msg("/start"))
    assert reply.chat_id == 10 and f"{SITE}/vacancies" in reply.text
    for word in ("Подписаться на такие вакансии", "/list", "/stop", "/help"):
        assert word in reply.text
    assert "\u2014" not in reply.text


def test_start_with_code_binds_chat_and_confirms(conn):
    row, code = service.subscribe_telegram(conn, user(conn), {"q": "бариста", "city": "Донецк"}, frequency="instant", now=NOW)
    (reply,) = plan(conn, msg(f"/start {code}", chat=77))
    assert "бариста в Донецке" in reply.text and "сразу" in reply.text
    assert service.get(conn, row["id"])["tg_chat_id"] == 77 and service.get(conn, row["id"])["status"] == "active"


def test_start_with_bot_suffix_and_second_use_of_code(conn):
    _, code = service.subscribe_telegram(conn, None, {"q": "бариста"}, now=NOW)
    plan(conn, msg(f"/start@some_bot {code}", chat=1))
    (reply,) = plan(conn, msg(f"/start {code}", chat=2))
    assert "уже не действует" in reply.text and f"{SITE}/alerts" in reply.text
    assert service.subs_for_chat(conn, 2) == []


def test_start_with_garbage_code(conn):
    (reply,) = plan(conn, msg("/start ../../etc"))
    assert "уже не действует" in reply.text


def test_start_when_chat_is_full(conn):
    for i in range(service.MAX_PER_CHAT):
        _, code = service.subscribe_telegram(conn, None, {"q": f"п{i}"}, now=NOW)
        plan(conn, msg(f"/start {code}", chat=5))
    _, code = service.subscribe_telegram(conn, None, {"q": "лишняя"}, now=NOW)
    (reply,) = plan(conn, msg(f"/start {code}", chat=5))
    assert "много подписок" in reply.text


def test_list_shows_titles_escaped_and_status(conn):
    _, c1 = service.subscribe_telegram(conn, None, {"q": "бариста", "city": "Донецк"}, frequency="instant", now=NOW)
    _, c2 = service.subscribe_telegram(conn, None, {"q": "<b>x</b>"}, frequency="daily", now=NOW)
    plan(conn, msg(f"/start {c1}", chat=3))
    plan(conn, msg(f"/start {c2}", chat=3))
    (reply,) = plan(conn, msg("/list", chat=3))
    assert "Ваши подписки (2)" in reply.text and "1. бариста в Донецке - сразу" in reply.text
    assert "&lt;b&gt;x&lt;/b&gt;" in reply.text and "<b>x</b>" not in reply.text


def test_list_empty_points_to_site(conn):
    (reply,) = plan(conn, msg("/list", chat=4))
    assert "Подписок пока нет" in reply.text and f"{SITE}/vacancies" in reply.text


def test_stop_asks_first_then_disables_only_after_yes(conn):
    _, code = service.subscribe_telegram(conn, None, {"q": "бариста"}, now=NOW)
    plan(conn, msg(f"/start {code}", chat=6))
    (ask,) = plan(conn, msg("/stop", chat=6))
    assert "Отключить" in ask.text and [b["callback_data"] for b in ask.markup["inline_keyboard"][0]] == [bot.STOP_YES, bot.STOP_NO]
    assert len(service.subs_for_chat(conn, 6)) == 1  # одна команда ничего не отключает
    cb = {"update_id": 2, "callback_query": {"id": "cb1", "data": bot.STOP_NO, "message": {"message_id": 9, "chat": {"id": 6, "type": "private"}}}}
    (no,) = plan(conn, cb)
    assert "ничего не меняю" in no.text and no.edit_message_id == 9 and len(service.subs_for_chat(conn, 6)) == 1
    cb["callback_query"]["data"] = bot.STOP_YES
    (yes,) = plan(conn, cb)
    assert "отключены" in yes.text and yes.callback_id == "cb1" and service.subs_for_chat(conn, 6) == []
    (again,) = plan(conn, msg("/stop", chat=6))
    assert "нечего" in again.text


def test_stop_does_not_touch_other_chats(conn):
    for chat in (1, 2):
        _, code = service.subscribe_telegram(conn, None, {"q": f"п{chat}"}, now=NOW)
        plan(conn, msg(f"/start {code}", chat=chat))
    cb = {"update_id": 2, "callback_query": {"id": "c", "data": bot.STOP_YES, "message": {"message_id": 1, "chat": {"id": 1, "type": "private"}}}}
    plan(conn, cb)
    assert service.subs_for_chat(conn, 1) == [] and len(service.subs_for_chat(conn, 2)) == 1


def test_help_and_unknown_input(conn):
    (h,) = plan(conn, msg("/help"))
    assert "/list" in h.text and "/stop" in h.text and f"{SITE}/alerts" in h.text
    (u,) = plan(conn, msg("/dance"))
    assert "Такой команды нет" in u.text
    (t,) = plan(conn, msg("привет"))
    assert "только на команды" in t.text


def test_groups_and_foreign_updates_are_ignored(conn):
    assert plan(conn, msg("/start", kind="group")) == []
    assert plan(conn, {"update_id": 3, "edited_message": {}}) == []
    assert plan(conn, {"update_id": 4}) == []


def test_blocking_the_bot_disables_subscriptions(conn):
    _, code = service.subscribe_telegram(conn, None, {"q": "бариста"}, now=NOW)
    plan(conn, msg(f"/start {code}", chat=8))
    event = {"update_id": 5, "my_chat_member": {"chat": {"id": 8, "type": "private"}, "new_chat_member": {"status": "kicked"}}}
    assert plan(conn, event) == []
    assert service.subs_for_chat(conn, 8) == []
    assert conn.execute("SELECT status, last_error FROM alert_subscriptions").fetchone()[1] == "бот заблокирован"


def test_replies_are_sent_through_the_api(conn):
    api = FakeBotApi()
    replies = [bot.Reply(1, "привет", {"inline_keyboard": []}), bot.Reply(2, "правка", edit_message_id=7, callback_id="x", callback_text="ok")]
    asyncio.run(bot.send_replies(api, replies))
    assert api.sent[0]["chat_id"] == 1 and api.sent[0]["text"] == "привет"
    assert api.edited == [{"chat_id": 2, "message_id": 7, "text": "правка"}] and api.answered == [("x", "ok")]


def test_send_failure_does_not_stop_other_replies(conn):
    from rabota.telegram_bot.api import BotApiError

    api = FakeBotApi()
    api.errors[1] = BotApiError(403, "Forbidden: bot was blocked by the user")
    asyncio.run(bot.send_replies(api, [bot.Reply(1, "а"), bot.Reply(2, "б")]))
    assert [m["chat_id"] for m in api.sent] == [2]


def test_nothing_in_texts_has_long_dash_or_internal_words(conn):
    texts = [bot.welcome(SITE), bot.help_text(SITE), bot.list_text([], SITE)]
    for text in texts:
        assert "\u2014" not in text
        for word in ("vault", "Obsidian", "Claude", "Cursor"):
            assert word not in text
    vac(conn)
