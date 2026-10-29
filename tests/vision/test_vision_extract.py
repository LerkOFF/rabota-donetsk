"""Из картинки в Vacancy: нормализация телефонов, решение об отправке, афиши, баннеры, расхождения."""
from __future__ import annotations

import httpx
import pytest
import respx
from vision_helpers import SIDECAR_DIR, CountingProvider, make_image

from rabota.models import RawPost, Vacancy
from rabota.vision.extract import (
    build_vacancies,
    extract_from_image,
    needs_image_recognition,
    normalize_phone,
    normalize_telegram,
    pay_numbers_in_text,
    phones_in_text,
    vacancies_from_images,
)
from rabota.vision.images import VisionCache
from rabota.vision.providers import ProviderError, SidecarProvider, VisionProvider

LONG_FULL_TEXT = (
    "Требуется продавец-кассир в магазин продуктов, г. Донецк, ул. Артёма, 12. Оплата 48 500 руб. в месяц, "
    "график 2/2, оформление по ТК, обучение на месте, форма выдаётся. Звоните по телефону +7 949 000-11-22 "
    "с 9 до 18 или приходите на собеседование."
)


def raw_post(text: str = "", images: list[str] | None = None, ext: str = "1") -> RawPost:
    return RawPost(
        source_type="vk",
        source="club68230454",
        external_id=ext,
        url=f"https://vk.example/wall-1_{ext}",
        text=text,
        published_at="2026-10-29T08:00:00+00:00",
        image_urls=images if images is not None else ["https://img.example/a.jpg"],
    )


@pytest.fixture()
def cache(tmp_path) -> VisionCache:
    return VisionCache(tmp_path / "cache")


# телефоны


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+7 949 000-00-01", "+79490000001"),
        ("8 (949) 000-00-02", "+79490000002"),
        ("949 000 00 03", "+79490000003"),
        ("79490000004", "+79490000004"),
        ("+7(856)300-00-05", "+78563000005"),
        ("062 300-00-06", "+78563000006"),
        ("335", "335"),
        ("3-35", "335"),
        ("  112 ", "112"),
        ("12", None),
        ("123456789012", None),
        ("позвоните", None),
        ("", None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_phones_in_text_and_telegram():
    assert phones_in_text("Тел. 8-949-000-00-01, или +7 949 000 00 02") == ["+79490000001", "+79490000002"]
    assert phones_in_text("зарплата 48 500 руб.") == []
    assert normalize_telegram("https://t.me/user_name") == "@user_name"
    assert normalize_telegram("@user") == "@user"
    assert normalize_telegram("user") == "@user"
    assert normalize_telegram("не ник!") is None


def test_pay_numbers_ignore_phones():
    nums = pay_numbers_in_text("от 48 500 - 56 000 руб, звоните +7 949 000-00-01, или 50к")
    assert {48500, 56000, 50000} <= nums
    assert not any(n > 1_000_000 for n in nums)


# нужна ли картинка


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", True),
        ("Работа!", True),
        ("Требуются продавцы, подробности на фото. " * 6, True),  # длинный, но без оплаты и телефона
        (LONG_FULL_TEXT, False),
    ],
)
def test_needs_image_recognition(text, expected):
    assert needs_image_recognition(text) is expected


# афиши


def test_multi_position_poster_gives_five_vacancies(poster_bytes, cache):
    post = raw_post("Работа в Геркулес Moloko", ["https://img.example/poster.jpg"], ext="77")
    with respx.mock:
        respx.get("https://img.example/poster.jpg").mock(return_value=httpx.Response(200, content=poster_bytes))
        result = vacancies_from_images(post, SidecarProvider([SIDECAR_DIR]), cache=cache)

    assert len(result) == 5
    assert all(isinstance(v, Vacancy) and v.kind == "vacancy" for v in result)
    assert [v.profession for v in result] == [
        "Продавец, кассир", "Приёмщик товаров", "Грузчик", "Охранник", "Уборщик",
    ]
    first = result[0]
    assert (first.pay_min, first.pay_max, first.pay_period) == (48500, 56000, "месяц")
    assert first.employer == "Геркулес Moloko"
    assert first.contacts == {"phones": ["335"]}
    assert "Геркулес" in first.summary and "335" in first.summary
    for v in result:
        assert (v.source_type, v.source, v.external_id) == ("vk", "club68230454", "77")
        assert v.source_url == post.url and v.published_at == post.published_at


