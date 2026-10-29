"""Слушатель Telegram: пользовательская сессия Telethon, только чтение.

    python -m rabota tg-login     один раз, вручную: телефон, код из Telegram, пароль двухфакторной защиты
    python -m rabota worker       слушатель работает внутри воркера

Что делает слушатель:
  - события NewMessage по каналам из списка дают посты сразу;
  - при старте и каждые TG_SWEEP_SECONDS (по умолчанию 5 минут) догоняет пропущенное через iter_messages:
    не глубже срока жизни вакансии и не больше CATCHUP_LIMIT сообщений на канал, так что перезапуск,
    обрыв связи и каналы, на которые аккаунт не подписан, не теряют посты;
  - состояние (последний id) на канал лежит в таблице ingest_state;
  - переподключается с нарастающей паузой, на FloodWait спит нужное время с запасом.

Клиент обёрнут в ReadOnlyClient: отправка сообщений и любые записывающие вызовы запрещены.
Настройки только из окружения, секреты в журнал не пишутся.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import os
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from rabota import config
from rabota.models import RawPost
from rabota.realtime import state
from rabota.realtime.ingest import Ingestor
from rabota.realtime.scheduler import backoff_delay

try:  # слушатель необязателен: без telethon воркер работает, Telegram пропускается
    from telethon import TelegramClient, errors, events
except ImportError:  # pragma: no cover - проверяется отдельным тестом через подмену
    TelegramClient = errors = events = None  # type: ignore[assignment]

log = logging.getLogger("rabota.realtime.telegram")

SOURCE_TYPE = "telegram"
FIRST_RUN_LIMIT = 30          # первый запуск: до стольких последних постов за срок жизни вакансии
CATCHUP_LIMIT = 200           # догон: не больше сообщений на канал
MAX_PHOTOS = 4                # фото на пост
THUMB_SIDE = 1280             # длинная сторона уменьшенной копии
FLOOD_MARGIN = 5.0            # запас к паузе FloodWait, секунд
FLOOD_RETRIES = 3
STABLE_SESSION_SECONDS = 120  # сессия дольше этого считается удачной: пауза переподключения сбрасывается

DEFAULT_SESSION = "data/telegram"
DEFAULT_CHANNELS_FILE = "data/channels.txt"


class TelegramNotConfigured(RuntimeError):
    """Не хватает настроек или сессии: слушатель честно пропускается."""


class ReadOnlyViolation(PermissionError):
    """Попытка записывающего вызова через клиент только для чтения."""


class StopRequested(Exception):
    """Остановка по сигналу во время ожидания."""


# ---------------------------------------------------------------- настройки

def parse_proxy(value: str) -> tuple | None:
    """socks5://host:port или socks5://user:pass@host:port -> кортеж Telethon (python-socks). Пусто -> None."""
    value = (value or "").strip()
    if not value:
        return None
    parsed = urlparse(value if "://" in value else "socks5://" + value)
    scheme = parsed.scheme.lower()
    if scheme not in ("socks5", "socks4", "http"):
        raise ValueError("TG_PROXY: поддерживаются socks5://, socks4:// и http://")
    if not parsed.hostname or not parsed.port:
        raise ValueError("TG_PROXY: нужен адрес вида socks5://хост:порт")
    user = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None
    return (scheme, parsed.hostname, parsed.port, True, user, password)


def normalize_channel(line: str) -> str | None:
    """'@name', 'https://t.me/name', 't.me/s/name/123', 'name' -> 'name'. Комментарий и мусор -> None."""
    text = line.split("#", 1)[0].strip()
    if not text:
        return None
    text = re.sub(r"^(?:https?://)?(?:t|telegram)\.me/(?:s/)?", "", text, flags=re.IGNORECASE)
    text = text.lstrip("@").split("/", 1)[0].split("?", 1)[0]
    return text if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,31}", text) else None


