"""Раздел «Вакансии»: фильтры, поиск, сортировка, маски контактов, состояние, история, повтор."""
from datetime import UTC, datetime, timedelta

import pytest

from rabota.admin import audit, complaints, vacancies
from rabota.db import save_vacancy
from rabota.models import RawPost
from rabota.web import queries
from tests.admin.helpers import ADMIN, NOW, make_vac

NOW_REAL = datetime.now(UTC)


def fresh() -> str:
    return (NOW_REAL - timedelta(hours=3)).replace(microsecond=0).isoformat()


def old() -> str:
    return (NOW_REAL - timedelta(days=40)).replace(microsecond=0).isoformat()


@pytest.fixture
def mk(conn):
    """mk(profession=..., created_at=..., **sql_columns) -> id. Колонки вне Vacancy правятся запросом."""

    def _mk(created_at: str | None = None, sql: dict | None = None, **kw) -> int:
        kw.setdefault("published_at", fresh())
        kw.setdefault("last_seen_at", fresh())
        vid = save_vacancy(conn, make_vac(**kw))
        cols = dict(sql or {})
        if created_at:
            cols["created_at"] = created_at
        if cols:
            conn.execute(f"UPDATE vacancies SET {', '.join(c + '=?' for c in cols)} WHERE id=?", [*cols.values(), vid])
            conn.commit()
        return vid

    return _mk


def ids(conn, now=NOW_REAL, **params) -> list[int]:
    f = vacancies.parse(params)
    page = vacancies.listing(conn, f, now, per_page=200)
    return [v["id"] for v in page.items]


@pytest.fixture
def zoo(mk):
    """Набор вакансий во всех состояниях."""
    z = {}
    z["live"] = mk(profession="бариста", employer="Кофейня Уют", contacts={"phones": ["+79491234567"]})
    z["hidden"] = mk(profession="грузчик", status="hidden")
    z["expired"] = mk(profession="повар", published_at=old(), last_seen_at=old())
    z["pending"] = mk(profession="курьер", source_type="site", source="site", sql={"moderation_status": "pending", "origin": "direct", "status": "hidden"})
    z["rejected"] = mk(profession="охранник", sql={"moderation_status": "rejected", "status": "hidden"})
    z["foreign"] = mk(profession="водитель", city="Новосибирск")
    z["dup"] = mk(profession="бариста", sql={"duplicate_of": z["live"]})
    z["susp"] = mk(profession="менеджер", suspicious=True)
    z["military"] = mk(profession="контракт", kind="military")
    z["vk"] = mk(profession="продавец", source_type="vk", source="club1", city=None)
    return z


# ------------------------------------------------------------------ разбор адреса

def test_parse_defaults_and_garbage():
    f = vacancies.parse({})
    assert (f.sort, f.dir, f.period, f.page, f.status) == ("created", "desc", "all", 1, "")
    assert not f.active and f.query() == ""
    g = vacancies.parse({"status": "evil", "sort": "id; DROP", "dir": "up", "period": "year", "page": "-5", "flag": "x", "mod": "z"})
    assert (g.status, g.sort, g.dir, g.period, g.page, g.flag, g.mod) == ("", "created", "desc", "all", 1, "", "")
    assert vacancies.parse({"page": "abc"}).page == 1


def test_query_keeps_only_non_defaults():
    f = vacancies.parse({"status": "hidden", "q": "бариста", "sort": "pay", "dir": "asc", "period": "7d", "page": "3"})
    assert f.params() == {"q": "бариста", "status": "hidden", "period": "7d", "sort": "pay", "dir": "asc", "page": "3"}
    assert "page" not in f.query_without_page()
    assert f.active


# ------------------------------------------------------------------ список видит всё

def test_list_shows_every_state(conn, zoo):
    assert sorted(ids(conn)) == sorted(zoo.values())


@pytest.mark.parametrize("params, expected", [
    ({"status": "hidden"}, {"hidden", "pending", "rejected"}),
    ({"status": "expired"}, {"expired"}),
    ({"status": "active"}, {"live", "foreign", "dup", "susp", "military", "vk"}),
    ({"mod": "pending"}, {"pending"}),
    ({"mod": "rejected"}, {"rejected"}),
    ({"origin": "direct"}, {"pending"}),
    ({"source": "vk"}, {"vk"}),
    ({"city": "Новосибирск"}, {"foreign"}),
    ({"city": "донецк"}, {"live", "hidden", "expired", "pending", "rejected", "dup", "susp", "military"}),
    ({"city": vacancies.FOREIGN_CITY}, {"foreign"}),
    ({"flag": "duplicate"}, {"dup"}),
    ({"flag": "suspicious"}, {"susp"}),
    ({"flag": "foreign"}, {"foreign"}),
    ({"flag": "stale"}, set()),
    ({"kind": "other"}, {"military"}),
    ({"kind": "vacancy", "status": "hidden"}, {"hidden", "pending", "rejected"}),
    ({"prof": "ГРУЗ"}, {"hidden"}),
    ({"text": "кофе"}, {"live", "hidden", "expired", "pending", "rejected", "foreign", "dup", "susp", "military", "vk"}),
])
def test_filters(conn, zoo, params, expected):
    assert set(ids(conn, **params)) == {zoo[k] for k in expected}


