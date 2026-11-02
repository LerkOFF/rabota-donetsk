"""Раздел «Вакансии» через сайт: доступ, список, карточка, действия, CSRF, журнал, сообщение на один показ."""
import re
from datetime import UTC, datetime, timedelta

import pytest

from tests.accounts.harness import PASSWORD, login, make_user
from tests.web.admin_harness import add_vacancy, apost, login_admin, make_env, meta, one, rows


def fresh() -> str:
    return (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0).isoformat()


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e


@pytest.fixture
def admin(env):
    return login_admin(env)


def vac(env, **kw):
    kw.setdefault("published_at", fresh())
    kw.setdefault("last_seen_at", fresh())
    return add_vacancy(env, **kw)


def plain(html: str) -> str:
    """Видимый текст: без атрибутов и тегов."""
    return re.sub(r"<[^>]+>", " ", html)


# ------------------------------------------------------------------ доступ

def test_guest_goes_to_login(env):
    vid = vac(env)
    for path in ("/admin/vacancies", f"/admin/vacancies/{vid}"):
        r = env.client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=/admin")


@pytest.mark.parametrize("role", ["job_seeker", "employer"])
def test_other_roles_get_404(env, role):
    vid = vac(env)
    make_user(env, f"{role}@example.test", role=role)
    assert login(env, f"{role}@example.test", PASSWORD).status_code == 303
    assert env.client.get("/admin/vacancies").status_code == 404
    assert env.client.get(f"/admin/vacancies/{vid}").status_code == 404
    for action in ("duplicate", "unduplicate", "hide", "restore"):
        assert env.client.post(f"/admin/vacancies/{vid}/{action}", data={}).status_code == 404


def test_posts_require_csrf_and_origin(env, admin):
    vid, main = vac(env), vac(env, profession="повар")
    for action in ("duplicate", "unduplicate", "hide", "restore"):
        path = f"/admin/vacancies/{vid}/{action}"
        assert env.client.post(path, data={"main_id": str(main)}).status_code == 403
        token = re.search(r'name="csrf_token" value="([^"]+)"', env.client.get("/admin").text).group(1)
        assert env.client.post(path, data={"csrf_token": token, "main_id": str(main)},
                               headers={"origin": "https://evil.example"}).status_code == 403
        assert env.client.post(path, data={"csrf_token": "0" * 64, "main_id": str(main)}).status_code == 403
    assert one(env, "SELECT duplicate_of, status FROM vacancies WHERE id=?", vid)["duplicate_of"] is None
    assert not rows(env, "SELECT 1 FROM audit_log WHERE target_id=?", str(vid))


# ------------------------------------------------------------------ список

def test_list_page_basics(env, admin):
    vac(env)
    r = env.client.get("/admin/vacancies")
    assert r.status_code == 200
    assert "noindex" in r.headers["x-robots-tag"] and "no-store" in r.headers["cache-control"]
    assert "noindex" in meta(r.text, "robots") and 40 < len(meta(r.text, "description")) < 200
    assert '<h1>Вакансии</h1>' in r.text and r.text.count("<h1") == 1
    assert 'href="/admin/vacancies" aria-current="page"' in r.text  # пункт меню выделен


def test_list_shows_hidden_expired_foreign_pending(env, admin):
    live = vac(env, profession="бариста")
    hidden = vac(env, profession="грузчик", status="hidden")
    pending = vac(env, profession="курьер", moderation_status="pending", status="hidden")
    foreign = vac(env, profession="водитель", city="Новосибирск")
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    expired = vac(env, profession="повар", published_at=old, last_seen_at=old)
    html = env.client.get("/admin/vacancies").text
    for vid in (live, hidden, pending, foreign, expired):
        assert f'href="/admin/vacancies/{vid}"' in html
    assert "Найдено: 5" in html
    assert "В выдаче" in html and "На проверке" in html and "Истекла" in html and "Город вне выдачи: Новосибирск" in html


def test_list_filters_from_url(env, admin):
    live = vac(env, profession="бариста")
    hidden = vac(env, profession="грузчик", status="hidden")
    html = env.client.get("/admin/vacancies?status=hidden").text
    assert f'href="/admin/vacancies/{hidden}"' in html and f'href="/admin/vacancies/{live}"' not in html
    html = env.client.get("/admin/vacancies?flag=visible").text
    assert f'href="/admin/vacancies/{live}"' in html and f'href="/admin/vacancies/{hidden}"' not in html
    html = env.client.get("/admin/vacancies?prof=груз&sort=pay&dir=asc&period=today").text
    assert f'href="/admin/vacancies/{hidden}"' in html
    assert 'value="груз"' in html and '<option value="today" selected>' in html