def load_channels(path: Path | str | None, base: list[str] | None = None) -> list[str]:
    """Каналы: base (по умолчанию SOURCES сборщика) плюс файл, по строке на канал. Без повторов, регистр сохраняется."""
    if base is None:
        from rabota.collectors.telegram import SOURCES

        base = list(SOURCES)
    result: dict[str, str] = {}
    for name in base:
        norm = normalize_channel(name)
        if norm:
            result.setdefault(norm.lower(), norm)
    if path and Path(path).is_file():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            norm = normalize_channel(line)
            if norm:
                result.setdefault(norm.lower(), norm)
    return list(result.values())


def session_file(session_path: Path | str) -> Path:
    p = Path(session_path)
    return p if p.suffix == ".session" else p.with_name(p.name + ".session")


@dataclass(frozen=True)
class TelegramSettings:
    api_id: int
    api_hash: str = field(repr=False)
    session_path: Path
    proxy: tuple | None = field(default=None, repr=False)
    channels: tuple[str, ...] = ()
    media_dir: Path = Path("data/media")
    lookback_days: int = 21
    sweep_seconds: float = 300.0
    group_wait: float = 2.5          # сколько ждать остальные фото альбома
    catchup_pause: float = 1.0       # пауза между каналами при догоне
    photo_pause: float = 0.3         # пауза после скачивания фото при догоне
    reconnect_base: float = 5.0
    reconnect_cap: float = 300.0
    first_run_limit: int = FIRST_RUN_LIMIT
    catchup_limit: int = CATCHUP_LIMIT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, require_session: bool = True) -> TelegramSettings:
        env = os.environ if env is None else env
        raw_id, api_hash = (env.get("TG_API_ID") or "").strip(), (env.get("TG_API_HASH") or "").strip()
        if not raw_id or not api_hash:
            raise TelegramNotConfigured("не заданы TG_API_ID и TG_API_HASH")
        try:
            api_id = int(raw_id)
        except ValueError:
            raise TelegramNotConfigured("TG_API_ID должен быть числом") from None
        root = config.ROOT
        session = Path((env.get("TG_SESSION_PATH") or "").strip() or root / DEFAULT_SESSION)
        if not session.is_absolute():
            session = root / session
        try:
            proxy = parse_proxy(env.get("TELEGRAM_PROXY") or env.get("TG_PROXY", ""))
        except ValueError as exc:
            raise TelegramNotConfigured(str(exc)) from None
        channels_file = Path((env.get("TG_CHANNELS_FILE") or "").strip() or root / DEFAULT_CHANNELS_FILE)
        if not channels_file.is_absolute():
            channels_file = root / channels_file
        from rabota.vision.images import media_dir

        settings = cls(
            api_id=api_id,
            api_hash=api_hash,
            session_path=session,
            proxy=proxy,
            channels=tuple(load_channels(channels_file)),
            media_dir=media_dir(),
            lookback_days=config.VACANCY_TTL_DAYS,
            sweep_seconds=float(env.get("TG_SWEEP_SECONDS") or 300),
        )
        if require_session and not session_file(session).is_file():
            raise TelegramNotConfigured(
                "нет файла сессии: войдите один раз командой `python -m rabota tg-login`"
            )
        if errors is None:
            raise TelegramNotConfigured("не установлен пакет telethon")
        return settings


# ---------------------------------------------------------------- клиент только для чтения

_READ_ONLY_METHODS = frozenset({
    "connect", "disconnect", "is_connected", "is_user_authorized", "get_me",
    "get_entity", "get_input_entity", "get_messages", "iter_messages", "download_media",
    "add_event_handler", "remove_event_handler", "run_until_disconnected",
})


class ReadOnlyClient:
    """Пропускает только чтение. send_message, send_file, forward, edit, delete, отметка прочитанным и
    сырые запросы client(...) запрещены: аккаунт владельца ничего не отправляет и не меняет."""

    def __init__(self, client: Any) -> None:
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name: str) -> Any:
        if name in _READ_ONLY_METHODS:
            return getattr(object.__getattribute__(self, "_client"), name)
        raise ReadOnlyViolation(f"клиент Telegram только для чтения, вызов {name} запрещён")

    def __setattr__(self, name: str, value: Any) -> None:
        raise ReadOnlyViolation("клиент Telegram только для чтения")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise ReadOnlyViolation("клиент Telegram только для чтения, сырые запросы запрещены")


