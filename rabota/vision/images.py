"""Картинки: скачивание, perceptual hash, кэш на диске, отсев повторяющихся баннеров."""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import imagehash
from PIL import Image, UnidentifiedImageError

from rabota.config import ROOT, USER_AGENT

log = logging.getLogger("rabota.vision")

CACHE_DIR = ROOT / "data" / "vision_cache"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DOWNLOAD_TIMEOUT = 20.0
DOWNLOAD_ATTEMPTS = 2
# сколько разных постов должны принести одну и ту же картинку, чтобы считать её баннером
BANNER_MIN_POSTS = 3
# расстояние Хэмминга, при котором две картинки считаются одной (пережатие, ресайз)
NEAR_DISTANCE = 4
SEEN_FILE = "_seen.json"
BLOCKLIST_FILE = "_banners.json"
MAX_KEYS_PER_HASH = 50


class ImageError(Exception):
    """Картинку не удалось скачать или прочитать."""


def download_image(
    url: str,
    *,
    timeout: float = DOWNLOAD_TIMEOUT,
    max_bytes: int = MAX_IMAGE_BYTES,
    client: httpx.Client | None = None,
) -> bytes:
    """Скачивает картинку с лимитом размера. Лимит проверяется по ходу чтения, а не после."""
    if not url.lower().startswith(("http://", "https://")):
        raise ImageError(f"не http-адрес: {url[:80]}")
    own = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    try:
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                return _fetch_once(http, url, timeout, max_bytes)
            except httpx.TransportError as exc:
                # сетевой сбой (обрыв, таймаут): один повтор, дальше сдаёмся
                if attempt == DOWNLOAD_ATTEMPTS:
                    raise ImageError(f"ошибка загрузки {url[:80]}: {type(exc).__name__}") from exc
            except httpx.HTTPError as exc:
                raise ImageError(f"ошибка загрузки {url[:80]}: {type(exc).__name__}") from exc
    finally:
        if own:
            http.close()
    raise ImageError(f"не удалось скачать {url[:80]}")  # недостижимо, для типизации


def _fetch_once(http: httpx.Client, url: str, timeout: float, max_bytes: int) -> bytes:
    with http.stream("GET", url, timeout=timeout) as resp:
        if resp.status_code != 200:
            raise ImageError(f"HTTP {resp.status_code} для {url[:80]}")
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise ImageError(f"картинка больше лимита ({declared} байт)")
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise ImageError(f"картинка больше лимита ({max_bytes} байт)")
    return bytes(buf)


def open_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageError("не удалось прочитать картинку") from exc
    return img


def image_hash(data: bytes) -> str:
    """Perceptual hash (phash, 64 бита) в виде 16 hex-символов."""
    return str(imagehash.phash(open_image(data).convert("RGB")))


def hash_distance(a: str, b: str) -> int:
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class VisionCache:
    """Кэш на диске: <hash>.json с ответом провайдера, учёт повторов и список известных баннеров."""

    def __init__(self, directory: Path | str | None = None):
        self.dir = Path(directory) if directory else CACHE_DIR

    def _path(self, h: str) -> Path:
        return self.dir / f"{h}.json"

    def hashes(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return [p.stem for p in self.dir.glob("*.json") if not p.name.startswith("_")]

    def get(self, h: str, max_distance: int = 0) -> dict[str, Any] | None:
        """Ответ по хешу. С max_distance > 0 ищет и похожие картинки."""
        path = self._path(h)
        if path.is_file():
            data = _read_json(path, None)
            if isinstance(data, dict):
                return data
        if max_distance > 0:
            for other in self.hashes():
                if len(other) == len(h) and hash_distance(h, other) <= max_distance:
                    data = _read_json(self._path(other), None)
                    if isinstance(data, dict):
                        return data
        return None

    def put(self, h: str, data: dict[str, Any]) -> None:
        _write_json(self._path(h), data)

    # повторы и баннеры

    def _canonical(self, seen: dict[str, list[str]], h: str) -> str:
        if h in seen:
            return h
        for other in seen:
            if len(other) == len(h) and hash_distance(h, other) <= 2:
                return other
        return h

    def note_seen(self, h: str, post_key: str) -> int:
        """Запоминает, что картинка встретилась в посте. Возвращает число разных постов с ней."""
        path = self.dir / SEEN_FILE
        seen: dict[str, list[str]] = _read_json(path, {})
        key = self._canonical(seen, h)
        keys = seen.setdefault(key, [])
        if post_key not in keys:
            keys.append(post_key)
            del keys[:-MAX_KEYS_PER_HASH]
            _write_json(path, seen)
        return len(keys)

    def seen_count(self, h: str) -> int:
        seen: dict[str, list[str]] = _read_json(self.dir / SEEN_FILE, {})
        return len(seen.get(self._canonical(seen, h), []))

    def block(self, h: str) -> None:
        """Вручную помечает картинку как баннер."""
        path = self.dir / BLOCKLIST_FILE
        blocked: list[str] = _read_json(path, [])
        if h not in blocked:
            blocked.append(h)
            _write_json(path, blocked)

    def is_blocked(self, h: str) -> bool:
        blocked: list[str] = _read_json(self.dir / BLOCKLIST_FILE, [])
        return any(len(b) == len(h) and hash_distance(h, b) <= 2 for b in blocked)

    def is_banner(self, h: str, min_posts: int = BANNER_MIN_POSTS) -> bool:
        """Баннер: в ручном списке или повторяется в min_posts и более разных постах.

        Картинка, по которой уже есть ответ kind=vacancy, баннером не считается: афишу
        вакансии могут перепостить в нескольких каналах.
        """
        cached = self.get(h, max_distance=2)
        if cached is not None and cached.get("kind") == "vacancy":
            return False
        if self.is_blocked(h):
            return True
        return self.seen_count(h) >= min_posts
