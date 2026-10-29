"""Файлы для сервера: юниты systemd, vhost nginx, fail2ban, logrotate; правила репозитория."""
from __future__ import annotations

import configparser
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
DOMAIN = "работа-донецк.рф"
PUNY = DOMAIN.encode("idna").decode()
PLACEHOLDERS = {"<USER>", "<PROJECT_DIR>", "<SSH_KEY>", "<PROXY_USER>", "<PROXY_HOST>"}
UNITS = sorted(DEPLOY.glob("*.service")) + sorted(DEPLOY.glob("*.timer"))


def parse_unit(path: Path) -> dict[str, dict[str, list[str]]]:
    sections: dict[str, dict[str, list[str]]] = {}
    current = None
    text = path.read_text(encoding="utf-8").replace("\\\n", " ")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = sections.setdefault(line.strip("[]"), {})
        elif "=" in line and current is not None:
            k, v = line.split("=", 1)
            current.setdefault(k, []).append(v)
    return sections


def test_punycode_domain_is_right():
    assert PUNY == "xn----7sbadrg9awfdp1a2d.xn--p1ai"
    for path in (DEPLOY / "nginx-rabota.conf", ROOT / "scripts" / "smoke.sh", ROOT / "scripts" / "lib-deploy.sh"):
        assert PUNY in path.read_text(encoding="utf-8"), path.name


@pytest.mark.parametrize("path", UNITS, ids=lambda p: p.name)
def test_units_parse_and_use_only_known_placeholders(path):
    text = path.read_text(encoding="utf-8")
    assert set(re.findall(r"<[A-Z_]+>", text)) <= PLACEHOLDERS
    units = parse_unit(path)
    assert "Unit" in units
    if path.suffix == ".service":
        assert "Service" in units
    else:
        assert "Timer" in units and "Install" in units


SANDBOX = ["NoNewPrivileges", "PrivateTmp", "ProtectSystem", "RestrictAddressFamilies"]


@pytest.mark.parametrize("name", ["rabota-web.service", "rabota-worker.service", "rabota-backup.service",
                                  "rabota-alert.service", "rabota-expire.service"])
def test_services_are_sandboxed_and_run_unprivileged(name):
    svc = parse_unit(DEPLOY / name)["Service"]
    assert svc["User"] == ["<USER>"]
    assert svc["NoNewPrivileges"] == ["true"] and svc["PrivateTmp"] == ["true"]
    assert svc["ProtectSystem"] == ["strict"]
    assert any("data" in p for p in svc["ReadWritePaths"])
    assert "root" not in svc["User"][0]


def test_web_service_matches_the_server_plan():
    svc = parse_unit(DEPLOY / "rabota-web.service")["Service"]
    cmd = svc["ExecStart"][0]
    assert "uvicorn rabota.web.app:app" in cmd and "--host 127.0.0.1" in cmd and "--port 8000" in cmd
    assert "--workers 1" in cmd and "--proxy-headers" in cmd and "--forwarded-allow-ips 127.0.0.1" in cmd
    assert "--log-config" in cmd
    assert svc["MemoryMax"] == ["350M"] and svc["Restart"] == ["always"]
    assert "LOG_FORMAT=json" in svc["Environment"]
    assert any("media" in p for p in svc["ReadWritePaths"])


def test_worker_has_memory_cap_and_telegram_note():
    svc = parse_unit(DEPLOY / "rabota-worker.service")["Service"]
    assert svc["MemoryMax"] == ["450M"] and svc["KillSignal"] == ["SIGTERM"]
    assert "TELEGRAM_PROXY" in (DEPLOY / "rabota-worker.service").read_text(encoding="utf-8")


def test_timers_point_to_existing_services():
    for timer in DEPLOY.glob("*.timer"):
        target = parse_unit(timer)["Timer"]["Unit"][0]
        assert (DEPLOY / target).exists(), f"{timer.name} -> {target}"


def test_backup_timer_runs_daily_and_service_verifies_the_copy():
    timer = parse_unit(DEPLOY / "rabota-backup.timer")["Timer"]
    assert "Persistent" in timer and timer["Persistent"] == ["true"]
    assert "03:30" in timer["OnCalendar"][0]
    svc = parse_unit(DEPLOY / "rabota-backup.service")["Service"]
    assert svc["ExecStart"][0].endswith("scripts/backup.sh")
    assert "verify-backup.sh" in svc["ExecStartPost"][0]