def make_client(settings: TelegramSettings) -> Any:
    """Клиент Telethon: сессия в файле, прокси из настроек, устройство называется честно."""
    if TelegramClient is None:  # pragma: no cover
        raise TelegramNotConfigured("не установлен пакет telethon")
    settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        str(settings.session_path),
        settings.api_id,
        settings.api_hash,
        proxy=settings.proxy,
        device_model="Rabota Donetsk reader",
        system_version="Linux",
        app_version="1.0",
        lang_code="ru",
        system_lang_code="ru-RU",
        flood_sleep_threshold=0,  # FloodWait всегда поднимается наверх, паузу выбираем сами
    )


def login() -> int:
    """Интерактивный вход: телефон, код, пароль двухфакторной защиты. Запускает владелец вручную."""
    try:
        settings = TelegramSettings.from_env(require_session=False)
    except TelegramNotConfigured as exc:
        print(f"Не настроено: {exc}")
        return 2

    async def go() -> None:
        client = make_client(settings)
        try:
            await client.start()  # спросит телефон, код и пароль в терминале
            me = await client.get_me()
            print(f"Вход выполнен: {getattr(me, 'first_name', '') or 'аккаунт'}.")
        finally:
            await client.disconnect()

    asyncio.run(go())
    path = session_file(settings.session_path)
    if path.is_file():
        path.chmod(0o600)
        print(f"Сессия сохранена в {path}. Файл секретный, в git не кладём.")
    return 0


# ---------------------------------------------------------------- сообщения в посты

def save_thumbnail(data: bytes, media_dir: Path, side: int = THUMB_SIDE) -> Path:
    """Фото в уменьшенную JPEG-копию media_dir/<хеш>.jpg. Повторное фото даёт тот же файл."""
    from PIL import Image

    media_dir.mkdir(parents=True, exist_ok=True)
    path = (media_dir / (hashlib.sha1(data).hexdigest()[:16] + ".jpg")).resolve()
    if path.is_file():
        return path
    img = Image.open(io.BytesIO(data))
    img.load()
    img = img.convert("RGB")
    img.thumbnail((side, side))
    tmp = path.with_suffix(".tmp")
    img.save(tmp, format="JPEG", quality=85)
    tmp.replace(path)
    return path


def group_units(messages: list[Any]) -> list[list[Any]]:
    """Сообщения по возрастанию id в единицы: подряд идущие с одним grouped_id (альбом) склеиваются."""
    units: list[list[Any]] = []
    for m in messages:
        gid = getattr(m, "grouped_id", None)
        if gid and units and getattr(units[-1][0], "grouped_id", None) == gid:
            units[-1].append(m)
        else:
            units.append([m])
    return units


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def unit_post(channel: str, unit: list[Any], image_urls: list[str]) -> RawPost | None:
    """Единица (сообщение или альбом) в RawPost. None для служебных сообщений и пустых."""
    unit = sorted(unit, key=lambda m: m.id)
    if any(getattr(m, "action", None) for m in unit):
        return None
    text = max(((m.raw_text or "") for m in unit), key=len)
    has_photo = any(getattr(m, "photo", None) for m in unit)
    if not text.strip() and not has_photo:
        return None
    first = unit[0]
    extra: dict[str, Any] = {}
    views = [m.views for m in unit if getattr(m, "views", None)]
    if views:
        extra["views"] = max(views)
    links: list[str] = []
    for m in unit:
        for ent in getattr(m, "entities", None) or []:
            url = getattr(ent, "url", None)
            if isinstance(url, str) and url.startswith("http") and url not in links:
                links.append(url)
    if links:
        extra["links"] = links
    if any(getattr(m, "video", None) for m in unit):
        extra["has_video"] = True
    if any(getattr(m, "fwd_from", None) for m in unit):
        extra["forwarded"] = True
    if len(unit) > 1:
        extra["album"] = len(unit)
    published = _aware(getattr(first, "date", None))
    return RawPost(
        source_type=SOURCE_TYPE,
        source=channel,
        external_id=str(first.id),
        url=f"https://t.me/{channel}/{first.id}",
        text=text,
        published_at=published.isoformat() if published else None,
        image_urls=image_urls,
        extra=extra,
    )


