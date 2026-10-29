"""Скрипты развёртывания: синтаксис, shellcheck (если есть), резервная копия, восстановление, сертификат Xray."""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
SHELL_FILES = sorted(SCRIPTS.glob("*.sh"))

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")


def run(cmd, env=None, cwd=None, input_text=None):
    full = {**os.environ, **(env or {})}
    return subprocess.run(cmd, check=False, capture_output=True, text=True, env=full, cwd=cwd, timeout=60, input=input_text)


def test_expected_scripts_exist_and_are_executable():
    names = {p.name for p in SHELL_FILES}
    assert {"backup.sh", "restore.sh", "verify-backup.sh", "deploy.sh", "bootstrap-server.sh", "smoke.sh",
            "check-health.sh", "xray-cert-add.sh", "lib-sqlite.sh", "lib-deploy.sh"} <= names
    for p in SHELL_FILES:
        if p.name.startswith("lib-"):
            continue
        assert p.stat().st_mode & stat.S_IXUSR, f"{p.name} не исполняемый"
        assert p.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash"), p.name


@needs_bash
@pytest.mark.parametrize("path", SHELL_FILES, ids=lambda p: p.name)
def test_bash_syntax(path):
    r = run(["bash", "-n", str(path)])
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck не установлен")
@pytest.mark.parametrize("path", SHELL_FILES, ids=lambda p: p.name)
def test_shellcheck(path):
    r = run(["shellcheck", "-x", "-S", "warning", str(path)])
    assert r.returncode == 0, r.stdout


def test_scripts_are_strict_mode():
    for name in ("backup.sh", "restore.sh", "verify-backup.sh", "deploy.sh", "bootstrap-server.sh", "xray-cert-add.sh"):
        assert "set -euo pipefail" in (SCRIPTS / name).read_text(encoding="utf-8"), name


# ------------------------------------------------------------------ резервные копии

