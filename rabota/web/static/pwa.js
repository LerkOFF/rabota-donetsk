/* Установка на телефон: регистрация service worker и ненавязчивая подсказка «Установить».
   Подсказка показывается один раз со второго визита, только на телефоне; отказ запоминается на 90 дней.
   Сайт без этого скрипта работает как обычно. */
(function () {
  "use strict";
  var KEY = "pwa-install";
  var QUIET_DAYS = 90;
  var NO_PROMPT = ["/login", "/register", "/logout", "/verify", "/forgot", "/reset", "/account", "/cabinet", "/admin", "/razmestit", "/offline"];

  /* ---------- service worker ---------- */
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () { /* без https или в приватном окне: ничего страшного */ });
    });
  }

  /* кнопка «Обновить страницу» на офлайн-странице */
  var reload = document.querySelector("[data-reload]");
  if (reload) reload.addEventListener("click", function () { location.reload(); });

  /* ---------- состояние подсказки ---------- */
  function load() {
    try { return JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) { return {}; }
  }
  function save(data) {
    try { localStorage.setItem(KEY, JSON.stringify(data)); } catch (e) { /* хранилище недоступно */ }
  }
  function standalone() {
    return (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || window.navigator.standalone === true;
  }
  function blockedPath() {
    var p = location.pathname;
    return NO_PROMPT.some(function (x) { return p === x || p.indexOf(x + "/") === 0; });
  }

  var state = load();
  var counted = false;
  try { counted = sessionStorage.getItem(KEY + "-seen") === "1"; } catch (e) { counted = false; }
  if (!counted) {
    state.visits = (state.visits || 0) + 1;
    save(state);
    try { sessionStorage.setItem(KEY + "-seen", "1"); } catch (e) { /* нет sessionStorage */ }
  }

  var quiet = state.state === "installed" ||
    (state.state === "dismissed" && Date.now() - (state.at || 0) < QUIET_DAYS * 86400000);
  var phone = window.matchMedia && window.matchMedia("(max-width: 899px)").matches;
  if (standalone() || quiet || !phone || (state.visits || 0) < 2 || blockedPath()) return;

  var ios = /iphone|ipad|ipod/i.test(navigator.userAgent) && !/crios|fxios|edgios/i.test(navigator.userAgent);
  var deferred = null;

  function remember(kind) {
    var s = load();
    s.state = kind;
    s.at = Date.now();
    save(s);
  }

  function bar(text, action) {
    var el = document.createElement("section");
    el.className = "pwa-bar";
    el.setAttribute("aria-label", "Установка на телефон");
    el.innerHTML =
      "<img class=\"pwa-bar__ico\" src=\"/static/icons/icon-192.png\" width=\"44\" height=\"44\" alt=\"\">" +
      "<div><p class=\"pwa-bar__t\">Работа Донецк на главном экране</p><p class=\"pwa-bar__x\"></p></div>" +
      "<div class=\"pwa-bar__a\"><button type=\"button\" class=\"btn btn--primary btn--sm\" data-go></button>" +
      "<button type=\"button\" class=\"btn btn--ghost btn--sm\" data-no>Не сейчас</button></div>";
    el.querySelector(".pwa-bar__x").textContent = text;
    el.querySelector("[data-go]").textContent = action;
    document.body.appendChild(el);
    el.querySelector("[data-no]").addEventListener("click", function () { remember("dismissed"); el.remove(); });
    return el;
  }

  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    deferred = event;
    setTimeout(function () {
      if (!deferred || document.querySelector(".pwa-bar")) return;
      var el = bar("Вакансии открываются быстрее, как обычное приложение. Бесплатно, без магазина приложений.", "Установить");
      el.querySelector("[data-go]").addEventListener("click", function () {
        el.remove();
        deferred.prompt();
        deferred.userChoice.then(function (choice) {
          remember(choice && choice.outcome === "accepted" ? "installed" : "dismissed");
          deferred = null;
        });
      });
    }, 4000);
  });
  window.addEventListener("appinstalled", function () { remember("installed"); });

  if (ios) {
    setTimeout(function () {
      if (document.querySelector(".pwa-bar")) return;
      var el = bar("Нажмите «Поделиться» в браузере и выберите «На экран Домой»: сайт будет открываться как приложение.", "Понятно");
      el.querySelector("[data-go]").addEventListener("click", function () { remember("dismissed"); el.remove(); });
    }, 4000);
  }
})();
