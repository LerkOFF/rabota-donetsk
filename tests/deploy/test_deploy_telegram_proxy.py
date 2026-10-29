"""Прокси для Telegram: один адрес TELEGRAM_PROXY для слушателя, сборщика t.me и уведомлений."""
from __future__ import annotations

from typing import ClassVar

import httpx
import pytest

from rabota.collectors import telegram as tg_collector
from rabota.realtime.telegram_listener import TelegramSettings, parse_proxy


class Recorder:
    kwargs: ClassVar[dict] = {}

    def __init__(self, **kw):
        Recorder.kwargs = kw

    def get(self, *a, **k):
        raise httpx.ConnectError("нет сети в тесте")

    def close(self):
        pass


@pytest.mark.parametrize("env,expected", [
    ({"TELEGRAM_PROXY": "socks5://10.1.1.1:1080"}, "socks5://10.1.1.1:1080"),
    ({"TG_PROXY": "socks5://10.2.2.2:1080"}, "socks5://10.2.2.2:1080"),
    ({}, None),
])
def test_tme_collector_uses_configured_proxy(monkeypatch, env, expected):
    monkeypatch.delenv("TELEGRAM_PROXY", raising=False)
    monkeypatch.delenv("TG_PROXY", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(tg_collector.httpx, "Client", Recorder)
    monkeypatch.setattr(tg_collector, "SOURCES", [])
    tg_collector.fetch(limit=1)
    assert Recorder.kwargs.get("proxy") == expected


def test_httpx_accepts_socks_proxy_urls():
    """Установлен пакет socksio: клиент с socks5-прокси создаётся без ошибки."""
    with httpx.Client(proxy="socks5://user:pass@127.0.0.1:1080"):
        pass


def test_listener_reads_telegram_proxy_and_old_name(tmp_path):
    base = {"TG_API_ID": "1", "TG_API_HASH": "h", "TG_SESSION_PATH": str(tmp_path / "s")}
    new = TelegramSettings.from_env({**base, "TELEGRAM_PROXY": "socks5://a:1"}, require_session=False)
    assert new.proxy[:3] == ("socks5", "a", 1)
    old = TelegramSettings.from_env({**base, "TG_PROXY": "socks5://b:2"}, require_session=False)
    assert old.proxy[:3] == ("socks5", "b", 2)
    both = TelegramSettings.from_env({**base, "TELEGRAM_PROXY": "socks5://a:1", "TG_PROXY": "socks5://b:2"}, require_session=False)
    assert both.proxy[1] == "a"
    assert TelegramSettings.from_env(base, require_session=False).proxy is None


def test_proxy_with_credentials_is_not_in_settings_repr(tmp_path):
    s = TelegramSettings.from_env({"TG_API_ID": "1", "TG_API_HASH": "h", "TG_SESSION_PATH": str(tmp_path / "s"),
                                   "TELEGRAM_PROXY": "socks5://user:hunter2@h:1080"}, require_session=False)
    assert "hunter2" not in repr(s)
    assert parse_proxy("socks5://user:hunter2@h:1080")[4:] == ("user", "hunter2")
