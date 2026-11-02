"""Пульс: границы периодов по местному времени, сравнение, плитки, спарклайн и доступ к странице."""
from datetime import UTC, datetime, timedelta

import pytest

from rabota.admin import pulse
from tests.accounts.harness import PASSWORD, login, make_user
from tests.admin.helpers import NOW  # 2026-10-29 12:00 UTC = 15:00 в Донецке
from tests.web.admin_harness import add_vacancy, login_admin, make_env, meta

# ------------------------------------------------------------------ границы периодов


def local(period_dt: datetime) -> str:
    return period_dt.strftime("%Y-%m-%d %H:%M")


def test_now_is_converted_to_donetsk_time():
    assert pulse.local_now(NOW) == datetime(2026, 10, 29, 15, 0)
    assert pulse.local_now(NOW.replace(tzinfo=None)) == datetime(2026, 10, 29, 15, 0)  # наивное время это UTC


def test_today_starts_at_local_midnight_not_utc():
    p = pulse.build_period("today", NOW)
    assert local(p.start) == "2026-10-29 00:00" and local(p.end) == "2026-10-29 15:00"
    assert pulse.to_utc_text(p.start) == "2026-10-28 21:00:00"  # местная полночь это 21:00 UTC предыдущего дня
    # сравнение с вчерашним днём до этого же времени
    assert local(p.prev_start) == "2026-10-28 00:00" and local(p.prev_end) == "2026-10-28 15:00"
    assert p.step == "hour"


def test_yesterday_and_seven_days():
    y = pulse.build_period("yesterday", NOW)
    assert (local(y.start), local(y.end)) == ("2026-10-28 00:00", "2026-10-29 00:00")
    assert (local(y.prev_start), local(y.prev_end)) == ("2026-10-27 00:00", "2026-10-28 00:00")
    s = pulse.build_period("7d", NOW)
    assert local(s.start) == "2026-10-23 00:00" and local(s.end) == "2026-10-29 15:00"
    assert s.prev_end == s.start and s.end - s.start == s.prev_end - s.prev_start
    assert len(pulse.buckets(s)) == 7


def test_thirty_days_and_month():
    t = pulse.build_period("30d", NOW)
    assert local(t.start) == "2026-09-30 00:00" and len(pulse.buckets(t)) == 30
    m = pulse.build_period("month", NOW)
    assert local(m.start) == "2026-10-01 00:00"
    assert local(m.prev_start) == "2026-09-01 00:00" and local(m.prev_end) == "2026-09-29 15:00"
    assert len(pulse.buckets(m)) == 29


def test_month_comparison_is_clipped_to_previous_month_length():
    now = datetime(2026, 3, 31, 12, 0, tzinfo=UTC)  # в феврале 28 дней
    m = pulse.build_period("month", now)
    assert local(m.prev_start) == "2026-02-01 00:00" and local(m.prev_end) == "2026-03-01 00:00"


def test_late_utc_evening_is_already_next_local_day():
    now = datetime(2026, 10, 29, 22, 30, tzinfo=UTC)  # 01:30 30 октября в Донецке
    assert local(pulse.build_period("today", now).start) == "2026-10-30 00:00"


def test_all_period_has_no_comparison_and_weekly_step_when_long():
    short = pulse.build_period("all", NOW, first=datetime(2026, 10, 20, 9, 0))
    assert not short.has_prev and short.step == "day" and local(short.start) == "2026-10-20 00:00"
    long = pulse.build_period("all", NOW, first=datetime(2026, 1, 5, 9, 0))
    assert long.step == "week"
    points = pulse.buckets(long)
    assert max(i for _, i in points) + 1 == (len(points) + 6) // 7


def test_unknown_period_falls_back_to_default():
    assert pulse.normalize("zzz") == pulse.DEFAULT_PERIOD and pulse.normalize(None) == pulse.DEFAULT_PERIOD
    assert [k for k, _ in pulse.PERIODS] == ["today", "yesterday", "7d", "30d", "month", "all"]


# ------------------------------------------------------------------ сравнение и спарклайн

def test_delta_green_when_more_is_better():
    d = pulse.delta(15, 10, lower_better=False)
    assert d["tone"] == "good" and d["pct"] == 50 and d["text"] == "+50%"
    d = pulse.delta(5, 10, lower_better=False)
    assert d["tone"] == "bad" and d["text"].endswith("50%") and d["arrow"] == "↓"


def test_delta_reversed_when_less_is_better():
    assert pulse.delta(3, 10, lower_better=True)["tone"] == "good"
    assert pulse.delta(12, 10, lower_better=True)["tone"] == "bad"


