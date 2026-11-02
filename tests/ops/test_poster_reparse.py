"""Метка «нужна афиша» и перечтение постов: мусорная вакансия заменяется, нечитаемая афиша ждёт, повторный запуск ничего не дублирует.

Сеть и Tesseract подменены: картинка отдаётся из словаря, провайдер читает «распознанный» текст по её байтам.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rabota import __main__ as cli
from rabota.db import connect, save_raw
from rabota.models import RawPost
from rabota.ops import poster_reparse as pr
from rabota.realtime.ingest import ingest_post
from rabota.vision import ocr
from rabota.vision.images import VisionCache
from rabota.vision.providers import VisionProvider

TEXT_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "vision" / "ocr_text"
GOOD = (TEXT_DIR / "poster_detailing.txt").read_text(encoding="utf-8")
NOISE = '„оо\nee hE\n| ] >\nsf a cf\n=\n.) №:\npce @\n+79495055755\nxq zz ' * 3
CAPTION = "📞 По всем вопросам обращаться по телефону:\n+7 949 505-57-55"


def picture(tag: int) -> bytes:
    """Маленькая картинка, разная для разных tag (текст на ней «читает» подставной провайдер)."""
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (128, 128), (tag * 19 % 256, tag * 37 % 256, tag * 53 % 256))
    ImageDraw.Draw(img).rectangle((tag % 90, tag * 7 % 90, tag % 90 + 30, tag * 7 % 90 + 30), fill=(255, 255, 255))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


class FakeOcr(VisionProvider):
    """Читает картинку «как Tesseract»: текст по байтам из словаря, с версией, как у настоящего провайдера."""

    cacheable = True
    version = ocr.OCR_VERSION

    def __init__(self, texts: dict[bytes, str]):
        self.texts = texts
        self.calls = 0

    def extract(self, image_bytes: bytes, hint_text: str = ""):
        self.calls += 1
        return ocr.result_from_text(self.texts[image_bytes], hint_text)


@pytest.fixture
def conn():
    return connect(":memory:")


@pytest.fixture
def cache(tmp_path):
    return VisionCache(tmp_path / "cache")


def post(ext="31217", text=CAPTION, url="https://cdn.example/a.jpg", source="Vakansiii_dnr") -> RawPost:
    return RawPost("telegram", source, ext, f"https://t.me/{source}/{ext}", text, image_urls=[url])


def vacancies(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM vacancies ORDER BY id")]


def test_unreadable_poster_makes_no_vacancy_and_marks_the_post(conn, cache):
    img = picture(1)
    provider = FakeOcr({img: NOISE})
    raw = post()
    from rabota.realtime import ingest

    # кэш и сеть подменены через провайдера: картинку отдаёт fetch
    res = ingest.new_image_vacancies(raw, provider, set(), conn, fetch=lambda u: img, cache=cache)
    assert res == []
    state = pr.get_state(conn, raw.source, raw.external_id)
    assert state["status"] == pr.NEEDS and state["ocr_version"] == ocr.OCR_VERSION


def test_ingest_post_with_noise_poster_does_not_publish_junk(conn, monkeypatch, cache):
    img = picture(2)
    monkeypatch.setattr("rabota.vision.extract.download_image", lambda url, **kw: img)
    monkeypatch.setattr("rabota.vision.extract.VisionCache", lambda *a, **k: cache)
    stats = ingest_post(conn, post(), FakeOcr({img: NOISE}))
    assert stats["new"] and stats["vacancies"] == 0
    assert vacancies(conn) == []
    assert pr.get_state(conn, "Vakansiii_dnr", "31217")["status"] == pr.NEEDS


def test_ingest_post_with_readable_poster_publishes_and_marks_read(conn, monkeypatch, cache):
    img = picture(3)
    monkeypatch.setattr("rabota.vision.extract.download_image", lambda url, **kw: img)
    monkeypatch.setattr("rabota.vision.extract.VisionCache", lambda *a, **k: cache)
    stats = ingest_post(conn, post(), FakeOcr({img: GOOD}))
    assert stats["from_images"] == 1
    [vac] = vacancies(conn)
    assert vac["profession"] == "автомойщик" and vac["status"] == "active" and vac["moderation_status"] == "approved"
    assert json.loads(vac["contacts"])["phones"] == ["+79495055755"]
    assert pr.get_state(conn, "Vakansiii_dnr", "31217")["status"] == pr.READ


def test_post_with_long_text_is_not_marked(conn, cache):
    img = picture(4)
    long_text = "Продаём цемент оптом, доставка по Донецку. " * 8
    provider = FakeOcr({img: NOISE})
    from rabota.realtime import ingest

    ingest.new_image_vacancies(post("1", long_text), provider, set(), conn, fetch=lambda u: img, cache=cache)
    assert pr.get_state(conn, "Vakansiii_dnr", "1")["status"] == pr.READ  # длинный текст сам по себе: ждать афишу нечего


def test_needs_retry_when_version_is_older_or_images_changed():
    raw = post()
    state = {"status": pr.NEEDS, "ocr_version": ocr.OCR_VERSION, "image_key": pr.urls_key(raw.image_urls)}
    assert not pr.needs_retry(state, raw)
    assert pr.needs_retry({**state, "ocr_version": ocr.OCR_VERSION - 1}, raw)
    assert pr.needs_retry(state, post(url="https://cdn.example/other.jpg"))
    assert not pr.needs_retry({**state, "status": pr.READ, "ocr_version": 0}, raw)
    assert not pr.needs_retry(None, raw)


def legacy_junk(conn, raw: RawPost, profession="„оо") -> int:
    """Мусорная вакансия, оставшаяся на сайте со времён старого распознавания (как id 1088)."""
    save_raw(conn, raw)
    cur = conn.execute(
        "INSERT INTO vacancies(profession, source_type, source, source_url, external_id, contacts, kind, status, suspicious)"
        " VALUES (?,?,?,?,?,?,?,?,0)",
        (profession, raw.source_type, raw.source, raw.url, raw.external_id, '{"phones": ["+79495055755"]}', "vacancy", "active"),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_reparse_replaces_junk_vacancy_with_the_read_poster(conn, cache):
    img = picture(5)
    raw = post(url="u1")
    junk_id = legacy_junk(conn, raw)
    res = pr.reparse(conn, FakeOcr({img: GOOD}), fetch=lambda u: img, cache=cache)
    assert res["candidates"] == 1 and res["created"] == 1 and res["replaced"] == 1
    rows = {r["profession"]: r for r in vacancies(conn)}
    assert rows["автомойщик"]["status"] == "active" and rows["автомойщик"]["district"] == "Ленинский"
    old = rows["„оо"]
    assert old["id"] == junk_id and old["status"] == "hidden" and old["moderation_status"] == "rejected"
    assert old["moderation_note"].startswith(pr.REPLACED_NOTE)
    assert pr.get_state(conn, raw.source, raw.external_id)["status"] == pr.READ


def test_reparse_is_idempotent(conn, cache):
    img = picture(6)
    legacy_junk(conn, post(url="u2"))
    provider = FakeOcr({img: GOOD})
    pr.reparse(conn, provider, fetch=lambda u: img, cache=cache)
    before = vacancies(conn)
    again = pr.reparse(conn, provider, fetch=lambda u: img, cache=cache)
    assert again["candidates"] == 0 and again["created"] == 0 and again["replaced"] == 0
    assert vacancies(conn) == before


def test_reparse_keeps_junk_and_marks_post_when_poster_still_unreadable(conn, cache):
    img = picture(7)
    raw = post(url="u3")
    junk_id = legacy_junk(conn, raw)
    res = pr.reparse(conn, FakeOcr({img: NOISE}), fetch=lambda u: img, cache=cache)
    assert res["created"] == 0 and res["replaced"] == 0 and res["still_unread"] == 1
    assert [r["id"] for r in vacancies(conn)] == [junk_id]
    assert pr.get_state(conn, raw.source, raw.external_id)["status"] == pr.NEEDS
    # пока распознавание не улучшилось (та же версия) и картинка та же, пост снова не перечитывается
    assert pr.candidates(conn) == [(raw, "junk")]  # мусорная вакансия остаётся кандидатом, пока её не заменили


def test_reparse_picks_up_posts_marked_needs_poster_after_ocr_improves(conn, cache):
    img = picture(8)
    raw = post("77", text="", url="u4")
    save_raw(conn, raw)
    pr.set_state(conn, raw, pr.NEEDS, note="афиша не прочиталась")
    assert pr.candidates(conn) == []  # версия та же, картинка та же
    conn.execute("UPDATE poster_state SET ocr_version = ocr_version - 1")
    conn.commit()
    res = pr.reparse(conn, FakeOcr({img: GOOD}), fetch=lambda u: img, cache=cache)
    assert res["candidates"] == 1 and res["created"] == 1
    assert pr.get_state(conn, raw.source, "77")["status"] == pr.READ


def test_unmarked_old_post_without_vacancy_is_a_candidate(conn):
    save_raw(conn, post("90", url="u5"))
    assert [reason for _, reason in pr.candidates(conn)] == ["unmarked"]


def test_dry_run_reads_but_writes_nothing(conn, cache):
    img = picture(9)
    legacy_junk(conn, post(url="u6"))
    before = vacancies(conn)
    res = pr.reparse(conn, FakeOcr({img: GOOD}), dry_run=True, fetch=lambda u: img, cache=cache)
    assert res["items"][0]["professions"] == ["автомойщик"] and res["created"] == 0
    assert vacancies(conn) == before
    assert not list(Path(cache.dir).glob("*.json")) if cache.dir.exists() else True
    assert conn.execute("SELECT COUNT(*) FROM poster_state").fetchone()[0] == 0


def test_source_filter(conn, cache):
    img = picture(10)
    legacy_junk(conn, post("1", url="u7", source="chan_a"))
    legacy_junk(conn, post("2", url="u8", source="chan_b"))
    res = pr.reparse(conn, FakeOcr({img: GOOD}), source="chan_b", fetch=lambda u: img, cache=cache)
    assert res["candidates"] == 1 and res["items"][0]["post"] == "chan_b/2"


def test_old_cached_answer_is_ignored_after_the_ocr_version_grows(cache):
    from rabota.vision.extract import extract_from_image

    img = picture(11)
    provider = FakeOcr({img: GOOD})
    first = extract_from_image(img, CAPTION, provider, cache=cache)
    assert first["kind"] == "vacancy" and provider.calls == 1
    extract_from_image(img, CAPTION, provider, cache=cache)
    assert provider.calls == 1  # тот же результат из кэша
    cache.put(first["image_hash"], {"kind": "other", "vacancies": [], "raw_text": "„оо"})  # ответ старого распознавания без версии
    again = extract_from_image(img, CAPTION, provider, cache=cache)
    assert again["kind"] == "vacancy" and provider.calls == 2


def test_cli_reparse_posters(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    c = connect(path)
    legacy_junk(c, post(url="u9"))
    c.close()
    monkeypatch.setattr(cli.config, "DB_PATH", path)
    img = picture(12)
    monkeypatch.setattr(cli, "_vision_provider", lambda: FakeOcr({img: GOOD}))
    monkeypatch.setattr("rabota.vision.extract.download_image", lambda url, **kw: img)
    monkeypatch.setattr("rabota.vision.extract.VisionCache", lambda *a, **k: VisionCache(tmp_path / "vc"))
    assert cli.main(["reparse-posters", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "автомойщик" in out and "пробный запуск" in out
    assert cli.main(["reparse-posters", "--source", "Vakansiii_dnr"]) == 0
    assert "создано 1" in capsys.readouterr().out
