"""Автомодерация вакансий работодателей: оценка (чистая функция) и применение решения в базе."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from rabota.admin import stoplist
from rabota.db import connect
from rabota.employers import automod

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

CLEAN = {
    "profession": "бариста", "summary": "Готовить кофе и общаться с гостями, обучим с нуля.", "employer": "Кофейня Зерно",
    "pay_min": 30000, "pay_max": 40000, "pay_period": "месяц", "contacts": {"phones": ["+79490000077"]},
}


def vac(**over):
    return {**CLEAN, **over}


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO users(email, password_hash, role) VALUES ('o@example.test', '!', 'employer')")
    c.commit()
    yield c
    c.close()


def sl_with(conn, kind, value):
    stoplist.add(conn, kind, value, now=NOW)
    return stoplist.Stoplist.load(conn)


# ------------------------------------------------------------------ оценка

def test_clean_vacancy_goes_to_regular_queue():
    r = automod.assess(vac())
    assert r.decision == automod.QUEUE and r.flags == []


def test_verified_company_without_flags_is_approved():
    r = automod.assess(vac(), company={"verified_at": "2026-10-01T10:00:00+00:00"})
    assert r.decision == automod.APPROVE


def test_verified_company_with_flag_still_goes_to_review():
    r = automod.assess(vac(summary="Работа удалённо, пишите в ТГ https://t.me/job"), company={"verified_at": "2026-10-01"})
    assert r.decision == automod.REVIEW and "links" in r.flags


def test_blocked_company_is_rejected_even_if_verified():
    r = automod.assess(vac(), company={"verified_at": "2026-10-01", "blocked_at": "2026-10-02"})
    assert r.decision == automod.REJECT and r.flags == ["blocked_company"] and r.reject_text()


@pytest.mark.parametrize("kind,value,over", [
    ("phone", "+7 949 000-00-77", {}),
    ("employer", "Кофейня Зерно", {}),
    ("nick", "@scammer_tg", {"contacts": {"telegram": "@scammer_tg"}}),
])
def test_stoplist_contacts_reject(conn, kind, value, over):
    r = automod.assess(vac(**over), sl=sl_with(conn, kind, value))
    assert r.decision == automod.REJECT and r.flags == ["stoplist_contact"]
    assert r.stop_entry_id and r.reasons


def test_stoplist_keyword_goes_to_review(conn):
    r = automod.assess(vac(summary="Лёгкий заработок на дому"), sl=sl_with(conn, "keyword", "лёгкий заработок"))
    assert r.decision == automod.REVIEW and r.flags == ["stoplist_keyword"]


@pytest.mark.parametrize("text", [
    "Требуется закладчик, высокая оплата", "Нужен дроппер для карт", "Интим услуги и эскорт",
])
def test_forbidden_content_is_rejected(text):
    r = automod.assess(vac(summary=text))
    assert r.decision == automod.REJECT and "forbidden" in r.flags


@pytest.mark.parametrize("text", [
    "Нужна предоплата за оформление", "Вступительный взнос 5000 рублей", "Платное обучение, потом работа",
    "Нужно внести залог за форму", "Стартовый взнос обязателен",
])
def test_money_demands_go_to_review(text):
    r = automod.assess(vac(summary=text))
    assert r.decision == automod.REVIEW and "money" in r.flags


def test_normal_text_has_no_money_flag():
    assert "money" not in automod.assess(vac(summary="Оплата два раза в месяц, оформление по ТК, обучение за счёт работодателя")).flags


@pytest.mark.parametrize("period,pay,high", [
    ("месяц", 250000, True), ("месяц", 120000, False), ("день", 15000, True), ("смена", 3000, False),
    ("час", 3000, True), ("", 900000, False),
])
def test_high_pay_flag(period, pay, high):
    assert automod.pay_is_high(vac(pay_min=pay, pay_max=None, pay_period=period)) is high


def test_links_and_caps_and_flood_flags():
    assert "links" in automod.assess(vac(summary="Подробности на www.example.test")).flags
    assert "links" in automod.assess(vac(summary="Пишите в лс по вакансии")).flags
    shout = "СРОЧНО ТРЕБУЮТСЯ СОТРУДНИКИ НА СКЛАД ВЫСОКАЯ ОПЛАТА ЗВОНИТЕ СЕЙЧАС"
    assert automod.caps_ratio(shout) > 0.9 and "caps" in automod.assess(vac(summary=shout)).flags
    assert automod.caps_ratio("КОРОТКО") == 0.0  # короткий текст не оцениваем
    assert "flood" in automod.assess(vac(), recent_posts=6).flags
    assert "flood" not in automod.assess(vac(), recent_posts=5).flags


def test_flag_labels_and_loading():
    assert automod.flag_labels('["money", "links"]') == [automod.FLAG_LABELS["money"], automod.FLAG_LABELS["links"]]
    assert automod.load_flags(None) == [] and automod.load_flags("не json") == [] and automod.load_flags('{"a": 1}') == []
    assert automod.load_flags(["x"]) == ["x"]


# ------------------------------------------------------------------ применение в базе

def add_vacancy(conn, owner=1, company=None, **over):
    from rabota.models import Vacancy
    from rabota.web import queries as q

    fields = {"profession": "бариста", "city": "Донецк", "category": "общепит", "summary": CLEAN["summary"],
              "employer": "Кофейня Зерно", "contacts": {"phones": ["+79490000077"]}, "source_type": "site",
              "source": "razmestit", "pay_min": 30000, "pay_max": 40000, "pay_period": "месяц", **over}
    return q.add_site_vacancy(conn, Vacancy(**fields), premoderate=True, owner_user_id=owner, company_id=company)


def row(conn, vid):
    return conn.execute("SELECT * FROM vacancies WHERE id = ?", (vid,)).fetchone()


def test_moderate_new_queues_clean_vacancy(conn):
    vid = add_vacancy(conn)
    result = automod.moderate_new(conn, vid, NOW)
    r = row(conn, vid)
    assert result.decision == automod.QUEUE
    assert (r["moderation_status"], r["status"], r["automod_flags"]) == ("pending", "hidden", None)


def test_moderate_new_rejects_and_counts_stoplist_hit(conn):
    stoplist.add(conn, "phone", "+79490000077", now=NOW)
    vid = add_vacancy(conn)
    automod.moderate_new(conn, vid, NOW)
    r = row(conn, vid)
    assert (r["moderation_status"], r["status"]) == ("rejected", "hidden") and r["moderation_note"]
    assert json.loads(r["automod_flags"]) == ["stoplist_contact"]
    assert conn.execute("SELECT hits FROM stoplist").fetchone()[0] == 1
    assert conn.execute("SELECT 1 FROM audit_log WHERE action = 'vacancy.automod_reject'").fetchone()


def test_moderate_new_review_keeps_in_queue_with_flags(conn):
    vid = add_vacancy(conn, summary="Нужна предоплата за оформление, работа простая")
    result = automod.moderate_new(conn, vid, NOW)
    r = row(conn, vid)
    assert result.decision == automod.REVIEW and r["moderation_status"] == "pending"
    assert json.loads(r["automod_flags"]) == ["money"]
    assert conn.execute("SELECT 1 FROM audit_log WHERE action = 'vacancy.automod_review'").fetchone()


def test_moderate_new_approves_for_verified_company(conn):
    conn.execute("INSERT INTO companies(owner_user_id, name, verified_at) VALUES (1, 'Кофейня Зерно', '2026-10-01T10:00:00+00:00')")
    conn.commit()
    vid = add_vacancy(conn, company=1)
    automod.moderate_new(conn, vid, NOW)
    r = row(conn, vid)
    assert (r["moderation_status"], r["status"]) == ("approved", "active")
    assert conn.execute("SELECT 1 FROM audit_log WHERE action = 'vacancy.automod_approve'").fetchone()


def test_moderate_new_blocked_company_rejects(conn):
    conn.execute("INSERT INTO companies(owner_user_id, name, blocked_at) VALUES (1, 'Кофейня Зерно', '2026-10-01T10:00:00+00:00')")
    conn.commit()
    vid = add_vacancy(conn, company=1)
    automod.moderate_new(conn, vid, NOW)
    assert row(conn, vid)["moderation_status"] == "rejected"


def test_flood_counts_vacancies_of_last_day_only(conn):
    ids = [add_vacancy(conn, profession=f"кассир{i}") for i in range(7)]
    assert automod.recent_posts(conn, 1, NOW, exclude_id=ids[-1]) == 6
    assert automod.recent_posts(conn, None, NOW) == 0
    last = automod.moderate_new(conn, ids[-1], NOW)
    assert "flood" in last.flags


def test_without_premoderation_only_flags_are_written(conn):
    vid = add_vacancy(conn, summary="Нужна предоплата за оформление, работа простая")
    conn.execute("UPDATE vacancies SET moderation_status='approved', status='active' WHERE id = ?", (vid,))
    conn.commit()
    automod.moderate_new(conn, vid, NOW, premoderate=False)
    r = row(conn, vid)
    assert (r["moderation_status"], r["status"]) == ("approved", "active") and json.loads(r["automod_flags"]) == ["money"]


def test_missing_vacancy_is_ignored(conn):
    assert automod.moderate_new(conn, 999, NOW).decision == automod.QUEUE
