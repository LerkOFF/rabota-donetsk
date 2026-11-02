"""Автопостинг в канал: рубрики, пороги, тихие часы, потолок в час, дубли, сбои. Клиент подменён, сети нет."""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from rabota.alerts import jobs
from rabota.telegram_bot import channel
from rabota.telegram_bot.api import BotApiError
from tests.alerts.helpers import SITE, FakeBotApi, vac

# 09:30 по Донецку (UTC+3): утро, подходит подработке; 14:00: день, топ зарплат; 23:30: тишина
MORNING = datetime(2026, 11, 2, 6, 30, tzinfo=UTC)
DAY = datetime(2026, 11, 2, 11, 0, tzinfo=UTC)
NIGHT = datetime(2026, 11, 2, 20, 30, tzinfo=UTC)
CHANNEL = "-100500"


def aged(conn, ids, now, minutes=10):
    stamp = (now - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    for vid in ids:
        conn.execute("UPDATE vacancies SET created_at = ? WHERE id = ?", (stamp, vid))
    conn.commit()
    return ids


def many(conn, n, now, minutes=10, **kw):
    return aged(conn, [vac(conn, profession=f"профессия {i}", **kw) for i in range(n)], now, minutes)


@pytest.fixture
def api():
    return FakeBotApi()


def post(conn, api, now):
    return asyncio.run(channel.run_channel_cycle(conn, api, CHANNEL, now, site_url=SITE))


def test_nothing_to_post_on_empty_site(conn):
    assert channel.plan_next(conn, MORNING, SITE) is None


def test_side_job_rubric_in_the_morning_needs_three(conn):
    many(conn, 2, MORNING, employment="подработка")
    early = channel.plan_next(conn, MORNING, SITE)
    assert early is None or early.rubric != "side_today"  # двух подработок мало, остаётся рубрика часа
    many(conn, 1, MORNING, employment="на сегодня")
    p = channel.plan_next(conn, MORNING, SITE)
    assert p.rubric == "side_today" and p.key == "side_today:2026-11-02" and len(p.rows) == 3
    assert "Подработка на сегодня" in p.text and f"{SITE}/vacancies?side_job=1" in p.text and p.button_text == "Вся подработка"


def test_top_pay_rubric_in_the_afternoon_sorted_by_pay(conn):
    for i, pay in enumerate((20000, 90000, 50000)):
        aged(conn, [vac(conn, profession=f"продавец {i}", pay_min=pay, pay_period="месяц")], DAY, minutes=60)
    p = channel.plan_next(conn, DAY, SITE)
    assert p.rubric == "top_pay" and [r["pay_min"] for r in p.rows] == [90000, 50000, 20000]
    assert f"{SITE}/vacancies?sort=pay" in p.text


def test_new_hour_rubric_needs_two_and_keys_by_hour(conn):
    many(conn, 1, DAY)
    assert channel.plan_next(conn, DAY, SITE) is None
    many(conn, 1, DAY)
    p = channel.plan_next(conn, DAY, SITE)
    assert p.rubric == "new_hour" and p.key == "new_hour:2026-11-02T14" and "Новые вакансии часа" in p.text


def test_night_is_quiet(conn):
    many(conn, 5, NIGHT)
    assert channel.plan_next(conn, NIGHT, SITE) is None
    early = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)  # 07:30 по Донецку
    many(conn, 5, early)
    assert channel.plan_next(conn, early, SITE) is None


def test_switched_off_in_admin(conn):
    many(conn, 5, DAY)
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('channel_enabled', '0', 'x')")
    conn.commit()
    assert channel.plan_next(conn, DAY, SITE) is None


def test_posting_records_and_never_repeats_vacancies(conn, api):
    ids = many(conn, 3, DAY)
    result = post(conn, api, DAY)
    assert result == {"posted": "new_hour", "error": ""}
    sent = api.sent
    assert len(sent) == 1 and sent[0]["chat_id"] == CHANNEL and "inline_keyboard" in sent[0]["markup"]
    assert conn.execute("SELECT COUNT(*) FROM channel_vacancies").fetchone()[0] == 3
    # те же вакансии в следующий час и в другой рубрике не публикуются
    later = DAY + timedelta(hours=1, minutes=5)
    aged(conn, ids, later)
    assert channel.plan_next(conn, later, SITE) is None


