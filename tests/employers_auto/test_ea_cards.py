"""Автоматические карточки работодателей: создание, дедупликация, отсев, пробный прогон, повторный запуск."""
from __future__ import annotations

import sqlite3

import pytest

from rabota import __main__ as cli
from rabota.db import connect
from rabota.employers_auto import cards
from tests.employers_auto.kit import NOW, auto_company, raw_post, vacancy


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "ea.db")
    yield c
    c.close()


def rows(conn, sql, *args):
    return conn.execute(sql, args).fetchall()


def test_attach_creates_card_and_links_vacancy(conn):
    vid = vacancy(conn, "ООО «Кофейня Зерно»")
    report = cards.attach_pending(conn, now=NOW)
    assert (report.created, report.linked) == (1, 0)
    v = rows(conn, "SELECT * FROM vacancies WHERE id=?", vid)[0]
    assert v["employer"] == "ООО «Кофейня Зерно»", "название в вакансии не перезаписывается"
    c = cards.get(conn, v["company_id"])
    assert c["origin"] == "auto" and c["owner_user_id"] is None
    assert c["legal_form"] == "ООО" and c["norm_name"] == "кофейня зерно"
    assert c["city"] == "Донецк" and c["first_seen_at"] and c["last_vacancy_at"]
    assert v["company_state"] == "linked"


def test_same_company_in_different_spellings_is_one_card(conn):
    ids = [vacancy(conn, n) for n in ("ООО «Кофейня Зерно»", "кофейня  зерно", 'ООО "Кофейня Зерно"', "Кофейня Зёрно")]
    report = cards.attach_pending(conn, now=NOW)
    assert report.created == 1 and report.linked == 3
    assert len({r[0] for r in rows(conn, f"SELECT company_id FROM vacancies WHERE id IN ({','.join('?' * 4)})", *ids)}) == 1
    assert rows(conn, "SELECT COUNT(*) FROM companies")[0][0] == 1


def test_same_name_in_other_city_is_other_card(conn):
    vacancy(conn, "Кофейня Зерно", city="Донецк")
    vacancy(conn, "Кофейня Зерно", city="Макеевка")
    assert cards.attach_pending(conn, now=NOW).created == 2


def test_card_without_city_is_found_and_enriched_later(conn):
    vacancy(conn, "Пекарня Хлеб", city=None)
    cards.attach_pending(conn, now=NOW)
    first = rows(conn, "SELECT id, city FROM companies")[0]
    assert first["city"] is None
    vacancy(conn, "Пекарня Хлеб", city="Донецк")
    report = cards.attach_pending(conn, now=NOW)
    assert report.created == 0
    assert rows(conn, "SELECT COUNT(*) FROM companies")[0][0] == 1


def test_hh_employer_id_wins_over_name(conn):
    a = vacancy(conn, "Банк ПСБ, Старт карьеры", source_type="hh", source="hh.ru", external_id="1")
    b = vacancy(conn, "ПСБ Банк", source_type="hh", source="hh.ru", external_id="2")
    raw_post(conn, "hh", "hh.ru", "1", {"company": {"id": 777, "name": "Банк ПСБ"}})
    raw_post(conn, "hh", "hh.ru", "2", {"company": {"id": 777, "name": "Банк ПСБ"}})
    report = cards.attach_pending(conn, now=NOW)
    assert report.created == 1 and report.linked == 1
    ids = {r[0] for r in rows(conn, "SELECT company_id FROM vacancies WHERE id IN (?,?)", a, b)}
    assert len(ids) == 1
    c = cards.get(conn, ids.pop())
    assert c["name"] == "Банк ПСБ" and c["source_ref"] == "hh:777" and c["source_kind"] == "hh"


def test_trudvsem_inn_is_reference(conn):
    vacancy(conn, "МУП Горсвет", source_type="trudvsem", source="trudvsem", external_id="9")
    raw_post(conn, "trudvsem", "trudvsem", "9", {"company": {"name": "МУП Горсвет", "inn": "9300000001"}})
    cards.attach_pending(conn, now=NOW)
    assert rows(conn, "SELECT source_ref FROM companies")[0][0] == "inn:9300000001"


@pytest.mark.parametrize("name,reason", [
    ("Иван Иванов", "person"), ("Бариста", "profession"), ("не указано", "placeholder"), ("Аб", "short"),
])
def test_skipped_names_leave_no_card(conn, name, reason):
    vid = vacancy(conn, name)
    report = cards.attach_pending(conn, now=NOW)
    assert report.created == 0 and report.skipped[reason] == 1
    v = rows(conn, "SELECT company_id, company_state FROM vacancies WHERE id=?", vid)[0]
    assert v["company_id"] is None and v["company_state"] == f"skip:{reason}"
    assert rows(conn, "SELECT COUNT(*) FROM companies")[0][0] == 0


def test_vacancy_without_employer_is_not_touched(conn):
    vid = vacancy(conn, None)
    assert cards.attach_pending(conn, now=NOW).scanned == 0
    assert rows(conn, "SELECT company_state FROM vacancies WHERE id=?", vid)[0][0] is None


def test_direct_vacancies_are_not_auto_linked(conn):
    vid = vacancy(conn, "Кофейня Зерно")
    conn.execute("UPDATE vacancies SET origin='direct' WHERE id=?", (vid,))
    conn.commit()
    assert cards.attach_pending(conn, now=NOW).scanned == 0


