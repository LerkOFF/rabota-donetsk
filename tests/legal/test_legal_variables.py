"""Данные оператора из окружения: подстановка, заглушки, предупреждения в логе."""
import logging

import pytest

from rabota import legal

ENV_NAMES = ("OPERATOR_NAME", "OPERATOR_STATUS", "OPERATOR_INN", "OPERATOR_EMAIL", "OPERATOR_ADDRESS", "SITE_URL", "SITE_NAME")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (*ENV_NAMES, "LEGAL_UPDATED_AT"):
        monkeypatch.delenv(name, raising=False)
    legal.reset_warnings()


def test_placeholders_when_nothing_is_set():
    v = legal.variables()
    for key in ("operator_name", "operator_status", "operator_inn", "operator_email", "operator_address"):
        assert v[key].startswith("[укажите ") and v[key].endswith("]")
    assert v["site_url"] == "https://работа-донецк.рф"
    assert v["site_name"] == "Работа Донецк"
    assert v["updated_at"] == legal.UPDATED_AT


def test_values_come_from_environment(monkeypatch):
    monkeypatch.setenv("OPERATOR_NAME", "Тестов Тест")
    monkeypatch.setenv("OPERATOR_STATUS", "самозанятый")
    monkeypatch.setenv("OPERATOR_INN", "000000000000")
    monkeypatch.setenv("OPERATOR_EMAIL", "legal@example.test")
    monkeypatch.setenv("OPERATOR_ADDRESS", "Тестовая улица, 1")
    monkeypatch.setenv("SITE_URL", "https://example.test/")
    monkeypatch.setenv("SITE_NAME", "Тест-сайт")
    monkeypatch.setenv("LEGAL_UPDATED_AT", "01.01.2030")
    v = legal.variables()
    assert v == {
        "operator_name": "Тестов Тест", "operator_status": "самозанятый", "operator_inn": "000000000000",
        "operator_email": "legal@example.test", "operator_address": "Тестовая улица, 1",
        "site_url": "https://example.test", "site_name": "Тест-сайт", "updated_at": "01.01.2030",
    }
    assert legal.missing_settings() == []


def test_blank_value_counts_as_missing(monkeypatch):
    monkeypatch.setenv("OPERATOR_EMAIL", "   ")
    assert legal.variables()["operator_email"].startswith("[укажите ")
    assert "OPERATOR_EMAIL" in legal.missing_settings()


def test_warning_logged_for_each_missing_variable_once(caplog):
    with caplog.at_level(logging.WARNING, logger="rabota.legal"):
        legal.variables()
        legal.variables()
    messages = [r.getMessage() for r in caplog.records]
    for name in ("OPERATOR_NAME", "OPERATOR_STATUS", "OPERATOR_INN", "OPERATOR_EMAIL", "OPERATOR_ADDRESS"):
        assert sum(name in m for m in messages) == 1


def test_no_warning_when_everything_is_set(monkeypatch, caplog):
    for name in ("OPERATOR_NAME", "OPERATOR_STATUS", "OPERATOR_INN", "OPERATOR_EMAIL", "OPERATOR_ADDRESS"):
        monkeypatch.setenv(name, "x")
    with caplog.at_level(logging.WARNING, logger="rabota.legal"):
        legal.variables()
    assert not caplog.records


def test_explicit_env_mapping_is_used():
    v = legal.variables({"OPERATOR_NAME": "Имя", "SITE_URL": "https://a.test"})
    assert v["operator_name"] == "Имя"
    assert v["site_url"] == "https://a.test"
