"""Логотип компании: проверка типа и размера, перекодирование, разбор multipart."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from rabota.employers import logo


def encode(size=(300, 200), fmt="PNG", mode="RGB", color=(200, 30, 30), **kw) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, fmt, **kw)
    return buf.getvalue()


def opened(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_accepts_png_jpeg_webp_and_outputs_small_png(fmt):
    out = logo.process_logo(encode(fmt=fmt))
    img = opened(out)
    assert img.format == "PNG" and max(img.size) == logo.OUT_SIDE


def test_small_image_is_not_enlarged():
    img = opened(logo.process_logo(encode(size=(64, 64))))
    assert img.size == (64, 64)


def test_alpha_is_kept():
    out = logo.process_logo(encode(size=(100, 100), mode="RGBA", color=(1, 2, 3, 0)))
    assert opened(out).mode == "RGBA"


@pytest.mark.parametrize("data,code", [
    (b"", "empty"),
    (b"GIF89a" + b"\x00" * 40, "broken"),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "broken"),
    (b"MZ\x90\x00" + b"\x00" * 200, "broken"),
    (b"\x89PNG\r\n\x1a\n" + b"junk" * 20, "broken"),
])
def test_rejects_non_images(data, code):
    with pytest.raises(logo.LogoError) as exc:
        logo.process_logo(data)
    assert exc.value.code == code and exc.value.text


def test_rejects_gif_even_if_valid():
    with pytest.raises(logo.LogoError) as exc:
        logo.process_logo(encode(fmt="GIF", mode="P", color=1))
    assert exc.value.code == "format"


def test_rejects_big_file():
    with pytest.raises(logo.LogoError) as exc:
        logo.process_logo(b"x" * (logo.MAX_BYTES + 1))
    assert exc.value.code == "big_file"


def test_rejects_huge_dimensions():
    with pytest.raises(logo.LogoError) as exc:
        logo.process_logo(encode(size=(logo.MAX_SIDE + 1, 40)))
    assert exc.value.code == "big_side"


def test_rejects_too_small():
    with pytest.raises(logo.LogoError) as exc:
        logo.process_logo(encode(size=(20, 20)))
    assert exc.value.code == "small_side"


def test_rejects_animated_webp():
    frames = [Image.new("RGB", (64, 64), c) for c in ((255, 0, 0), (0, 255, 0))]
    buf = io.BytesIO()
    frames[0].save(buf, "WEBP", save_all=True, append_images=frames[1:], duration=100)
    with pytest.raises(logo.LogoError) as exc:
        logo.process_logo(buf.getvalue())
    assert exc.value.code == "animated"


def test_executable_content_in_file_does_not_survive():
    payload = b"<?php system($_GET['c']); ?><script>alert(1)</script>"
    out = logo.process_logo(encode(fmt="PNG") + payload)
    assert payload not in out and b"<script" not in out and b"<?php" not in out
    assert opened(out).format == "PNG"


def test_metadata_is_stripped():
    img = Image.new("RGB", (120, 120), (5, 5, 5))
    exif = Image.Exif()
    exif[0x010E] = "секретное описание"
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    out = logo.process_logo(buf.getvalue())
    assert b"Exif" not in out and "секретное".encode() not in out
    assert not opened(out).info.get("exif")


def test_png_text_chunks_are_stripped():
    from PIL.PngImagePlugin import PngInfo

    meta = PngInfo()
    meta.add_text("Comment", "<script>x</script>")
    buf = io.BytesIO()
    Image.new("RGB", (100, 100)).save(buf, "PNG", pnginfo=meta)
    out = logo.process_logo(buf.getvalue())
    assert b"tEXt" not in out and b"<script>" not in out


def test_safe_name_and_url():
    assert logo.safe_name("12-0123456789.png", 12) == "12-0123456789.png"
    assert logo.safe_name("12-0123456789.png", 13) is None
    for bad in ("../etc/passwd", "12-xyz.png", "12-0123456789.svg", "", None, "12-0123456789.png/../a"):
        assert logo.safe_name(bad) is None
    assert logo.logo_url({"id": 12, "logo_path": "12-0123456789.png"}) == "/company/12/logo?v=0123456789"
    assert logo.logo_url({"id": 12, "logo_path": None}) == ""
    assert logo.logo_url({"id": 12, "logo_path": "13-0123456789.png"}) == ""


def test_save_replaces_old_files(tmp_path):
    d = tmp_path / "logos"
    first = logo.save_logo(d, 5, b"one")
    (d / "6-aaaaaaaaaa.png").write_bytes(b"other company")
    second = logo.save_logo(d, 5, b"two")
    assert first != second and not (d / first).exists() and (d / second).read_bytes() == b"two"
    assert (d / "6-aaaaaaaaaa.png").exists()
    logo.remove_logo(d, 5)
    assert not (d / second).exists() and (d / "6-aaaaaaaaaa.png").exists()


def test_parse_multipart_binary_safe():
    data = bytes(range(256)) * 4 + b"\r\n--XX--\r\nend"
    body = (b'--XX\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\nabc\r\n'
            b'--XX\r\nContent-Disposition: form-data; name="logo"; filename="a.png"\r\nContent-Type: image/png\r\n\r\n'
            + data + b"\r\n--XX--\r\n")
    fields, files = logo.parse_multipart("multipart/form-data; boundary=XX", body)
    assert fields == {"csrf_token": "abc"}
    assert files["logo"][0] == "a.png"


def test_parse_multipart_rejects_other_types():
    with pytest.raises(logo.LogoError):
        logo.parse_multipart("application/x-www-form-urlencoded", b"a=b")
    with pytest.raises(logo.LogoError):
        logo.parse_multipart("multipart/form-data", b"x")


def test_error_text_unknown_code():
    assert logo.error_text("nope") == "Не удалось сохранить логотип."
    assert logo.error_text("format").startswith("Подходят только")