def make_db(path: Path, vacancies: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE vacancies (id INTEGER PRIMARY KEY, profession TEXT)")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    conn.executemany("INSERT INTO vacancies(profession) VALUES (?)", [(f"проф {i}",) for i in range(vacancies)])
    conn.execute("INSERT INTO users(email) VALUES ('a@b.test')")
    conn.commit()
    conn.close()
    return path


@pytest.fixture(params=["sqlite3", "python"])
def env(request, tmp_path):
    """Окружение для скриптов: с настоящим sqlite3 и без него (запасной путь через python)."""
    if request.param == "sqlite3" and shutil.which("sqlite3") is None:
        pytest.skip("sqlite3 не установлен")
    data = tmp_path / "data"
    db = make_db(data / "rabota.db")
    backups = tmp_path / "backups"
    e = {"DB_PATH": str(db), "BACKUP_DIR": str(backups), "SQLITE_PYTHON": os.sys.executable}
    if request.param == "python":
        # PATH без sqlite3: оставляем только каталоги с нужными утилитами через ссылки
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for tool in ("bash", "gzip", "find", "du", "cut", "date", "mktemp", "stat", "tar", "mv", "rm", "mkdir", "cat", "dirname",
                     "sed", "sort", "tail", "head", "chown", "env", "tr", "awk", "grep", "ls", "cp"):
            found = shutil.which(tool)
            if found:
                (bin_dir / tool).symlink_to(found)
        e["PATH"] = str(bin_dir)
    return e, backups, db


@needs_bash
def test_backup_creates_valid_copy_and_verify_passes(env):
    e, backups, _ = env
    r = run(["bash", str(SCRIPTS / "backup.sh")], e)
    assert r.returncode == 0, r.stdout + r.stderr
    copies = list(backups.glob("rabota-*.db.gz"))
    assert len(copies) == 1
    with gzip.open(copies[0]) as fh:
        assert fh.read(16).startswith(b"SQLite format 3")
    v = run(["bash", str(SCRIPTS / "verify-backup.sh")], e)
    assert v.returncode == 0, v.stdout + v.stderr
    assert "вакансий 3" in v.stdout and "пользователей 1" in v.stdout
    assert not list(backups.glob(".work.*")), "рабочая папка должна удаляться"


@needs_bash
def test_backup_is_consistent_while_database_is_open_in_wal_mode(env):
    e, backups, db = env
    writer = sqlite3.connect(db)
    writer.execute("INSERT INTO vacancies(profession) VALUES ('в журнале WAL')")
    writer.commit()  # данные пока только в -wal
    try:
        assert run(["bash", str(SCRIPTS / "backup.sh")], e).returncode == 0
    finally:
        writer.close()
    copy = next(backups.glob("rabota-*.db.gz"))
    restored = backups / "r.db"
    restored.write_bytes(gzip.decompress(copy.read_bytes()))
    assert sqlite3.connect(restored).execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 4


@needs_bash
def test_backup_rotation_removes_only_old_copies(env):
    e, backups, _ = env
    backups.mkdir()
    old = backups / "rabota-20200101-000000.db.gz"
    old_files = backups / "rabota-20200101-000000-files.tar.gz"
    fresh = backups / "rabota-20990101-000000.db.gz"
    other = backups / "keep-me.txt"
    for f in (old, old_files, fresh, other):
        f.write_bytes(b"x")
    ancient = time.time() - 20 * 86400
    for f in (old, old_files, other):
        os.utime(f, (ancient, ancient))
    r = run(["bash", str(SCRIPTS / "backup.sh")], {**e, "BACKUP_KEEP_DAYS": "14"})
    assert r.returncode == 0, r.stderr
    assert not old.exists() and not old_files.exists()
    assert fresh.exists() and other.exists()


@needs_bash
def test_backup_includes_session_files_when_present(env):
    e, backups, db = env
    (db.parent / "telegram.session").write_bytes(b"session")
    assert run(["bash", str(SCRIPTS / "backup.sh")], e).returncode == 0
    assert len(list(backups.glob("rabota-*-files.tar.gz"))) == 1


@needs_bash
def test_backup_refuses_missing_database(tmp_path):
    r = run(["bash", str(SCRIPTS / "backup.sh")], {"DB_PATH": str(tmp_path / "none.db"), "BACKUP_DIR": str(tmp_path / "b")})
    assert r.returncode == 1 and "база не найдена" in r.stderr


@needs_bash
def test_verify_detects_corrupt_and_stale_copies(env, tmp_path):
    e, backups, _ = env
    backups.mkdir()
    bad = backups / "rabota-20260101-000000.db.gz"
    bad.write_bytes(gzip.compress("это не база данных".encode() * 200))
    r = run(["bash", str(SCRIPTS / "verify-backup.sh"), str(bad)], e)
    assert r.returncode == 1 and "целостн" in r.stderr + r.stdout
    broken = backups / "rabota-20260102-000000.db.gz"
    broken.write_bytes(b"not gzip")
    assert run(["bash", str(SCRIPTS / "verify-backup.sh"), str(broken)], e).returncode == 1
    assert run(["bash", str(SCRIPTS / "backup.sh")], e).returncode == 0
    good = max(backups.glob("rabota-*.db.gz"))
    old = time.time() - 3 * 86400
    os.utime(good, (old, old))
    stale = run(["bash", str(SCRIPTS / "verify-backup.sh"), str(good), "--max-age-hours", "36"], e)
    assert stale.returncode == 1 and "старше" in stale.stderr
    assert run(["bash", str(SCRIPTS / "verify-backup.sh"), str(good), "--max-age-hours", "999"], e).returncode == 0


@needs_bash
def test_restore_roundtrip_and_safety(env, tmp_path):
    e, backups, db = env
    assert run(["bash", str(SCRIPTS / "backup.sh")], e).returncode == 0
    copy = next(backups.glob("rabota-*.db.gz"))
    # развернуть в другой файл, боевую базу не трогая
    target = tmp_path / "check" / "restored.db"
    r = run(["bash", str(SCRIPTS / "restore.sh"), str(copy), "--target", str(target)], e)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sqlite3.connect(target).execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 3
    # без --yes боевая база не заменяется
    refused = run(["bash", str(SCRIPTS / "restore.sh"), str(copy)], e)
    assert refused.returncode == 1 and "--yes" in refused.stderr
    # порча базы и восстановление
    db.write_bytes(b"garbage")
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    done = run(["bash", str(SCRIPTS / "restore.sh"), str(copy), "--yes"], {**e, "RESTORE_SERVICES": ""})
    assert done.returncode == 0, done.stdout + done.stderr
    assert sqlite3.connect(db).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert list(db.parent.glob("rabota.db.before-restore-*")), "старая база должна сохраняться рядом"


@needs_bash
def test_restore_rejects_corrupt_copy_and_keeps_database(env, tmp_path):
    e, backups, db = env
    backups.mkdir()
    bad = backups / "bad.db.gz"
    bad.write_bytes(gzip.compress(b"x" * 5000))
    before = db.read_bytes()
    r = run(["bash", str(SCRIPTS / "restore.sh"), str(bad), "--yes"], {**e, "RESTORE_SERVICES": ""})
    assert r.returncode == 1 and db.read_bytes() == before


# ------------------------------------------------------------------ Xray

def xray_env(tmp_path, with_hook=True):
    live = tmp_path / "le" / "xn--test.xn--p1ai"
    live.mkdir(parents=True)
    (live / "fullchain.pem").write_text("CERT", encoding="utf-8")
    (live / "privkey.pem").write_text("KEY", encoding="utf-8")
    config = tmp_path / "xray" / "config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"inbounds": [
        {"tag": "other", "port": 1},
        {"tag": "vless-tcp", "port": 443, "streamSettings": {"tlsSettings": {"certificates": [
            {"certificateFile": "/old.pem", "keyFile": "/old.key"}]}}},
    ]}), encoding="utf-8")
    hook = tmp_path / "hooks" / "xray-certs.sh"
    if with_hook:
        hook.parent.mkdir()
        hook.write_text("#!/bin/sh\ncp /a /b\nsystemctl restart xray\n", encoding="utf-8")
    bin_dir = tmp_path / "stubs"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    # xray: -test принимает только корректный JSON с тегом vless-tcp и, как настоящий xray, не угадывает формат
    # по имени файла без расширения .json (временный файл скрипта называется config.json.new.XXXXXX); systemctl пишет вызовы
    (bin_dir / "xray").write_text(
        f"#!/bin/sh\n{os.sys.executable} -c \"import json,sys; a=sys.argv[1:]; p=a[a.index('-config')+1]; "
        "fmt=a[a.index('-format')+1] if '-format' in a else ''; "
        "ok=fmt=='json' or p.endswith('.json'); "
        "c=json.load(open(p)); sys.exit(0 if ok and c.get('inbounds') and not c.get('broken') else 1)\" \"$@\"\n",
        encoding="utf-8")
    (bin_dir / "systemctl").write_text(f"#!/bin/sh\necho \"$@\" >> {log}\nexit ${{SYSTEMCTL_RC:-0}}\n", encoding="utf-8")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return {
        "XRAY_CONFIG": str(config), "XRAY_CERTS_DIR": str(tmp_path / "xray" / "certs"), "LE_LIVE_DIR": str(tmp_path / "le"),
        "XRAY_HOOK": str(hook), "XRAY_BIN": str(bin_dir / "xray"), "XRAY_ALLOW_NONROOT": "1",
        "XRAY_PYTHON": os.sys.executable, "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }, config, hook, log


XRAY = str(SCRIPTS / "xray-cert-add.sh")
DOMAIN = "xn--test.xn--p1ai"


@needs_bash
def test_xray_adds_certificate_backup_hook_and_restarts(tmp_path):
    e, config, hook, log = xray_env(tmp_path)
    r = run(["bash", XRAY, DOMAIN], e)
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(config.read_text(encoding="utf-8"))
    certs = data["inbounds"][1]["streamSettings"]["tlsSettings"]["certificates"]
    assert len(certs) == 2 and certs[1]["certificateFile"].endswith(f"{DOMAIN}.fullchain.pem")
    assert data["inbounds"][0] == {"tag": "other", "port": 1}
    assert len(list(config.parent.glob("config.json.bak-*"))) == 1
    assert (config.parent / "certs" / f"{DOMAIN}.fullchain.pem").read_text() == "CERT"
    assert oct((config.parent / "certs" / f"{DOMAIN}.privkey.pem").stat().st_mode & 0o777) == "0o640"
    text = hook.read_text(encoding="utf-8")
    assert f"rabota-donetsk:{DOMAIN} begin" in text
    assert text.index("rabota-donetsk") < text.index("systemctl restart xray"), "блок должен идти до перезапуска"
    assert "restart xray" in log.read_text()


@needs_bash
def test_xray_is_idempotent(tmp_path):
    e, config, hook, _log = xray_env(tmp_path)
    assert run(["bash", XRAY, DOMAIN], e).returncode == 0
    first, first_hook = config.read_text(), hook.read_text()
    again = run(["bash", XRAY, DOMAIN], e)
    assert again.returncode == 0, again.stderr
    assert config.read_text() == first and hook.read_text() == first_hook
    assert len(list(config.parent.glob("config.json.bak-*"))) == 1


@needs_bash
def test_xray_creates_hook_when_missing(tmp_path):
    e, _, hook, _ = xray_env(tmp_path, with_hook=False)
    assert run(["bash", XRAY, DOMAIN], e).returncode == 0
    text = hook.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh") and "systemctl restart xray" in text and os.access(hook, os.X_OK)


@needs_bash
def test_xray_rolls_back_when_service_fails(tmp_path):
    e, config, _, _ = xray_env(tmp_path)
    original = config.read_text(encoding="utf-8")
    r = run(["bash", XRAY, DOMAIN], {**e, "SYSTEMCTL_RC": "1"})
    assert r.returncode == 1
    assert config.read_text(encoding="utf-8") == original


@needs_bash
def test_xray_rejects_config_that_fails_test_and_leaves_it_untouched(tmp_path):
    e, config, _, _ = xray_env(tmp_path)
    bad = json.loads(config.read_text())
    bad["broken"] = True
    config.write_text(json.dumps(bad), encoding="utf-8")
    before = config.read_text()
    r = run(["bash", XRAY, DOMAIN], e)
    assert r.returncode == 1 and config.read_text() == before
    assert not list(config.parent.glob("config.json.bak-*"))


@needs_bash
def test_xray_dry_run_changes_nothing(tmp_path):
    e, config, hook, log = xray_env(tmp_path)
    before, hook_before = config.read_text(), hook.read_text()
    r = run(["bash", XRAY, DOMAIN, "--dry-run"], e)
    assert r.returncode == 0 and "dry-run" in r.stdout
    assert config.read_text() == before and hook.read_text() == hook_before
    assert not (config.parent / "certs").exists() and not log.exists()


@needs_bash
def test_xray_input_validation(tmp_path):
    e, *_ = xray_env(tmp_path)
    assert run(["bash", XRAY], e).returncode == 1
    assert run(["bash", XRAY, "bad domain;rm"], e).returncode == 1
    assert run(["bash", XRAY, "xn--absent.xn--p1ai"], e).returncode == 1
    e2 = {**e, "XRAY_INBOUND_TAG": "no-such-tag"}
    assert run(["bash", XRAY, DOMAIN], e2).returncode != 0


# ------------------------------------------------------------------ мониторинг

@needs_bash
def test_check_health_exit_codes_with_stub_curl(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def stub(body: str, code: str):
        (bin_dir / "curl").write_text(f"#!/bin/sh\nprintf '%s\\n%s' '{body}' '{code}'\n", encoding="utf-8")
        (bin_dir / "curl").chmod(0o755)
        return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    script = str(SCRIPTS / "check-health.sh")
    assert run(["bash", script, "https://x.test"], stub('{"status": "ok"}', "200")).returncode == 0
    warn = run(["bash", script, "https://x.test"], stub('{"status": "degraded"}', "200"))
    assert warn.returncode == 1 and warn.stdout.startswith("WARNING")
    assert run(["bash", script, "https://x.test", "--strict"], stub('{"status": "degraded"}', "200")).returncode == 2
    crit = run(["bash", script, "https://x.test"], stub('{"status": "fail"}', "503"))
    assert crit.returncode == 2 and crit.stdout.startswith("CRITICAL")
    assert run(["bash", script, "https://x.test"], stub("", "000")).returncode == 2


@needs_bash
def test_smoke_script_reports_unreachable_site_as_failure(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    (bin_dir / "curl").chmod(0o755)
    r = run(["bash", str(SCRIPTS / "smoke.sh"), "https://x.test"], {"PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert r.returncode == 1 and "ОШИБКА" in r.stdout


@needs_bash
def test_smoke_script_passes_against_local_app(tmp_path):
    """Живой прогон smoke.sh против приложения, поднятого локально (без nginx: проверки http/www/сжатия опущены или мягкие)."""
    pytest.importorskip("uvicorn")
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "DB_PATH": str(tmp_path / "smoke.db"), "SITE_URL": f"http://127.0.0.1:{port}"}
    proc = subprocess.Popen([os.sys.executable, "-m", "uvicorn", "rabota.web.app:app", "--port", str(port), "--log-level", "error"],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                import urllib.request

                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        r = run(["bash", str(SCRIPTS / "smoke.sh"), f"http://127.0.0.1:{port}"], {"SMOKE_DEEP": "skip"})
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    out = r.stdout
    # без nginx нет сжатия; всё остальное должно проходить
    failed = [line for line in out.splitlines() if line.startswith("ОШИБКА")]
    assert all("не сжимается" in line for line in failed), out