def test_uvicorn_log_config_is_valid_and_json():
    cfg = json.loads((DEPLOY / "uvicorn-log.json").read_text(encoding="utf-8"))
    import logging.config

    names = ["uvicorn", "uvicorn.error", "uvicorn.access", "rabota"]
    saved = {n: (list(logging.getLogger(n).handlers), logging.getLogger(n).propagate, logging.getLogger(n).level) for n in names}
    try:
        logging.config.dictConfig(cfg)
        assert cfg["formatters"]["json"]["()"] == "rabota.ops.logs.JsonFormatter"
    finally:  # настройка не должна влиять на остальные тесты
        for n, (handlers, propagate, level) in saved.items():
            lg = logging.getLogger(n)
            lg.handlers[:] = handlers
            lg.propagate = propagate
            lg.setLevel(level)


# ------------------------------------------------------------------ nginx

NGINX = (DEPLOY / "nginx-rabota.conf").read_text(encoding="utf-8")


def nginx_code() -> str:
    return "\n".join(line for line in NGINX.splitlines() if not line.strip().startswith("#"))


def test_nginx_matches_xray_fallback_scheme():
    code = nginx_code()
    assert code.count("listen 127.0.0.1:8081 proxy_protocol http2;") == 2
    assert code.count("listen 127.0.0.1:8082 proxy_protocol;") == 2
    assert code.count("set_real_ip_from 127.0.0.1;") == 2 and code.count("real_ip_header proxy_protocol;") == 2
    assert "listen 80;" in code and "/.well-known/acme-challenge/" in code and "root /var/www/acme;" in code
    assert "default_server" not in code and "ssl_certificate" not in code and "listen 443" not in code
    assert f"return 301 https://{PUNY}$request_uri;" in code
    assert f"www.{PUNY}" in code
    assert "proxy_pass http://rabota_app;" in code and "server 127.0.0.1:8000;" in code


def test_nginx_does_not_conflict_with_other_vhosts():
    code = nginx_code()
    assert "gzip" not in code and "brotli" not in code, "сжатие включено глобально на сервере"
    for zone in re.findall(r"zone=(\w+):", code):
        assert zone.startswith("rabota_"), zone
    for name in re.findall(r"^map \S+ (\$\w+)", code, re.MULTILINE) + re.findall(r"^upstream (\w+)", code, re.MULTILINE):
        assert name.lstrip("$").startswith("rabota_"), name
    assert "server_name" in code and "_;" not in code


def test_nginx_rate_limits_cover_forms_and_login():
    code = nginx_code()
    assert "limit_req_status 429;" in code
    for zone in ("rabota_auth", "rabota_forms", "rabota_pages", "rabota_health"):
        assert f"zone={zone}:" in code and code.count(f"zone={zone} ") >= 1
    auth_loc = re.search(r"location ~ \^/\(([^)]*)\)", code).group(1)
    for path in ("login", "register", "forgot", "reset/"):
        assert path in auth_loc
    assert "$request_method $rabota_post_key" in code and "POST    $binary_remote_addr" in code


def test_nginx_static_media_admin_and_errors():
    code = nginx_code()
    assert "alias <PROJECT_DIR>/rabota/web/static/;" in code
    assert "$rabota_static_cache" in code and "immutable" in code
    assert "alias <PROJECT_DIR>/media/;" in code and "data/media" not in code.replace("Копии фото Telegram (data/media)", "")
    assert "location ^~ /admin" in code
    assert "error_page 502 503 504 /rabota-50x.html;" in code
    assert (DEPLOY / "errors" / "50x.html").exists()
    assert "location ~ /\\. {" in code and "return 404;" in code
    assert "autoindex off;" in code


def test_nginx_proxy_headers_are_safe():
    code = nginx_code()
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in code
    assert "$proxy_add_x_forwarded_for" not in code
    assert "proxy_set_header X-Forwarded-Proto https;" in code
    assert "proxy_set_header Host $host;" in code
    assert code.count("Strict-Transport-Security") >= 3
    assert "proxy_hide_header Strict-Transport-Security;" in code


