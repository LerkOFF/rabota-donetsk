/* Кнопка "Показать" у полей пароля. Без скрипта поле остаётся обычным. */
(function () {
  document.querySelectorAll("[data-toggle-password]").forEach(function (btn) {
    var input = btn.parentElement.querySelector("input");
    if (!input) return;
    btn.hidden = false;
    btn.addEventListener("click", function () {
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.textContent = show ? "Скрыть" : "Показать";
      btn.setAttribute("aria-pressed", show ? "true" : "false");
      btn.setAttribute("aria-label", show ? "Скрыть пароль" : "Показать пароль");
    });
  });
})();

/* Telegram: пока открыта страница ожидания, раз в пару секунд спрашиваем статус. Без скрипта работают кнопки. */
(function () {
  var POLL_MS = 2500;
  var MAX_MS = 16 * 60 * 1000;

  function poll(url, onData) {
    var started = Date.now();
    var timer = null;
    function tick() {
      if (Date.now() - started > MAX_MS) return;
      if (document.hidden) { timer = setTimeout(tick, POLL_MS); return; }
      fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" }, cache: "no-store" })
        .then(function (r) { if (!r.ok) throw new Error("status " + r.status); return r.json(); })
        .then(function (data) { if (onData(data) !== true) timer = setTimeout(tick, POLL_MS); })
        .catch(function () { timer = setTimeout(tick, POLL_MS * 2); });
    }
    timer = setTimeout(tick, POLL_MS);
    return function stop() { clearTimeout(timer); };
  }

  var login = document.querySelector("[data-tg-login][data-tg-status-url]");
  if (login) {
    var form = login.querySelector("[data-tg-complete]");
    var wait = login.querySelector("[data-tg-wait]");
    var current = login.className.match(/tg-login--(\w+)/);
    current = current ? current[1] : "";
    if (wait) wait.hidden = false;
    poll(login.getAttribute("data-tg-status-url"), function (data) {
      var state = data && data.state;
      if (!state || state === current) return false;
      if (state === "confirmed" && form) { form.submit(); return true; }
      window.location.reload();
      return true;
    });
  }

  var confirmed = document.querySelector(".tg-login--confirmed [data-tg-complete]");
  if (confirmed) confirmed.submit();

  var account = document.querySelector("[data-tg-account][data-tg-status-url]");
  if (account) {
    poll(account.getAttribute("data-tg-status-url"), function (data) {
      if (data && data.linked) { window.location.assign("/account?saved=tg_linked"); return true; }
      return false;
    });
  }
})();
