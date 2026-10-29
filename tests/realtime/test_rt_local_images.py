"""Скачивание картинок понимает file:// и читает только папку медиа."""
from __future__ import annotations

import pytest
from rt_helpers import make_jpeg

from rabota.vision import images
from rabota.vision.images import ImageError, download_image


@pytest.fixture
def media(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    return d


def test_reads_local_file(media):
    f = media / "a.jpg"
    f.write_bytes(make_jpeg(1))
    assert download_image(f.as_uri()) == f.read_bytes()


def test_percent_encoded_and_plain_forms(media):
    f = media / "фото 1.jpg"
    f.write_bytes(b"x")
    assert download_image(f.as_uri()) == b"x"
    assert download_image("file://" + str(f)) == b"x"


def test_file_outside_media_dir_is_refused(tmp_path, media):
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"s")
    with pytest.raises(ImageError):
        download_image(secret.as_uri())
    with pytest.raises(ImageError):
        download_image((media / ".." / "secret.txt").as_uri())


def test_symlink_escape_is_refused(tmp_path, media):
    (tmp_path / "secret.txt").write_bytes(b"s")
    (media / "link.jpg").symlink_to(tmp_path / "secret.txt")
    with pytest.raises(ImageError):
        download_image((media / "link.jpg").as_uri())


def test_missing_file_and_remote_host(media):
    with pytest.raises(ImageError):
        download_image((media / "none.jpg").as_uri())
    with pytest.raises(ImageError):
        download_image("file://evil.example/etc/passwd")


def test_size_limit(media):
    f = media / "big.jpg"
    f.write_bytes(b"0" * 100)
    with pytest.raises(ImageError):
        download_image(f.as_uri(), max_bytes=10)


def test_http_still_works_and_other_schemes_refused():
    with pytest.raises(ImageError):
        download_image("ftp://example.com/a.jpg")


def test_media_dir_default_without_env(monkeypatch):
    monkeypatch.delenv("MEDIA_DIR")
    assert images.media_dir() == images.ROOT / "data" / "media"