def test_service_poster_gives_empty_list(wall_bytes, cache):
    post = raw_post("", ["https://img.example/w.jpg"])
    assert vacancies_from_images(post, SidecarProvider([SIDECAR_DIR]), cache=cache, fetch=lambda u: wall_bytes) == []


@pytest.mark.parametrize("kind", ["service", "military", "ad", "other"])
def test_non_vacancy_kinds_give_nothing(cache, kind):
    # даже если провайдер ошибочно приложил вакансии, kind главный
    answer = {"kind": kind, "vacancies": [{"profession": "Мастер"}], "raw_text": "x"}
    prov = CountingProvider(answer)
    post = raw_post("")
    assert vacancies_from_images(post, prov, cache=cache, fetch=lambda u: make_image(21)) == []
    assert len(prov.calls) == 1


def test_military_recruitment_text_gives_nothing(cache):
    answer = {"kind": "military", "vacancies": [], "raw_text": "Набор на контрактную службу, выплаты 1 500 000"}
    assert vacancies_from_images(raw_post(""), CountingProvider(answer), cache=cache, fetch=lambda u: make_image(22)) == []


# решение об отправке


def test_image_not_sent_when_text_is_complete(cache):
    prov = CountingProvider()
    post = raw_post(LONG_FULL_TEXT)
    assert vacancies_from_images(post, prov, cache=cache, fetch=lambda u: make_image(23)) == []
    assert prov.calls == []


def test_post_without_images_never_calls_provider(cache):
    prov = CountingProvider()
    assert vacancies_from_images(raw_post("", images=[]), prov, cache=cache) == []
    assert prov.calls == []


def test_same_image_twice_in_one_post_counts_once(cache, poster_bytes):
    post = raw_post("", ["https://img.example/p.jpg", "https://img.example/p.jpg?x=1"])
    res = vacancies_from_images(post, SidecarProvider([SIDECAR_DIR]), cache=cache, fetch=lambda u: poster_bytes)
    assert len(res) == 5


# баннеры и кэш


def test_repeating_banner_is_filtered_by_hash(cache):
    banner = make_image(31)
    prov = CountingProvider({"kind": "ad", "vacancies": [], "raw_text": "Разместить вакансию"})
    for i in range(6):
        assert vacancies_from_images(raw_post("", ext=str(i)), prov, cache=cache, fetch=lambda u: banner) == []
    # после трёх разных постов с одной картинкой провайдер больше не вызывается
    assert len(prov.calls) == 2
    res = extract_from_image(banner, provider=prov, cache=cache, post_key="x")
    assert res["skipped"] == "banner" and res["kind"] == "ad"


def test_banner_from_manual_blocklist_is_skipped(cache):
    img = make_image(32)
    prov = CountingProvider()
    extract_from_image(img, provider=prov, cache=cache)
    assert len(prov.calls) == 1
    cache.block(extract_from_image(img, provider=prov, cache=cache)["image_hash"])
    assert extract_from_image(img, provider=prov, cache=cache)["skipped"] == "banner"
    assert len(prov.calls) == 2  # вторая попытка до блокировки вызвала провайдера, после блокировки нет


def test_repeated_vacancy_poster_is_not_banner(cache):
    img = make_image(33)
    answer = {"kind": "vacancy", "vacancies": [{"profession": "Грузчик", "phones": ["335"]}], "raw_text": ""}
    prov = CountingProvider(answer, cacheable=True)
    for i in range(5):
        res = vacancies_from_images(raw_post("", ext=str(i)), prov, cache=cache, fetch=lambda u: img)
        assert [v.profession for v in res] == ["Грузчик"]
    assert len(prov.calls) == 1  # остальные из кэша