def test_list_search_by_number_and_phone(env, admin):
    a = vac(env, profession="бариста", contacts={"phones": ["+79491112233"]})
    b = vac(env, profession="повар", contacts={"phones": ["+79495556677"]})
    html = env.client.get(f"/admin/vacancies?q={b}").text
    assert f'href="/admin/vacancies/{b}"' in html and f'href="/admin/vacancies/{a}"' not in html
    html = env.client.get("/admin/vacancies?q=" + "8 949 111-22-33").text
    assert f'href="/admin/vacancies/{a}"' in html and f'href="/admin/vacancies/{b}"' not in html


def test_list_empty_states(env, admin):
    assert "Вакансий пока нет" in env.client.get("/admin/vacancies").text
    vac(env)
    html = env.client.get("/admin/vacancies?q=яяяя").text
    assert "Ничего не нашлось" in html and "Сбросить фильтры" in html


def test_list_ignores_garbage_params(env, admin):
    vac(env)
    r = env.client.get("/admin/vacancies?status=x&sort=1;drop&page=-3&period=y&flag=%00&q=" + "а" * 500)
    assert r.status_code == 200


def test_list_paginates(env, admin):
    for i in range(27):
        vac(env, profession=f"профессия {i}")
    html = env.client.get("/admin/vacancies").text
    assert "Страница 1 из 2, всего 27" in html and 'rel="next"' in html
    html2 = env.client.get("/admin/vacancies?page=2&sort=id").text
    assert "Страница 2 из 2" in html2 and "page=1" in html2


def test_list_contacts_hidden_until_click(env, admin):
    vac(env, contacts={"phones": ["+79491234567"], "email": "boss@example.test"})
    html = env.client.get("/admin/vacancies").text
    assert "+7 949 ***-**-67" in html and "b***@example.test" in html
    visible = plain(html)
    assert "1234567" not in visible and "boss@example.test" not in visible
    assert 'data-reveal-btn' in html


def test_list_markup_for_drag_and_drop(env, admin):
    vac(env)
    html = env.client.get("/admin/vacancies").text
    assert 'data-sortable="vacancies-columns"' in html and 'data-sortable="vacancies-tiles"' in html
    assert re.search(r'<th[^>]+data-key="vacancy"[^>]+draggable="true"', html)


# ------------------------------------------------------------------ карточка

def test_card_has_fields_history_raw_and_hides_contacts(env, admin):
    from rabota.db import connect, save_raw
    from rabota.models import RawPost

    conn = connect(env.db_path)
    save_raw(conn, RawPost(source_type="telegram", source="@demo", external_id="55", url="https://t.me/demo/55",
                           text="Нужен бариста, звоните +7 949 123-45-67", published_at=fresh()))
    conn.close()
    vid = vac(env, external_id="55", contacts={"phones": ["+79491234567"], "telegram": "@kofe"})
    r = env.client.get(f"/admin/vacancies/{vid}")
    assert r.status_code == 200 and "noindex" in meta(r.text, "robots")
    html = r.text
    assert "История" in html and "Добавлена в базу" in html
    assert "Все поля записи" in html and "Внешний номер" in html
    assert "Исходный пост источника" in html and 'href="https://t.me/demo/55"' in html
    visible = plain(html)
    assert "Нужен бариста, звоните +7 949 ***-**-67" in visible
    assert "123-45-67" not in visible and "1234567" not in visible and "@kofe" not in visible
    assert 'data-sortable="vacancy-main"' in html and 'data-sortable="vacancy-side"' in html
    assert re.search(r'<section[^>]+data-key="history"[^>]+draggable="true"', html)


def test_card_unknown_vacancy_404(env, admin):
    assert env.client.get("/admin/vacancies/99999").status_code == 404


