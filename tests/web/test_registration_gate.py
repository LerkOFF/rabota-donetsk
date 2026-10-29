"""Флаг REGISTRATION_OPEN: пока он выключен, аккаунты не создаются, остальной сайт работает."""
import pytest

from tests.accounts.harness import env_impl, post, reg_data

env = pytest.fixture(env_impl)


def test_registration_is_open_by_default(env):
    r = env.client.get("/register")
    assert 'name="email"' in r.text


def test_closed_registration_shows_notice_instead_of_form(env, monkeypatch):
    monkeypatch.setenv("REGISTRATION_OPEN", "0")
    r = env.client.get("/register")
    assert r.status_code == 200
    assert 'name="email"' not in r.text and 'name="password"' not in r.text
    assert "Регистрация откроется позже" in r.text
    assert "noindex" in r.text


def test_closed_registration_does_not_create_accounts(env, monkeypatch):
    token_page = env.client.get("/register").text  # форма с токеном, пока регистрация открыта
    assert 'name="csrf_token"' in token_page
    monkeypatch.setenv("REGISTRATION_OPEN", "0")
    r = post(env, "/register", reg_data("closed@example.test"), page="/login")
    assert r.status_code == 403
    assert env.user("closed@example.test") is None
    assert env.mailer.outbox == []


def test_login_still_works_when_registration_is_closed(env, monkeypatch):
    monkeypatch.setenv("REGISTRATION_OPEN", "0")
    r = env.client.get("/login")
    assert r.status_code == 200 and 'name="password"' in r.text
