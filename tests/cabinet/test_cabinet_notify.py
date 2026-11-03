"""Правка вакансии в кабинете, ушедшая на повторную проверку, сообщает владельцу в Telegram."""
from __future__ import annotations

import pytest

from rabota import cabinet
from rabota.employers import notify
from tests.cabinet.conftest import NOW, make_row
from tests.cabinet.test_cabinet_service import vacancy


@pytest.fixture
def sent(monkeypatch):
    monkeypatch.setenv("MODERATION_TG_CHAT_ID", "123456")
    monkeypatch.setenv("MODERATION_TG_BOT_TOKEN", "1:token")
    monkeypatch.setenv("SITE_URL", "https://example.test")
    out: list[str] = []
    monkeypatch.setattr(notify, "sender", lambda token, chat, text: out.append(text) or True)
    monkeypatch.setattr(notify, "synchronous", True)
    return out


def test_edit_sent_to_review_notifies(conn, boss, sent):
    vid = make_row(conn, boss["id"])
    assert cabinet.update_vacancy(conn, boss["id"], vid, vacancy(pay_max="55000"), NOW) == "review"
    assert len(sent) == 1 and "отредактировали" in sent[0] and f"/admin/vacancies/{vid}" in sent[0]


def test_resubmitted_rejected_vacancy_notifies(conn, boss, sent):
    vid = make_row(conn, boss["id"], status="hidden", moderation="rejected", note="Нет контакта")
    assert cabinet.update_vacancy(conn, boss["id"], vid, vacancy(), NOW) == "resubmitted"
    assert len(sent) == 1 and "снова" in sent[0]


def test_harmless_edit_is_silent(conn, boss, sent):
    vid = make_row(conn, boss["id"])
    assert cabinet.update_vacancy(conn, boss["id"], vid, vacancy(schedule="5/2"), NOW) == "saved"
    assert sent == []
