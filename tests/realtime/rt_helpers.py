"""Помощники тестов реального времени: подмена клиента Telethon, часы, тексты постов."""
from __future__ import annotations

import asyncio
import heapq
import io
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from PIL import Image, ImageDraw

from rabota.vision.providers import VisionProvider

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

# короткий пост: картинка нужна (текста мало)
SHORT = "Нужен бариста, Донецк, 4000 за смену, +79490000001"
# полный пост: оплата, телефон и адрес в тексте, картинка не нужна
FULL = (
    "Требуется продавец-кассир в магазин продуктов, г. Донецк, ул. Артёма, 12. Оплата 48 500 руб. в месяц, "
    "график 2/2, оформление по ТК, обучение на месте, форма выдаётся. Звоните по телефону +7 949 000-11-22 "
    "с 9 до 18 или приходите на собеседование."
)


def make_jpeg(seed: int = 1, size: int = 300) -> bytes:
    img = Image.new("RGB", (size, size), (240, 240, 240))
    d = ImageDraw.Draw(img)
    rnd = seed * 2654435761 % 2**32
    for _ in range(12):
        rnd = (rnd * 1103515245 + 12345) % 2**31
        x, y = rnd % (size - 60), (rnd >> 8) % (size - 60)
        d.rectangle((x, y, x + 30 + rnd % 50, y + 20 + (rnd >> 4) % 60), fill=(rnd % 255, (rnd >> 5) % 255, (rnd >> 9) % 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@dataclass
class FakeMessage:
    id: int
    raw_text: str = ""
    date: datetime = NOW
    grouped_id: int | None = None
    photo: bytes | None = None
    entities: list[Any] = field(default_factory=list)
    views: int | None = None
    video: Any = None
    fwd_from: Any = None
    action: Any = None


def msgs(first: int, count: int, *, text: str = SHORT, age_days: float = 0.0, step_minutes: float = 1.0) -> list[FakeMessage]:
    """count сообщений с id first..first+count-1, у каждого свой номер телефона, чтобы не склеивались."""
    out = []
    for i in range(count):
        n = first + i
        when = NOW - timedelta(days=age_days) + timedelta(minutes=step_minutes * i)
        out.append(FakeMessage(n, text.replace("0001", f"{n % 10000:04d}") + f" Вакансия {n}", when))
    return out


class FakeEvent:
    def __init__(self, message: FakeMessage, chat_id: int) -> None:
        self.message = message
        self.chat_id = chat_id
        self.chat = None


class FakeTelegram:
    """Клиент Telethon без сети: каналы с сообщениями, обработчик событий, учёт вызовов."""

    def __init__(self, channels: dict[str, list[FakeMessage]], *, authorized: bool = True) -> None:
        self.channels = {k.lower(): v for k, v in channels.items()}
        self.ids = {name: 1000 + i for i, name in enumerate(self.channels)}
        self.authorized = authorized
        self.handler = None
        self.connected = False
        self.disconnected = asyncio.Event()
        self.calls: list[tuple] = []
        self.sent: list[Any] = []
        self.flood: list[Exception] = []      # исключения, которые отдаст ближайший iter_messages
        self.connect_error: Exception | None = None
        self.unresolvable: set[str] = set()
        self.downloads = 0

    async def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnected.set()

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_input_entity(self, name: str):
        self.calls.append(("resolve", name))
        if name.lower() in self.unresolvable or name.lower() not in self.ids:
            raise ValueError(f"нет канала {name}")
        return SimpleNamespace(channel_id=self.ids[name.lower()], name=name)

    async def iter_messages(self, entity, limit=None, min_id=0, reverse=False):
        self.calls.append(("iter", entity.name, limit, min_id))
        if self.flood:
            raise self.flood.pop(0)
        rows = [m for m in self.channels[entity.name.lower()] if m.id > min_id]
        rows.sort(key=lambda m: m.id, reverse=not reverse)
        for m in rows[: limit if limit else None]:
            await asyncio.sleep(0)
            yield m

    async def download_media(self, message, file=bytes, **kwargs):
        self.downloads += 1
        return message.photo

    def add_event_handler(self, callback, builder=None) -> None:
        self.handler = callback

    async def run_until_disconnected(self) -> None:
        await self.disconnected.wait()

    async def get_me(self):
        return SimpleNamespace(first_name="Тест")

    # записывающие вызовы: тесты проверяют, что до них не доходит
    async def send_message(self, *args, **kwargs):
        self.sent.append(("send_message", args, kwargs))

    async def send_file(self, *args, **kwargs):
        self.sent.append(("send_file", args, kwargs))

    async def __call__(self, request):
        self.sent.append(("request", request))

    # управление из тестов
    def push(self, channel: str, message: FakeMessage) -> None:
        """Живое сообщение: добавить в канал и вызвать обработчик событий."""
        self.channels[channel.lower()].append(message)
        assert self.handler is not None, "обработчик событий ещё не зарегистрирован"
        asyncio.get_running_loop().create_task(self.handler(FakeEvent(message, self.ids[channel.lower()])))


class FakeSleeper:
    """Сон без ожидания: короткие паузы пролетают сразу, длинные (от 100 с) висят до отмены."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if seconds >= 100:
            await asyncio.Event().wait()
        await asyncio.sleep(0)


class FakeClock:
    """Виртуальное время для планировщика: sleep ждёт, пока драйвер не переведёт часы к ближайшему пробуждению."""

    def __init__(self) -> None:
        self.t = 0.0
        self._sleepers: list[tuple[float, int, asyncio.Future]] = []
        self._seq = 0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        fut = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._sleepers, (self.t + max(0.0, seconds), self._seq, fut))
        await fut

    async def drive(self, scheduler, until: float) -> None:
        """Крутить расписание в виртуальном времени до момента until, затем остановить."""
        stop = asyncio.Event()
        task = asyncio.create_task(scheduler.run(stop))
        try:
            while True:
                for _ in range(30):                 # дать всем задачам дойти до очередного сна
                    await asyncio.sleep(0)
                if not self._sleepers:
                    break
                wake, _, fut = self._sleepers[0]
                if wake > until:
                    break
                heapq.heappop(self._sleepers)
                self.t = wake
                if not fut.done():
                    fut.set_result(None)
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)


class ImageProvider(VisionProvider):
    """Ответ картинки: вакансия «Бариста» с телефоном. Считает вызовы."""

    cacheable = False

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result or {
            "kind": "vacancy",
            "vacancies": [{"profession": "Бариста", "phones": ["+79491112233"], "pay_min": 4000, "pay_period": "смена"}],
            "raw_text": "",
        }
        self.calls = 0

    def extract(self, image_bytes: bytes, hint_text: str = "") -> dict[str, Any]:
        self.calls += 1
        return self.result


def run(coro):
    return asyncio.run(coro)
