/* Подсказки в строке поиска: профессии и места, по мере ввода. Без скриптов поиск работает как обычная форма.
   Источник: /suggest (ответ кэшируется на минуту, ответов на пустой запрос столько же, сколько популярных значений). */
(function () {
  "use strict";
  var inputs = document.querySelectorAll("input[data-suggest]");
  if (!inputs.length || !window.fetch) return;
  var seq = 0;

  function esc(text) {
    var d = document.createElement("div");
    d.textContent = text;
    return d.innerHTML;
  }

  inputs.forEach(function (input) {
    var field = input.getAttribute("data-suggest") === "where" ? "where" : "q";
    var box = input.closest(".sbar__field");
    if (!box) return;
    var list = document.createElement("ul");
    list.className = "sug";
    list.id = (input.id || "sug" + seq) + "-sug";
    list.setAttribute("role", "listbox");
    list.hidden = true;
    box.appendChild(list);
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", list.id);
    input.removeAttribute("list");

    var items = [], active = -1, timer = 0, last = null, memo = {};

    function close() {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    }
    function mark(i) {
      active = i;
      Array.prototype.forEach.call(list.children, function (li, n) {
        li.setAttribute("aria-selected", n === i ? "true" : "false");
        if (n === i) input.setAttribute("aria-activedescendant", li.id);
      });
    }
    function choose(i) {
      if (!items[i]) return;
      input.value = items[i].value;
      close();
    }
    function render(data) {
      items = data;
      list.innerHTML = "";
      if (!data.length) { close(); return; }
      data.forEach(function (it, i) {
        var li = document.createElement("li");
        li.className = "sug__item";
        li.id = list.id + "-" + i;
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", "false");
        li.innerHTML = "<span>" + esc(it.label) + "</span><span class=\"sug__n\">" + it.n + "</span>";
        li.addEventListener("mousedown", function (e) { e.preventDefault(); choose(i); });
        list.appendChild(li);
      });
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      active = -1;
    }
    function load() {
      var text = input.value.trim().slice(0, 40);
      var key = field + "|" + text.toLowerCase();
      if (memo[key]) { render(memo[key]); return; }
      var mine = ++seq;
      if (last) last.abort();
      last = window.AbortController ? new AbortController() : null;
      fetch("/suggest?f=" + field + "&q=" + encodeURIComponent(text), { signal: last ? last.signal : undefined, credentials: "omit" })
        .then(function (r) { return r.ok ? r.json() : { items: [] }; })
        .then(function (json) {
          if (mine !== seq) return;
          memo[key] = json.items || [];
          if (document.activeElement === input) render(memo[key]);
        })
        .catch(function () { /* нет сети или отмена: подсказок просто нет */ });
    }

    input.addEventListener("focus", load);
    input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(load, 120); });
    input.addEventListener("blur", function () { setTimeout(close, 120); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (list.hidden) { load(); return; }
        e.preventDefault();
        var n = items.length;
        mark(e.key === "ArrowDown" ? (active + 1) % n : (active - 1 + n) % n);
      } else if (e.key === "Enter" && !list.hidden && active >= 0) {
        e.preventDefault();
        choose(active);
      } else if (e.key === "Escape" && !list.hidden) {
        e.preventDefault();
        close();
      }
    });
  });
})();
