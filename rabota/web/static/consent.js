/* Согласие на аналитику. Счётчик Яндекс Метрики загружается только после "Принять аналитику".
   Выбор хранится в localStorage этого браузера. Без согласия или без скриптов никакие сторонние запросы не идут. */
(function () {
  var self = document.currentScript;
  var counter = self && self.getAttribute("data-metrika-id");
  if (!counter) return;
  var webvisor = self.getAttribute("data-webvisor") !== "0";
  var KEY = "rabota_consent";
  var banner = document.getElementById("cookie-consent");
  var memory = null;

  function read() {
    try {
      var v = JSON.parse(localStorage.getItem(KEY));
      if (v && v.v === 1) return v;
    } catch (e) { /* хранилище недоступно */ }
    return memory;
  }
  function write(analytics) {
    var value = { v: 1, analytics: analytics, at: new Date().toISOString() };
    memory = value;
    try { localStorage.setItem(KEY, JSON.stringify(value)); } catch (e) { /* выбор действует до закрытия страницы */ }
  }

  function privatePage() {
    return /^\/(login|register|forgot|reset|verify|account|cabinet|admin|messages)/.test(location.pathname);
  }
  function loadMetrika() {
    if (window["ym-disable-" + counter] || document.getElementById("ym-tag")) return;
    window.ym = window.ym || function () { (window.ym.a = window.ym.a || []).push(arguments); };
    window.ym.l = Date.now();
    var tag = document.createElement("script");
    tag.id = "ym-tag";
    tag.async = true;
    tag.src = "https://mc.yandex.ru/metrika/tag.js";
    document.head.appendChild(tag);
    window.ym(Number(counter), "init", {
      clickmap: true,
      trackLinks: true,
      accurateTrackBounce: true,
      trackHash: true,
      webvisor: webvisor && !privatePage()
    });
  }
  function dropCookies() {
    document.cookie.split(";").forEach(function (c) {
      var name = c.split("=")[0].trim();
      if (/^(_ym|_ga|yandexuid|yabs-sid|ymex|i$|yuidss|_yasc)/.test(name)) {
        document.cookie = name + "=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
      }
    });
  }

  /* Пока окно видно, оно закрывает низ страницы: сообщаем высоту стилям, чтобы фокус с клавиатуры не уходил под него */
  function reserve() {
    var h = banner && !banner.hidden ? banner.offsetHeight : 0;
    document.documentElement.style.setProperty("--consent-h", h ? h + "px" : "0px");
  }
  function show(visible) {
    if (banner) banner.hidden = !visible;
    reserve();
  }
  window.addEventListener("resize", reserve);
  function choose(analytics) {
    var was = read();
    write(analytics);
    show(false);
    if (analytics) loadMetrika();
    else if (was && was.analytics) { dropCookies(); location.reload(); }
  }

  if (banner) {
    banner.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-consent]");
      if (btn) choose(btn.getAttribute("data-consent") === "accept");
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !banner.hidden) show(false);
    });
  }

  /* ссылка "Настройки cookie" в подвале, чтобы выбор можно было изменить */
  var legal = document.querySelector(".footer__legal ul");
  if (legal) {
    var li = document.createElement("li");
    var link = document.createElement("button");
    link.type = "button";
    link.className = "consent-link";
    link.textContent = "Настройки cookie";
    link.addEventListener("click", function () {
      show(true);
      var b = banner && banner.querySelector("[data-consent]");
      if (b) b.focus();
    });
    li.appendChild(link);
    legal.appendChild(li);
  }

  var saved = read();
  if (saved && saved.analytics) loadMetrika();
  else if (!saved) show(true);
})();