def test_same_hour_key_is_posted_once(conn, api):
    many(conn, 3, DAY)
    post(conn, api, DAY)
    many(conn, 3, DAY + timedelta(minutes=20))
    assert channel.plan_next(conn, DAY + timedelta(minutes=20), SITE) is None


def test_hourly_cap(conn, api):
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('channel_posts_per_hour', '1', 'x')")
    conn.commit()
    many(conn, 3, DAY)
    assert post(conn, api, DAY)["posted"]
    later = DAY + timedelta(minutes=30)
    many(conn, 3, later)
    assert channel.plan_next(conn, later, SITE) is None
    nxt = DAY + timedelta(hours=1, minutes=1)
    many(conn, 3, nxt)
    assert channel.plan_next(conn, nxt, SITE) is not None


def test_failure_is_recorded_and_retry_waits_ten_minutes(conn, api):
    many(conn, 3, DAY)
    api.fail_all = BotApiError(400, "Bad Request: chat not found")
    result = post(conn, api, DAY)
    assert result["posted"] == "" and "chat not found" in result["error"]
    row = conn.execute("SELECT ok, error FROM channel_posts").fetchone()
    assert row["ok"] == 0 and "chat not found" in row["error"]
    assert conn.execute("SELECT COUNT(*) FROM channel_vacancies").fetchone()[0] == 0
    assert channel.plan_next(conn, DAY + timedelta(minutes=5), SITE) is None
    api.fail_all = None
    assert post(conn, api, DAY + timedelta(minutes=11))["posted"] == "new_hour"


def test_hidden_vacancies_are_not_posted(conn):
    ids = many(conn, 3, DAY)
    conn.execute("UPDATE vacancies SET status = 'archived' WHERE id = ?", (ids[0],))
    conn.commit()
    hidden = conn.execute("SELECT COUNT(*) FROM vacancies WHERE id = ? AND " + channel.q.VISIBLE_SQL, (ids[0],)).fetchone()[0] == 0
    assert hidden, "архивная вакансия должна быть скрыта на сайте"
    p = channel.plan_next(conn, DAY, SITE)
    assert p is not None and ids[0] not in [r["id"] for r in p.rows]


def test_text_is_clean_and_html_safe(conn):
    many(conn, 2, DAY, employer="A & <b>B</b>")
    p = channel.plan_next(conn, DAY, SITE)
    assert "\u2014" not in p.text and "<b>B</b>" not in p.text and "A &amp; " in p.text
    assert len(p.text) <= 4096
    for word in ("vault", "Obsidian", "Claude", "Cursor"):
        assert word not in p.text


def test_no_channel_id_means_no_channel_job(tmp_path):
    names = [j.name for j in jobs.build_alert_jobs(tmp_path / "x.db", {"TG_BOT_TOKEN": "1:t"})]
    assert "channel" not in names
    names = [j.name for j in jobs.build_alert_jobs(tmp_path / "x.db", {"TG_BOT_TOKEN": "1:t", "TG_CHANNEL_ID": CHANNEL})]
    assert "channel" in names


def test_channel_job_posts_with_fake_client(tmp_path):
    from rabota.web.queries import open_conn

    path = tmp_path / "chan.db"
    c = open_conn(path)
    many(c, 3, DAY)
    c.close()
    api = FakeBotApi()
    built = jobs.build_alert_jobs(path, {"TG_BOT_TOKEN": "1:t", "TG_CHANNEL_ID": CHANNEL}, api_factory=lambda cfg: api,
                                  clock=lambda: DAY, site_url=SITE)
    job = next(j for j in built if j.name == "channel")
    asyncio.run(job.run())
    assert len(api.sent) == 1 and api.closed