def test_unique_index_blocks_second_auto_card(conn):
    auto_company(conn, "Кофейня Зерно")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO companies(name, city, origin, norm_name) VALUES ('Кофейня Зерно','Донецк','auto','кофейня зерно')")


def test_merged_card_is_followed(conn):
    main = auto_company(conn, "Кофейня Зерно")
    dup = auto_company(conn, "Зерно Кофейня", source="@other")
    conn.execute("UPDATE companies SET merged_into=?, norm_name='зерно кофейня' WHERE id=?", (main, dup))
    conn.commit()
    vid = vacancy(conn, "Зерно Кофейня")
    cards.attach_pending(conn, now=NOW)
    assert rows(conn, "SELECT company_id FROM vacancies WHERE id=?", vid)[0][0] == main


def test_registered_company_is_not_taken_over_by_name(conn):
    """Совпадение названия не даёт права на чужую карточку: автокарточка отдельная, дубль решает администратор."""
    conn.execute("INSERT INTO users(email, password_hash, role) VALUES ('b@example.test','x','employer')")
    uid = conn.execute("SELECT id FROM users").fetchone()[0]
    conn.execute("INSERT INTO companies(owner_user_id, name, city, origin, norm_name) "
                 "VALUES (?, 'Кофейня Зерно', 'Донецк', 'registered', 'кофейня зерно')", (uid,))
    conn.commit()
    vid = vacancy(conn, "Кофейня Зерно")
    cards.attach_pending(conn, now=NOW)
    v = rows(conn, "SELECT company_id, owner_user_id FROM vacancies WHERE id=?", vid)[0]
    assert v["owner_user_id"] is None
    assert cards.get(conn, v["company_id"])["origin"] == "auto"
    assert rows(conn, "SELECT COUNT(*) FROM companies")[0][0] == 2


def test_backfill_report_dry_run_and_idempotence(conn):
    vacancy(conn, "Кофейня Зерно")
    vacancy(conn, "Кофейня Зерно")
    vacancy(conn, "Иван Иванов")
    dry = cards.backfill(conn, dry_run=True, now=NOW)
    assert dry.dry_run and dry.created == 1 and dry.linked == 1 and dry.skipped["person"] == 1
    assert rows(conn, "SELECT COUNT(*) FROM companies")[0][0] == 0
    assert rows(conn, "SELECT COUNT(*) FROM vacancies WHERE company_id IS NOT NULL")[0][0] == 0
    real = cards.backfill(conn, now=NOW)
    assert (real.created, real.linked, real.skipped["person"]) == (dry.created, dry.linked, 1)
    again = cards.backfill(conn, now=NOW)
    assert again.scanned == 0 and again.created == 0
    assert rows(conn, "SELECT COUNT(*) FROM companies")[0][0] == 1


def test_recheck_retries_skipped(conn):
    vid = vacancy(conn, "Кофейня Зерно")
    conn.execute("UPDATE vacancies SET company_state='skip:garbage' WHERE id=?", (vid,))
    conn.commit()
    assert cards.backfill(conn, now=NOW).scanned == 0
    report = cards.backfill(conn, now=NOW, recheck=True)
    assert report.created == 1


def test_cli_backfill_prints_report(conn, capsys):
    vacancy(conn, "Кофейня Зерно")
    vacancy(conn, "Бариста")
    assert cli.cmd_employers_backfill(conn, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "пробный прогон" in out and "новых карточек: 1" in out and "пропущено вакансий: 1" in out
    assert rows(conn, "SELECT COUNT(*) FROM companies")[0][0] == 0
    cli.cmd_employers_backfill(conn, dry_run=False)
    assert rows(conn, "SELECT COUNT(*) FROM companies")[0][0] == 1


def test_disabled_setting_is_readable(conn):
    assert cards.enabled(conn) is True


def test_counts_and_refresh_dates(conn):
    cid = auto_company(conn, "Кофейня Зерно", count=2)
    vid = rows(conn, "SELECT id FROM vacancies WHERE company_id=?", cid)[0][0]
    conn.execute("UPDATE vacancies SET status='archived' WHERE id=?", (vid,))
    conn.commit()
    assert cards.counts(conn, cid) == {"visible": 1, "archived": 1, "total": 2}


def test_old_database_gets_nullable_owner_and_columns(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT);
    CREATE TABLE companies (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      name TEXT NOT NULL, city TEXT, created_at TEXT, verified INTEGER NOT NULL DEFAULT 0
    );
    INSERT INTO users(email) VALUES ('a@example.test');
    INSERT INTO companies(owner_user_id, name, city) VALUES (1, 'Старая', 'Донецк');
    """)
    db.commit()
    db.close()
    for _ in range(2):  # повторный запуск безопасен
        c = connect(path)
        cols = {r[1]: r for r in c.execute("PRAGMA table_info(companies)")}
        assert cols["owner_user_id"][3] == 0
        assert {"origin", "norm_name", "legal_form", "source_kind", "source_ref", "first_seen_at", "last_vacancy_at", "claimed_at"} <= set(cols)
        old = c.execute("SELECT name, origin, owner_user_id FROM companies").fetchone()
        assert tuple(old) == ("Старая", "registered", 1)
        c.execute("INSERT INTO companies(owner_user_id, name, origin) VALUES (NULL, 'Авто', 'auto')")
        c.rollback()
        c.close()
