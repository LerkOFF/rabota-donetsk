"""Настройки: config.py, проверка перед запуском, образец .env."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from rabota import config
from rabota.ops import preflight

ROOT = Path(__file__).resolve().parents[2]

GOOD = {
    "ENVIRONMENT": "production", "SITE_URL": "https://работа-донецк.рф", "WEB_TRUST_PROXY": "1",
    "SECRET_KEY": "k" * 40, "MAIL_BACKEND": "smtp", "SMTP_HOST": "smtp.mail.test", "SMTP_FROM": "noreply@rabota.test",
    "SMTP_USER": "u", "SMTP_PASSWORD": "p", "OPERATOR_NAME": "a", "OPERATOR_STATUS": "b", "OPERATOR_INN": "1",
    "OPERATOR_EMAIL": "e@e.test", "OPERATOR_ADDRESS": "c", "CONTACT_EMAIL": "c@e.test", "LLM_API_KEY": "x",
    "VK_ACCESS_TOKEN": "x", "TG_API_ID": "123", "TG_API_HASH": "h", "TELEGRAM_PROXY": "socks5://127.0.0.1:1080",
    "HEALTH_TOKEN": "t", "ALERT_TG_BOT_TOKEN": "b", "ALERT_TG_CHAT_ID": "1",
}


def errors(env):
    return {p.name for p in preflight.check(env) if p.level == preflight.ERROR}


def warns(env):
    return {p.name for p in preflight.check(env) if p.level == preflight.WARN}


def test_complete_production_env_has_no_problems():
    assert preflight.check(GOOD) == []


def test_empty_env_reports_the_main_errors():
    found = errors({})
    assert {"ENVIRONMENT", "SITE_URL", "WEB_TRUST_PROXY", "MAIL_BACKEND"} <= found


@pytest.mark.parametrize("change, expected", [
    ({"DEBUG": "1"}, "DEBUG"),
    ({"SITE_URL": "http://работа-донецк.рф"}, "SITE_URL"),
    ({"SITE_URL": "https://localhost"}, "SITE_URL"),
    ({"WEB_TRUST_PROXY": "0"}, "WEB_TRUST_PROXY"),
    ({"SECRET_KEY": "short"}, "SECRET_KEY"),
    ({"MAIL_BACKEND": "file"}, "MAIL_BACKEND"),
    ({"SMTP_HOST": ""}, "SMTP_HOST"),
    ({"SMTP_FROM": "noreply@example.ru"}, "SMTP_FROM"),
    ({"METRIKA_ID": "12x"}, "METRIKA_ID"),
    ({"WEBMASTER_VERIFY_FILE": "../x"}, "WEBMASTER_VERIFY_FILE"),
    ({"TELEGRAM_PROXY": "ftp://h:1"}, "TELEGRAM_PROXY"),
    ({"ALERT_TG_CHAT_ID": ""}, "ALERT_TG_BOT_TOKEN"),
    ({"TG_API_ID": "abc"}, "TG_API_ID"),
])
def test_each_breakage_is_an_error(change, expected):
    assert expected in errors({**GOOD, **change})


@pytest.mark.parametrize("drop, expected", [
    ("OPERATOR_INN", "OPERATOR_*"), ("LLM_API_KEY", "LLM_API_KEY"), ("VK_ACCESS_TOKEN", "VK_ACCESS_TOKEN"),
    ("TELEGRAM_PROXY", "TELEGRAM_PROXY"), ("HEALTH_TOKEN", "HEALTH_TOKEN"), ("SECRET_KEY", "SECRET_KEY"),
])
def test_missing_optional_parts_are_warnings(drop, expected):
    env = {k: v for k, v in GOOD.items() if k != drop}
    assert expected in warns(env) and expected not in errors(env)


def test_old_proxy_name_still_works():
    env = {k: v for k, v in GOOD.items() if k != "TELEGRAM_PROXY"}
    env["TG_PROXY"] = "socks5://10.0.0.1:1080"
    assert "TELEGRAM_PROXY" not in warns(env) | errors(env)


def test_problem_text_never_contains_secret_values():
    env = {**GOOD, "SECRET_KEY": "tiny-secret-value", "SMTP_PASSWORD": "pw-123456"}
    text = "\n".join(str(p) for p in preflight.check(env))
    assert "tiny-secret-value" not in text and "pw-123456" not in text


def test_config_helpers(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    assert config.environment() == "development" and not config.is_production()
    monkeypatch.setenv("ENVIRONMENT", " Production ")
    assert config.is_production()
    monkeypatch.setenv("DEBUG", "yes")
    assert config.debug()
    monkeypatch.setenv("DEBUG", "0")
    assert not config.debug()
    monkeypatch.setenv("SITE_URL", "https://x.test/")
    assert config.site_url() == "https://x.test"
    monkeypatch.setenv("METRIKA_ID", "123")
    assert config.metrika_id() == "123"
    monkeypatch.setenv("METRIKA_ID", "12;3")
    assert config.metrika_id() == ""
    monkeypatch.delenv("TG_PROXY", raising=False)
    monkeypatch.setenv("TELEGRAM_PROXY", " socks5://h:1 ")
    assert config.telegram_proxy() == "socks5://h:1"
    monkeypatch.delenv("TELEGRAM_PROXY")
    monkeypatch.setenv("TG_PROXY", "socks5://old:2")
    assert config.telegram_proxy() == "socks5://old:2"


def test_env_example_lists_every_variable_the_code_reads():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", example, re.MULTILINE))
    needed = {
        "ENVIRONMENT", "DEBUG", "SITE_URL", "SECRET_KEY", "WEB_TRUST_PROXY", "DB_PATH", "MAIL_BACKEND", "SMTP_HOST",
        "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_SECURITY", "VK_ACCESS_TOKEN", "LLM_API_KEY",
        "LLM_PROVIDER", "LLM_MODEL_VISION", "TG_API_ID", "TG_API_HASH", "TELEGRAM_PROXY", "MEDIA_DIR", "METRIKA_ID",
        "WEBMASTER_VERIFY_FILE", "HEALTH_TOKEN", "ALERT_TG_BOT_TOKEN", "ALERT_TG_CHAT_ID", "BACKUP_KEEP_DAYS",
        "OPERATOR_NAME", "OPERATOR_EMAIL", "LOG_FORMAT", "CONTACT_EMAIL",
    }
    assert needed <= declared, sorted(needed - declared)
    assert "DATABASE_URL" not in declared


def test_env_example_has_no_real_secrets():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        if re.match(r"^[A-Z_]*(TOKEN|SECRET|PASSWORD|KEY|HASH)[A-Z_]*=", line):
            assert line.split("=", 1)[1].strip() == "", line


def run_preflight(tmp_path, env_text, *args):
    env_file = tmp_path / ".env"
    env_file.write_text(env_text, encoding="utf-8")
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "preflight.py"), "--env-file", str(env_file), *args],
                          check=False, capture_output=True, text=True, cwd=ROOT, timeout=120)


def test_preflight_cli_fails_on_bad_env_and_prints_no_secret(tmp_path):
    r = run_preflight(tmp_path, "SECRET_KEY=abc\nSMTP_PASSWORD=supersecretvalue\n")
    assert r.returncode == 1 and "ошибка" in r.stdout
    assert "supersecretvalue" not in r.stdout + r.stderr


def test_preflight_cli_passes_on_good_env(tmp_path):
    text = "\n".join(f"{k}={v}" for k, v in GOOD.items()) + "\n"
    r = run_preflight(tmp_path, text)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "настройки в порядке" in r.stdout


def test_preflight_smoke_runs_app_without_network(tmp_path):
    text = "\n".join(f"{k}={v}" for k, v in GOOD.items()) + "\n"
    r = run_preflight(tmp_path, text, "--smoke")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "дымовая проверка приложения прошла" in r.stdout
