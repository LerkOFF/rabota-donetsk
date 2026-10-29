"""Функции развёртывания (scripts/lib-deploy.sh) на временных папках и заглушках nginx и systemctl."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")


@pytest.fixture
def sandbox(tmp_path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    calls = tmp_path / "calls.log"
    (stubs / "systemctl").write_text(f"#!/bin/sh\necho \"systemctl $*\" >> {calls}\n", encoding="utf-8")
    (stubs / "nginx").write_text(f"#!/bin/sh\necho \"nginx $*\" >> {calls}\n[ -f {tmp_path}/nginx_fail ] && {{ echo bad >&2; exit 1; }}\nexit 0\n", encoding="utf-8")
    for f in stubs.iterdir():
        f.chmod(0o755)
    avail, enabled, units = tmp_path / "available", tmp_path / "enabled", tmp_path / "units"
    for d in (avail, enabled, units):
        d.mkdir()
    env = {
        **os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "APP_USER": "webuser", "APP_DIR": "/srv/site",
        "DEPLOY_SRC": str(ROOT / "deploy"), "UNITS_DIR": str(units), "NGINX_AVAILABLE": str(avail), "NGINX_ENABLED": str(enabled),
    }
    return env, tmp_path, calls


def call(env, snippet):
    return subprocess.run(["bash", "-c", f'source "{ROOT}/scripts/lib-deploy.sh"; {snippet}'], check=False,
                          capture_output=True, text=True, env=env)


def test_units_are_rendered_and_installation_is_idempotent(sandbox):
    env, tmp, calls = sandbox
    first = call(env, "id() { echo 0; }; install_units")
    assert first.returncode == 0, first.stderr
    units = tmp / "units"
    names = {p.name for p in units.iterdir()}
    assert {"rabota-web.service", "rabota-worker.service", "rabota-backup.timer", "rabota-alert.timer"} <= names
    web = (units / "rabota-web.service").read_text(encoding="utf-8")
    assert "User=webuser" in web and "WorkingDirectory=/srv/site" in web and "<USER>" not in web and "<PROJECT_DIR>" not in web
    assert "systemctl daemon-reload" in calls.read_text()
    calls.write_text("")
    second = call(env, "id() { echo 0; }; install_units")
    assert second.returncode == 0
    assert "daemon-reload" not in calls.read_text(), "без изменений перезагрузка systemd не нужна"
    assert not list(units.glob("*.bak-*"))


def test_nginx_install_links_validates_and_reloads(sandbox):
    env, tmp, calls = sandbox
    r = call(env, "id() { echo 0; }; install_nginx")
    assert r.returncode == 0, r.stderr
    site = tmp / "available" / "site-rabota"
    assert site.exists() and "/srv/site/rabota/web/static/" in site.read_text(encoding="utf-8")
    assert (tmp / "enabled" / "site-rabota").is_symlink()
    assert "nginx -t" in calls.read_text() and "systemctl reload nginx" in calls.read_text()
    again = call(env, "id() { echo 0; }; install_nginx; echo rc=$?")
    assert "rc=10" in again.stdout


def test_nginx_failure_restores_previous_config_and_never_touches_other_vhosts(sandbox):
    env, tmp, calls = sandbox
    other = tmp / "available" / "site-other"
    other.write_text("server { listen 80; }", encoding="utf-8")
    assert call(env, "id() { echo 0; }; install_nginx").returncode == 0
    site = tmp / "available" / "site-rabota"
    good = site.read_text(encoding="utf-8")
    # меняем шаблон через другую папку проекта и ломаем nginx -t
    (tmp / "nginx_fail").write_text("1", encoding="utf-8")
    env2 = {**env, "APP_DIR": "/srv/changed"}
    calls.write_text("")
    r = call(env2, "id() { echo 0; }; install_nginx; echo rc=$?")
    assert "rc=1" in r.stdout
    assert site.read_text(encoding="utf-8") == good, "прежняя конфигурация должна вернуться"
    assert other.read_text(encoding="utf-8") == "server { listen 80; }"
    assert "reload nginx" not in calls.read_text()


def test_first_failed_install_leaves_nothing_behind(sandbox):
    env, tmp, _ = sandbox
    (tmp / "nginx_fail").write_text("1", encoding="utf-8")
    r = call(env, "id() { echo 0; }; install_nginx; echo rc=$?")
    assert "rc=1" in r.stdout
    assert not (tmp / "available" / "site-rabota").exists() and not (tmp / "enabled" / "site-rabota").exists()


def test_render_replaces_placeholders_only(sandbox):
    env, tmp, _ = sandbox
    src = tmp / "t.txt"
    src.write_text("u=<USER> d=<PROJECT_DIR> keep=<OTHER>\n", encoding="utf-8")
    r = call(env, f'render "{src}"')
    assert r.stdout == "u=webuser d=/srv/site keep=<OTHER>\n"
