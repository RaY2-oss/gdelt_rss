// Шесть мелочей, ради которых не нужен фреймворк: атлас, поиск по всему
// корпусу, полоса дат, фильтр по видимой ленте, переключатель темы и кнопка
// атласа, проявляющаяся после прокрутки. Всё остальное на странице —
// статический HTML, а движение — на CSS (см. animation-timeline в style.css).
(function () {
  'use strict';

  var root = document.documentElement;
  var q$ = function (s) { return document.querySelector(s); };
  var all = function (s) {
    return Array.prototype.slice.call(document.querySelectorAll(s));
  };
  var el = function (tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  // Полоса дат и фильтр по слову режут одну и ту же ленту, поэтому и решение
  // о показе строки принимается в одном месте. apply() объявлен здесь, чтобы
  // полоса дат могла его дёрнуть, даже если фильтра на странице нет.
  var apply = function () {};
  var dayPass = null; // null — все дни

  // ── Атлас ─────────────────────────────────────────────────────────────
  // Панель ложится поверх страницы и ничего не двигает, поэтому и состояние
  // между страницами не помнится: открытый по памяти атлас закрывал бы ленту
  // на каждом переходе.
  var atlas = document.getElementById('atlas');
  var atlasBtn = q$('.atlas-btn');
  var scrim = q$('.atlas__scrim');

  var setAtlas = function (open) {
    root.classList.toggle('is-atlas', open);
    if (atlasBtn) atlasBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (atlas) atlas.setAttribute('aria-hidden', open ? 'false' : 'true');
    if (scrim) scrim.hidden = !open;
  };

  if (atlas && atlasBtn) {
    atlasBtn.addEventListener('click', function () {
      setAtlas(!root.classList.contains('is-atlas'));
    });
    if (scrim) scrim.addEventListener('click', function () { setAtlas(false); });
    var closeBtn = q$('.atlas__close');
    if (closeBtn) closeBtn.addEventListener('click', function () { setAtlas(false); });
  }

  // ── Поиск по всему корпусу ────────────────────────────────────────────
  // На странице лежит только её лента, а искать хочется по всей неделе,
  // поэтому корпус приезжает отдельным файлом — но лениво, по первому
  // касанию: тем, кто просто читает, он не стоит ничего.
  //
  // Группировка (страна, объект, дата) не отдельный экран «расширенный
  // поиск», а полоски под строкой: они считаются по тому, что уже нашлось, и
  // показывают только то, чем эту выборку и правда можно сузить.
  var find = document.getElementById('find');
  var findBtn = q$('.jump');

  if (find && findBtn) {
    var input = find.querySelector('.find__input');
    var listBox = find.querySelector('[data-find-list]');
    var groupBox = find.querySelector('[data-find-groups]');
    var tagBox = find.querySelector('[data-find-tags]');
    var seedBox = find.querySelector('[data-find-seeds]');
    var statBox = find.querySelector('[data-find-stat]');

    var IDX = null, pending = null;
    var st = { q: '', c: null, e: null, d: null };
    var LIMIT = 60;

    var load = function () {
      if (IDX) return Promise.resolve(IDX);
      if (!pending) {
        pending = fetch('/search.json').then(function (r) { return r.json(); })
          .then(function (j) {
            // Один плоский стог на запись: страна и домен ищутся тем же
            // подстрочным поиском, что и заголовок, — отдельные поля дали бы
            // три прохода вместо одного.
            j.hay = j.s.map(function (s) {
              return (s[0] + ' ' + j.c[s[1]][1] + ' ' + s[5] + ' ' + s[6]).toLowerCase();
            });
            IDX = j;
            return j;
          });
      }
      return pending;
    };

    var entName = function (key) {
      return (IDX && IDX.n && IDX.n[key]) || key.replace(/(^|[\s-])\S/g, function (m) {
        return m.toUpperCase();
      });
    };

    var hits = function () {
      var q = st.q.trim().toLowerCase();
      var out = [];
      for (var i = 0; i < IDX.s.length; i++) {
        var s = IDX.s[i];
        if (st.c && st.c.indexOf(s[1]) < 0) continue;
        if (st.d != null && s[2] !== st.d) continue;
        if (st.e && (';' + s[6] + ';').indexOf(';' + st.e + ';') < 0) continue;
        if (q && IDX.hay[i].indexOf(q) < 0) continue;
        out.push(s);
      }
      return out;
    };

    var tier = function (score) {
      return score >= 85 ? 4 : score >= 70 ? 3 : score >= 50 ? 2 : score >= 30 ? 1 : 0;
    };

    // Полоска фасета: подпись, число и снятие по повторному нажатию.
    var group = function (title, pairs, pick) {
      if (pairs.length < 2) return;
      var box = el('div', 'find__group');
      box.appendChild(el('span', 'find__gcap', title));
      pairs.slice(0, 8).forEach(function (p) {
        var b = el('button', 'find__gbtn', p.label);
        b.type = 'button';
        b.appendChild(el('b', null, p.n));
        b.addEventListener('click', function () { pick(p.value); render(); });
        box.appendChild(b);
      });
      groupBox.appendChild(box);
    };

    var facets = function (rows) {
      groupBox.textContent = '';
      var byC = {}, byE = {}, byD = {};
      rows.forEach(function (s) {
        byC[s[1]] = (byC[s[1]] || 0) + 1;
        byD[s[2]] = (byD[s[2]] || 0) + 1;
        s[6].split(';').forEach(function (e) { if (e) byE[e] = (byE[e] || 0) + 1; });
      });
      var top = function (obj, label, min) {
        return Object.keys(obj).filter(function (k) { return obj[k] >= (min || 1); })
          .sort(function (a, b) { return obj[b] - obj[a]; })
          .map(function (k) { return { value: k, n: obj[k], label: label(k) }; });
      };

      if (!st.c) group('Страна', top(byC, function (k) { return IDX.c[k][1]; }), function (v) {
        st.c = [+v];
      });
      if (!st.e) group('Объект', top(byE, entName, 2), function (v) { st.e = v; });
      // Даты — по календарю, а не по числу сюжетов: полоска дат читается
      // как ось, и «01.01» между «27.07» и «25.07» сбивает счёт.
      if (st.d == null) group('Дата', top(byD, function (k) { return IDX.d[k].slice(5).split('-').reverse().join('.'); })
        .sort(function (a, b) { return b.value - a.value; }),
        function (v) { st.d = +v; });
      groupBox.hidden = !groupBox.firstChild;
    };

    var tags = function () {
      tagBox.textContent = '';
      var add = function (label, drop) {
        var b = el('button', 'find__tag', label);
        b.type = 'button';
        b.appendChild(el('i', null, '×'));
        b.addEventListener('click', function () { drop(); render(); });
        tagBox.appendChild(b);
      };
      if (st.c) add(st.c.length > 1 ? st.c.length + ' стран' : IDX.c[st.c[0]][1],
                    function () { st.c = null; });
      if (st.e) add(entName(st.e), function () { st.e = null; });
      if (st.d != null) add(IDX.d[st.d], function () { st.d = null; });
      tagBox.hidden = !tagBox.firstChild;
    };

    var render = function () {
      if (!IDX) return;
      var rows = hits();
      var narrowed = st.q.trim() || st.c || st.e || st.d != null;
      if (seedBox) seedBox.hidden = !!narrowed;
      tags();
      // Пока ничего не сужено, фасеты — копия затравок; показываем их с первого
      // введённого слова.
      facets(narrowed ? rows : []);

      listBox.textContent = '';
      rows.slice(0, LIMIT).forEach(function (s) {
        var li = el('li', 'find__row');
        li.style.setProperty('--w', s[3]);
        li.dataset.tier = tier(s[3]);
        var a = el('a', 'find__link', s[0]);
        a.href = s[4];
        a.rel = 'noopener nofollow';
        li.appendChild(a);
        var meta = el('p', 'find__meta');
        meta.appendChild(el('span', 'find__score', s[3]));
        meta.appendChild(el('span', null, IDX.c[s[1]][1]));
        meta.appendChild(el('span', null, s[5]));
        meta.appendChild(el('span', null, IDX.d[s[2]].slice(5).split('-').reverse().join('.')));
        li.appendChild(meta);
        listBox.appendChild(li);
      });

      statBox.textContent = !narrowed ? IDX.s.length + ' сюжетов за неделю'
        : rows.length ? (rows.length > LIMIT ? 'Первые ' + LIMIT + ' из ' + rows.length
                                             : 'Нашлось: ' + rows.length)
        : 'Ничего не нашлось. Снимите один из фильтров или попробуйте другое слово.';
    };

    // Каждое открытие начинается с чистого листа. Иначе поиск, закрытый с
    // тремя фасетами, открывался бы с ними же — и на пустой запрос показывал
    // «ничего не нашлось», не объясняя, что ищет он вовсе не то, что набрано.
    var open = function (seed, scoped) {
      find.hidden = false;
      root.classList.add('is-find');
      findBtn.setAttribute('aria-expanded', 'true');
      st = { q: '', c: null, e: null, d: null };
      input.value = '';
      load().then(function () {
        if (seed) st.e = seed;
        // «С учётом страницы»: объект со страницы страны ищется в её пределах,
        // со страницы региона — в пределах его стран. Иначе нажатие на имя в
        // блоке «кто в новостях» уводило бы из страны в мир.
        if (scoped) {
          var c = document.body.dataset.country, r = document.body.dataset.region, i;
          if (c) {
            for (i = 0; i < IDX.c.length; i++) if (IDX.c[i][0] === c) st.c = [i];
          } else if (r) {
            for (i = 0; i < IDX.g.length; i++) if (IDX.g[i][0] === r) st.c = IDX.g[i][2];
          }
        }
        render();
        input.focus();
      });
    };

    var close = function () {
      find.hidden = true;
      root.classList.remove('is-find');
      findBtn.setAttribute('aria-expanded', 'false');
    };

    findBtn.addEventListener('click', function () { open(); });
    find.querySelector('.find__close').addEventListener('click', close);
    find.addEventListener('click', function (e) { if (e.target === find) close(); });

    input.addEventListener('input', function () { st.q = input.value; render(); });

    // Затравки: имена под строкой, объекты лида, имена в «кто в новостях».
    document.addEventListener('click', function (e) {
      var b = e.target.closest ? e.target.closest('[data-seed]') : null;
      if (b) { open(b.dataset.seed, 'seedScope' in b.dataset); return; }
      if (e.target.closest && e.target.closest('[data-find-open]')) {
        var f = q$('[data-filter]');
        open();
        if (f && f.value) { st.q = f.value; input.value = f.value; render(); }
      }
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        if (root.classList.contains('is-find')) close();
        else if (root.classList.contains('is-atlas')) setAtlas(false);
        return;
      }
      var tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea') return;
      if (e.key === '/' || ((e.metaKey || e.ctrlKey) && e.key === 'k')) {
        e.preventDefault();
        open();
      }
    });
  }

  // ── Полоса дат: фильтр, который заодно график ─────────────────────────
  var stories = all('.story');
  var strip = q$('[data-days]');
  var btns = all('.days__btn');

  if (strip && btns.length && stories.length) {
    var reset = strip.querySelector('[data-days-reset]');

    // Гистограмма считается по самой ленте, а не приезжает из сборщика:
    // цифра под столбиком и число строк после фильтра не могут разойтись.
    var per = {};
    stories.forEach(function (n) {
      var d = n.dataset.day;
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
      return btns.filter(function (b) { return b.classList.contains('is-on'); }).length;
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

  // ── Фильтр по видимой ленте: слово + даты ─────────────────────────────
  var word = q$('[data-filter]');
  var counter = q$('[data-filter-count]');
  var none = q$('.feed__none');

  if (stories.length) {
    apply = function () {
      var v = word ? word.value.trim().toLowerCase() : '';
      var shown = 0;
      stories.forEach(function (n) {
        var hit = (!v || (n.dataset.find || '').indexOf(v) !== -1) &&
                  (!dayPass || dayPass.indexOf(n.dataset.day) !== -1);
        n.hidden = !hit;
        if (hit) shown++;
      });
      var narrowed = v || dayPass;
      if (counter) counter.textContent = narrowed ? shown + ' из ' + stories.length : '';
      if (none) none.hidden = !narrowed || shown > 0;
    };

    if (word) word.addEventListener('input', apply);
    // страница могла восстановиться из bfcache с непустым полем
    if (word && word.value) apply();
  }

  // ── Подсказки фильтра: словарь этой страницы, а не всего корпуса ───────
  // Поиск по всей неделе (.find) подсказывает объектами всего корпуса — здесь
  // это было бы враньём: фильтр режет ТУ ленту, что на странице, и подсказка
  // «Индонезия» на странице Кении не нашла бы ничего. Словарь собирается по
  // самой ленте — страны, издания, имена, — поэтому у страны в подсказках её
  // издания и её люди, у региона — его страны.
  var sugg = q$('[data-sugg]');
  if (sugg && word && stories.length) {
    var dict = null;

    var build = function () {
      var count = {};
      var add = function (s) {
        s = (s || '').trim();
        if (s.length > 1) count[s] = (count[s] || 0) + 1;
      };
      stories.forEach(function (n) {
        add((n.querySelector('.story__country') || {}).textContent);
        add((n.querySelector('.story__source') || {}).textContent);
        Array.prototype.forEach.call(n.querySelectorAll('.ents button'),
          function (b) { add(b.textContent); });
      });
      return Object.keys(count)
        .sort(function (a, b) { return count[b] - count[a] || a.localeCompare(b); })
        .map(function (k) { return { label: k, n: count[k] }; });
    };

    var draw = function () {
      if (!dict) dict = build();
      var v = word.value.trim().toLowerCase();
      var rows = dict.filter(function (d) {
        return !v || (d.label.toLowerCase().indexOf(v) !== -1 && d.label.toLowerCase() !== v);
      }).slice(0, 8);
      sugg.textContent = '';
      rows.forEach(function (d) {
        var b = el('button', 'filter__opt', d.label);
        b.type = 'button';
        b.appendChild(el('b', null, d.n));
        // mousedown, а не click: до click поле успевает потерять фокус, панель
        // закрывается, и нажатие приходит уже в пустоту
        b.addEventListener('mousedown', function (e) {
          e.preventDefault();
          word.value = d.label;
          apply();
          draw();
          word.focus();
        });
        sugg.appendChild(b);
      });
      sugg.hidden = !rows.length;
    };

    word.addEventListener('focus', draw);
    word.addEventListener('input', draw);
    word.addEventListener('blur', function () { sugg.hidden = true; });
    word.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !sugg.hidden) { e.stopPropagation(); sugg.hidden = true; }
    });
  }

  // ── Свернуть текст снизу ──────────────────────────────────────────────
  // Второй <summary> в <details> невозможен, а дочитавшему до конца незачем
  // листать к началу, чтобы закрыть. Три строки вместо своего аккордеона.
  document.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('[data-close-full]') : null;
    if (!b) return;
    var box = b.closest('details');
    box.open = false;
    box.querySelector('summary').scrollIntoView({ block: 'nearest' });
  });

  // ── Проявление при прокрутке там, где нет CSS-таймлайнов ──────────────
  // В Chromium всё движение считает CSS (animation-timeline: view()), и сюда
  // мы не заходим. В Firefox и Safari таймлайнов нет — без этого лента у
  // половины читателей просто возникала целиком, без единого перехода.
  if (window.IntersectionObserver &&
      !(window.CSS && CSS.supports && CSS.supports('animation-timeline: view()')) &&
      !matchMedia('(prefers-reduced-motion: reduce)').matches) {
    root.classList.add('no-tl');
    var eye = new IntersectionObserver(function (rows) {
      rows.forEach(function (r) {
        if (!r.isIntersecting) return;
        r.target.classList.add('is-in');
        eye.unobserve(r.target);
      });
    }, { rootMargin: '0px 0px -8% 0px' });
    all('.story:not(.story--lead), .chips__item, .who__list li, .rmap')
      .forEach(function (n) { eye.observe(n); });
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
