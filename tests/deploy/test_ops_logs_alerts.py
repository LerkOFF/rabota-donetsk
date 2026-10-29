"""JSON-журнал, счётчики, уведомления в Telegram."""
from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from rabota.ops import alerts
from rabota.ops.logs import CounterHandler, JsonFormatter, counters, redact, setup_logging

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def test_json_formatter_one_line_with_fields():
    rec = logging.LogRecord("rabota.x", logging.WARNING, __file__, 1, "привет %s", ("мир",), None)
    line = JsonFormatter().format(rec)
    data = json.loads(line)
    assert "\n" not in line
    assert data["level"] == "WARNING" and data["logger"] == "rabota.x" and data["msg"] == "привет мир"
    assert data["ts"].endswith("+00:00")


def test_json_formatter_includes_exception_and_redacts():
    try:
        raise ValueError("token=SECRET123 failed")
    except ValueError:
        import sys

        rec = logging.LogRecord("a", logging.ERROR, __file__, 1, "ошибка socks5://u:pw@h:1", (), sys.exc_info())
    data = json.loads(JsonFormatter().format(rec))
    assert "SECRET123" not in json.dumps(data) and "pw@" not in json.dumps(data)
    assert "ValueError" in data["exc"]


def test_redact_patterns():
    assert "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ_1234567" not in redact("bot123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ_1234567/send")
    assert redact("password=abc&x=1") == "password=<скрыто>&x=1"
    assert redact("обычный текст") == "обычный текст"


def test_counter_handler_counts_warnings_and_errors():
    counters.reset()
    log = logging.getLogger("rabota.test.counter")
    log.propagate = False
    log.setLevel(logging.DEBUG)
    handler = CounterHandler()
    log.addHandler(handler)
    try:
        log.info("не считается")
        log.warning("w")
        log.error("e1")
        log.error("e2")
    finally:
        log.removeHandler(handler)
    snap = counters.snapshot()
    assert snap["log_warning"] == 1 and snap["log_error"] == 2


def test_setup_logging_json_mode(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    try:
        setup_logging(logging.INFO)
        stream = io.StringIO()
        mine = next(h for h in root.handlers if isinstance(h.formatter, JsonFormatter))
        mine.stream = stream
        logging.getLogger("rabota.demo").info("запущено")
        assert json.loads(stream.getvalue())["msg"] == "запущено"
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])


def report(**over):
    base = {"status": "ok", "db": "ok", "worker": "ok", "groups": {
        "telegram": {"state": "ok", "total": 8, "bad": 0, "problems": []},
        "vk": {"state": "ok", "total": 3, "bad": 0, "problems": []},
    }}
    base.update(over)
    return base


def down_telegram():
    return report(status="fail", groups={
        "telegram": {"state": "down", "total": 8, "bad": 8, "problems": [
            {"key": "telegram:a", "label": "Telegram a", "status": "stale", "age_seconds": 9999,
             "last_error": "переподключение socks5://u:secret@h:1080"}]},
        "vk": {"state": "ok", "total": 3, "bad": 0, "problems": []}})


def test_problems_from_report():
    assert alerts.problems_from(report()) == {}
    found = alerts.problems_from(down_telegram())
    assert list(found) == ["group:telegram"] and "8 из 8" in found["group:telegram"]
    assert "worker" in alerts.problems_from(report(worker="missing"))
    assert "db" in alerts.problems_from(report(db="fail"))
    partial = report(groups={"vk": {"state": "degraded", "total": 3, "bad": 1, "problems": []}})
    assert list(alerts.problems_from(partial)) == ["part:vk"]


class Sink:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def __call__(self, text):
        self.sent.append(text)
        return self.ok


def test_alert_sent_once_then_silent_then_recovery(tmp_path):
    state = tmp_path / "state.json"
    sink = Sink()
    first = alerts.run(down_telegram(), state, sink, NOW)
    assert first and "Сбой" in first and "secret" not in first
    assert alerts.run(down_telegram(), state, sink, NOW + timedelta(minutes=5)) is None
    assert len(sink.sent) == 1
    done = alerts.run(report(), state, sink, NOW + timedelta(minutes=10))
    assert done and "Восстановилось" in done and len(sink.sent) == 2
    assert alerts.run(report(), state, sink, NOW + timedelta(minutes=15)) is None


def test_reminder_after_six_hours(tmp_path):
    state = tmp_path / "state.json"
    sink = Sink()
    alerts.run(down_telegram(), state, sink, NOW)
    assert alerts.run(down_telegram(), state, sink, NOW + timedelta(hours=5)) is None
    again = alerts.run(down_telegram(), state, sink, NOW + timedelta(hours=6, minutes=1))
    assert again and "остаются" in again


def test_failed_send_is_retried_next_time(tmp_path):
    state = tmp_path / "state.json"
    bad, good = Sink(ok=False), Sink()
    assert alerts.run(down_telegram(), state, bad, NOW) is None
    assert not state.exists()
    assert alerts.run(down_telegram(), state, good, NOW + timedelta(minutes=5)) is not None


def test_dry_run_sends_and_stores_nothing(tmp_path):
    state = tmp_path / "state.json"
    sink = Sink()
    text = alerts.run(down_telegram(), state, sink, NOW, dry_run=True)
    assert text and not sink.sent and not state.exists()


def test_main_is_silent_without_token(monkeypatch, capsys):
    monkeypatch.delenv("ALERT_TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALERT_TG_CHAT_ID", raising=False)
    assert alerts.main([]) == 0
    assert "выключены" in capsys.readouterr().out


def test_telegram_sender_uses_proxy_and_hides_token_on_failure(monkeypatch):
    import httpx

    seen = {}

    class FakeClient:
        def __init__(self, **kw):
            seen.update(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json):
            seen["url"], seen["json"] = url, json
            return httpx.Response(200)

    monkeypatch.setattr(alerts.httpx, "Client", FakeClient)
    assert alerts.telegram_sender("123:TOKEN", "42", "socks5://127.0.0.1:1080")("привет") is True
    assert seen["proxy"] == "socks5://127.0.0.1:1080" and "bot123:TOKEN" in seen["url"]
    assert seen["json"]["chat_id"] == "42"


@pytest.mark.parametrize("proxy,expected", [("", None), ("socks5://h:1", "socks5://h:1")])
def test_telegram_sender_proxy_optional(monkeypatch, proxy, expected):
    import httpx

    captured = {}

    class FakeClient:
        def __init__(self, **kw):
            captured.update(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json):
            return httpx.Response(500)

    monkeypatch.setattr(alerts.httpx, "Client", FakeClient)
    assert alerts.telegram_sender("t", "c", proxy)("x") is False
    assert captured["proxy"] == expected