def test_delta_neutral_flat_and_missing():
    assert pulse.delta(12, 10, lower_better=None)["tone"] == "flat"
    assert pulse.delta(4, 4, lower_better=False)["tone"] == "flat"
    none = pulse.delta(4, None, lower_better=False)
    assert none["tone"] == "none" and none["text"] == "нет сравнения"
    assert pulse.delta(None, 4, lower_better=False)["tone"] == "none"
    from_zero = pulse.delta(3, 0, lower_better=False)
    assert from_zero["pct"] is None and from_zero["tone"] == "good"


def test_spark_geometry():
    assert pulse.spark([5]) is None and pulse.spark([]) is None
    s = pulse.spark([0, 5, 10])
    assert s["line"].startswith("M0.0 29.0") and s["max"] == 10 and s["total"] == 15
    assert s["y"] == 3.0  # наибольшее значение у верхнего края
    flat = pulse.spark([0, 0, 0])
    assert flat is not None and flat["total"] == 0


# ------------------------------------------------------------------ плитки на базе

def stamp(conn, table, rowid, column, value):
    conn.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (value, rowid))
    conn.commit()


def tile(data, key):
    return next(t for t in data["tiles"] if t["key"] == key)


def test_tiles_count_by_local_day_and_compare(conn, add):
    inside = add(profession="a", source_type="telegram", source="@a")
    stamp(conn, "vacancies", inside, "created_at", "2026-10-28 21:30:00")  # 00:30 местного, уже сегодня
    late_yesterday = add(profession="b", source_type="vk", source="club1")
    stamp(conn, "vacancies", late_yesterday, "created_at", "2026-10-28T20:59:00+00:00")  # 23:59 вчера
    midday = add(profession="d", source_type="telegram", source="@a")
    stamp(conn, "vacancies", midday, "created_at", "2026-10-28 08:00:00")  # вчера в 11:00, входит в сравнение
    site = add(profession="c", source_type="telegram", source="@a")
    conn.execute("UPDATE vacancies SET origin='direct', created_at='2026-10-29 08:00:00' WHERE id=?", (site,))
    conn.commit()
    today = pulse.build(conn, "today", NOW)
    new = tile(today, "new")
    assert new["value"] == 2 and new["prev"] == 1
    assert dict(new["breakdown"]) == {"Telegram": 1, "Разместили на сайте": 1}
    assert new["delta"]["tone"] == "good" and new["delta"]["pct"] == 100
    assert len(new["series"]) == 15 and new["series"][0] == 1 and new["series"][11] == 1
    yesterday = pulse.build(conn, "yesterday", NOW)
    assert tile(yesterday, "new")["value"] == 2  # и 23:59, и 11:00
    assert tile(today, "moderation")["value"] == 1 and "в очереди" in tile(today, "moderation")["note"]


def test_registrations_responses_and_complaints(conn, add):
    vid = add(profession="x")
    conn.execute("INSERT INTO users(id,email,password_hash,role,created_at) VALUES "
                 "(1,'a@x.ru','h','job_seeker','2026-10-29 09:00:00'),(2,'b@x.ru','h','employer','2026-10-29T09:30:00+00:00'),"
                 "(3,'c@x.ru','h','job_seeker','2026-10-20 09:00:00')")
    conn.execute("INSERT INTO applications(vacancy_id,user_id,created_at) VALUES (?,1,'2026-10-29 10:00:00')", (vid,))
    conn.execute("INSERT INTO complaints(vacancy_id,reason,created_at) VALUES (?,'мошенничество','2026-10-29 10:00:00')", (vid,))
    conn.execute("INSERT INTO reports(target_type,target_id,reason,created_at) VALUES ('user',2,'спам','2026-10-28 10:00:00')")
    conn.commit()
    data = pulse.build(conn, "7d", NOW)
    reg = tile(data, "registrations")
    assert reg["value"] == 2 and dict(reg["breakdown"]) == {"соискатели": 1, "работодатели": 1}
    assert tile(data, "responses")["value"] == 1
    cmp_ = tile(data, "complaints")
    assert cmp_["value"] == 2 and cmp_["lower_better"] is True
    assert dict(cmp_["breakdown"]) == {"на вакансии": 1, "на сообщения и людей": 1}
    assert tile(pulse.build(conn, "all", NOW), "registrations")["value"] == 3


