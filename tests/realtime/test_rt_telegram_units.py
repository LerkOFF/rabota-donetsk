"""Слушатель Telegram: настройки, прокси, список каналов, защита только чтение, сообщения в посты."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PIL import Image
from rt_helpers import NOW, FakeMessage, FakeTelegram, make_jpeg, run

from rabota.realtime import telegram_listener as tl
from rabota.realtime.telegram_listener import (
    ReadOnlyClient,
    ReadOnlyViolation,
    TelegramNotConfigured,
    TelegramSettings,
)

SECRET_HASH = "0123456789abcdef0123456789abcdef"


# ---------------------------------------------------------------- прокси и каналы

def test_parse_proxy_variants():
    assert tl.parse_proxy("") is None
    assert tl.parse_proxy("  ") is None
    assert tl.parse_proxy("socks5://host.example:1080") == ("socks5", "host.example", 1080, True, None, None)
    assert tl.parse_proxy("host.example:1080") == ("socks5", "host.example", 1080, True, None, None)
    assert tl.parse_proxy("socks5://us%40er:p%3Ass@10.0.0.1:9050") == ("socks5", "10.0.0.1", 9050, True, "us@er", "p:ss")
    assert tl.parse_proxy("http://h:3128")[0] == "http"


@pytest.mark.parametrize("bad", ["ftp://h:1", "socks5://hostonly", "socks5://:1080"])
def test_parse_proxy_rejects_garbage(bad):
    with pytest.raises(ValueError):
        tl.parse_proxy(bad)


@pytest.mark.parametrize("line,expected", [
    ("rabota_dnr", "rabota_dnr"), ("@Rabota_Dnr", "Rabota_Dnr"), ("https://t.me/rabota_dnr", "rabota_dnr"),
    ("t.me/s/rabota_dnr/123", "rabota_dnr"), ("  @abc_def  # комментарий", "abc_def"),
    ("# только комментарий", None), ("", None), ("a", None), ("не канал", None), ("123abc", None),
])
def test_normalize_channel(line, expected):
    assert tl.normalize_channel(line) == expected


def test_load_channels_merges_sources_and_file(tmp_path):
    f = tmp_path / "channels.txt"
    f.write_text("# мои каналы\n@Extra_One\n\nhttps://t.me/extra_two\nrabota_donetskv\nEXTRA_ONE\n", encoding="utf-8")
    got = tl.load_channels(f)
    from rabota.collectors.telegram import SOURCES

    assert got[: len(SOURCES)] == SOURCES
    assert got[len(SOURCES):] == ["Extra_One", "extra_two"]


def test_load_channels_without_file_uses_sources():
    from rabota.collectors.telegram import SOURCES

    assert tl.load_channels(None) == SOURCES
    assert tl.load_channels("/nonexistent/channels.txt") == SOURCES
    assert tl.load_channels(None, base=["a_b_c"]) == ["a_b_c"]


def test_session_file_name():
    assert str(tl.session_file("/x/tg")).endswith("/x/tg.session")
    assert str(tl.session_file("/x/tg.session")).endswith("/x/tg.session")


# ---------------------------------------------------------------- настройки из окружения

def env(tmp_path, **extra):
    base = {"TG_API_ID": "12345", "TG_API_HASH": SECRET_HASH, "TG_SESSION_PATH": str(tmp_path / "sess")}
    base.update(extra)
    return base


def test_not_configured_without_api(tmp_path):
    with pytest.raises(TelegramNotConfigured):
        TelegramSettings.from_env({})
    with pytest.raises(TelegramNotConfigured):
        TelegramSettings.from_env({"TG_API_ID": "1"})
    with pytest.raises(TelegramNotConfigured, match="числом"):
        TelegramSettings.from_env(env(tmp_path, TG_API_ID="abc"))


def test_not_configured_without_session_file(tmp_path):
    with pytest.raises(TelegramNotConfigured, match="tg-login"):
        TelegramSettings.from_env(env(tmp_path))
    s = TelegramSettings.from_env(env(tmp_path), require_session=False)
    assert s.api_id == 12345 and s.proxy is None


def test_configured_with_session(tmp_path):
    (tmp_path / "sess.session").write_bytes(b"")
    (tmp_path / "ch.txt").write_text("@my_chan\n", encoding="utf-8")
    s = TelegramSettings.from_env(env(tmp_path, TG_PROXY="socks5://p.example:1080", TG_CHANNELS_FILE=str(tmp_path / "ch.txt"),
                                      TG_SWEEP_SECONDS="120"))
    assert s.proxy[:3] == ("socks5", "p.example", 1080)
    assert "my_chan" in s.channels and s.sweep_seconds == 120.0
    assert s.lookback_days == 21


def test_bad_proxy_means_not_configured(tmp_path):
    with pytest.raises(TelegramNotConfigured, match="TG_PROXY"):
        TelegramSettings.from_env(env(tmp_path, TG_PROXY="ftp://x:1"), require_session=False)


def test_default_session_and_channels_paths_are_under_data(tmp_path):
    s = TelegramSettings.from_env({"TG_API_ID": "1", "TG_API_HASH": "h"}, require_session=False)
    assert s.session_path == tmp_path / "data" / "telegram"


def test_settings_repr_hides_secrets(tmp_path):
    s = TelegramSettings.from_env(env(tmp_path, TG_PROXY="socks5://u:pw@h.example:1"), require_session=False)
    text = repr(s)
    assert SECRET_HASH not in text and "pw" not in text


def test_login_without_settings_returns_2(capsys):
    assert tl.login() == 2
    assert "Не настроено" in capsys.readouterr().out


def test_make_client_arguments(tmp_path, monkeypatch):
    captured = {}

    class Dummy:
        def __init__(self, *args, **kwargs):
            captured["args"], captured["kwargs"] = args, kwargs

    monkeypatch.setattr(tl, "TelegramClient", Dummy)
    s = TelegramSettings.from_env(env(tmp_path, TG_PROXY="socks5://p:1"), require_session=False)
    tl.make_client(s)
    assert captured["args"][0] == str(tmp_path / "sess") and captured["args"][1] == 12345
    assert captured["kwargs"]["proxy"][:3] == ("socks5", "p", 1)
    assert captured["kwargs"]["flood_sleep_threshold"] == 0


# ---------------------------------------------------------------- только чтение

def test_read_only_blocks_every_write():
    fake = FakeTelegram({"c_chan": []})
    ro = ReadOnlyClient(fake)
    for name in ("send_message", "send_file", "forward_messages", "edit_message", "delete_messages",
                 "send_read_acknowledge", "pin_message", "action", "start", "sign_in", "log_out", "upload_file"):
        with pytest.raises(ReadOnlyViolation):
            getattr(ro, name)
    assert fake.sent == []


def test_send_message_fails_even_when_called():
    fake = FakeTelegram({"c_chan": []})
    ro = ReadOnlyClient(fake)

    async def go():
        await ro.send_message("me", "привет")

    with pytest.raises(ReadOnlyViolation):
        run(go())
    assert fake.sent == []


def test_raw_requests_and_attribute_writes_blocked():
    fake = FakeTelegram({})
    ro = ReadOnlyClient(fake)

    async def go():
        await ro(SimpleNamespace(name="SendMessageRequest"))

    with pytest.raises(ReadOnlyViolation):
        run(go())
    with pytest.raises(ReadOnlyViolation):
        ro.send_message = lambda *a: None
    assert fake.sent == []


def test_read_calls_pass_through():
    fake = FakeTelegram({"c_chan": [FakeMessage(1, "x")]})
    ro = ReadOnlyClient(fake)

    async def go():
        await ro.connect()
        assert await ro.is_user_authorized()
        ent = await ro.get_input_entity("c_chan")
        got = [m async for m in ro.iter_messages(ent, limit=5)]
        await ro.disconnect()
        return got

    assert [m.id for m in run(go())] == [1]


def test_real_telethon_client_is_blocked_too(tmp_path):
    from telethon import TelegramClient

    client = TelegramClient(str(tmp_path / "s"), 1, "h")
    ro = ReadOnlyClient(client)
    with pytest.raises(ReadOnlyViolation):
        _ = ro.send_message
    assert callable(ro.iter_messages)


# ---------------------------------------------------------------- фото

def test_save_thumbnail_shrinks_and_names_by_hash(tmp_path):
    data = make_jpeg(2, size=2000)
    p = tl.save_thumbnail(data, tmp_path / "m")
    assert p.suffix == ".jpg" and len(p.stem) == 16 and p.is_absolute()
    img = Image.open(p)
    assert img.format == "JPEG" and max(img.size) == tl.THUMB_SIDE
    assert tl.save_thumbnail(data, tmp_path / "m") == p
    assert tl.save_thumbnail(make_jpeg(3), tmp_path / "m") != p


def test_save_thumbnail_rejects_garbage(tmp_path):
    with pytest.raises(Exception):  # noqa: B017 - PIL бросает разные ошибки
        tl.save_thumbnail(b"not an image", tmp_path / "m")


# ---------------------------------------------------------------- сообщения в посты

def test_group_units_joins_contiguous_album():
    ms = [FakeMessage(1), FakeMessage(2, grouped_id=7), FakeMessage(3, grouped_id=7), FakeMessage(4), FakeMessage(5, grouped_id=7)]
    assert [[m.id for m in u] for u in tl.group_units(ms)] == [[1], [2, 3], [4], [5]]


def test_unit_post_fields():
    m = FakeMessage(10, "Нужен повар", NOW, views=120, entities=[SimpleNamespace(url="https://hh.ru/vacancy/1"),
                                                                 SimpleNamespace(url="tg://x"), SimpleNamespace()])
    p = tl.unit_post("Chan_One", [m], ["file:///a.jpg"])
    assert (p.source_type, p.source, p.external_id) == ("telegram", "Chan_One", "10")
    assert p.url == "https://t.me/Chan_One/10" and p.text == "Нужен повар"
    assert p.published_at == "2026-10-29T12:00:00+00:00" and p.image_urls == ["file:///a.jpg"]
    assert p.extra == {"views": 120, "links": ["https://hh.ru/vacancy/1"]}


def test_unit_post_album_takes_first_id_and_longest_text():
    ms = [FakeMessage(12, "", NOW, 5, photo=b"x"), FakeMessage(11, "Подпись к альбому", NOW, 5, photo=b"x"),
          FakeMessage(13, "", NOW, 5, photo=b"x")]
    p = tl.unit_post("c_chan", ms, [])
    assert p.external_id == "11" and p.text == "Подпись к альбому" and p.extra["album"] == 3


def test_unit_post_skips_service_and_empty():
    assert tl.unit_post("c_chan", [FakeMessage(1, "текст", action=object())], []) is None
    assert tl.unit_post("c_chan", [FakeMessage(2, "  ")], []) is None
    assert tl.unit_post("c_chan", [FakeMessage(3, "", photo=b"x")], []) is not None


def test_unit_post_flags_and_dates():
    naive = datetime(2026, 10, 29, 9, 0)
    p = tl.unit_post("c_chan", [FakeMessage(1, "т", naive, video=True, fwd_from=True)], [])
    assert p.extra == {"has_video": True, "forwarded": True} and p.published_at == "2026-10-29T09:00:00+00:00"
    tz = timezone(timedelta(hours=3))
    p2 = tl.unit_post("c_chan", [FakeMessage(1, "т", datetime(2026, 10, 29, 12, 0, tzinfo=tz))], [])
    assert p2.published_at == "2026-10-29T09:00:00+00:00"
    assert tl.unit_post("c_chan", [FakeMessage(1, "т", None)], []).published_at is None


def test_channel_lookup_uses_ids_then_username():
    from rabota.realtime.ingest import Ingestor

    s = TelegramSettings(1, "h", session_path=__import__("pathlib").Path("/x"), channels=("Chan_One",))
    L = tl.TelegramListener(s, Ingestor(":memory:"))
    L._by_id[1000] = "Chan_One"
    assert L._channel_of(SimpleNamespace(chat_id=1000)) == "Chan_One"
    assert L._channel_of(SimpleNamespace(chat_id=5, chat=SimpleNamespace(username="CHAN_one"))) == "Chan_One"
    assert L._channel_of(SimpleNamespace(chat_id=5, chat=None)) is None
    assert UTC
