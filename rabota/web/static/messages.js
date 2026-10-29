/* Отклики и переписка: счётчик знаков, отправка по Ctrl+Enter, защита от двойной отправки, прокрутка к последнему
   сообщению, закрытие меню по клику снаружи. Без скриптов страницы работают: формы отправляются обычным способом. */
(function () {
  "use strict";

  /* счётчик знаков у полей с data-counter */
  document.querySelectorAll("textarea[data-counter]").forEach(function (area) {
    var out = document.getElementById(area.getAttribute("data-counter"));
    var num = out && out.querySelector("[data-count]");
    var max = parseInt(area.getAttribute("maxlength") || "0", 10);
    function update() {
      if (!num) return;
      num.textContent = String(area.value.length);
      out.classList.toggle("is-over", max > 0 && area.value.length >= max);
    }
    area.addEventListener("input", update);
    update();
  });

  /* поле сообщения растёт вместе с текстом, Ctrl+Enter и Cmd+Enter отправляют */
  document.querySelectorAll("[data-composer] textarea").forEach(function (area) {
    function fit() {
      area.style.height = "auto";
      area.style.height = Math.min(area.scrollHeight + 2, 224) + "px";
    }
    area.addEventListener("input", fit);
    area.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && area.form) {
        e.preventDefault();
        if (area.value.trim() && area.form.requestSubmit) area.form.requestSubmit();
      }
    });
    fit();
  });

  /* защита от двойной отправки и подсказка, что идёт отправка */
  document.querySelectorAll("form[data-busy-form]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (form.getAttribute("data-sending") === "1") { e.preventDefault(); return; }
      form.setAttribute("data-sending", "1");
      form.querySelectorAll("button[type=submit]").forEach(function (btn) {
        if (e.submitter && btn !== e.submitter) return;
        btn.setAttribute("aria-busy", "true");
        var text = btn.getAttribute("data-busy-text");
        if (text) {
          var label = btn.lastChild;
          if (label && label.nodeType === 3) { btn.setAttribute("data-label", label.textContent); label.textContent = text; }
        }
      });
    });
  });
  /* возврат по кнопке "Назад": форма снова доступна */
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    document.querySelectorAll("form[data-sending]").forEach(function (form) {
      form.removeAttribute("data-sending");
      form.querySelectorAll("button[aria-busy]").forEach(function (btn) {
        btn.removeAttribute("aria-busy");
        var label = btn.getAttribute("data-label");
        if (label !== null && btn.lastChild && btn.lastChild.nodeType === 3) btn.lastChild.textContent = label;
      });
    });
  });

  /* переписка открывается на последнем сообщении, если нет якоря и это не страница с ошибкой формы */
  var chat = document.querySelector("[data-chat]");
  var end = document.getElementById("chat-end");
  if (chat && end && !location.hash && !document.querySelector("[role=alert]") && location.search.indexOf("before=") === -1) {
    var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var composer = document.querySelector(".composer");
    if (composer) end.style.scrollMarginBottom = composer.offsetHeight + 16 + "px";
    end.scrollIntoView({ block: "end", behavior: reduce ? "auto" : "smooth" });
  }

  /* меню действий переписки закрывается кликом снаружи и клавишей Esc */
  var menu = document.querySelector("[data-menu-popover]");
  if (menu) {
    document.addEventListener("click", function (e) { if (menu.open && !menu.contains(e.target)) menu.open = false; });
    menu.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && menu.open) { menu.open = false; menu.querySelector("summary").focus(); }
    });
  }

  /* слабая сеть: предупреждаем, что сообщение не уйдёт, пока нет соединения */
  var warn = null;
  function setOffline(off) {
    if (off && !warn) {
      warn = document.createElement("p");
      warn.className = "notice notice--warn net-warning";
      warn.setAttribute("role", "status");
      warn.textContent = "Нет соединения. Дождитесь сети и отправьте ещё раз: текст останется в поле.";
      document.body.appendChild(warn);
    } else if (!off && warn) {
      warn.remove();
      warn = null;
    }
  }
  window.addEventListener("offline", function () { setOffline(true); });
  window.addEventListener("online", function () { setOffline(false); });
})();
