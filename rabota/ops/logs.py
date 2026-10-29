"""Журналы и счётчики ошибок.

LOG_FORMAT=json: одна строка JSON на запись, удобно для journald и разбора jq. Иначе обычный текст.
Счётчики лежат в памяти процесса и показываются в расширенном /healthz/deep (с токеном).
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from rabota import config

PLAIN_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_MAX_EXC = 3000

# ключи и пароли в журнал не пишем, даже если они попали в текст ошибки случайно
_SECRETS = (
    (re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}"), "bot<скрыто>"),
    (re.compile(r"(?i)\b(token|password|passwd|secret|api[_-]?key|access_token|hash)=([^\s&\"']+)"), r"\1=<скрыто>"),
    (re.compile(r"(?i)(socks5|socks4|http)://[^\s/@:]+:[^\s/@]+@"), r"\1://<скрыто>@"),
    (re.compile(r"(?i)\b(authorization|cookie|x-health-token):\s*\S+"), r"\1: <скрыто>"),
)


def redact(text: str) -> str:
    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)
    return text


class Counters:
    """Потокобезопасные счётчики: ошибки журнала и ответы 5xx/429."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: Counter[str] = Counter()
        self.started_at = datetime.now(UTC)

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._values[name] += n

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)

    def reset(self) -> None:
        with self._lock:
            self._values.clear()


counters = Counters()


class CounterHandler(logging.Handler):
    """Считает записи WARNING и ERROR (в том числе непойманные исключения, которые пишет uvicorn)."""

    def __init__(self, level: int = logging.WARNING) -> None:
        super().__init__(level)

    def emit(self, record: logging.LogRecord) -> None:
        counters.inc("log_error" if record.levelno >= logging.ERROR else "log_warning")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        if record.exc_info:
            data["exc"] = redact(self.formatException(record.exc_info))[-_MAX_EXC:]
        return json.dumps(data, ensure_ascii=False)


def log_format() -> str:
    return "json" if config.get("LOG_FORMAT").strip().lower() == "json" else "text"


def make_formatter() -> logging.Formatter:
    return JsonFormatter() if log_format() == "json" else logging.Formatter(PLAIN_FORMAT)


def setup_logging(level: int = logging.INFO, quiet: tuple[str, ...] = ()) -> None:
    """Настроить корневой журнал: JSON или текст, плюс счётчик ошибок. Повторный вызов заменяет обработчики."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    handler.setFormatter(make_formatter())
    root.addHandler(handler)
    root.addHandler(CounterHandler())
    root.setLevel(level)
    for name in quiet:
        logging.getLogger(name).setLevel(logging.WARNING)


class StatusCounterMiddleware:
    """ASGI: считает ответы по классам (2xx..5xx) и отдельно 429. Тело и заголовки не трогает."""

    def __init__(self, app: Callable[..., Any]) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status = int(message["status"])
                counters.inc(f"http_{status // 100}xx")
                if status == 429:
                    counters.inc("http_429")
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        except Exception:
            counters.inc("http_unhandled")
            raise
