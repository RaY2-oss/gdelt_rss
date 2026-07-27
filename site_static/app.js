// Три мелочи, ради которых не нужен фреймворк: переход к стране, фильтр по
// ленте, переключатель темы. Всё остальное на странице — статический HTML.
(function () {
  'use strict';

  // ── Переход к стране ──────────────────────────────────────────────────
  var nav = window.__NAV__ || [];
  var jump = document.getElementById('jump');
  var list = document.getElementById('countries');

  if (jump && list) {
    nav.forEach(function (c) {
      var o = document.createElement('option');
      o.value = c.n;
      o.label = c.r;
      list.appendChild(o);
    });

    var go = function () {
      var q = jump.value.trim().toLowerCase();
      if (!q) return;
      var hit = nav.find(function (c) { return c.n.toLowerCase() === q; }) ||
                nav.find(function (c) { return c.n.toLowerCase().indexOf(q) === 0; });
      if (hit) location.href = hit.u;
    };

    // change ловит выбор из datalist мышью, keydown — ввод с клавиатуры
    jump.addEventListener('change', go);
    jump.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); go(); }
    });
  }

  // ── Фильтр по ленте ───────────────────────────────────────────────────
  var input = document.querySelector('[data-filter]');
  var counter = document.querySelector('[data-filter-count]');
  var none = document.querySelector('.feed__none');
  var stories = Array.prototype.slice.call(document.querySelectorAll('.story'));

  if (input && stories.length) {
    var apply = function () {
      var q = input.value.trim().toLowerCase();
      var shown = 0;
      stories.forEach(function (el) {
        var hit = !q || el.dataset.find.indexOf(q) !== -1;
        el.hidden = !hit;
        if (hit) shown++;
      });
      if (counter) counter.textContent = q ? shown + ' из ' + stories.length : '';
      if (none) none.hidden = !q || shown > 0;
    };
    input.addEventListener('input', apply);
    // страница могла восстановиться из bfcache с непустым полем
    if (input.value) apply();
  }

  // ── Тема ──────────────────────────────────────────────────────────────
  var order = ['auto', 'light', 'dark'];
  var names = { auto: 'Тема: как в системе', light: 'Тема: светлая', dark: 'Тема: тёмная' };
  var button = document.querySelector('.theme');

  if (button) {
    var root = document.documentElement;
    var sync = function () {
      button.title = names[root.dataset.theme] || names.auto;
      button.setAttribute('aria-label', button.title);
    };
    button.addEventListener('click', function () {
      var next = order[(order.indexOf(root.dataset.theme) + 1) % order.length];
      root.dataset.theme = next;
      try {
        if (next === 'auto') localStorage.removeItem('theme');
        else localStorage.setItem('theme', next);
      } catch (e) {}
      sync();
    });
    sync();
  }
})();
