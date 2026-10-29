/* Минимум скриптов: тема, меню, выдвижная панель фильтров, автоприменение фильтров. Без скриптов сайт работает. */
(function () {
  var root = document.documentElement;
  var desktop = window.matchMedia("(min-width: 900px)");

  /* ---------- тема ---------- */
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

  /* ---------- меню на телефоне ---------- */
  var menuBtn = document.querySelector("[data-menu-toggle]");
  var menuPanel = document.querySelector("[data-menu-panel]");
  function setMenu(open) {
    if (!menuBtn || !menuPanel) return;
    menuPanel.classList.toggle("is-open", open);
    menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
    menuBtn.setAttribute("aria-label", open ? "Закрыть меню" : "Открыть меню");
  }
  if (menuBtn && menuPanel) {
    menuBtn.addEventListener("click", function () { setMenu(!menuPanel.classList.contains("is-open")); });
    desktop.addEventListener("change", function () { setMenu(false); });
  }

  /* ---------- меню пользователя: закрывается кликом снаружи и Esc ---------- */
  var userMenu = document.querySelector("[data-menu]");
  if (userMenu) {
    document.addEventListener("click", function (e) {
      if (userMenu.open && !userMenu.contains(e.target)) userMenu.open = false;
    });
    userMenu.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && userMenu.open) { userMenu.open = false; userMenu.querySelector("summary").focus(); }
    });
  }

  /* ---------- панель фильтров на телефоне ---------- */
  var panelBtn = document.querySelector("[data-filters-toggle]");
  var panel = document.querySelector("[data-filters]");
  var scrim = document.querySelector("[data-scrim]");
  function focusables() {
    return panel.querySelectorAll("a[href], button:not([disabled]), input:not([disabled]), select, summary");
  }
  function setPanel(open) {
    if (!panel || !panelBtn) return;
    panel.classList.toggle("is-open", open);
    panelBtn.setAttribute("aria-expanded", open ? "true" : "false");
    if (scrim) scrim.hidden = !open;
    document.body.style.overflow = open ? "hidden" : "";
    if (open) {
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-modal", "true");
      var close = panel.querySelector("[data-filters-close]");
      if (close) close.focus();
    } else {
      panel.removeAttribute("role");
      panel.removeAttribute("aria-modal");
    }
  }
  if (panel && panelBtn) {
    panelBtn.addEventListener("click", function () { setPanel(true); });
    panel.querySelectorAll("[data-filters-close]").forEach(function (b) {
      b.addEventListener("click", function () { setPanel(false); panelBtn.focus(); });
    });
    if (scrim) scrim.addEventListener("click", function () { setPanel(false); panelBtn.focus(); });
    panel.addEventListener("keydown", function (e) {
      if (!panel.classList.contains("is-open")) return;
      if (e.key === "Escape") { setPanel(false); panelBtn.focus(); return; }
      if (e.key !== "Tab") return;
      var items = focusables();
      var first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
    desktop.addEventListener("change", function () { setPanel(false); });
  }

  var errors = document.getElementById("error-summary");
  if (errors) errors.focus();

  /* на компьютере фильтры применяются сразу, на телефоне по кнопке "Показать"; сортировка всегда сразу */
  var form = document.querySelector("[data-autosubmit]");
  if (form) {
    form.addEventListener("change", function (e) {
      var always = e.target && e.target.hasAttribute && e.target.hasAttribute("data-submit-on-change");
      if (always || desktop.matches) form.requestSubmit();
    });
  }
})();
