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
