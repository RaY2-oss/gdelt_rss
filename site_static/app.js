// Пять мелочей, ради которых не нужен фреймворк: атлас, переход к стране,
// полоса дат, фильтр по ленте и переключатель темы. Всё остальное на
// странице — статический HTML.
(function () {
  'use strict';

  var root = document.documentElement;
  var q$ = function (s) { return document.querySelector(s); };
  var all = function (s) {
    return Array.prototype.slice.call(document.querySelectorAll(s));
  };

  // Полоса дат и фильтр по слову режут одну и ту же ленту, поэтому и решение
  // о показе строки принимается в одном месте. apply() объявлен здесь, чтобы
  // полоса дат могла его дёрнуть, даже если фильтра на странице нет.
  var apply = function () {};
  var dayPass = null; // null — все дни

  // ── Атлас ─────────────────────────────────────────────────────────────
  var atlas = document.getElementById('atlas');
  var atlasBtn = q$('.atlas-btn');
  var scrim = q$('.atlas__scrim');
  var wide = window.matchMedia('(min-width: 64rem)');

  if (atlas && atlasBtn) {
    var setAtlas = function (open) {
      root.classList.toggle('is-atlas', open);
      atlasBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
      atlas.setAttribute('aria-hidden', open ? 'false' : 'true');
      if (scrim) scrim.hidden = !open;
      // Помним только на широком экране: там атлас сдвигает страницу и жить с
      // ним можно. На узком он ложится поверх — открывать его на каждой
      // странице заново значило бы прятать саму ленту.
      try { if (wide.matches) localStorage.setItem('atlas', open ? '1' : '0'); } catch (e) {}
    };

    atlasBtn.addEventListener('click', function () {
      setAtlas(!root.classList.contains('is-atlas'));
    });
    if (scrim) scrim.addEventListener('click', function () { setAtlas(false); });
    var closeBtn = q$('.atlas__close');
    if (closeBtn) closeBtn.addEventListener('click', function () { setAtlas(false); });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && root.classList.contains('is-atlas')) setAtlas(false);
    });

    // Экран сузили при открытом атласе — сдвиг пропал, накрытие появилось.
    wide.addEventListener('change', function () {
      if (root.classList.contains('is-atlas')) setAtlas(true);
    });

    // Состояние восстановил inline-скрипт в <head> (до отрисовки), кнопке и
    // затемнению об этом ещё не сказали.
    setAtlas(root.classList.contains('is-atlas'));
  }

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
      var v = jump.value.trim().toLowerCase();
      if (!v) return;
      var hit = nav.find(function (c) { return c.n.toLowerCase() === v; }) ||
                nav.find(function (c) { return c.n.toLowerCase().indexOf(v) === 0; });
      if (hit) location.href = hit.u;
    };

    // change ловит выбор из datalist мышью, keydown — ввод с клавиатуры
    jump.addEventListener('change', go);
    jump.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); go(); }
    });
  }

  var stories = all('.story');

  // ── Полоса дат: фильтр, который заодно график ─────────────────────────
  var strip = q$('[data-days]');
  var btns = all('.days__btn');

  if (strip && btns.length && stories.length) {
    var reset = strip.querySelector('[data-days-reset]');

    // Гистограмма считается по самой ленте, а не приезжает из сборщика:
    // цифра под столбиком и число строк после фильтра не могут разойтись.
    var per = {};
    stories.forEach(function (el) {
      var d = el.dataset.day;
      per[d] = (per[d] || 0) + 1;
    });
    var peak = 1;
    btns.forEach(function (b) { peak = Math.max(peak, per[b.dataset.day] || 0); });
    btns.forEach(function (b) {
      var n = per[b.dataset.day] || 0;
      b.querySelector('.days__n').textContent = n || '';
      b.querySelector('.days__bar > i').style.setProperty('--h', Math.round(n / peak * 100));
      b.title = b.title + ' — ' + n;
    });

    var mark = function (lo, hi) {
      var keys = [];
      btns.forEach(function (b, i) {
        var on = i >= lo && i <= hi;
        b.classList.toggle('is-on', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
        if (on) keys.push(b.dataset.day);
      });
      var whole = lo === 0 && hi === btns.length - 1;
      dayPass = whole ? null : keys;
      if (reset) reset.hidden = whole;
      apply();
    };

    var span = function () {
      var on = btns.filter(function (b) { return b.classList.contains('is-on'); });
      return on.length;
    };

    // Клик — один день; повторный клик по нему же — снова вся неделя.
    // Протяжка — период. Один жест, два действия, без выпадающих списков.
    var from = -1, dragged = false;

    strip.addEventListener('pointerdown', function (e) {
      var b = e.target.closest('.days__btn');
      if (!b) return;
      from = btns.indexOf(b);
      dragged = false;
    });

    strip.addEventListener('pointermove', function (e) {
      if (from < 0 || !e.buttons) return;
      var b = e.target.closest('.days__btn');
      if (!b) return;
      var to = btns.indexOf(b);
      if (to === from && !dragged) return;
      dragged = true;
      mark(Math.min(from, to), Math.max(from, to));
    });

    document.addEventListener('pointerup', function () {
      if (from >= 0 && !dragged) {
        var solo = btns[from].classList.contains('is-on') && span() === 1;
        mark(solo ? 0 : from, solo ? btns.length - 1 : from);
      }
      from = -1;
    });

    // Клавиатура: pointer-события её не касаются, click от Enter/Space придёт
    // сюда — но только если жеста мышью не было.
    strip.addEventListener('click', function (e) {
      var b = e.target.closest('.days__btn');
      if (!b || e.detail) return; // detail=0 значит «с клавиатуры»
      var i = btns.indexOf(b);
      var solo = b.classList.contains('is-on') && span() === 1;
      mark(solo ? 0 : i, solo ? btns.length - 1 : i);
    });

    if (reset) reset.addEventListener('click', function () { mark(0, btns.length - 1); });
  }

  // ── Фильтр по ленте: слово + порог важности + даты ────────────────────
  var input = q$('[data-filter]');
  var counter = q$('[data-filter-count]');
  var none = q$('.feed__none');
  var views = all('[data-view]');

  if (stories.length) {
    var floor = 0;
    apply = function () {
      var v = input ? input.value.trim().toLowerCase() : '';
      var shown = 0;
      stories.forEach(function (el) {
        var hit = (!v || el.dataset.find.indexOf(v) !== -1) &&
                  (+el.dataset.score || 0) >= floor &&
                  (!dayPass || dayPass.indexOf(el.dataset.day) !== -1);
        el.hidden = !hit;
        if (hit) shown++;
      });
      var narrowed = v || floor || dayPass;
      if (counter) counter.textContent = narrowed ? shown + ' из ' + stories.length : '';
      if (none) none.hidden = !narrowed || shown > 0;
    };

    views.forEach(function (b) {
      b.addEventListener('click', function () {
        floor = +b.dataset.view || 0;
        views.forEach(function (o) { o.classList.toggle('is-on', o === b); });
        apply();
      });
    });

    if (input) input.addEventListener('input', apply);
    // страница могла восстановиться из bfcache с непустым полем
    if (input && input.value) apply();
  }

  // ── Тема ──────────────────────────────────────────────────────────────
  var order = ['auto', 'light', 'dark'];
  var names = { auto: 'Тема: как в системе', light: 'Тема: светлая', dark: 'Тема: тёмная' };
  var button = q$('.theme');

  if (button) {
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
