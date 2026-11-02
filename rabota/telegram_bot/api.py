"""Клиент Bot API. Только методы, которые нужны: приём команд, отправка сообщений, ответ на нажатие кнопки.

Токен живёт в адресе запроса, поэтому тексты ошибок строятся без адреса: наружу попадает только тип сбоя и ответ Telegram.
В тестах вместо HttpBotApi подставляется любой объект с теми же async-методами (см. FakeBotApi в tests/alerts).
"""
from __future__ import annotations

from typing import Any, Protocol

import httpx

API_URL = "https://api.telegram.org/bot{token}/{method}"
LONG_POLL = 25


class BotApiError(Exception):
    """Ошибка Bot API или сети. code: код ответа Telegram, 0 при сетевом сбое. Текст не содержит токена."""

    def __init__(self, code: int, description: str, retry_after: int = 0):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after

    @property
    def chat_gone(self) -> bool:
        """Получатель недоступен насовсем: бот заблокирован, чат удалён, аккаунт закрыт."""
        text = self.description.lower()
        return self.code == 403 or "chat not found" in text or "user is deactivated" in text


class BotApi(Protocol):
    async def get_me(self) -> dict[str, Any]: ...
    async def delete_webhook(self) -> None: ...
    async def get_updates(self, offset: int, timeout: int = LONG_POLL) -> list[dict[str, Any]]: ...
    async def send_message(
        self, chat_id: int | str, text: str, *, reply_markup: dict[str, Any] | None = None, parse_mode: str = "HTML",
    ) -> int: ...
    async def edit_message_text(self, chat_id: int | str, message_id: int, text: str) -> None: ...
    async def answer_callback_query(self, callback_id: str, text: str = "") -> None: ...
    async def aclose(self) -> None: ...


def url_button(text: str, url: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": text, "url": url}]]}


class HttpBotApi:
    def __init__(self, token: str, proxy: str = "", timeout: float = 15.0, client: httpx.AsyncClient | None = None):
        self._token = token
        self._proxy = proxy
        self._timeout = timeout
        self._client = client

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=10.0), proxy=self._proxy or None,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def call(self, method: str, payload: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        try:
            resp = await self._http().post(
                API_URL.format(token=self._token, method=method), json=payload or {},
                timeout=timeout if timeout is not None else self._timeout,
            )
        except httpx.HTTPError as exc:
            raise BotApiError(0, f"сеть: {type(exc).__name__}") from None
        try:
            data = resp.json()
        except ValueError:
            raise BotApiError(resp.status_code, "ответ не JSON") from None
        if not isinstance(data, dict) or not data.get("ok"):
            body = data if isinstance(data, dict) else {}
            params = body.get("parameters") or {}
            raise BotApiError(
                int(body.get("error_code") or resp.status_code), str(body.get("description") or "ошибка")[:200],
                int(params.get("retry_after") or 0),
            )
        return data.get("result")

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe")  # type: ignore[no-any-return]

    async def delete_webhook(self) -> None:
        await self.call("deleteWebhook", {"drop_pending_updates": False})

    async def get_updates(self, offset: int, timeout: int = LONG_POLL) -> list[dict[str, Any]]:
        result = await self.call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query", "my_chat_member"]},
            timeout=timeout + 10.0,
        )
        return result if isinstance(result, list) else []

    async def send_message(
        self, chat_id: int | str, text: str, *, reply_markup: dict[str, Any] | None = None, parse_mode: str = "HTML",
    ) -> int:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = await self.call("sendMessage", payload)
        return int((result or {}).get("message_id") or 0)

    async def edit_message_text(self, chat_id: int | str, message_id: int, text: str) -> None:
        await self.call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
        })

    async def answer_callback_query(self, callback_id: str, text: str = "") -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})
