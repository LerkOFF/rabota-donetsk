/* Выбранная тема ставится до первой отрисовки, чтобы не мигала. */
(function () {
  var root = document.documentElement;
  root.classList.add("js");
  try {
    var saved = localStorage.getItem("theme");
    if (saved === "dark" || saved === "light") root.setAttribute("data-theme", saved);
  } catch (e) { /* хранилище недоступно: остаётся системная тема */ }
})();
