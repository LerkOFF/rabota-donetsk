/* Админка: тема, подтверждение опасных действий, шаблоны причин отказа. Без скриптов все формы работают. */
(function () {
  var root = document.documentElement;

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
  }

  /* ---------- подтверждение: кнопка с data-confirm спрашивает один раз ---------- */
  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-confirm]") : null;
    if (!btn) return;
    if (!window.confirm(btn.getAttribute("data-confirm"))) e.preventDefault();
  });

  /* ---------- шаблон причины отказа подставляется в текст, если он пустой или это прежний шаблон ---------- */
  document.querySelectorAll("[data-reject]").forEach(function (form) {
    var select = form.querySelector("[data-reason-select]");
    var text = form.querySelector("[data-reason-text]");
    if (!select || !text) return;
    var last = "";
    select.addEventListener("change", function () {
      var opt = select.options[select.selectedIndex];
      var tpl = opt ? opt.getAttribute("data-text") || "" : "";
      if (!text.value || text.value === last) {
        text.value = tpl;
        last = tpl;
      }
    });
  });

  /* ---------- сообщения об ошибках получают фокус ---------- */
  var err = document.getElementById("adm-error") || document.getElementById("error-summary");
  if (err) err.focus();
})();
