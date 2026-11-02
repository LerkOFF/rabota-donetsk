"""Админка: блок «Страницы в индексе» на странице Пульса."""
from __future__ import annotations

from rabota.web import content_routes
from tests.web.admin_harness import add_vacancy, login_admin, make_env


def test_index_counts_by_type(conn, add):
    for i in range(6):
        add(profession="бариста", district="Ворошиловский", employment="подработка", ago=i + 1,
            pay_min=30000 + i * 1000, pay_period="месяц")
    for i in range(3):
        add(profession="кассир", district="Ворошиловский", employment="постоянно", ago=20 + i)
    rows = {r["key"]: r for r in content_routes.index_counts(conn)}
    assert set(rows) == {"selections", "professions", "salary", "articles", "places"}
    assert rows["professions"]["n"] == 2  # бариста и кассир: по три вакансии и больше
    assert rows["places"]["n"] >= 1
    # зарплаты: одна профессия с пятью вакансиями с оплатой, общая страница пока тонкая (меньше трёх профессий)
    assert rows["salary"]["n"] == 1
    assert rows["articles"]["n"] == 9  # восемь статей и страница блога
    assert dict(rows["selections"]["parts"])["главные"] >= 1
    for r in rows.values():
        assert r["title"] and r["note"] and r["href"].startswith("/")


def test_pulse_page_shows_index_block_with_sortable_keys(tmp_path):
    env = make_env(tmp_path)
    with env.client:
        login_admin(env)
        add_vacancy(env)
        r = env.client.get("/admin/pulse")
    assert r.status_code == 200
    assert 'data-sortable="pulse-index"' in r.text and "Страницы в индексе поисковиков" in r.text
    for key in ("selections", "professions", "salary", "articles", "places"):
        assert f'data-key="{key}"' in r.text
    assert "\u2014" not in r.text
