"""Установка на телефон: manifest, значки, service worker (что кэширует и чего не кэширует), подсказка «Установить»."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from rabota.web import pwa
from rabota.web import seo as seolib
from rabota.web.app import create_app
from tests.web.conftest import NOW
from tests.web.test_seo import Page

STATIC = Path(pwa.STATIC_DIR)


def test_manifest_is_valid_and_complete(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    assert m["name"] == "Работа Донецк" and m["short_name"] == "Работа" and m["lang"] == "ru"
    assert m["display"] == "standalone" and m["start_url"].startswith("/") and m["scope"] == "/"
    assert m["theme_color"].startswith("#") and m["background_color"].startswith("#")
    assert len(m["short_name"]) <= 12
    sizes = {(i["sizes"], i["purpose"]) for i in m["icons"]}
    assert ("192x192", "any") in sizes and ("512x512", "any") in sizes and ("512x512", "maskable") in sizes
    for shortcut in m["shortcuts"]:
        assert shortcut["url"].startswith("/") and shortcut["name"]
    assert "\u2014" not in r.text


def test_manifest_icons_exist_with_declared_size(client):
    for icon in client.get("/manifest.webmanifest").json()["icons"]:
        assert icon["type"] == "image/png"
        file = STATIC / icon["src"].removeprefix("/static/")
        assert file.exists(), icon["src"]
        w, h = Image.open(file).size
        assert f"{w}x{h}" == icon["sizes"], icon["src"]
        assert client.get(icon["src"]).status_code == 200


def test_maskable_icon_has_full_bleed_background_and_safe_zone():
    img = Image.open(STATIC / "icons" / "maskable-512.png").convert("RGBA")
    for corner in ((0, 0), (511, 0), (0, 511), (511, 511)):
        assert img.getpixel(corner)[3] == 255, "у maskable фон должен доходить до краёв"
    # знак (белый) не выходит за центральные 80 процентов
    white = [(x, y) for x in range(0, 512, 4) for y in range(0, 512, 4) if img.getpixel((x, y))[:3] == (255, 255, 255)]
    assert white and all(51 <= x <= 461 and 51 <= y <= 461 for x, y in white)


def test_regular_icon_has_rounded_transparent_corners():
    img = Image.open(STATIC / "icons" / "icon-512.png").convert("RGBA")
    assert img.getpixel((0, 0))[3] == 0 and img.getpixel((256, 256))[3] == 255


def test_pages_link_manifest_icons_and_both_theme_colors(client):
    html = client.get("/about").text
    page = Page(html)
    assert page.links["manifest"] == "/manifest.webmanifest"
    assert page.links["apple-touch-icon"].startswith("/static/icons/")
    assert 'content="#f6f8fc" media="(prefers-color-scheme: light)"' in html
    assert 'content="#0f172a" media="(prefers-color-scheme: dark)"' in html
    assert "/static/pwa.js" in html and "/static/suggest.js" in html


def test_service_worker_is_served_from_root_without_cache(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == "no-cache" and r.headers["service-worker-allowed"] == "/"
    assert "__VERSION__" not in r.text and "__PRECACHE__" not in r.text and "__NEVER__" not in r.text


def test_service_worker_never_cache_list_covers_every_private_section(client):
    js = client.get("/sw.js").text
    never = json.loads(js.split("const NEVER = ")[1].split(";\n")[0])
    for prefix in seolib.PRIVATE_PREFIXES:
        assert prefix in never, prefix
    for extra in ("/razmestit", "/suggest", "/sw.js", "/cabinet", "/admin", "/account"):
        assert extra in never
    assert "complaint|apply|respond|favorite" in js  # формы вакансии
    precache = json.loads(js.split("const PRECACHE = ")[1].split(";\n")[0])
    assert "/offline" in precache
    for url in precache:
        path = url.split("?")[0]
        assert not any(path == p or path.startswith(p + "/") for p in never), url  # оболочка не пересекается с закрытым
        assert path.startswith("/static/") or path == "/offline"


def test_service_worker_version_follows_the_newest_shell_file(monkeypatch):
    monkeypatch.setattr(pwa, "_mtime", lambda name: 5000 if name == "site.css" else 100)
    template = int(pwa.SW_TEMPLATE.stat().st_mtime)  # правка самого service worker тоже меняет версию
    assert pwa.shell_version() == str(max(5000, template))
    assert any(u.startswith("/static/site.css?v=5000") for u in pwa.precache_urls())
    monkeypatch.setattr(pwa, "_mtime", lambda name: 9000 if name == "app.js" else 100)
    assert pwa.shell_version() == str(max(9000, template))  # правка любого файла оболочки меняет версию и сбрасывает старый кэш


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="нужен node")
def test_service_worker_behaviour_in_node(client, tmp_path):
    """Запускаем настоящий service worker в песочнице: что он берёт из кэша, а что пропускает."""
    sw = tmp_path / "sw.js"
    sw.write_text(client.get("/sw.js").text, encoding="utf-8")
    runner = tmp_path / "run.js"
    runner.write_text(r"""
