"""Общие помощники тестов подписок: чистая база, вакансии, пользователи, поддельный клиент Bot API."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from rabota.accounts import passwords
from rabota.accounts.tokens import iso
from rabota.db import save_vacancy
from rabota.models import Vacancy
from rabota.telegram_bot.api import BotApiError

NOW = datetime.now(UTC).replace(microsecond=0)
SECRET = b"alerts-test-secret-" + b"x" * 16
SITE = "https://example.test"


def vac(conn, **kw: Any) -> int:
    """Вакансия, видимая на сайте (опубликована только что), поля переопределяются."""
    kw.setdefault("profession", "бариста")
    kw.setdefault("city", "Донецк")
    kw.setdefault("category", "общепит")
    kw.setdefault("summary", "Готовить кофе.")
    kw.setdefault("contacts", {"phones": ["+79490000000"]})
    kw.setdefault("source_type", "telegram")
    kw.setdefault("source", "@demo")
    kw.setdefault("published_at", (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds"))
    return save_vacancy(conn, Vacancy(**kw))


def user(conn, email: str = "anna@example.test", *, verified: bool = True, status: str = "active", role: str = "job_seeker") -> dict[str, Any]:
    cur = conn.execute(
        "INSERT INTO users(email, password_hash, role, name, status, email_verified_at, consent_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (email, passwords.hash_password("tulip-garden-77"), role, "Анна", status, iso(NOW) if verified else None, iso(NOW), iso(NOW)),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


class FakeBotApi:
    """Подмена клиента Bot API: ничего не отправляет по сети, запоминает вызовы и отдаёт заготовленные ответы."""

    def __init__(self, updates: list[list[dict[str, Any]]] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.answered: list[tuple[str, str]] = []
        self.batches = list(updates or [])
        self.errors: dict[Any, BotApiError] = {}     # chat_id -> ошибка при отправке
        self.fail_all: BotApiError | None = None
        self.poll_error: BotApiError | None = None
        self.closed = False
        self.webhook_deleted = False
        self.offsets: list[int] = []
        self._id = 100

    async def get_me(self) -> dict[str, Any]:
        return {"id": 1, "username": "test_bot"}

    async def delete_webhook(self) -> None:
        self.webhook_deleted = True

    async def get_updates(self, offset: int, timeout: int = 25) -> list[dict[str, Any]]:
        self.offsets.append(offset)
        if self.poll_error is not None:
            raise self.poll_error
        return self.batches.pop(0) if self.batches else []

    async def send_message(self, chat_id, text, *, reply_markup=None, parse_mode="HTML") -> int:
        if self.fail_all is not None:
            raise self.fail_all
        if chat_id in self.errors:
            raise self.errors[chat_id]
        self._id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup, "parse_mode": parse_mode})
        return self._id

    async def edit_message_text(self, chat_id, message_id, text) -> None:
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})

    async def answer_callback_query(self, callback_id: str, text: str = "") -> None:
        self.answered.append((callback_id, text))

    async def aclose(self) -> None:
        self.closed = True