def test_nginx_braces_balance():
    code = nginx_code()
    assert code.count("{") == code.count("}")


def test_error_page_has_both_themes_and_no_external_resources():
    html = (DEPLOY / "errors" / "50x.html").read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in html and "viewport" in html
    assert not re.search(r"(src|href)=\"https?://", html)


@pytest.mark.skipif(shutil.which("nginx") is None, reason="nginx не установлен: конфигурация проверена только текстовыми тестами")
def test_nginx_t_passes(tmp_path):
    (tmp_path / "static").mkdir()
    conf = tmp_path / "nginx.conf"
    site = NGINX.replace("<PROJECT_DIR>", str(tmp_path))
    conf.write_text(
        f"events {{}}\nerror_log {tmp_path}/e.log;\npid {tmp_path}/n.pid;\n"
        f"http {{\n  access_log off;\n  client_body_temp_path {tmp_path}/b;\n  proxy_temp_path {tmp_path}/p;\n"
        f"  fastcgi_temp_path {tmp_path}/f;\n  uwsgi_temp_path {tmp_path}/u;\n  scgi_temp_path {tmp_path}/s;\n"
        f"{site}\n}}\n", encoding="utf-8")
    r = subprocess.run(["nginx", "-t", "-c", str(conf)], check=False, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ------------------------------------------------------------------ fail2ban, logrotate

def test_fail2ban_filter_matches_nginx_limit_line():
    text = (DEPLOY / "fail2ban" / "filter.d" / "rabota-limit.conf").read_text(encoding="utf-8")
    regex = re.search(r"^failregex = (.+)$", text, re.MULTILINE).group(1).replace("<HOST>", r"(?P<host>[\d.]+)")
    line = ('2026/10/29 12:00:00 [error] 1#1: *5 limiting requests, excess: 5.480 by zone "rabota_auth", '
            'client: 203.0.113.7, server: x, request: "POST /login HTTP/1.1"')
    assert re.search(regex, line).group("host") == "203.0.113.7"
    assert not re.search(regex, line.replace("rabota_auth", "other_zone"))
    jail = configparser.ConfigParser()
    jail.read(DEPLOY / "fail2ban" / "jail.d" / "rabota.conf", encoding="utf-8")
    assert jail["rabota-limit"]["filter"] == "rabota-limit" and jail["rabota-limit"]["enabled"] == "false"


def test_logrotate_config():
    text = (DEPLOY / "logrotate-rabota").read_text(encoding="utf-8")
    assert "/var/log/rabota/*.log" in text and "rotate 8" in text and "compress" in text


# ------------------------------------------------------------------ правила репозитория

PUBLIC_FILES = (
    list(DEPLOY.rglob("*")) + list((ROOT / "scripts").glob("*")) + [ROOT / ".env.example", ROOT / "docs" / "DEPLOY.md"]
    + list((ROOT / "rabota" / "ops").glob("*.py")) + [ROOT / "rabota" / "web" / "security.py", ROOT / "rabota" / "web" / "analytics.py"]
)
FORBIDDEN = ["obsidian", "vault", "pass/", "claude", "cursor", "codex", "агент", "/users/lerk", "adventcher", "joulerk", "[["]


@pytest.mark.parametrize("path", [p for p in PUBLIC_FILES if p.is_file() and p.suffix != ".pyc"], ids=lambda p: str(p.relative_to(ROOT)))
def test_no_internal_kitchen_no_long_dash_no_secrets(path):
    text = path.read_text(encoding="utf-8")
    low = text.lower()
    for word in FORBIDDEN:
        if word == "[[" and path.suffix in (".py", ".sh"):
            continue  # Callable[[...]] и конструкции bash
        assert word not in low, f"{path.name}: «{word}»"
    assert "—" not in text, f"{path.name}: длинное тире"
    assert not re.search(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b", text), "похоже на токен бота"
    assert not re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text)


def test_deploy_script_keeps_backup_and_alert_timers_running():
    """Таймеры резервной копии и уведомлений должны запускаться при каждом развёртывании, а не только при первой установке."""
    text = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    body = text.split("restart_services() {", 1)[1].split("}", 1)[0]
    assert "rabota-backup.timer" in body and "rabota-alert.timer" in body
