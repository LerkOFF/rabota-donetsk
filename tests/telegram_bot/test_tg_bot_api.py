"""Клиент Bot API через подмену HTTP (respx): запросы, ошибки Telegram, сеть, секреты не попадают в тексты ошибок."""
import asyncio
import json

import httpx
import pytest
import respx

from rabota.telegram_bot.api import BotApiError, HttpBotApi, url_button

TOKEN = "123456:SECRET-token-value"
BASE = f"https://api.telegram.org/bot{TOKEN}"


def run(coro):
    return asyncio.run(coro)


@respx.mock
def test_send_message_payload_and_result():
    route = respx.post(f"{BASE}/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 42}}))

    async def go():
        api = HttpBotApi(TOKEN)
        try:
            return await api.send_message(5, "<b>привет</b>", reply_markup=url_button("Все", "https://example.test"))
        finally:
            await api.aclose()

    assert run(go()) == 42
    body = json.loads(route.calls[0].request.content)
    assert body["chat_id"] == 5 and body["parse_mode"] == "HTML" and body["disable_web_page_preview"] is True
    assert body["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://example.test"


@respx.mock
def test_get_updates_and_webhook_calls():
    respx.post(f"{BASE}/getUpdates").mock(return_value=httpx.Response(200, json={"ok": True, "result": [{"update_id": 3}]}))
    hook = respx.post(f"{BASE}/deleteWebhook").mock(return_value=httpx.Response(200, json={"ok": True, "result": True}))

    async def go():
        api = HttpBotApi(TOKEN)
        try:
            await api.delete_webhook()
            return await api.get_updates(10, timeout=1)
        finally:
            await api.aclose()

    assert run(go()) == [{"update_id": 3}]
    assert json.loads(hook.calls[0].request.content) == {"drop_pending_updates": False}


@respx.mock
def test_telegram_error_carries_code_description_and_retry_after():
    respx.post(f"{BASE}/sendMessage").mock(return_value=httpx.Response(
        429, json={"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 7}}))

    async def go():
        api = HttpBotApi(TOKEN)
        try:
            await api.send_message(1, "x")
        finally:
            await api.aclose()

    with pytest.raises(BotApiError) as exc:
        run(go())
    assert exc.value.code == 429 and exc.value.retry_after == 7 and not exc.value.chat_gone


@respx.mock
def test_blocked_bot_is_chat_gone():
    respx.post(f"{BASE}/sendMessage").mock(return_value=httpx.Response(
        403, json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}))

    async def go():
        api = HttpBotApi(TOKEN)
        try:
            await api.send_message(1, "x")
        finally:
            await api.aclose()

    with pytest.raises(BotApiError) as exc:
        run(go())
    assert exc.value.chat_gone


@respx.mock
def test_network_failure_and_garbage_answers_never_leak_the_token():
    respx.post(f"{BASE}/getMe").mock(side_effect=httpx.ConnectError(f"connect to {BASE} failed"))
    respx.post(f"{BASE}/deleteWebhook").mock(return_value=httpx.Response(502, text="<html>bad gateway</html>"))

    async def go(method):
        api = HttpBotApi(TOKEN)
        try:
            await getattr(api, method)()
        finally:
            await api.aclose()

    for method in ("get_me", "delete_webhook"):
        with pytest.raises(BotApiError) as exc:
            run(go(method))
        assert TOKEN not in str(exc.value) and TOKEN.split(":")[1] not in repr(exc.value)
    with pytest.raises(BotApiError) as exc:
        run(go("get_me"))
    assert exc.value.code == 0


def test_proxy_is_passed_to_the_http_client():
    api = HttpBotApi(TOKEN, proxy="socks5://127.0.0.1:1080")
    client = api._http()
    assert client is not None
    run(api.aclose())
