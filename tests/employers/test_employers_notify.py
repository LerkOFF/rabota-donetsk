"""Уведомление владельцу в Telegram о вакансии, ожидающей модерации."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rabota.admin import stoplist
from rabota.db import connect
from rabota.employers import automod, notify

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO users(email, password_hash, role) VALUES ('o@example.test', '!', 'employer')")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def sent(monkeypatch):
    """Включает уведомления и собирает отправленное вместо запроса к Telegram."""
    for name in ("MODERATION_TG_BOT_TOKEN", "ALERT_TG_BOT_TOKEN", "TG_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MODERATION_TG_CHAT_ID", "123456")
    monkeypatch.setenv("MODERATION_TG_BOT_TOKEN", "1:token")
    monkeypatch.setenv("SITE_URL", "https://example.test")
    out: list[tuple[str, str, str]] = []
    monkeypatch.setattr(notify, "sender", lambda token, chat, text: out.append((token, chat, text)) or True)
    monkeypatch.setattr(notify, "synchronous", True)
    return out


def add_vacancy(conn, owner=1, company=None, premoderate=True, **over):
    from rabota.models import Vacancy
    from rabota.web import queries as q

    fields = {"profession": "бариста", "city": "Донецк", "category": "общепит", "summary": "Готовить кофе, обучим с нуля.",
              "employer": "Кофейня Зерно", "contacts": {"phones": ["+79490000077"]}, "source_type": "site",
              "source": "razmestit", "pay_min": 30000, "pay_max": 40000, "pay_period": "месяц", **over}
    return q.add_site_vacancy(conn, Vacancy(**fields), premoderate=premoderate, owner_user_id=owner, company_id=company)


def test_new_vacancy_in_queue_notifies_owner(conn, sent):
    vid = add_vacancy(conn)
    automod.moderate_new(conn, vid, NOW)
    assert len(sent) == 1
    token, chat, text = sent[0]
    assert (token, chat) == ("1:token", "123456")
    assert f"#{vid}" in text and "бариста, Кофейня Зерно" in text and "30000-40000 ₽ в месяц" in text
    assert text.endswith(f"https://example.test/admin/vacancies/{vid}")


def test_review_flags_are_in_message(conn, sent):
    vid = add_vacancy(conn, summary="Нужна предоплата за оформление")
    automod.moderate_new(conn, vid, NOW)
    assert "Пометки: Просьба заплатить или внести деньги" in sent[0][2]


def test_rejected_by_automod_is_silent(conn, sent):
    stoplist.add(conn, "phone", "+79490000077", now=NOW)
    vid = add_vacancy(conn)
    automod.moderate_new(conn, vid, NOW)
    assert sent == []


def test_auto_approved_is_silent(conn, sent):
    conn.execute("INSERT INTO companies(owner_user_id, name, verified_at) VALUES (1, 'Зерно', '2026-10-01T10:00:00+00:00')")
    conn.commit()
    vid = add_vacancy(conn, company=1)
    automod.moderate_new(conn, vid, NOW)
    assert sent == []


def test_without_premoderation_is_silent(conn, sent):
    vid = add_vacancy(conn, premoderate=False)
    automod.moderate_new(conn, vid, NOW, premoderate=False)
    assert sent == []


def test_disabled_without_chat_or_token(conn, sent, monkeypatch):
    monkeypatch.delenv("MODERATION_TG_CHAT_ID")
    vid = add_vacancy(conn)
    automod.moderate_new(conn, vid, NOW)
    assert sent == []
    monkeypatch.setenv("MODERATION_TG_CHAT_ID", "123456")
    monkeypatch.delenv("MODERATION_TG_BOT_TOKEN")
    assert notify.moderation_pending(conn, vid) is False


def test_token_falls_back_to_alert_then_site_bot(monkeypatch):
    monkeypatch.setenv("MODERATION_TG_CHAT_ID", "5")
    monkeypatch.delenv("MODERATION_TG_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TG_BOT_TOKEN", "site")
    assert notify.settings() == ("site", "5")
    monkeypatch.setenv("ALERT_TG_BOT_TOKEN", "alert")
    assert notify.settings() == ("alert", "5")


def test_sender_failure_does_not_break_moderation(conn, sent, monkeypatch):
    def boom(token, chat, text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(notify, "sender", boom)
    vid = add_vacancy(conn)
    automod.moderate_new(conn, vid, NOW)
    assert conn.execute("SELECT moderation_status FROM vacancies WHERE id = ?", (vid,)).fetchone()[0] == "pending"