const fs = require("fs"), vm = require("vm");
const listeners = {}; const stored = {}; const opened = []; let deleted = [];
const fakeCache = { add: async () => {}, put: async (req) => { stored[req.url] = true; }, match: async () => undefined };
const sandbox = {
  URL, Response, console,
  self: { location: { origin: "https://example.test" }, addEventListener: (n, f) => { listeners[n] = f; },
          skipWaiting: async () => {}, clients: { claim: async () => {} } },
  caches: { open: async (name) => { opened.push(name); return fakeCache; }, match: async () => undefined,
            keys: async () => ["rabota-shell-old", "other"], delete: async (k) => { deleted.push(k); return true; } },
  fetch: async () => new Response("ok"),
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), sandbox);
function ask(method, path, mode) {
  let handled = false;
  const event = { request: { method, url: "https://example.test" + path, mode: mode || "no-cors" }, respondWith: () => { handled = true; }, waitUntil: () => {} };
  listeners.fetch(event);
  return handled;
}
const out = {
  static_get: ask("GET", "/static/site.css?v=1"),
  offline_get: ask("GET", "/offline"),
  post_static: ask("POST", "/static/site.css"),
  post_form: ask("POST", "/razmestit"),
  cabinet: ask("GET", "/cabinet/vacancies"),
  admin: ask("GET", "/admin/pulse"),
  messages: ask("GET", "/messages"),
  suggest: ask("GET", "/suggest?q=ба"),
  api_vacancy: ask("GET", "/vacancy/5"),
  vacancy_apply: ask("GET", "/vacancy/5/apply"),
  other_origin: (() => { let h = false; listeners.fetch({ request: { method: "GET", url: "https://evil.test/static/a.js", mode: "no-cors" }, respondWith: () => { h = true; } }); return h; })(),
  navigate_get: ask("GET", "/vacancies", "navigate"),
  navigate_post: ask("POST", "/razmestit", "navigate"),
};
(async () => {
  let activated; listeners.activate({ waitUntil: (p) => { activated = p; } }); await activated;
  out.deleted = deleted;
  console.log(JSON.stringify(out));
})();
""", encoding="utf-8")
    res = subprocess.run([NODE, str(runner), str(sw)], capture_output=True, text=True, timeout=30, check=False)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["static_get"] and out["offline_get"] and out["navigate_get"]  # оболочка и переходы: обрабатываются
    for key in ("post_static", "post_form", "cabinet", "admin", "messages", "suggest", "api_vacancy", "vacancy_apply",
                "other_origin", "navigate_post"):
        assert out[key] is False, f"{key}: service worker не должен вмешиваться"
    assert out["deleted"] == ["rabota-shell-old"]  # старый кэш чистится, чужие ключи не трогаем


def test_service_worker_never_stores_html_pages(client):
    js = client.get("/sw.js").text
    # HTML на переходах всегда берётся из сети, в кэш кладутся только файлы из /static/ и офлайн-страница
    assert 'request.mode === "navigate"' in js and "cache.put" in js
    assert 'url.pathname.startsWith("/static/")' in js


def test_offline_page_has_no_personal_data(tmp_path):
    from tests.accounts.harness import build_env, login, make_user

    env = build_env(tmp_path)
    with env.client:
        make_user(env, "anna@example.test", role="job_seeker", name="Анна Секретова")
        assert login(env, "anna@example.test").status_code == 303
        r = env.client.get("/offline")
    assert r.status_code == 200 and "Анна" not in r.text and "anna@example.test" not in r.text
    assert "noindex" in r.headers["x-robots-tag"] and "Нет подключения к сети" in r.text


def test_csp_allows_service_worker_with_and_without_metrika(tmp_path, monkeypatch):
    from rabota.web.security import build_csp

    assert "worker-src" not in build_csp(metrika=False)  # default-src 'self' уже разрешает /sw.js
    assert "worker-src 'self' blob:" in build_csp(metrika=True)
    app = create_app(tmp_path / "x.db", site_url="https://example.test", now=lambda: NOW)
    with TestClient(app) as c:
        assert "default-src 'self'" in c.get("/sw.js").headers["content-security-policy"]


def test_install_prompt_script_rules():
    js = (STATIC / "pwa.js").read_text(encoding="utf-8")
    assert 'register("/sw.js"' in js
    assert "display-mode: standalone" in js and "navigator.standalone" in js  # уже установлено: не показываем
    assert "max-width: 899px" in js  # только на телефоне
    assert "visits" in js and "< 2" in js  # со второго визита
    assert "dismissed" in js and "QUIET_DAYS" in js and "localStorage" in js  # отказ запоминается
    assert "beforeinstallprompt" in js and "appinstalled" in js
    for private in ("/cabinet", "/admin", "/login", "/razmestit"):
        assert private in js  # на формах и в кабинете подсказки нет
    assert "\u2014" not in js


def test_pwa_files_have_no_inline_style_or_script_needs():
    """Политика безопасности запрещает встроенные скрипты и стили: подсказка работает через классы и файлы."""
    js = (STATIC / "pwa.js").read_text(encoding="utf-8") + (STATIC / "suggest.js").read_text(encoding="utf-8")
    assert ".style." not in js and 'setAttribute("style"' not in js and "eval(" not in js


def test_robots_hides_helper_endpoints(client):
    text = client.get("/robots.txt").text
    assert "Disallow: /suggest" in text and "Disallow: /offline" in text