# ---------------------------------------------------------------- слушатель

def _raw_exists(conn: Any, channel: str, external_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM raw_posts WHERE source = ? AND external_id = ?", (channel, external_id)
    ).fetchone()
    return row is not None


class TelegramListener:
    def __init__(
        self,
        settings: TelegramSettings,
        ingestor: Ingestor,
        *,
        client_factory: Callable[[TelegramSettings], Any] = make_client,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.ingestor = ingestor
        self._factory = client_factory
        self._sleep = sleep
        self._now = now
        self._monotonic = monotonic
        self._rng = rng or random.Random()
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue[tuple[str, list[Any]]] = asyncio.Queue()
        self._groups: dict[tuple[str, Any], list[Any]] = {}
        self._timers: dict[tuple[str, Any], asyncio.Task] = {}
        self._by_id: dict[int, str] = {}
        self._entities: dict[str, Any] = {}
        self._names = {c.lower(): c for c in settings.channels}
        self.stats: dict[str, int] = {"posts": 0, "new": 0, "vacancies": 0, "sessions": 0}

    # --- служебное

    def key(self, channel: str) -> str:
        return state.source_key(SOURCE_TYPE, channel)

    def request_stop(self) -> None:
        self._stop.set()

    async def _nap(self, seconds: float) -> None:
        """Сон, который прерывается остановкой. После него остановка поднимает StopRequested."""
        if seconds > 0:
            if self._sleep is not None:
                await self._sleep(seconds)
            else:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), seconds)
        if self._stop.is_set():
            raise StopRequested

    async def _guard(self, factory: Callable[[], Awaitable[Any]], what: str) -> Any:
        """Вызов с обработкой FloodWait: спим сколько велено плюс запас и повторяем."""
        for attempt in range(1, FLOOD_RETRIES + 1):
            try:
                return await factory()
            except errors.FloodWaitError as exc:
                wait = float(exc.seconds) + FLOOD_MARGIN
                log.warning("FloodWait %s с (%s), пауза %.0f с", exc.seconds, what, wait)
                if attempt == FLOOD_RETRIES:
                    raise
                await self._nap(wait)

    # --- состояние и ошибки

    async def _mark_all_error(self, message: str) -> None:
        for channel in self.settings.channels:
            await self.ingestor.acall(state.mark_error, self.key(channel), message)

    def _pending_min(self, channel: str) -> int | None:
        ids = [m.id for (ch, _), msgs in self._groups.items() if ch == channel for m in msgs]
        return min(ids) if ids else None

    # --- каналы

    async def _resolve_channels(self, client: ReadOnlyClient) -> None:
        for channel in self.settings.channels:
            if channel in self._entities:
                continue
            try:
                entity = await self._guard(lambda c=channel: client.get_input_entity(c), f"канал {channel}")
            except errors.FloodWaitError:
                raise
            except (ValueError, errors.RPCError) as exc:
                log.warning("канал %s недоступен: %s", channel, type(exc).__name__)
                await self.ingestor.acall(state.mark_error, self.key(channel), f"канал недоступен: {type(exc).__name__}")
                continue
            self._entities[channel] = entity
            ident = getattr(entity, "channel_id", None) or getattr(entity, "id", None)
            if ident:
                self._by_id[int(ident)] = channel
                self._by_id[-(10**12 + int(ident))] = channel
            await self._nap(self.settings.catchup_pause / 4)

    def _channel_of(self, event: Any) -> str | None:
        chat_id = getattr(event, "chat_id", None)
        if chat_id in self._by_id:
            return self._by_id[chat_id]
        username = getattr(getattr(event, "chat", None), "username", None)
        return self._names.get(username.lower()) if username else None

    # --- сообщения

    async def _download_photos(self, client: ReadOnlyClient, unit: list[Any], paced: bool) -> list[str]:
        urls: list[str] = []
        for m in unit:
            if len(urls) >= MAX_PHOTOS:
                break
            if not getattr(m, "photo", None):
                continue
            try:
                data = await self._guard(lambda m=m: client.download_media(m, file=bytes), "фото")
                if not data:
                    continue
                path = save_thumbnail(data, self.settings.media_dir)
            except errors.FloodWaitError:
                raise
            except Exception as exc:  # noqa: BLE001 - не скачалось фото, пост остаётся с текстом
                log.warning("фото сообщения %s не сохранено: %s", m.id, type(exc).__name__)
                continue
            urls.append(path.as_uri())
            if paced:
                await self._nap(self.settings.photo_pause)
        return urls

    async def _build_post(self, client: ReadOnlyClient, channel: str, unit: list[Any], paced: bool) -> RawPost | None:
        from rabota.vision.extract import needs_image_recognition

        text = max(((m.raw_text or "") for m in unit), key=len)
        images: list[str] = []
        # картинка нужна только когда по ней что-то распознаётся (см. needs_image_recognition)
        if any(getattr(m, "photo", None) for m in unit) and needs_image_recognition(text):
            images = await self._download_photos(client, sorted(unit, key=lambda m: m.id), paced)
        return unit_post(channel, unit, images)

    async def _process_units(
        self, client: ReadOnlyClient, channel: str, units: list[list[Any]], *, paced: bool
    ) -> dict[str, int]:
        async with self._lock:
            posts: list[RawPost] = []
            max_id = 0
            for unit in units:
                max_id = max(max_id, *(m.id for m in unit))
                if await self.ingestor.acall(_raw_exists, channel, str(min(m.id for m in unit))):
                    continue  # уже принято (событие и догон видели одно и то же): фото второй раз не качаем
                post = await self._build_post(client, channel, unit, paced)
                if post is not None:
                    posts.append(post)
            totals = await self.ingestor.aingest_many(posts) if posts else {}
            pending = self._pending_min(channel)
            last_id = max_id if pending is None or pending > max_id else max(0, pending - 1)
            await self.ingestor.acall(state.mark_ok, self.key(channel), last_id or None)
        for k in ("posts", "new", "vacancies"):
            self.stats[k] += totals.get(k, 0)
        return totals

    async def _history(self, client: ReadOnlyClient, channel: str, last_id: int | None) -> list[Any]:
        cutoff = self._now() - timedelta(days=self.settings.lookback_days)
        limit = self.settings.first_run_limit if last_id is None else self.settings.catchup_limit
        found: list[Any] = []
        # от новых к старым: если пропущено больше лимита, берём свежее, а не самое старое
        async for m in client.iter_messages(self._entities[channel], limit=limit, min_id=last_id or 0):
            date = _aware(getattr(m, "date", None))
            if date is not None and date < cutoff:
                break
            found.append(m)
        found.sort(key=lambda m: m.id)
        return found

    async def catch_up_channel(self, client: ReadOnlyClient, channel: str) -> dict[str, int]:
        last_id = await self.ingestor.acall(state.get_last_id, self.key(channel))
        messages = await self._guard(lambda: self._history(client, channel, last_id), f"история {channel}")
        return await self._process_units(client, channel, group_units(messages), paced=True)

    async def sweep(self, client: ReadOnlyClient) -> None:
        """Догнать пропущенное по всем каналам. Сбой одного канала не мешает остальным."""
        await self._resolve_channels(client)
        for channel in self.settings.channels:
            if channel not in self._entities:
                continue
            try:
                await self.catch_up_channel(client, channel)
            except errors.FloodWaitError:
                raise
            except (StopRequested, asyncio.CancelledError):
                raise
            except (ValueError, errors.RPCError) as exc:
                log.warning("канал %s: догон не удался: %s", channel, type(exc).__name__)
                await self.ingestor.acall(state.mark_error, self.key(channel), f"{type(exc).__name__}")
                self._entities.pop(channel, None)  # в следующий раз определим заново
            await self._nap(self.settings.catchup_pause)

    # --- живые события

    async def _on_new_message(self, event: Any) -> None:
        channel = self._channel_of(event)
        message = getattr(event, "message", None)
        if channel is None or message is None:
            return
        gid = getattr(message, "grouped_id", None)
        if not gid:
            self._queue.put_nowait((channel, [message]))
            return
        key = (channel, gid)
        self._groups.setdefault(key, []).append(message)
        timer = self._timers.pop(key, None)
        if timer is not None:
            timer.cancel()
        self._timers[key] = asyncio.create_task(self._flush_group_later(key))

    async def _flush_group_later(self, key: tuple[str, Any]) -> None:
        await asyncio.sleep(self.settings.group_wait)
        self._timers.pop(key, None)
        unit = self._groups.pop(key, [])
        if unit:
            self._queue.put_nowait((key[0], unit))

    async def _consume(self, client: ReadOnlyClient) -> None:
        while True:
            channel, unit = await self._queue.get()
            try:
                await self._process_units(client, channel, [sorted(unit, key=lambda m: m.id)], paced=False)
            except errors.FloodWaitError:
                raise
            except Exception as exc:  # noqa: BLE001 - сообщение подберёт следующий догон, так как last_id не сдвинут
                log.warning("канал %s: сообщение не принято: %s: %s", channel, type(exc).__name__, exc)
                await self.ingestor.acall(state.mark_error, self.key(channel), type(exc).__name__)

    async def _sweeper(self, client: ReadOnlyClient) -> None:
        while True:
            await self._nap(self.settings.sweep_seconds * self._rng.uniform(0.9, 1.1))
            await self.sweep(client)

    # --- сессия и цикл переподключения

    async def _session(self) -> None:
        client = ReadOnlyClient(self._factory(self.settings))
        self._entities.clear()
        self._by_id.clear()
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise errors.AuthKeyUnregisteredError(request=None)
            self.stats["sessions"] += 1
            await self._resolve_channels(client)
            if self._entities:
                client.add_event_handler(self._on_new_message, events.NewMessage(chats=list(self._entities.values())))
            await self.sweep(client)
            log.info("telegram: слушаю %d каналов из %d", len(self._entities), len(self.settings.channels))
            tasks = {
                asyncio.create_task(self._consume(client), name="tg-consume"),
                asyncio.create_task(self._sweeper(client), name="tg-sweep"),
                asyncio.create_task(client.run_until_disconnected(), name="tg-run"),
                asyncio.create_task(self._stop.wait(), name="tg-stop"),
            }
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in list(self._timers.values()):
                    t.cancel()
                self._timers.clear()
                self._groups.clear()
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            for t in done:
                if not t.cancelled() and t.exception() is not None and not isinstance(t.exception(), StopRequested):
                    raise t.exception()  # type: ignore[misc]
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Слушать до остановки. Обрывы связи переживает с нарастающей паузой, сессию без прав не повторяет."""
        if stop is not None:
            self._stop = stop
        fatal = (
            errors.AuthKeyUnregisteredError, errors.SessionRevokedError, errors.SessionExpiredError,
            errors.UserDeactivatedError, errors.UserDeactivatedBanError,
        )
        failures = 0
        while not self._stop.is_set():
            started = self._monotonic()
            try:
                await self._session()
                problem = "соединение закрыто"
            except StopRequested:
                break
            except fatal as exc:
                msg = f"сессия Telegram не действует ({type(exc).__name__}): войдите заново командой tg-login"
                log.error("telegram: %s", msg)
                await self._mark_all_error(msg)
                return
            except errors.FloodWaitError as exc:
                problem = f"FloodWait {exc.seconds} с"
                log.warning("telegram: %s", problem)
                try:
                    await self._nap(float(exc.seconds) + FLOOD_MARGIN)
                except StopRequested:
                    break
                continue
            except Exception as exc:  # noqa: BLE001 - любой сбой связи: переподключаемся
                problem = f"{type(exc).__name__}"
                log.warning("telegram: сессия прервана: %s", problem)
            if self._stop.is_set():
                break
            failures = 0 if self._monotonic() - started >= STABLE_SESSION_SECONDS else failures + 1
            await self._mark_all_error(f"переподключение: {problem}")
            delay = backoff_delay(max(failures, 1), self.settings.reconnect_base, self.settings.reconnect_cap)
            delay *= self._rng.uniform(0.8, 1.2)
            log.info("telegram: переподключение через %.0f с", delay)
            try:
                await self._nap(delay)
            except StopRequested:
                break
