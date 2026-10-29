/* Минимум скриптов: переключатель темы, панель фильтров, автоприменение фильтров. */
(function () {
  var root = document.documentElement;

  function effectiveTheme() {
    var set = root.getAttribute("data-theme");
    if (set) return set;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function syncToggle(btn) {
    var dark = effectiveTheme() === "dark";
    btn.setAttribute("aria-pressed", dark ? "true" : "false");
    btn.setAttribute("aria-label", dark ? "Включить светлую тему" : "Включить тёмную тему");
  }

  var toggle = document.querySelector("[data-theme-toggle]");
  if (toggle) {
    syncToggle(toggle);
    toggle.addEventListener("click", function () {
      var next = effectiveTheme() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("theme", next); } catch (e) { /* не сохранили, не страшно */ }
      syncToggle(toggle);
    });
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () { syncToggle(toggle); });
  }

  var panelBtn = document.querySelector("[data-filters-toggle]");
  var panel = document.getElementById("filters");
  if (panelBtn && panel) {
    panelBtn.addEventListener("click", function () {
      var open = panel.classList.toggle("is-open");
      panelBtn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  var errors = document.getElementById("error-summary");
  if (errors) errors.focus();

  var form = document.querySelector("[data-autosubmit]");
  if (form) {
    form.addEventListener("change", function () {
      if (window.matchMedia("(min-width: 900px)").matches) form.requestSubmit();
    });
  }
})();
