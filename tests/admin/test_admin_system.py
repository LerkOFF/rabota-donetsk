"""Раздел «Система»: состояния задач, интеграции без значений, диск, версия, журнал без секретов, работа без источников."""
from datetime import timedelta

import pytest

from rabota.admin import system
from rabota.realtime import state
from tests.admin.helpers import NOW
from tests.web.admin_harness import login_admin, make_env

# ------------------------------------------------------------------ состояния

@pytest.mark.parametrize("age,error,newer,expected", [
    (None, None, False, "unknown"),
    (60, None, False, "ok"),
    (1000, None, False, "late"),
    (5000, None, False, "silent"),
    (60, "сбой", True, "error"),
    (60, "старая ошибка", False, "ok"),
    (None, "сбой", True, "error"),
])
def test_classify(age, error, newer, expected):
    assert system.classify(age, 1800, error, newer) == expected


def test_worker_block_reports_each_state(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("HEARTBEAT_PATH", str(tmp_path / "hb"))
    from rabota.realtime.health import write_heartbeat

    write_heartbeat(tmp_path / "hb", NOW)
    state.mark_ok(conn, "poll:trudvsem", now=NOW - timedelta(minutes=2))
    state.mark_ok(conn, "poll:hh", now=NOW - timedelta(hours=10))
    state.mark_ok(conn, "poll:mczn", now=NOW - timedelta(minutes=1))
    state.mark_error(conn, "poll:mczn", "TimeoutError: нет ответа", now=NOW)
    block = system.worker_block(None, conn, NOW)
    assert block["status"] == "ok"
    by_key = {s["key"]: s for s in block["sources"]}
    assert by_key["poll:trudvsem"]["state"] == "ok"
    assert by_key["poll:hh"]["state"] == "silent"
    assert by_key["poll:mczn"]["state"] == "error" and "TimeoutError" in by_key["poll:mczn"]["error"]
    assert by_key["poll:telegram"]["state"] == "unknown"


def test_system_builds_without_worker_sources_backups_or_git(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("HEARTBEAT_PATH", str(tmp_path / "no" / "hb"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "no-backups"))
    monkeypatch.setattr(system.config, "ROOT", tmp_path)
    data = system.build(conn, tmp_path / "missing.db", NOW)
    assert data["worker"]["status"] == "missing"
    assert data["version"]["commit"] == "" and data["version"]["python"]
    assert data["disk"]["db_bytes"] is None and data["disk"]["db_bytes_text"] == "нет данных"
    assert {j["state"] for j in data["jobs"]} == {"unknown"}
    assert data["problems"] >= 1
    assert data["log"]["lines"] == []


def test_health_failure_does_not_break(conn, tmp_path):
    def broken(_conn):
        raise RuntimeError("проверка сломана")

    data = system.build(conn, tmp_path / "x.db", NOW, health=broken)
    assert "RuntimeError" in data["worker"]["error"] and data["sources"] == []


def test_expire_job_leaves_mark_for_system(conn):
    from rabota.realtime.worker import EXPIRE_STATE_KEY, expire_job

    expire_job(conn, now=NOW)
    jobs = {j["label"]: j for j in system.background_jobs(conn, NOW)}
    assert EXPIRE_STATE_KEY == "job:expire" and jobs["Уборка старых вакансий"]["state"] == "ok"


def test_latest_backup_age(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    assert system.latest_backup(NOW) == (None, None)
    (tmp_path / "rabota-1.db.gz").write_bytes(b"x")
    stamp, age = system.latest_backup(NOW.replace(year=2100))
    assert stamp and age > 0


# ------------------------------------------------------------------ очереди и интеграции

def test_queues_count_pending_and_survive_missing_tables(conn, add):
    add(moderation_status="pending", status="hidden")
    by = {q["label"]: q for q in system.queues(conn, NOW)}
    assert by["Вакансии на проверке"]["count"] == 1
    conn.execute("DROP TABLE reports")
    conn.commit()
    by = {q["label"]: q for q in system.queues(conn, NOW)}
    assert by["Жалобы на сообщения"]["count"] is None


SECRET = "s3cret-value-123456"


def test_integrations_show_flags_not_values(monkeypatch):
    for name in ("VK_ACCESS_TOKEN", "HH_CLIENT_ID", "HH_CLIENT_SECRET", "TELEGRAM_PROXY", "METRIKA_ID", "INDEXNOW_KEY", "LLM_PROVIDER",
                 "ALERT_TG_BOT_TOKEN", "ALERT_TG_CHAT_ID", "HEALTH_TOKEN", "SMTP_HOST", "TG_API_ID", "TG_API_HASH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAIL_BACKEND", "file")
    assert not any(i["ok"] for i in system.integrations())
    monkeypatch.setenv("VK_ACCESS_TOKEN", SECRET)
    monkeypatch.setenv("TELEGRAM_PROXY", "socks5://user:" + SECRET + "@proxy.example:1080")
    monkeypatch.setenv("METRIKA_ID", "12345678")
    monkeypatch.setenv("INDEXNOW_KEY", "abcdef" + SECRET)
    monkeypatch.setenv("MAIL_BACKEND", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("LLM_PROVIDER", "ocr")
    by = {i["name"]: i for i in system.integrations()}
    for name in ("ВКонтакте", "Прокси для Telegram", "Яндекс Метрика", "IndexNow", "Почта", "Модель или распознавание афиш"):
        assert by[name]["ok"], name
    assert not by["hh.ru"]["ok"]
    flat = " ".join(f"{i['name']} {i['what']}" for i in system.integrations())
    assert SECRET not in flat and "proxy.example" not in flat and "12345678" not in flat and "smtp.example.test" not in flat


# ------------------------------------------------------------------ журнал

def test_log_tail_skips_secret_lines_and_filters(tmp_path, monkeypatch):
    log = tmp_path / "app.log"
    lines = [
        "2026-10-29 12:00:01 INFO старт",
        "2026-10-29 12:00:02 ERROR hh: ошибка соединения",
        "2026-10-29 12:00:03 INFO запрос token=abc123 выполнен",
        "2026-10-29 12:00:04 INFO Authorization: Bearer xyz",
        "2026-10-29 12:00:05 INFO socks5://user:pw@host:1080 подключён",
        "2026-10-29 12:00:06 INFO SMTP_PASSWORD=hunter2",
        "2026-10-29 12:00:07 INFO готово",
    ]
    log.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setenv("LOG_FILE", str(log))
    tail = system.log_tail("app")
    texts = [x["text"] for x in tail["lines"]]
    assert texts[0].endswith("готово") and texts[-1].endswith("старт")
    assert len(texts) == 3 and tail["hidden"] == 4
    joined = " ".join(texts)
    for bad in ("abc123", "xyz", "hunter2", "pw@"):
        assert bad not in joined
    assert [x["err"] for x in tail["lines"]] == [False, True, False]
    filtered = system.log_tail("app", "ERROR")
    assert len(filtered["lines"]) == 1 and filtered["lines"][0]["err"]


def test_log_tail_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "nope.log"))
    tail = system.log_tail("app")
    assert tail["lines"] == [] and tail["available"] is False
    assert system.log_tail("../../etc/passwd")["key"] in tail["sources"]  # неизвестный ключ не читает произвольные пути


# ------------------------------------------------------------------ веб

@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e


def test_system_page_for_admin_without_sources(env, tmp_path, monkeypatch):
    monkeypatch.setenv("HEARTBEAT_PATH", str(tmp_path / "none" / "hb"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "none"))
    login_admin(env)
    r = env.client.get("/admin/system")
    assert r.status_code == 200
    for needle in ("Воркер не запущен", "Фоновые задачи", "Очереди", "Интеграции", "Диск и база", "Версия", "Журнал"):
        assert needle in r.text, needle
    assert 'data-sortable="system-blocks"' in r.text and r.text.count('draggable="true"') >= 6
    assert 'rel="canonical" href="https://example.test/admin/system"' in r.text


def test_system_page_hides_secret_values(env, monkeypatch, tmp_path):
    monkeypatch.setenv("VK_ACCESS_TOKEN", SECRET)
    monkeypatch.setenv("HEALTH_TOKEN", SECRET)
    log = tmp_path / "a.log"
    log.write_text(f"INFO ok\nINFO api_key={SECRET}\n", encoding="utf-8")
    monkeypatch.setenv("LOG_FILE", str(log))
    login_admin(env)
    r = env.client.get("/admin/system?log=app")
    assert r.status_code == 200 and SECRET not in r.text and "INFO ok" in r.text and "настроено" in r.text


def test_system_page_requires_admin(env):
    assert env.client.get("/admin/system", follow_redirects=False).status_code == 303


def test_system_log_filter_in_url(env, monkeypatch, tmp_path):
    log = tmp_path / "a.log"
    log.write_text("INFO alpha\nERROR beta\n", encoding="utf-8")
    monkeypatch.setenv("LOG_FILE", str(log))
    login_admin(env)
    r = env.client.get("/admin/system?log=app&grep=beta")
    assert "ERROR beta" in r.text and "INFO alpha" not in r.text
