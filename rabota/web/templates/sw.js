/* Service worker «Работа Донецк». Выдаётся с адреса /sw.js, значения ниже подставляет сервер (rabota/web/pwa.py).
   Правила: кэшируем только оболочку (стили, скрипты, шрифты, значки) и страницу «нет сети».
   Страницы с данными, кабинет, админка, формы и любые не-GET запросы в кэш не попадают никогда. */
"use strict";

const VERSION = "__VERSION__";
const SHELL_CACHE = "rabota-shell-" + VERSION;
const PRECACHE = __PRECACHE__;
const OFFLINE_URL = "/offline";
/* Пути, которые не кэшируются и не отдаются из кэша (список строится из seo.PRIVATE_PREFIXES) */
const NEVER = __NEVER__;

function isNever(pathname) {
  for (let i = 0; i < NEVER.length; i++) {
    const p = NEVER[i];
    if (pathname === p || pathname.startsWith(p + "/")) return true;
  }
  return /^\/vacancy\/[^/]+\/(complaint|apply|respond|favorite)(\/|$)/.test(pathname);
}

/* Можно ли обработать запрос кэшем: только GET на наш адрес, не из закрытого раздела. Вынесено для проверки в тестах. */
function cacheable(method, url, origin) {
  if (method !== "GET") return false;
  if (url.origin !== origin) return false;
  if (isNever(url.pathname)) return false;
  return url.pathname.startsWith("/static/") || url.pathname === OFFLINE_URL;
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => Promise.all(PRECACHE.map((u) => cache.add(u).catch(() => undefined))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("rabota-") && k !== SHELL_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);

  /* Переходы между страницами: всегда сеть; нет сети: страница «нет сети». HTML не сохраняем. */
  if (request.mode === "navigate") {
    if (request.method !== "GET" || url.origin !== self.location.origin) return;
    event.respondWith(
      fetch(request).catch(() => caches.match(OFFLINE_URL).then((r) => r || Response.error()))
    );
    return;
  }

  if (!cacheable(request.method, url, self.location.origin)) return;

  /* Оболочка: из кэша, а в фоне обновляем. Адреса с ?v= меняются при каждой правке файла. */
  event.respondWith(
    caches.open(SHELL_CACHE).then((cache) =>
      cache.match(request).then((hit) => {
        const refresh = fetch(request).then((response) => {
          if (response && response.ok && response.type === "basic") cache.put(request, response.clone());
          return response;
        });
        return hit || refresh;
      })
    ).catch(() => fetch(request))
  );
});
