"""Цикл приёма команд (getUpdates), доставка подписчикам и режим без токена. Клиент подменён."""
import asyncio
from datetime import timedelta

from rabota.alerts import conf, jobs, service
from rabota.realtime import state
from rabota.telegram_bot import deliver, runner
from rabota.telegram_bot.api import BotApiError
from tests.alerts.helpers import NOW, SITE, FakeBotApi, vac

CFG = conf.TelegramConfig(token="1:t", bot_username="test_bot", channel_id="-100", channel_username="chan")


def start_update(uid, code, chat=50):
    return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": chat, "type": "private"}, "text": f"/start {code}"}}


def drive(api, db_path, polls=1, **kw):
    """Запустить цикл бота и остановить его после нескольких опросов."""

    async def go():
        stop = asyncio.Event()
        real = api.get_updates
        calls = {"n": 0}

        async def counting(offset, timeout=25):
            calls["n"] += 1
            try:
                return await real(offset, timeout)
            finally:
                if calls["n"] >= polls:
                    stop.set()

        api.get_updates = counting
        await runner.run_bot(stop, CFG, db_path, site_url=SITE, api_factory=lambda cfg: api, poll_timeout=0, idle_sleep=0.01, **kw)

    asyncio.run(go())


def test_bot_replies_to_start_saves_offset_and_removes_webhook(conn, db_path):
    _, code = service.subscribe_telegram(conn, None, {"q": "бариста"}, now=NOW)
    api = FakeBotApi([[start_update(7, code)]])
    drive(api, db_path)
    assert api.webhook_deleted and api.closed
    assert api.sent and "Готово" in api.sent[0]["text"] and api.sent[0]["chat_id"] == 50
    assert state.get_last_id(conn, runner.OFFSET_KEY) == 7
    api2 = FakeBotApi()
    drive(api2, db_path)
    assert api2.offsets[0] == 8  # после перезапуска старые обновления не берутся


def test_bad_update_does_not_stop_the_others(conn, db_path, monkeypatch):
    from rabota.telegram_bot import bot

    real = bot.plan

    def flaky(conn_, update, **kw):
        if update["update_id"] == 1:
            raise RuntimeError("boom")
        return real(conn_, update, **kw)

    monkeypatch.setattr(bot, "plan", flaky)
    api = FakeBotApi([[{"update_id": 1, "message": {"chat": {"id": 1, "type": "private"}, "text": "/help"}},
                       {"update_id": 2, "message": {"chat": {"id": 2, "type": "private"}, "text": "/help"}}]])
    drive(api, db_path)
    assert [m["chat_id"] for m in api.sent] == [2]
    assert state.get_last_id(conn, runner.OFFSET_KEY) == 2


def test_revoked_token_stops_polling_and_is_recorded(conn, db_path):
    api = FakeBotApi()
    api.poll_error = BotApiError(401, "Unauthorized")
    drive(api, db_path, polls=5)
    assert len(api.offsets) == 1
    assert "токен бота не действует" in state.get_state(conn, runner.POLL_KEY).last_error


def test_network_error_is_retried_with_a_pause(conn, db_path, monkeypatch):
    pauses = []

    async def fake_sleep(stop, seconds):
        pauses.append(seconds)

    monkeypatch.setattr(runner, "_sleep", fake_sleep)
    api = FakeBotApi()
    api.poll_error = BotApiError(0, "сеть: ConnectError")
    drive(api, db_path, polls=3)
    assert len(api.offsets) == 3 and pauses == [5.0, 10.0, 20.0]
    assert "ConnectError" in state.get_state(conn, runner.POLL_KEY).last_error


def test_conflict_with_webhook_waits_longer(conn, db_path, monkeypatch):
    pauses = []

    async def fake_sleep(stop, seconds):
        pauses.append(seconds)

    monkeypatch.setattr(runner, "_sleep", fake_sleep)
    api = FakeBotApi()
    api.poll_error = BotApiError(409, "Conflict")
    drive(api, db_path, polls=1)
    assert pauses == [runner.CONFLICT_PAUSE]


def test_bot_switched_off_in_admin_does_not_poll(conn, db_path):
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('tg_bot_enabled', '0', 'x')")
    conn.commit()
    api = FakeBotApi([[start_update(1, "x")]])

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(runner.run_bot(stop, CFG, db_path, site_url=SITE, api_factory=lambda c: api, poll_timeout=0, idle_sleep=0.01))
        await asyncio.sleep(0.1)
        stop.set()
        await task

    asyncio.run(go())
    assert api.offsets == [] and api.sent == []


def test_no_token_means_no_bot_and_no_crash(db_path):
    async def go():
        stop = asyncio.Event()
        await runner.run_bot(stop, conf.TelegramConfig(), db_path, site_url=SITE, api_factory=lambda c: (_ for _ in ()).throw(AssertionError("не должен создаваться")))
        return runner.make_bot_task(stop, {}, db_path)

    assert asyncio.run(go()) is None


def test_bot_task_is_created_when_token_is_set(db_path):
    async def go():
        stop = asyncio.Event()
        task = runner.make_bot_task(stop, {"TG_BOT_TOKEN": "1:t"}, db_path)
        assert task is not None
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())


# ------------------------------------------------------------------ доставка подписчикам

def bound(conn, chat=60, frequency="instant", data=None):
    _, code = service.subscribe_telegram(conn, None, data or {"q": "бариста", "city": "Донецк"}, frequency=frequency, now=NOW)
    service.bind_telegram(conn, code, chat, NOW)


