/* Резюме: превью навыков тегами и подсказка к согласию. Без скриптов формы работают: навыки уходят строкой через запятую,
   галочка согласия видна всегда. Счётчики знаков и защита от двойной отправки приходят из messages.js. */
(function () {
  "use strict";

  /* навыки: под полем показываются теги так, как их увидит работодатель */
  document.querySelectorAll("input[data-tags]").forEach(function (input) {
    var out = document.getElementById(input.getAttribute("data-tags"));
    if (!out) return;
    function draw() {
      var seen = {};
      var tags = input.value.split(/[,;\n]+/).map(function (t) { return t.trim(); }).filter(function (t) {
        var key = t.toLowerCase();
        if (!t || seen[key]) return false;
        seen[key] = true;
        return true;
      });
      out.textContent = "";
      tags.forEach(function (t) {
        var li = document.createElement("li");
        li.className = "rs-tag";
        li.textContent = t;
        out.appendChild(li);
      });
      out.hidden = tags.length === 0;
    }
    input.addEventListener("input", draw);
    draw();
  });

  /* согласие нужно только для открытых вариантов: при «Скрыто» блок приглушается */
  var box = document.querySelector("[data-consent-box]");
  var radios = document.querySelectorAll("input[name=visibility]");
  if (box && radios.length) {
    var consent = box.querySelector("input[type=checkbox]");
    var update = function () {
      var hidden = document.querySelector("input[name=visibility]:checked");
      var off = !!hidden && hidden.value === "hidden";
      box.classList.toggle("is-muted", off);
      if (consent) consent.required = !off;
    };
    radios.forEach(function (r) { r.addEventListener("change", update); });
    update();
  }
})();
