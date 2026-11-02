// Админка: порядок блоков перетаскиванием на любой странице, мышью и пальцем.
// Разметка: контейнер data-sortable="<id>", элементы внутри data-key="<ключ>".
// Порядок запоминается в браузере (localStorage), «Сбросить порядок» возвращает исходный.
(function () {
  'use strict';

  var PREFIX = 'adm:order:';
  var store = {
    get: function (key) { try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (e) { return null; } },
    set: function (key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* приватный режим */ } },
    del: function (key) { try { localStorage.removeItem(key); } catch (e) { /* приватный режим */ } }
  };

  function items(box) {
    return Array.prototype.filter.call(box.children, function (el) { return el.hasAttribute('data-key'); });
  }

  function init(box) {
    var id = box.getAttribute('data-sortable');
    if (!id || box.__dnd) return;
    box.__dnd = true;
    var key = PREFIX + location.pathname + ':' + id;
    var original = items(box).map(function (el) { return el.getAttribute('data-key'); });

    var apply = function (order) {
      order.forEach(function (k) {
        var el = items(box).filter(function (x) { return x.getAttribute('data-key') === k; })[0];
        if (el) box.appendChild(el);
      });
    };
    var saved = store.get(key);
    if (Array.isArray(saved)) apply(saved);

    items(box).forEach(function (el) { el.setAttribute('draggable', 'true'); el.classList.add('dnd-item'); });
    box.classList.add('dnd-box');

    var dragged = null;
    var save = function () {
      store.set(key, items(box).map(function (el) { return el.getAttribute('data-key'); }));
      showReset();
    };
    var targetAt = function (x, y) {
      var el = document.elementFromPoint(x, y);
      el = el && el.closest('[data-key]');
      return el && el.parentNode === box ? el : null;
    };
    var place = function (target, x, y) {
      if (!dragged || !target || target === dragged) return;
      var r = target.getBoundingClientRect();
      // элементы в строку (плитки) делим по горизонтали, столбиком (блоки) по вертикали
      var inline = r.width < box.getBoundingClientRect().width * 0.9;
      var after = inline ? x > r.left + r.width / 2 : y > r.top + r.height / 2;
      box.insertBefore(dragged, after ? target.nextSibling : target);
    };

    // кнопка сброса появляется только когда порядок изменён
    var reset = null;
    var showReset = function () {
      var changed = items(box).map(function (el) { return el.getAttribute('data-key'); }).join() !== original.join();
      if (!reset) {
        reset = document.createElement('button');
        reset.type = 'button';
        reset.className = 'dnd-reset';
        reset.textContent = 'Сбросить порядок';
        reset.addEventListener('click', function () { store.del(key); apply(original); showReset(); });
        box.parentNode.insertBefore(reset, box);
      }
      reset.hidden = !changed;
    };
    showReset();

    box.addEventListener('dragstart', function (e) {
      // не начинать перетаскивание с полей ввода и ссылок
      if (e.target.closest && e.target.closest('input,textarea,select')) { e.preventDefault(); return; }
      dragged = e.target.closest('[data-key]');
      if (!dragged || dragged.parentNode !== box) { dragged = null; return; }
      dragged.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', dragged.getAttribute('data-key')); } catch (err) { /* старые браузеры */ }
    });
    box.addEventListener('dragover', function (e) {
      if (!dragged) return;
      e.preventDefault();
      place(targetAt(e.clientX, e.clientY), e.clientX, e.clientY);
    });
    box.addEventListener('drop', function (e) { if (dragged) e.preventDefault(); });
    box.addEventListener('dragend', function () {
      if (dragged) dragged.classList.remove('dragging');
      dragged = null;
      save();
    });

    // касание: долгое нажатие, потом вести пальцем
    var timer = null;
    box.addEventListener('touchstart', function (e) {
      if (e.target.closest('input,textarea,select,a,button')) return;
      var el = e.target.closest('[data-key]');
      if (!el || el.parentNode !== box) return;
      timer = setTimeout(function () {
        dragged = el;
        el.classList.add('dragging');
        if (navigator.vibrate) navigator.vibrate(15);
      }, 380);
    }, { passive: true });
    box.addEventListener('touchmove', function (e) {
      if (!dragged) { clearTimeout(timer); return; }
      e.preventDefault();
      var t = e.touches[0];
      place(targetAt(t.clientX, t.clientY), t.clientX, t.clientY);
    }, { passive: false });
    var endTouch = function () {
      clearTimeout(timer);
      if (dragged) { dragged.classList.remove('dragging'); dragged = null; save(); }
    };
    box.addEventListener('touchend', endTouch);
    box.addEventListener('touchcancel', endTouch);

    // с клавиатуры: Alt+стрелки двигают сфокусированный элемент
    box.addEventListener('keydown', function (e) {
      if (!e.altKey) return;
      var el = e.target.closest && e.target.closest('[data-key]');
      if (!el || el.parentNode !== box) return;
      var prev = ['ArrowUp', 'ArrowLeft'].indexOf(e.key) >= 0;
      var next = ['ArrowDown', 'ArrowRight'].indexOf(e.key) >= 0;
      if (!prev && !next) return;
      e.preventDefault();
      if (prev && el.previousElementSibling) box.insertBefore(el, el.previousElementSibling);
      if (next && el.nextElementSibling) box.insertBefore(el.nextElementSibling, el);
      el.focus && el.focus();
      save();
    });
  }

  function boot() { document.querySelectorAll('[data-sortable]').forEach(init); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
  // блоки, добавленные позже (подгрузка списка и т.п.)
  window.admDndInit = boot;
})();