def cycle(conn, api, now=NOW + timedelta(hours=1)):
    async def nosleep(s):
        return None

    return asyncio.run(deliver.run_telegram_cycle(conn, api, now, site_url=SITE, sleep=nosleep))


def test_new_vacancies_are_delivered_once(conn):
    bound(conn)
    a = vac(conn, employer="Кофейня", pay_min=30000, pay_period="месяц")
    api = FakeBotApi()
    assert cycle(conn, api)["sent"] == 1
    message = api.sent[0]
    assert message["chat_id"] == 60 and f"{SITE}/vacancy/{a}" in message["text"] and "Кофейня" in message["text"]
    assert "бариста в Донецке" in message["text"] and "/stop" in message["text"]
    assert message["markup"]["inline_keyboard"][0][0]["text"] == "Все вакансии"
    assert cycle(conn, api, NOW + timedelta(hours=2))["sent"] == 0 and len(api.sent) == 1
    vac(conn)
    assert cycle(conn, api, NOW + timedelta(hours=3))["sent"] == 1 and len(api.sent) == 2


def test_text_is_html_safe(conn):
    bound(conn, data={"q": "бариста"})
    vac(conn, profession="бариста <i>x</i>", employer="A & B")
    api = FakeBotApi()
    cycle(conn, api)
    assert "<i>x</i>" not in api.sent[0]["text"] and "&lt;i&gt;x&lt;/i&gt;" in api.sent[0]["text"] and "A &amp; B" in api.sent[0]["text"]


def test_ten_cards_at_most_and_message_fits_the_limit(conn):
    bound(conn)
    for _ in range(14):
        vac(conn, employer="Работодатель " + "я" * 40, district="Ворошиловский")
    api = FakeBotApi()
    cycle(conn, api)
    text = api.sent[0]["text"]
    assert text.count("/vacancy/") == 10 and "И ещё 4 на сайте" in text and len(text) <= 4096


def test_blocked_chat_disables_its_subscriptions(conn):
    bound(conn, chat=61)
    bound(conn, chat=62, data={"q": "бариста", "city": "Донецк"})
    vac(conn)
    api = FakeBotApi()
    api.errors[61] = BotApiError(403, "Forbidden: bot was blocked by the user")
    res = cycle(conn, api)
    assert res["stopped"] == 1 and res["sent"] == 1
    assert service.subs_for_chat(conn, 61) == [] and len(service.subs_for_chat(conn, 62)) == 1
    assert [m["chat_id"] for m in api.sent] == [62]


def test_flood_limit_stops_the_cycle_without_marking(conn):
    bound(conn)
    vac(conn)
    api = FakeBotApi()
    api.fail_all = BotApiError(429, "Too Many Requests", retry_after=30)
    res = cycle(conn, api)
    assert res["errors"] == 1 and res["sent"] == 0
    assert conn.execute("SELECT COUNT(*) FROM alert_sent").fetchone()[0] == 0
    api.fail_all = None
    assert cycle(conn, api, NOW + timedelta(hours=2))["sent"] == 1


def test_delivery_switched_off_in_admin(conn):
    bound(conn)
    vac(conn)
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('tg_bot_enabled', '0', 'x')")
    conn.commit()
    api = FakeBotApi()
    assert cycle(conn, api)["disabled"] is True and api.sent == []


def test_telegram_job_built_only_with_token(tmp_path):
    names = [j.name for j in jobs.build_alert_jobs(tmp_path / "x.db", {"MAIL_BACKEND": "pigeon"})]
    assert names == []
    names = [j.name for j in jobs.build_alert_jobs(tmp_path / "x.db", {"MAIL_BACKEND": "pigeon", "TG_BOT_TOKEN": "1:t"})]
    assert names == ["alerts-telegram"]
    names = [j.name for j in jobs.build_alert_jobs(tmp_path / "x.db", {"MAIL_BACKEND": "pigeon", "TG_BOT_TOKEN": "1:t", "TG_CHANNEL_ID": "-100"})]
    assert names == ["alerts-telegram", "channel"]


def test_telegram_job_runs_with_fake_client(tmp_path):
    from rabota.web.queries import open_conn

    path = tmp_path / "job.db"
    c = open_conn(path)
    bound(c)
    vac(c)
    c.close()
    api = FakeBotApi()
    built = jobs.build_alert_jobs(path, {"MAIL_BACKEND": "pigeon", "TG_BOT_TOKEN": "1:t"}, api_factory=lambda cfg: api,
                                  clock=lambda: NOW + timedelta(hours=1), site_url=SITE)
    asyncio.run(built[0].run())
    assert len(api.sent) == 1 and api.closed
    c = open_conn(path)
    assert c.execute("SELECT last_ok_at FROM ingest_state WHERE source='job:alerts-telegram'").fetchone()[0]
    c.close()


def test_config_defaults_and_links():
    cfg = conf.TelegramConfig.from_env({})
    assert cfg.bot_ready is False and cfg.channel_ready is False
    assert cfg.bot_url == "https://t.me/rabota_dnr_donetsk_bot" and cfg.channel_url == "https://t.me/dnr_donetsk_rabota"
    cfg = conf.TelegramConfig.from_env({"TG_BOT_TOKEN": "1:t", "TG_BOT_USERNAME": "@my_bot", "TG_CHANNEL_ID": "-5", "TELEGRAM_PROXY": "socks5://h:1"})
    assert cfg.bot_ready and cfg.channel_ready and cfg.bot_username == "my_bot" and cfg.proxy == "socks5://h:1"
    assert cfg.start_link("abc") == "https://t.me/my_bot?start=abc"

