"""Скачивание, хеш, кэш и отсев баннеров. Сеть подменена respx."""
from __future__ import annotations

import httpx
import pytest
import respx
from vision_helpers import make_image

from rabota.vision.images import (
    BANNER_MIN_POSTS,
    ImageError,
    VisionCache,
    download_image,
    hash_distance,
    image_hash,
    open_image,
)

URL = "https://sun1.userapi.example/img/a.jpg"


def test_hash_is_stable_and_survives_resize_and_recompress():
    from io import BytesIO

    from PIL import Image

    original = make_image(1, 512)
    h = image_hash(original)
    assert len(h) == 16
    small = Image.open(BytesIO(original)).resize((200, 200))
    buf = BytesIO()
    small.save(buf, "JPEG", quality=60)
    assert hash_distance(h, image_hash(buf.getvalue())) <= 4
    assert hash_distance(h, image_hash(make_image(2, 512))) > 10


def test_hash_of_broken_bytes_raises():
    with pytest.raises(ImageError):
        image_hash(b"not an image")
    with pytest.raises(ImageError):
        open_image(b"")


def test_fixture_poster_hash_matches_sidecar_name(poster_bytes):
    assert image_hash(poster_bytes) == "ce2e99916c936d54"


@respx.mock
def test_download_ok():
    data = make_image(3)
    respx.get(URL).mock(return_value=httpx.Response(200, content=data))
    assert download_image(URL) == data


@respx.mock
def test_download_rejects_oversize_by_stream():
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(ImageError, match="лимит"):
        download_image(URL, max_bytes=1000)


@respx.mock
def test_download_rejects_oversize_by_header():
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"x", headers={"content-length": "99999999"}))
    with pytest.raises(ImageError, match="лимит"):
        download_image(URL, max_bytes=1000)


@respx.mock
def test_download_http_error_and_timeout():
    respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(ImageError, match="404"):
        download_image(URL)
    respx.get(URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(ImageError, match="ConnectTimeout"):
        download_image(URL)


def test_download_rejects_non_http():
    with pytest.raises(ImageError):
        download_image("file:///etc/passwd")


def test_cache_roundtrip_and_near_lookup(tmp_path):
    cache = VisionCache(tmp_path / "c")
    h = image_hash(make_image(4))
    assert cache.get(h) is None
    cache.put(h, {"kind": "ad", "vacancies": [], "raw_text": "x"})
    assert cache.get(h)["kind"] == "ad"
    assert cache.hashes() == [h]
    # соседний хеш (один бит отличается) находится только с max_distance
    near = f"{int(h, 16) ^ 1:016x}"
    assert cache.get(near) is None
    assert cache.get(near, max_distance=2)["kind"] == "ad"


def test_banner_after_repeats_in_distinct_posts(tmp_path):
    cache = VisionCache(tmp_path)
    h = image_hash(make_image(5))
    for i in range(BANNER_MIN_POSTS - 1):
        cache.note_seen(h, f"tg:chan:{i}")
        assert not cache.is_banner(h)
    # повтор того же поста не считается
    assert cache.note_seen(h, "tg:chan:0") == BANNER_MIN_POSTS - 1
    cache.note_seen(h, "tg:chan:last")
    assert cache.is_banner(h)


def test_vacancy_poster_is_never_banner(tmp_path):
    cache = VisionCache(tmp_path)
    h = image_hash(make_image(6))
    cache.put(h, {"kind": "vacancy", "vacancies": [{"profession": "Грузчик"}], "raw_text": ""})
    for i in range(10):
        cache.note_seen(h, f"vk:g:{i}")
    assert not cache.is_banner(h)


def test_manual_blocklist(tmp_path):
    cache = VisionCache(tmp_path)
    h = image_hash(make_image(7))
    assert not cache.is_banner(h)
    cache.block(h)
    assert cache.is_banner(h)
    assert cache.hashes() == []  # служебные файлы не попадают в список ответов


@respx.mock
def test_download_retries_once_after_network_error():
    data = make_image(8)
    route = respx.get(URL).mock(side_effect=[httpx.ConnectError("reset"), httpx.Response(200, content=data)])
    assert download_image(URL) == data
    assert route.call_count == 2