def test_stale_flag_finds_active_but_outdated(conn, mk):
    stale = mk(profession="садовник", published_at=old(), last_seen_at=old(), sql={"status": "active"})
    mk(profession="свежая")
    assert ids(conn, flag="stale") == [stale]


def test_visible_flag_matches_site_condition(conn, zoo):
    visible = set(ids(conn, flag="visible"))
    on_site = {r["id"] for r in conn.execute(f"SELECT id FROM vacancies WHERE {queries.VISIBLE_SQL}")}
    assert visible == on_site == {zoo["live"], zoo["vk"]}
    assert set(ids(conn, flag="invisible")) == set(zoo.values()) - on_site


def test_visibility_agrees_with_sql_for_each_row(conn, zoo):
    on_site = {r["id"] for r in conn.execute(f"SELECT id FROM vacancies WHERE {queries.VISIBLE_SQL}")}
    page = vacancies.listing(conn, vacancies.parse({}), NOW_REAL, per_page=100)
    for v in page.items:
        assert v["visibility"]["visible"] == (v["id"] in on_site), (v["id"], v["visibility"]["reasons"])


def test_visibility_reasons_are_named(conn, zoo):
    page = {v["id"]: v for v in vacancies.listing(conn, vacancies.parse({}), NOW_REAL, per_page=100).items}
    text = lambda k: " ".join(r[0] for r in page[zoo[k]]["visibility"]["reasons"])
    assert "На проверке" in text("pending") and "Отклонена" in text("rejected")
    assert "Скрыта" in text("hidden") and "Истекла" in text("expired")
    assert f"Дубль №{zoo['live']}" in text("dup") and "Подозрительная" in text("susp")
    assert "Новосибирск" in text("foreign") and "Не вакансия" in text("military")


# ------------------------------------------------------------------ период

def test_period_bounds_use_local_day():
    # 29 октября 23:30 UTC уже 30 октября по Донецку (UTC+3)
    now = datetime(2026, 10, 29, 23, 30, tzinfo=UTC)
    assert vacancies.period_bounds("today", now) == ("2026-10-29 21:00:00", "2026-10-30 21:00:00")
    assert vacancies.period_bounds("yesterday", now) == ("2026-10-28 21:00:00", "2026-10-29 21:00:00")
    assert vacancies.period_bounds("7d", now)[0] == "2026-10-23 21:00:00"
    assert vacancies.period_bounds("month", now) == ("2026-09-30 21:00:00", "2026-10-31 21:00:00")
    assert vacancies.period_bounds("all", now) is None


def test_period_filter_by_created_at(conn, mk):
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    today = mk(profession="a", created_at="2026-10-29 08:00:00")
    yest = mk(profession="b", created_at="2026-10-28 15:00:00")
    last_week = mk(profession="c", created_at="2026-10-24 10:00:00")
    long_ago = mk(profession="d", created_at="2026-08-01 10:00:00")
    assert ids(conn, now, period="today") == [today]
    assert ids(conn, now, period="yesterday") == [yest]
    assert set(ids(conn, now, period="7d")) == {today, yest, last_week}
    assert set(ids(conn, now, period="30d")) == {today, yest, last_week}
    assert set(ids(conn, now, period="all")) == {today, yest, last_week, long_ago}
    assert set(ids(conn, now, period="month")) == {today, yest, last_week}


# ------------------------------------------------------------------ поиск

def test_search_by_number(conn, zoo):
    assert ids(conn, q=str(zoo["hidden"])) == [zoo["hidden"]]
    assert ids(conn, q="9999") == []


@pytest.mark.parametrize("query", ["+7 949 123-45-67", "8 (949) 123-45-67", "9491234567", "123-45-67"])
def test_search_by_phone_any_format(conn, zoo, query):
    assert ids(conn, q=query) == [zoo["live"]]


def test_search_by_profession_employer_and_yo(conn, zoo, mk):
    assert zoo["live"] in ids(conn, q="Бариста")
    assert ids(conn, q="уют") == [zoo["live"]]
    ele = mk(profession="ёлочник")
    assert ids(conn, q="елочник") == [ele]


