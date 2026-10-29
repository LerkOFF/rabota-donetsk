"""Дубли: на странице вакансии показываем, где ещё она опубликована, в ленте дубли не видны."""
import re
import sys
import types

from rabota.web import queries as q
from tests.web.conftest import make_vacancy

LINK = re.compile(r'class="vcard__link" href="/vacancy/(\d+)"')


def make_family(add):
    main = add(profession="бариста", source_type="telegram", source="@main_chan", source_url="https://t.me/main_chan/1")
    add(profession="бариста", duplicate_of=main, source_type="vk", source="club_barista", source_url="https://vk.com/wall-1_2", ago=20)
    add(profession="бариста", duplicate_of=main, source_type="max", source="max_rabota", source_url="https://max.example/p/3", ago=10)
    return main


def test_block_is_visible_with_duplicates(client, add):
    main = make_family(add)
    html = client.get(f"/vacancy/{main}").text
    assert "Эту вакансию также публикуют" in html and "data-also-published" in html
    assert ">ВКонтакте<" in html and ">MAX<" in html
    assert "club_barista" in html and "max_rabota" in html
    assert 'href="https://vk.com/wall-1_2"' in html and 'href="https://max.example/p/3"' in html
    assert 'rel="noopener nofollow ugc"' in html
    # главный источник блоком не дублируется
    assert "main_chan" in html and html.count("main_chan") >= 1
    assert "@main_chan" not in html.split("data-also-published")[1].split("</section>")[0]


def test_block_is_hidden_without_duplicates(client, add):
    vid = add(profession="кассир")
    assert "также публикуют" not in client.get(f"/vacancy/{vid}").text


def test_fallback_ignores_hidden_and_non_vacancy_duplicates(conn, add):
    main = add()
    add(duplicate_of=main, status="hidden", source_type="vk", source="скрытый")
    add(duplicate_of=main, kind="military", source_type="vk", source="военный")
    assert q._also_published_in_fallback(conn, main) == []


def test_unsafe_duplicate_url_is_not_a_link(client, add):
    main = add()
    add(duplicate_of=main, source_type="vk", source="club_x", source_url="javascript:alert(1)")
    html = client.get(f"/vacancy/{main}").text
    assert "club_x" in html and "javascript:alert" not in html


def test_duplicates_are_not_in_the_feed_but_main_has_a_tag(client, add):
    main = make_family(add)
    html = client.get("/vacancies").text
    assert [int(x) for x in LINK.findall(html)] == [main]
    assert "ещё в 2 источниках" in html
    home = client.get("/").text
    assert "ещё в 2 источниках" in home and len(LINK.findall(home)) == 1


def test_tag_wording_and_absence(client, add):
    one = add(profession="повар", ago=5)
    add(profession="повар", duplicate_of=one, source_type="vk", source="club", ago=1)
    alone = add(profession="грузчик", ago=9)
    html = client.get("/vacancies").text
    assert "ещё в 1 источнике" in html and html.count("also-tag") == 1
    assert alone and "ещё в 0" not in html


def test_tag_on_profession_and_similar(client, add):
    make_family(add)
    other = add(profession="кассир", category="общепит", ago=300)
    assert other
    prof = client.get("/profession/%D0%B1%D0%B0%D1%80%D0%B8%D1%81%D1%82%D0%B0").text
    assert len(LINK.findall(prof)) == 1 and "источниках" in prof
    sim = client.get(f"/vacancy/{other}").text
    assert "ещё в 2 источниках" in sim


def test_wrapper_prefers_pipeline_function(conn, add, monkeypatch):
    main = add()
    add(duplicate_of=main, source_type="vk", source="own")
    fake = types.ModuleType("rabota.pipeline.dedupe")
    fake.also_published_in = lambda c, vid: [{"source_type": "max", "source": "pipeline", "source_url": "https://x.example/1"}]
    monkeypatch.setitem(sys.modules, "rabota.pipeline.dedupe", fake)
    assert q.also_published_in(conn, main) == [{"source_type": "max", "source": "pipeline", "source_url": "https://x.example/1"}]


def test_fallback_query_keys_and_order(conn, add):
    main = add()
    add(duplicate_of=main, source_type="vk", source="b", source_url="https://vk.example/2", ago=5)
    add(duplicate_of=main, source_type="max", source="a", source_url="https://max.example/1", ago=50)
    items = q._also_published_in_fallback(conn, main)
    assert [i["source"] for i in items] == ["a", "b"]
    assert set(items[0]) == {"source_type", "source", "source_url"}
    assert make_vacancy().duplicate_of is None
