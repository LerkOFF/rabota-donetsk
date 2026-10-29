"""Лимиты частоты в памяти процесса: окно скользящее, устаревшие ключи периодически вычищаются."""
from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

# bucket: (сколько событий, за сколько секунд)
DEFAULT_LIMITS: dict[str, tuple[int, int]] = {
    "login_ip": (40, 900),
    "login_email": (8, 900),
    "register_ip": (12, 3600),
    "register_email": (3, 3600),
    "resend_ip": (12, 3600),
    "resend_email": (3, 3600),
    "forgot_ip": (12, 3600),
    "forgot_email": (3, 3600),
    "token_ip": (30, 3600),
    "change_email_user": (5, 3600),
    "password_user": (10, 900),
    "delete_user": (5, 900),
}

_MAX_KEYS = 50_000
_SWEEP_EVERY = 500


class RateLimiter:
    def __init__(
        self,
        limits: dict[str, tuple[int, int]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.clock = clock
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def _window(self, bucket: str, key: str) -> tuple[deque[float], int, int] | None:
        limit, window = self.limits.get(bucket, (0, 0))
        if limit <= 0:
            return None
        hits = self._hits.setdefault((bucket, key), deque())
        now = self.clock()
        while hits and now - hits[0] > window:
            hits.popleft()
        return hits, limit, window

    def allow(self, bucket: str, key: str) -> bool:
        """Учесть событие и сказать, можно ли его пропустить. Отказанные события окно не продлевают."""
        with self._lock:
            self._tick()
            state = self._window(bucket, key)
            if state is None:
                return True
            hits, limit, _ = state
            if len(hits) >= limit:
                return False
            hits.append(self.clock())
            return True

    def blocked(self, bucket: str, key: str) -> bool:
        """Исчерпан ли лимит, не учитывая новое событие."""
        with self._lock:
            state = self._window(bucket, key)
            return state is not None and len(state[0]) >= state[1]

    def record(self, bucket: str, key: str) -> None:
        """Учесть событие без проверки (например, неудачный вход)."""
        with self._lock:
            self._tick()
            state = self._window(bucket, key)
            if state is not None:
                state[0].append(self.clock())

    def reset(self, bucket: str, key: str) -> None:
        with self._lock:
            self._hits.pop((bucket, key), None)

    def size(self) -> int:
        return len(self._hits)

    def _tick(self) -> None:
        self._calls += 1
        if self._calls % _SWEEP_EVERY == 0 or len(self._hits) > _MAX_KEYS:
            self.sweep()

    def sweep(self) -> None:
        """Выбросить ключи, у которых все события вышли из окна."""
        now = self.clock()
        for (bucket, key), hits in list(self._hits.items()):
            window = self.limits.get(bucket, (0, 0))[1]
            while hits and now - hits[0] > window:
                hits.popleft()
            if not hits:
                del self._hits[(bucket, key)]