def test_search_by_company_name(conn, mk):
    conn.execute("INSERT INTO users(email, password_hash, role) VALUES ('o@example.test', 'x', 'employer')")
    conn.execute("INSERT INTO companies(owner_user_id, name) VALUES (1, 'Ромашка Плюс')")
    conn.commit()
    vid = mk(profession="кассир", sql={"company_id": 1})
    mk(profession="кассир другой")
    assert ids(conn, q="ромашка") == [vid]


def test_search_combines_with_filters(conn, zoo):
    assert ids(conn, q="бариста", flag="duplicate") == [zoo["dup"]]


def test_search_text_is_not_sql(conn, zoo):
    assert ids(conn, q="'; DROP TABLE vacancies; --") == []
    assert conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == len(zoo)


# ------------------------------------------------------------------ сортировка и страницы

def test_sort_and_pagination(conn, mk):
    made = [mk(profession=f"проф{i}") for i in range(7)]
    f = vacancies.parse({"sort": "id", "dir": "asc"})
    first = vacancies.listing(conn, f, NOW_REAL, per_page=3)
    assert [v["id"] for v in first.items] == made[:3] and first.total == 7 and first.pages == 3
    f2 = vacancies.parse({"sort": "id", "dir": "asc", "page": "99"})
    last = vacancies.listing(conn, f2, NOW_REAL, per_page=3)
    assert last.page == 3 and [v["id"] for v in last.items] == made[6:] and f2.page == 3
    assert ids(conn, sort="id", dir="desc") == made[::-1]


def test_sort_by_pay_puts_empty_last(conn, mk):
    a = mk(profession="a", pay_min=20000, pay_max=30000, pay_period="месяц")
    b = mk(profession="b")
    c = mk(profession="c", pay_min=50000, pay_max=60000, pay_period="месяц")
    assert ids(conn, sort="pay", dir="desc") == [c, a, b]
    assert ids(conn, sort="pay", dir="asc") == [a, c, b]


def test_counts_and_facets(conn, zoo):
    c = vacancies.counts(conn, NOW_REAL)
    assert c["all"] == len(zoo) and c["visible"] == 2 and c["pending"] == 1 and c["duplicate"] == 1 and c["foreign"] == 1
    assert c["expired"] == 1 and c["hidden"] == 3
    fac = vacancies.facets(conn)
    assert dict(fac["cities"])["Донецк"] >= 8 and ("vk", 1) in fac["sources"]


# ------------------------------------------------------------------ контакты скрыты

def test_masks():
    assert vacancies.mask_phone("+79491234567") == "+7 949 ***-**-67"
    assert vacancies.mask_phone("8 (949) 123-45-67") == "+7 949 ***-**-67"
    assert vacancies.mask_email("boss@example.test") == "b***@example.test"
    assert vacancies.mask_nick("@kofe_uyut") == "@k***"
    out = vacancies.mask_text("Звоните +7 949 123-45-67 или boss@example.test, телеграм @kofe_uyut. Оплата 45000 руб.")
    assert "123-45" not in out and "boss@" not in out and "kofe_uyut" not in out and "45000" in out


def test_contact_items_never_mask_into_full():
    items = vacancies.contact_items({"phones": ["+79491234567"], "telegram": "@abc", "email": "a@b.ru"})
    assert [i["kind"] for i in items] == ["Телефон", "Telegram", "Почта"]
    assert all(i["masked"] != i["full"] for i in items)


# ------------------------------------------------------------------ карточка

def test_history_collects_created_complaints_and_audit(conn, mk):
    vid = mk(profession="бариста", created_at="2026-10-20 10:00:00")
    cid = complaints.add(conn, vid, "Мошенничество: просят деньги", code="fraud", now=datetime(2026, 10, 21, 9, 0, tzinfo=UTC))
    audit.log(conn, ADMIN, "vacancy.hide", "vacancy", vid, {"reason": "проверка"}, now=datetime(2026, 10, 22, 9, 0, tzinfo=UTC))
    audit.log(conn, ADMIN, "vacancy.restore", "vacancy", vid, now=datetime(2026, 10, 23, 9, 0, tzinfo=UTC))
    audit.log(conn, ADMIN, "vacancy.hide", "vacancy", vid + 100, now=NOW)  # чужая вакансия не попадает
    events = vacancies.history(conn, vacancies.get(conn, vid), NOW)
    titles = [e["title"] for e in events]
    assert titles.index("Возвращена в выдачу") < titles.index("Скрыта из выдачи") < titles.index(f"Жалоба №{cid}")
    assert "Возвращена в выдачу" in titles and "Скрыта из выдачи" in titles and f"Жалоба №{cid}" in titles
    assert "Добавлена в базу" in titles
    hide = next(e for e in events if e["title"] == "Скрыта из выдачи")
    assert hide["who"] == ADMIN["email"] and "проверка" in hide["text"]
    assert len([t for t in titles if t.startswith("Скрыта из")]) == 1