def test_card_shows_complaint_and_audit_in_history(env, admin):
    vid = vac(env)
    conn = env.db()
    conn.execute("INSERT INTO complaints(vacancy_id, reason) VALUES (?, 'Мошенничество: просят деньги')", (vid,))
    conn.commit()
    conn.close()
    assert apost(env, f"/admin/vacancies/{vid}/hide", {"reason": "проверяем"}, page=f"/admin/vacancies/{vid}").status_code == 303
    html = env.client.get(f"/admin/vacancies/{vid}").text
    assert "Жалоба №1" in html and "Скрыта из выдачи" in html and "проверяем" in html and "admin@example.test" in html


# ------------------------------------------------------------------ действия

def test_hide_and_restore_are_logged_and_flash_once(env, admin):
    vid = vac(env)
    page = f"/admin/vacancies/{vid}"
    r = apost(env, f"{page}/hide", {"reason": "тест"}, page=page)
    assert r.status_code == 303 and r.headers["location"].startswith(page)
    assert one(env, "SELECT status FROM vacancies WHERE id=?", vid)["status"] == "hidden"
    first = env.client.get(r.headers["location"]).text
    assert "Вакансия скрыта." in first
    assert "Вакансия скрыта." not in env.client.get(page).text  # сообщение показывается один раз
    r = apost(env, f"{page}/restore", page=page)
    assert r.status_code == 303 and "Вакансия возвращена в выдачу." in env.client.get(r.headers["location"]).text
    actions = [x["action"] for x in rows(env, "SELECT action FROM audit_log WHERE target_id=? ORDER BY id", str(vid))]
    assert actions == ["vacancy.hide", "vacancy.restore"]


def test_mark_duplicate_logs_and_removes_from_feed(env, admin):
    main = vac(env, profession="бариста")
    dup = vac(env, profession="бариста")
    assert f"/vacancy/{dup}" in env.client.get("/vacancies").text
    page = f"/admin/vacancies/{dup}"
    r = apost(env, f"{page}/duplicate", {"main_id": f"№{main}"}, page=page)
    assert r.status_code == 303
    assert one(env, "SELECT duplicate_of FROM vacancies WHERE id=?", dup)["duplicate_of"] == main
    assert f"/vacancy/{dup}" not in env.client.get("/vacancies").text
    log = one(env, "SELECT * FROM audit_log WHERE action='vacancy.duplicate'")
    assert log["target_id"] == str(dup) and log["actor_email"] == "admin@example.test" and log["ip"]
    card = env.client.get(r.headers["location"]).text
    assert "Отмечена как повтор вакансии" in card and "отмечена повторяющейся" in card
    assert f'href="/admin/vacancies/{main}"' in env.client.get(page).text
    assert f'href="/admin/vacancies/{dup}"' in env.client.get(f"/admin/vacancies/{main}").text  # связанное
    # снять отметку
    r = apost(env, f"{page}/unduplicate", page=page)
    assert r.status_code == 303 and "Отметка о повторе снята." in env.client.get(r.headers["location"]).text
    assert one(env, "SELECT duplicate_of FROM vacancies WHERE id=?", dup)["duplicate_of"] is None
    assert rows(env, "SELECT 1 FROM audit_log WHERE action='vacancy.unduplicate'")


@pytest.mark.parametrize("value, message", [
    ("", "Укажите номер главной вакансии цифрами."),
    ("abc", "Укажите номер главной вакансии цифрами."),
    ("999", "нет: укажите номер существующей"),
    ("SELF", "не может быть повтором самой себя"),
])
def test_mark_duplicate_errors_do_not_change_anything(env, admin, value, message):
    vid = vac(env)
    page = f"/admin/vacancies/{vid}"
    r = apost(env, f"{page}/duplicate", {"main_id": str(vid) if value == "SELF" else value}, page=page)
    assert r.status_code == 422 and message in r.text
    assert one(env, "SELECT duplicate_of FROM vacancies WHERE id=?", vid)["duplicate_of"] is None
    assert not rows(env, "SELECT 1 FROM audit_log WHERE action LIKE 'vacancy.%duplicate'")


def test_unduplicate_without_mark_is_404(env, admin):
    vid = vac(env)
    page = f"/admin/vacancies/{vid}"
    assert apost(env, f"{page}/unduplicate", page=page).status_code == 404


def test_actions_on_missing_vacancy_404(env, admin):
    assert apost(env, "/admin/vacancies/9999/duplicate", {"main_id": "1"}, page="/admin").status_code == 404