def test_hidden_counts_audit_and_expired_counts_lifetime_end(conn, add):
    from rabota import config
    from rabota.admin import audit

    vid = add(profession="x")
    at = NOW - timedelta(minutes=5)
    audit.log(conn, {"id": 1, "email": "a@x.ru"}, "vacancy.hide", "vacancy", vid, now=at)
    audit.log(conn, None, "vacancy.autohide", "vacancy", vid, now=at)
    audit.log(conn, None, "vacancy.approve", "vacancy", vid, now=at)  # не скрытие
    ended = NOW - timedelta(days=config.VACANCY_TTL_DAYS, hours=1)  # срок жизни кончился час назад
    old = add(profession="old")
    conn.execute("UPDATE vacancies SET last_seen_at=? WHERE id=?", (ended.replace(microsecond=0).isoformat(), old))
    conn.commit()
    data = pulse.build(conn, "today", NOW)
    hidden = tile(data, "hidden")
    assert hidden["value"] == 2 and dict(hidden["breakdown"]) == {"вручную": 1, "автоскрытие по жалобам": 1}
    assert tile(data, "expired")["value"] == 1
    assert tile(data, "expired")["delta"]["tone"] != "bad"  # у «просрочено» нет оценки: без красного


def test_empty_database_shows_no_data_not_zero(conn):
    data = pulse.build(conn, "7d", NOW)
    for key in ("new", "hidden", "expired", "registrations", "responses", "complaints", "moderation"):
        t = tile(data, key)
        assert t["value"] is None and t["spark"] is None and t["delta"]["tone"] == "none", key


def test_real_zero_is_zero_when_data_exists_elsewhere(conn, add):
    vid = add(profession="x")
    conn.execute("UPDATE vacancies SET created_at='2026-01-01 10:00:00' WHERE id=?", (vid,))
    conn.commit()
    new = tile(pulse.build(conn, "today", NOW), "new")
    assert new["value"] == 0 and new["prev"] == 0 and new["delta"]["text"] == "без изменений"


def test_broken_source_gives_no_data(conn, add):
    add(profession="x")
    conn.execute("DROP TABLE reports")
    conn.commit()
    data = pulse.build(conn, "7d", NOW)
    assert tile(data, "complaints")["value"] is None
    assert tile(data, "new")["value"] is not None


# ------------------------------------------------------------------ страница и доступ

@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e


def test_guest_goes_to_login(env):
    r = env.client.get("/admin/pulse", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=/admin")


@pytest.mark.parametrize("role", ["job_seeker", "employer"])
def test_non_admin_gets_404(env, role):
    make_user(env, f"{role}@example.test", role=role)
    assert login(env, f"{role}@example.test", PASSWORD).status_code == 303
    assert env.client.get("/admin/pulse").status_code == 404
    assert env.client.get("/admin/pulse?period=30d").status_code == 404


def test_admin_page_renders_tiles_tabs_and_headers(env):
    login_admin(env)
    add_vacancy(env)
    r = env.client.get("/admin/pulse")
    assert r.status_code == 200
    assert "noindex" in r.headers["x-robots-tag"] and "no-store" in r.headers["cache-control"]
    assert "noindex" in meta(r.text, "robots")
    assert r.text.count("<h1") == 1 and "Пульс" in r.text
    for key in ("today", "yesterday", "7d", "30d", "month", "all"):
        assert f"/admin/pulse?period={key}" in r.text
    assert 'data-sortable="pulse-tiles"' in r.text
    for key in ("new", "moderation", "hidden", "expired", "registrations", "responses", "complaints"):
        assert f'data-key="{key}"' in r.text
    tiles_html, index_html = r.text.split('data-sortable="pulse-index"')
    assert tiles_html.count('draggable="true"') == 7
    assert index_html.count('draggable="true"') == 5  # блок «Страницы в индексе»: подборки, профессии, зарплаты, статьи, районы
    assert "<svg" in r.text and "—" not in r.text
    assert 'aria-current="page"' in r.text and 'href="/admin/pulse"' in r.text  # пункт меню


@pytest.mark.parametrize("period", ["today", "yesterday", "7d", "30d", "month", "all", "bogus"])
def test_every_period_opens(env, period):
    login_admin(env)
    r = env.client.get(f"/admin/pulse?period={period}")
    assert r.status_code == 200
    assert "нет данных" in r.text or "tile__n" in r.text


def test_current_period_tab_is_marked(env):
    login_admin(env)
    html = env.client.get("/admin/pulse?period=30d").text
    assert 'href="/admin/pulse?period=30d" aria-current="page"' in html


def test_dashboard_still_works_and_menu_order(env):
    login_admin(env)
    html = env.client.get("/admin").text
    assert html.index('href="/admin"') < html.index('href="/admin/pulse"') < html.index('href="/admin/moderation"')