def test_history_sorted_newest_first(conn, mk):
    vid = mk(profession="a", created_at="2026-10-01 10:00:00", published_at="2026-10-01T09:00:00+00:00", last_seen_at="2026-10-01T09:00:00+00:00")
    audit.log(conn, ADMIN, "vacancy.hide", "vacancy", vid, now=datetime(2026, 10, 5, tzinfo=UTC))
    audit.log(conn, ADMIN, "vacancy.restore", "vacancy", vid, now=datetime(2026, 10, 9, tzinfo=UTC))
    events = vacancies.history(conn, vacancies.get(conn, vid), NOW)
    from rabota.lifecycle import parse_iso

    stamps = [parse_iso(e["at"]) for e in events]
    assert stamps == sorted(stamps, reverse=True)
    titles = [e["title"] for e in events]
    assert titles.index("Возвращена в выдачу") < titles.index("Скрыта из выдачи") < titles.index("Добавлена в базу")
    assert "Срок жизни истёк" in titles  # запись старая, сохранилась просроченной


def test_raw_post_is_found_and_masked(conn, mk):
    from rabota.db import save_raw

    save_raw(conn, RawPost(source_type="telegram", source="@demo", external_id="77", url="https://t.me/demo/77",
                           text="Нужен бариста. Звонить +7 949 123-45-67", published_at=fresh()))
    vid = mk(profession="бариста", external_id="77")
    raw = vacancies.raw_post(conn, vacancies.get(conn, vid))
    assert raw and "123-45" in raw["text"] and "123-45" not in raw["masked_text"]
    assert raw["url_safe"] == "https://t.me/demo/77"
    assert vacancies.raw_post(conn, vacancies.get(conn, mk(profession="x", external_id="404"))) is None


# ------------------------------------------------------------------ повтор

def test_mark_duplicate_hides_from_site_and_logs(conn, mk):
    main, dup = mk(profession="бариста"), mk(profession="бариста")
    assert dup in {r["id"] for r in conn.execute(f"SELECT id FROM vacancies WHERE {queries.VISIBLE_SQL}")}
    assert vacancies.mark_duplicate(conn, dup, main, ADMIN, "10.0.0.1", NOW) == main
    assert conn.execute("SELECT duplicate_of FROM vacancies WHERE id=?", (dup,)).fetchone()[0] == main
    assert dup not in {r["id"] for r in conn.execute(f"SELECT id FROM vacancies WHERE {queries.VISIBLE_SQL}")}
    log = audit.recent(conn)[0]
    assert log["action"] == "vacancy.duplicate" and log["target_id"] == str(dup) and log["ip"] == "10.0.0.1"
    assert log["actor_email"] == ADMIN["email"] and str(main) in log["details"]


def test_mark_duplicate_flattens_chains(conn, mk):
    a, b, c = mk(profession="a"), mk(profession="b"), mk(profession="c")
    vacancies.mark_duplicate(conn, b, a, ADMIN, now=NOW)
    # c повтор b: главной станет a
    assert vacancies.mark_duplicate(conn, c, b, ADMIN, now=NOW) == a
    # a теперь повтор d: b и c переезжают к d
    d = mk(profession="d")
    vacancies.mark_duplicate(conn, a, d, ADMIN, now=NOW)
    got = {r["id"]: r["duplicate_of"] for r in conn.execute("SELECT id, duplicate_of FROM vacancies")}
    assert got[a] == got[b] == got[c] == d and got[d] is None


@pytest.mark.parametrize("bad", ["self", "missing", "loop"])
def test_mark_duplicate_rejects_bad_targets(conn, mk, bad):
    a, b = mk(profession="a"), mk(profession="b")
    if bad == "loop":
        vacancies.mark_duplicate(conn, b, a, ADMIN, now=NOW)
    target = {"self": a, "missing": 9999, "loop": b}[bad]
    with pytest.raises(ValueError):
        vacancies.mark_duplicate(conn, a, target, ADMIN, now=NOW)
    assert conn.execute("SELECT duplicate_of FROM vacancies WHERE id=?", (a,)).fetchone()[0] is None
    assert not [r for r in audit.recent(conn) if r["target_id"] == str(a) and r["action"] == "vacancy.duplicate"]


def test_clear_duplicate(conn, mk):
    a, b = mk(profession="a"), mk(profession="b")
    assert vacancies.clear_duplicate(conn, b, ADMIN, now=NOW) is False
    vacancies.mark_duplicate(conn, b, a, ADMIN, now=NOW)
    assert vacancies.clear_duplicate(conn, b, ADMIN, "1.2.3.4", NOW) is True
    assert conn.execute("SELECT duplicate_of FROM vacancies WHERE id=?", (b,)).fetchone()[0] is None
    assert audit.recent(conn)[0]["action"] == "vacancy.unduplicate"