def test_cache_hit_skips_provider_and_error_is_not_cached(cache):
    img = make_image(34)
    bad = CountingProvider()

    class Failing(VisionProvider):
        def extract(self, image_bytes, hint_text=""):
            raise ProviderError("недоступно")

    res = extract_from_image(img, provider=Failing(), cache=cache)
    assert res["error"] == "недоступно" and res["kind"] == "other"
    assert cache.hashes() == []
    # NullProvider-подобный (не cacheable) ответ тоже не оседает в кэше
    extract_from_image(img, provider=bad, cache=cache)
    assert cache.hashes() == []


def test_sidecar_in_cache_dir_is_used_without_provider(tmp_path, poster_bytes):
    """Демо: разметка лежит в кэше, провайдер не нужен."""
    from rabota.vision.images import image_hash

    cache = VisionCache(tmp_path)
    cache.put(image_hash(poster_bytes), {"kind": "ad", "vacancies": [], "raw_text": "из кэша"})
    prov = CountingProvider()
    res = extract_from_image(poster_bytes, provider=prov, cache=cache)
    assert res["cached"] is True and res["raw_text"] == "из кэша" and prov.calls == []


@respx.mock
def test_download_failure_gives_empty_result(cache):
    respx.get("https://img.example/a.jpg").mock(return_value=httpx.Response(404))
    prov = CountingProvider()
    assert vacancies_from_images(raw_post(""), prov, cache=cache) == []
    assert prov.calls == []
    assert "error" in extract_from_image("https://img.example/a.jpg", cache=cache, provider=prov)


def test_broken_bytes_are_handled(cache):
    res = extract_from_image(b"not an image", cache=cache, provider=CountingProvider())
    assert res["kind"] == "other" and "error" in res


# расхождение текста и картинки


def vacancy_answer(phones, pay_min=4000, pay_max=4000) -> dict:
    return {
        "kind": "vacancy",
        "vacancies": [
            {"profession": "Бариста", "pay_min": pay_min, "pay_max": pay_max, "pay_period": "смена",
             "phones": phones, "employer": "Кофейня", "extra_text": "Работа рядом с метро"}
        ],
        "raw_text": "",
    }


def test_text_wins_on_phone_conflict():
    post = raw_post("Бариста, звоните 8 949 000-11-22")
    vacs, conflicts = build_vacancies(vacancy_answer(["+7 949 000-33-44"]), post)
    assert conflicts == ["телефон"]
    assert vacs[0].contacts["phones"] == ["+79490001122"]
    assert "расходится с текстом" in vacs[0].summary and "телефон" in vacs[0].summary


def test_text_wins_on_pay_conflict():
    post = raw_post("Бариста, зарплата 5000 руб за смену")
    vacs, conflicts = build_vacancies(vacancy_answer(["+7 949 000-33-44"], 4000, 4000), post)
    assert "оплата" in conflicts
    assert vacs[0].pay_min is None and vacs[0].pay_max is None and vacs[0].pay_period is None
    assert vacs[0].contacts["phones"] == ["+79490003344"]  # текст телефона не содержит, берём с картинки


def test_matching_text_and_image_have_no_conflict():
    post = raw_post("Бариста, 4000 руб смена, +7 949 000-33-44")
    vacs, conflicts = build_vacancies(vacancy_answer(["8 949 000 33 44"]), post)
    assert conflicts == []
    assert vacs[0].pay_min == 4000 and vacs[0].contacts["phones"] == ["+79490003344"]
    assert "расходится" not in vacs[0].summary


def test_non_vacancy_result_builds_nothing():
    assert build_vacancies({"kind": "service", "vacancies": []}, raw_post("")) == ([], [])
