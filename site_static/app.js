// Приборы витрины, ради которых не нужен фреймворк: атлас, поиск по всему
// корпусу, полоса дат, фильтр по видимой ленте, листалка, отложенное,
// похожие сюжеты, клавиатура и переключатель темы. Всё остальное на
// странице — статический HTML, а движение — на CSS (см. animation-timeline
// в style.css).
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
  // Те же три формы, что и в фильтре plural у Jinja: карточки листалки набраны
  // здесь, а строки ленты — в шаблоне, и подписаны они обязаны быть одинаково.
  var plural = function (n, one, few, many) {
    var a = n % 100, b = n % 10;
    if (a > 10 && a < 20) return many;
    if (b === 1) return one;
    if (b > 1 && b < 5) return few;
    return many;
  };

  // Полоса дат и фильтр по слову режут одну и ту же ленту, поэтому и решение
  // о показе строки принимается в одном месте. apply() объявлен здесь, чтобы
  // полоса дат могла его дёрнуть, даже если фильтра на странице нет.
  var apply = function () {};
  var dayPass = null; // null — все дни
  // Подтемы — битовая маска (см. topics.py): 0 значит «все», а не «никакие».
  // Выбранных тем может быть несколько, и сюжет проходит по любой из них: тема
  // у сюжета тоже не одна (чипы для ИИ — и то, и другое).
  var topPass = 0;

  // ── Корпус архива ─────────────────────────────────────────────────────
  // Один файл на весь архив: страница носит с собой только первую сотню
  // сюжетов, а и поиску, и листалке нужны все шесть тысяч. Грузится лениво и
  // ровно один раз — обоим достаётся один и тот же разобранный объект.
  var CORPUS = null, corpusWait = null;

  var loadCorpus = function () {
    if (CORPUS) return Promise.resolve(CORPUS);
    if (!corpusWait) {
      corpusWait = fetch('/search.json').then(function (r) { return r.json(); })
        .then(function (j) {
          // Один плоский стог на запись: страна и домен ищутся тем же
          // подстрочным поиском, что и заголовок, — отдельные поля дали бы
          // три прохода вместо одного.
          j.hay = j.s.map(function (s) {
            return (s[0] + ' ' + j.c[s[1]][1] + ' ' + s[5] + ' ' + s[6]).toLowerCase();
          });
          CORPUS = j;
          return j;
        });
    }
    return corpusWait;
  };

  // Область страницы в индексах корпуса: страна — она сама, регион — его
  // страны, главная — весь мир (null).
  var scopeOf = function (j) {
    var c = document.body.dataset.country, r = document.body.dataset.region, i;
    if (c) {
      for (i = 0; i < j.c.length; i++) if (j.c[i][0] === c) return [i];
      return [];
    }
    if (r) {
      for (i = 0; i < j.g.length; i++) if (j.g[i][0] === r) return j.g[i][2];
      return [];
    }
    return null;
  };

  var tierOf = function (score) {
    return score >= 85 ? 4 : score >= 70 ? 3 : score >= 50 ? 2 : score >= 30 ? 1 : 0;
  };

  var dm = function (day) { return day.slice(5).split('-').reverse().join('.'); };

  var calm = matchMedia('(prefers-reduced-motion: reduce)');

  // Гистограмму дат рисует полоса дат, а пересчитывает листалка — когда
  // подтянет архив и в счёт войдёт не только то, что лежит в разметке.
  var drawHist = function () {};
  // То же с числами у подтем: пока архив не приехал, они считаны по сотне строк
  // разметки, и после его прихода обязаны пересчитаться — иначе кнопка обещает
  // девять сюжетов, а показывает двести.
  var countTops = function () {};
  // Подписи подтем по маске — для карточек хвоста ленты, которые рисует сам
  // app.js. Подписи лежат на кнопках полосы (data-name); без полосы карточка
  // остаётся без метки, а не без карточки.
  var topNames = function () { return []; };

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
  // На странице лежит только её лента, а искать хочется по всему архиву,
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

    var IDX = null;
    var st = { q: '', c: null, e: null, d: null };
    var LIMIT = 60;

    var load = function () {
      return loadCorpus().then(function (j) { IDX = j; return j; });
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
      rows.slice(0, LIMIT).forEach(function (s, i) {
        var li = el('li', 'find__row');
        li.style.setProperty('--w', s[3]);
        // волна проявления: потолок задержки — чтобы хвост списка не ждал
        li.style.setProperty('--i', Math.min(i, 14));
        li.dataset.tier = tierOf(s[3]);
        var a = el('a', 'find__link', s[0]);
        a.href = s[4];
        a.rel = 'noopener nofollow';
        li.appendChild(a);
        var meta = el('p', 'find__meta');
        meta.appendChild(el('span', 'find__score', s[3]));
        meta.appendChild(el('span', null, IDX.c[s[1]][1]));
        meta.appendChild(el('span', null, s[5]));
        meta.appendChild(el('span', null, dm(IDX.d[s[2]])));
        li.appendChild(meta);
        listBox.appendChild(li);
      });

      statBox.textContent = !narrowed ? IDX.s.length + ' сюжетов в архиве'
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
      if (e.key !== 'Escape') return;
      if (root.classList.contains('is-find')) close();
      else if (root.classList.contains('is-atlas')) setAtlas(false);
    });
  }

  // ── Полоса дат: фильтр, который заодно график ─────────────────────────
  var stories = all('.story');
  var strip = q$('[data-days]');
  var btns = all('.days__btn');

  if (strip && btns.length && stories.length) {
    var reset = strip.querySelector('[data-days-reset]');

    // Гистограмма считается по ленте, а не приезжает из сборщика: цифра под
    // столбиком и число строк после фильтра не могут разойтись. Пока архив не
    // подтянут, лента — это первая сотня в разметке; как только листалка его
    // получит, она позовёт drawHist ещё раз уже со всем корпусом.
    var base = btns.map(function (b) { return b.title; });
    drawHist = function (per) {
      var peak = 1;
      btns.forEach(function (b) { peak = Math.max(peak, per[b.dataset.day] || 0); });
      btns.forEach(function (b, i) {
        var n = per[b.dataset.day] || 0;
        b.querySelector('.days__n').textContent = n || '';
        b.querySelector('.days__bar > i').style.setProperty('--h', Math.round(n / peak * 100));
        b.title = base[i] + ' — ' + n;
      });
    };

    var per = {};
    stories.forEach(function (n) {
      var d = n.dataset.day;
      per[d] = (per[d] || 0) + 1;
    });
    drawHist(per);

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

    // Клик — один день; повторный клик по нему же — снова весь период.
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

  // ── Полоса подтем ─────────────────────────────────────────────────────
  // Лента отвечает, где и когда; подтема отвечает, про что. Тема у сюжета
  // проставлена на сборке словарём (topics.py) и приезжает маской в data-tp —
  // здесь только пересечение масок, никакого разбора текста.
  //
  // Кнопки по умолчанию выключены все, и это не то же самое, что полоса дат,
  // где включён весь период: даты — непрерывный отрезок, из которого вырезают,
  // а темы — набор, в который выбирают. Тема без сюжетов в этой области кнопку
  // не получает вовсе: девять подписей, из которых работают две, — это не
  // выбор, а список того, чего нет.
  var tops = q$('[data-tops]');
  if (tops && stories.length) {
    var tbtns = all('.tops__btn');
    var tReset = tops.querySelector('[data-tops-reset]');

    // Последняя кнопка — «Прочее» (topics.mask ставит его последним битом), и
    // на карточке оно не пишется: это не тема, а её отсутствие. В маске оно
    // всегда стоит одно, так что резать больше нечего.
    topNames = function (tp) {
      var out = [];
      for (var i = 0; tp && i < tbtns.length - 1 && out.length < 3; i++) {
        if (tp & +tbtns[i].dataset.bit) out.push(tbtns[i].dataset.name);
      }
      return out;
    };

    // tail — архивная часть ленты; пока листалка её не подтянула, счёт идёт по
    // той сотне, что лежит в разметке, и потом пересчитывается (см. ensure).
    countTops = function (tail) {
      var per = {};
      var add = function (tp) {
        if (!tp) return;
        tbtns.forEach(function (b) {
          if (tp & +b.dataset.bit) per[b.dataset.bit] = (per[b.dataset.bit] || 0) + 1;
        });
      };
      stories.forEach(function (n) { add(+n.dataset.tp || 0); });
      if (tail) tail.forEach(function (t) { add(t.s[9] || 0); });

      var live = 0;
      tbtns.forEach(function (b) {
        var n = per[b.dataset.bit] || 0;
        b.querySelector('.tops__n').textContent = n;
        // Кнопка пропавшей темы не просто гаснет, а уходит: нажимать не на что.
        // Уже выбранную не трогаем — иначе фильтр снимет сам себя.
        b.hidden = !n && !b.classList.contains('is-on');
        if (!b.hidden) live++;
      });
      tops.hidden = !live;
    };
    countTops(null);

    tops.addEventListener('click', function (e) {
      var b = e.target.closest('.tops__btn');
      if (!b) return;
      var on = !b.classList.contains('is-on');
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      topPass = 0;
      tbtns.forEach(function (x) {
        if (x.classList.contains('is-on')) topPass |= +x.dataset.bit;
      });
      if (tReset) tReset.hidden = !topPass;
      apply();
    });

    if (tReset) tReset.addEventListener('click', function () {
      tbtns.forEach(function (b) {
        b.classList.remove('is-on');
        b.setAttribute('aria-pressed', 'false');
      });
      topPass = 0;
      tReset.hidden = true;
      apply();
    });
  }

  // ── Отложенное ────────────────────────────────────────────────────────
  // Список закладок живёт в localStorage браузера. Сервера у витрины нет —
  // после часовой сборки всё раздаёт nginx, — поэтому «сохранить у себя»
  // здесь означает буквально у себя, и панель об этом честно предупреждает.
  // Запись самодостаточна (заголовок, страна, издание, дата): панель
  // рисуется без корпуса, а отложенный сюжет переживает своё выпадение из
  // архива — ссылка на издание остаётся рабочей.
  var KEPT = 'kept', KEPT_MAX = 200;
  var keptBox = document.getElementById('kept');
  var keptBtn = q$('.kept-btn');

  var keptRead = function () {
    try {
      var v = JSON.parse(localStorage.getItem(KEPT));
      return v && v.length ? v : [];
    } catch (e) { return []; }
  };

  var keptWrite = function (list) {
    try { localStorage.setItem(KEPT, JSON.stringify(list.slice(0, KEPT_MAX))); } catch (e) {}
  };

  // Строка ленты → запись. Работает и для карточек из разметки, и для
  // хвостовых: у обеих одни и те же классы, других строк на витрине нет.
  var keptOf = function (art) {
    // Адрес головной статьи лежит в data-url: заголовок перестал быть ссылкой,
    // а опознавать строку по-прежнему надо чем-то устойчивым.
    var url = art && art.dataset && art.dataset.url;
    var head = art && art.querySelector('.story__title');
    if (!url || !head) return null;
    var cty = art.querySelector('.story__country');
    var when = art.querySelector('.story__time');
    var outlet = art.querySelector('.story__solo a, .story__sources a, .story__source');
    // На странице страны колонки со страной у строк нет — она в заголовке
    // страницы, и без неё запись в панели была бы безымянной.
    var ttl = document.body.dataset.country ? q$('.page__title') : null;
    return {
      u: url,
      t: head.textContent.trim(),
      c: cty ? cty.textContent.trim() : (ttl ? ttl.textContent.trim() : ''),
      s: outlet ? outlet.textContent.trim() : '',
      d: when ? (when.getAttribute('datetime') || '').slice(0, 10) : ''
    };
  };

  var keepBtn = function () {
    var kb = el('button', 'keep');
    kb.type = 'button';
    kb.dataset.keep = '';
    kb.setAttribute('aria-pressed', 'false');
    kb.setAttribute('aria-label', 'Отложить сюжет');
    kb.appendChild(el('span', 'keep__ico'));
    return kb;
  };

  var keptSet = null;   // адреса отложенного; пересобирается при каждой правке

  var keepMark = function (art) {
    var mb = art.querySelector('[data-keep]');
    var ml = art.dataset && art.dataset.url;
    if (!mb || !ml) return;
    var isOn = !!keptSet[ml];
    mb.setAttribute('aria-pressed', isOn ? 'true' : 'false');
    mb.title = isOn ? 'Убрать из отложенного' : 'Отложить сюжет';
    art.classList.toggle('is-kept', isOn);
  };

  var keptDraw = function () {
    var saved = keptRead();
    keptSet = {};
    saved.forEach(function (r) { keptSet[r.u] = 1; });
    all('.story').forEach(keepMark);

    if (keptBtn) {
      keptBtn.hidden = !saved.length;
      var num = keptBtn.querySelector('[data-kept-n]');
      if (num) num.textContent = saved.length;
      keptBtn.title = 'Отложенное: ' + saved.length;
    }
    if (!keptBox) return;

    var pane = keptBox.querySelector('[data-kept-list]');
    var blank = keptBox.querySelector('[data-kept-empty]');
    var wipe = keptBox.querySelector('[data-kept-clear]');
    if (blank) blank.hidden = !!saved.length;
    if (wipe) wipe.hidden = !saved.length;
    if (!pane) return;
    pane.textContent = '';
    saved.forEach(function (r) {
      var li = el('li', 'kept__item');
      var a = el('a', 'kept__link', r.t);
      a.href = r.u;
      a.rel = 'noopener nofollow';
      li.appendChild(a);
      var m = el('p', 'kept__meta');
      if (r.c) m.appendChild(el('span', 'kept__country', r.c));
      if (r.s) m.appendChild(el('span', 'kept__src', r.s));
      if (r.d) m.appendChild(el('span', 'kept__day', dm(r.d)));
      li.appendChild(m);
      var off = el('button', 'kept__off', '×');
      off.type = 'button';
      off.dataset.keptOff = r.u;
      off.setAttribute('aria-label', 'Убрать из отложенного');
      li.appendChild(off);
      pane.appendChild(li);
    });
  };

  var keptToggle = function (art) {
    var rec = keptOf(art);
    if (!rec) return;
    var was = keptRead();
    var next = was.filter(function (r) { return r.u !== rec.u; });
    if (next.length === was.length) next.unshift(rec);
    keptWrite(next);
    keptDraw();
  };

  var setKept = function (open) {
    if (!keptBox) return;
    keptBox.hidden = !open;
    root.classList.toggle('is-kept-open', open);
    if (keptBtn) keptBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      var shut = keptBox.querySelector('.kept__close');
      if (shut) shut.focus();
    }
  };

  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var kp = e.target.closest('[data-keep]');
    if (kp) { keptToggle(kp.closest('.story')); return; }
    var off = e.target.closest('[data-kept-off]');
    if (off) {
      keptWrite(keptRead().filter(function (r) { return r.u !== off.dataset.keptOff; }));
      keptDraw();
      return;
    }
    if (e.target.closest('[data-kept-clear]')) { keptWrite([]); keptDraw(); return; }
    if (keptBtn && e.target.closest('.kept-btn')) {
      setKept(keptBox.hidden);
      return;
    }
    if (keptBox && (e.target === keptBox || e.target.closest('.kept__close'))) setKept(false);
  });

  keptDraw();

  // ── Похожие сюжеты ────────────────────────────────────────────────────
  // Соседей считает сборка (akin в site.py) по эмбеддингу тела статьи, а
  // лежат они в корпусе — индексами в том же массиве, что и сам поиск.
  // В разметке у строки только их число: шесть тысяч сюжетов по четыре
  // ссылки — четверть мегабайта, которую почти никто не раскроет.
  var akinAt = null;   // адрес → номер в корпусе

  var akinBox = function (n) {
    var det = el('details', 'akin');
    det.dataset.akin = '';
    // Подписан так же, как список источников этажом выше: сначала число,
    // потом что считаем. Два соседних раскрытия у одной строки, набранные
    // по-разному, читались как два разных механизма.
    det.appendChild(el('summary', 'akin__sum',
      n + ' ' + plural(n, 'похожий сюжет', 'похожих сюжета', 'похожих сюжетов')));
    var ol = el('ol', 'akin__list');
    ol.dataset.akinList = '';
    ol.appendChild(el('li', 'akin__wait', 'Ищем…'));
    det.appendChild(ol);
    return det;
  };

  var akinRow = function (j, i) {
    var row = j.s[i];
    var li = el('li', 'akin__item');
    var ln = el('a', 'akin__link', row[0]);
    ln.href = row[4];
    ln.rel = 'noopener nofollow';
    li.appendChild(ln);
    var mt = el('p', 'akin__meta');
    var cl = el('a', 'akin__country', j.c[row[1]][1]);
    cl.href = '/c/' + j.c[row[1]][0] + '.html';
    mt.appendChild(cl);
    mt.appendChild(el('span', 'akin__src', row[5]));
    var tm = el('time', 'akin__day', dm(j.d[row[2]]));
    tm.dateTime = j.d[row[2]];
    mt.appendChild(tm);
    li.appendChild(mt);
    return li;
  };

  var akinFill = function (det) {
    if (det.dataset.akin === 'done') return;
    det.dataset.akin = 'done';
    var host = det.closest('.story');
    var al = host && host.dataset && host.dataset.url;
    var slot = det.querySelector('[data-akin-list]');
    if (!al || !slot) return;
    loadCorpus().then(function (j) {
      if (!akinAt) {
        akinAt = {};
        for (var i = 0; i < j.s.length; i++) akinAt[j.s[i][4]] = i;
      }
      var at = akinAt[al];
      var near = at == null ? [] : (j.k && j.k[at]) || [];
      slot.textContent = '';
      if (!near.length) {
        slot.appendChild(el('li', 'akin__wait', 'Похожего в архиве не нашлось.'));
        return;
      }
      near.forEach(function (i) { slot.appendChild(akinRow(j, i)); });
    });
  };

  // toggle не всплывает — отсюда и перехват на погружении.
  document.addEventListener('toggle', function (e) {
    var box2 = e.target;
    if (box2 && box2.open && box2.classList &&
        box2.classList.contains('akin')) akinFill(box2);
  }, true);

  // ── Клавиши ───────────────────────────────────────────────────────────
  // Осталось ровно две штуки: Esc закрывает открытое, стрелки листают ленту
  // (см. листалку ниже). Одиночных букв — j/k/s/o/a/t/e/? — больше нет, и
  // вместе с ними ушла справка о них и выключатель к ней. Каждая из них
  // дублировала кнопку, которая и так на экране: отложить — кнопка слева от
  // строки, атлас и тема — кнопки в шапке, источники — свой треугольник.
  // Заодно снялся и повод для WCAG 2.1.4: правило про одиночные символы, а
  // Esc и стрелки под него не подпадают.
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (keptBox && !keptBox.hidden) setKept(false);
    // поиск и атлас закрывает свой обработчик
  });

  // ── Лента: фильтр и листалка ──────────────────────────────────────────
  // Одна выборка, один рисовальщик. Слово, даты и номер страницы режут один и
  // тот же список, поэтому и решение о показе строки принимается в одном
  // месте — иначе фильтр и листалка спорили бы за атрибут hidden.
  //
  // Список длиннее того, что лежит в разметке. В HTML приезжает первая сотня
  // сюжетов области (страна, регион или вся витрина), а хвост архива —
  // остальные тысячи — берётся из search.json, того же файла, которым живёт
  // поиск. Это не «ещё один способ показать новости», а единственный
  // возможный: шесть тысяч карточек в разметке — это шестьдесят мегабайт.
  //
  // Хвост карточек беднее: у корпуса нет ни подводки, ни полного текста —
  // заголовок, важность, страна, издание, дата и объекты. На двухсотой
  // странице ленты, отсортированной по важности, это ровно та плотность,
  // которая там и нужна.
  var word = q$('[data-filter]');
  var counter = q$('[data-filter-count]');
  var none = q$('.feed__none');
  var feedBox = q$('[data-feed]');
  var tailBox = q$('[data-feed-tail]');
  // Листалок две — над лентой и под ней, — поэтому список, а не один узел.
  // Верхняя нужна на странице страны: сто строк не пролистать вниз ради того,
  // чтобы понять, что дальше есть ещё сорок страниц.
  var pagers = all('[data-pager]');

  if (stories.length && feedBox) {
    var eachPager = function (fn) { pagers.forEach(fn); };
    var sizeSels = pagers.map(function (b) { return b.querySelector('[data-pg-size]'); })
      .filter(Boolean);

    var total = +feedBox.dataset.total || stories.length;
    var withCountry = !('nocountry' in feedBox.dataset);

    // Что уже лежит в разметке, листалка узнаёт по адресам. Сверять по номеру
    // нельзя: порядок в корпусе и в ленте один, но при равной важности
    // сборщик разводит сюжеты по числу изданий, а его в корпусе нет.
    var mine = {};
    stories.forEach(function (n) {
      if (n.dataset.url) mine[n.dataset.url] = 1;
    });

    var tail = null, tailAsked = false;
    var size = 20, page = 0, turn = 1, drawn = false;
    try { size = +localStorage.getItem('pgsize') || 20; } catch (e) {}
    if ([20, 50, 100].indexOf(size) < 0) size = 20;
    sizeSels.forEach(function (sel) { sel.value = size; });

    // Панель «Отбор»: та самая, в которой стоят полоса дат и полоса тем. Их
    // цифры считаются по ленте, а лента до подтягивания архива — сотня строк
    // из разметки, поэтому пока корпус в пути, счётчики приглушены: сотня не
    // должна выдавать себя за весь архив.
    var toolsBox = word && word.closest ? word.closest('.tools') : null;

    var ensure = function () {
      if (tailAsked || total <= stories.length) return;
      tailAsked = true;
      eachPager(function (b) { b.classList.add('is-loading'); });
      if (toolsBox) toolsBox.classList.add('is-stale');
      loadCorpus().then(function (j) {
        var scope = scopeOf(j);
        tail = [];
        for (var i = 0; i < j.s.length; i++) {
          var s = j.s[i];
          if (scope && scope.indexOf(s[1]) < 0) continue;
          if (mine[s[4]]) continue;
          tail.push({ s: s, hay: j.hay[i], day: j.d[s[2]], at: i });
        }
        eachPager(function (b) { b.classList.remove('is-loading'); });
        if (toolsBox) toolsBox.classList.remove('is-stale');
        // теперь гистограмма считается по всей глубине, а не по сотне
        var per = {};
        stories.forEach(function (n) { per[n.dataset.day] = (per[n.dataset.day] || 0) + 1; });
        tail.forEach(function (t) { per[t.day] = (per[t.day] || 0) + 1; });
        drawHist(per);
        countTops(tail);
        draw();
      });
    };

    var entRu = function (key) {
      return (CORPUS && CORPUS.n && CORPUS.n[key]) ||
        key.replace(/(^|[\s-])\S/g, function (m) { return m.toUpperCase(); });
    };

    var card = function (t, no) {
      var s = t.s;
      var art = el('article', 'story story--brief');
      art.style.setProperty('--w', s[3]);
      art.dataset.tier = tierOf(s[3]);
      art.dataset.day = t.day;
      var sig = el('div', 'story__sig');
      sig.appendChild(el('span', 'story__no', no));
      sig.appendChild(keepBtn());
      art.appendChild(sig);

      art.dataset.url = s[4];

      var body = el('div', 'story__body');
      body.appendChild(el('h3', 'story__title', s[0]));

      var meta = el('p', 'story__meta');
      var imp = el('span', 'story__imp');
      imp.title = 'Важность ' + s[3] + ' из 100';
      imp.appendChild(el('i', 'story__impbar'));
      imp.appendChild(document.createTextNode('важность '));
      imp.appendChild(el('b', null, s[3]));
      meta.appendChild(imp);
      if (withCountry) {
        var c = el('a', 'story__country', CORPUS.c[s[1]][1]);
        c.href = '/c/' + CORPUS.c[s[1]][0] + '.html';
        meta.appendChild(c);
      }
      if (s[8] > 1) meta.appendChild(el('span', 'story__outlets', s[8] + ' ' + plural(s[8], 'издание', 'издания', 'изданий')));
      var tm = el('time', 'story__time', dm(t.day));
      tm.dateTime = t.day;
      meta.appendChild(tm);
      topNames(s[9]).forEach(function (name) {
        meta.appendChild(el('span', 'story__top', name));
      });
      body.appendChild(meta);

      // Обзор — той же вёрсткой, что и в шаблоне (_story.html): предложения
      // взяты дословно у разных изданий, и подпись при каждом обязательна.
      // Ссылки у строки нет — в корпусе лежат только имена изданий, а выход
      // наружу у карточки один, ниже.
      if (s[7] && s[7].length) {
        var over = el('div', 'over');
        s[7].forEach(function (o) {
          var line = el('p', 'over__line', o[0] + ' ');
          if (o[1]) line.appendChild(el('span', 'over__src', o[1]));
          over.appendChild(line);
        });
        body.appendChild(over);
      }

      var names = (s[6] || '').split(';').filter(Boolean).slice(0, 8);
      if (names.length) {
        var ul = el('ul', 'ents');
        names.forEach(function (e) {
          var li = el('li');
          var b = el('button', null, entRu(e));
          b.type = 'button';
          b.dataset.seed = e;
          // как и у строк из разметки: имя со страницы страны ищется в её
          // пределах, а не уводит в мир
          if (document.body.dataset.country || document.body.dataset.region) b.dataset.seedScope = '';
          li.appendChild(b);
          ul.appendChild(li);
        });
        body.appendChild(ul);
      }
      // Выход наружу. Заголовок ссылкой больше не бывает — ни здесь, ни в
      // шаблоне, — а полного списка изданий в корпусе нет: у шести тысяч
      // сюжетов это ещё полтора мегабайта адресов. Поэтому одна ссылка, на то
      // издание, что возглавило список (см. source_rank в site.py); сколько
      // изданий всего, сказано строкой выше.
      var solo = el('p', 'story__solo');
      var a = el('a', null, s[5]);
      a.href = s[4];
      a.rel = 'noopener nofollow';
      solo.appendChild(a);
      body.appendChild(solo);

      // Похожие — той же разметкой, что и у строк из шаблона: раскрытие и
      // заполнение делает общий обработчик, номер соседей уже под рукой.
      var near = CORPUS.k && CORPUS.k[t.at];
      if (near && near.length) body.appendChild(akinBox(near.length));
      art.appendChild(body);
      keepMark(art);
      return art;
    };

    // Номера страниц: края всегда, окно вокруг текущей, между ними — многоточие.
    // Триста девятнадцать кнопок подряд — это не листалка, а вторая лента.
    //
    // Многоточие — кнопка, а не подпись. Раньше между «1» и «17» лежал
    // непроходимый провал: до девятой страницы можно было добраться только
    // девятью нажатиями «дальше». Теперь нажатие на пропуск показывает
    // страницу ровно посередине него, следующее — середину оставшейся
    // половины: двоичный поиск руками, три-четыре нажатия до любой страницы
    // из трёхсот, и строка при этом остаётся строкой.
    var shownPages = 1, opened = {};

    var gap = function (a, b) {
      var mid = a + ((b - a) >> 1);
      var n = el('button', 'pager__gap', '…');
      n.type = 'button';
      n.title = 'Показать страницу ' + (mid + 1);
      n.setAttribute('aria-label', 'Показать страницу ' + (mid + 1));
      n.addEventListener('click', function () { opened[mid] = 1; nums(shownPages); });
      return n;
    };

    var nums = function (pages) {
      shownPages = pages;
      var want = {}, i, o;
      want[0] = want[pages - 1] = 1;
      for (i = page - 1; i <= page + 1; i++) if (i >= 0 && i < pages) want[i] = 1;
      for (o in opened) if (+o > 0 && +o < pages) want[+o] = 1;
      var keys = Object.keys(want).map(Number).sort(function (a, b) { return a - b; });
      eachPager(function (box) {
        var slot = box.querySelector('[data-pg-nums]');
        if (!slot) return;
        slot.textContent = '';
        keys.forEach(function (p, k) {
          if (k && p - keys[k - 1] > 1) slot.appendChild(gap(keys[k - 1], p));
          var b = el('button', 'pager__num', p + 1);
          b.type = 'button';
          if (p === page) {
            b.className += ' is-here';
            b.setAttribute('aria-current', 'page');
          }
          b.addEventListener('click', function () { go(p); });
          slot.appendChild(b);
        });
      });
    };

    var draw = function () {
      var v = word ? word.value.trim().toLowerCase() : '';
      var narrowed = !!(v || dayPass || topPass);

      var live = [];
      stories.forEach(function (n) {
        if ((!v || (n.dataset.find || '').indexOf(v) !== -1) &&
            (!dayPass || dayPass.indexOf(n.dataset.day) !== -1) &&
            (!topPass || (+n.dataset.tp & topPass))) live.push(n);
        else n.hidden = true;
      });

      var rest = [];
      if (tail) {
        for (var i = 0; i < tail.length; i++) {
          var t = tail[i];
          if (v && t.hay.indexOf(v) === -1) continue;
          if (dayPass && dayPass.indexOf(t.day) < 0) continue;
          if (topPass && !(t.s[9] & topPass)) continue;
          rest.push(t);
        }
      }

      // Пока архив не приехал, глубину ленты берём из сборщика: сколько
      // сюжетов в области, страница знает и без корпуса. Под фильтром такой
      // оценки нет — там считаем по тому, что уже есть.
      var found = live.length + (tail ? rest.length
        : narrowed ? 0 : Math.max(0, total - stories.length));
      var pages = Math.max(1, Math.ceil(found / size));
      if (page >= pages) page = pages - 1;
      if (page < 0) page = 0;
      var from = page * size, to = from + size;

      live.forEach(function (n, i) {
        var on = i >= from && i < to;
        n.hidden = !on;
        if (!on) return;
        var no = n.querySelector('.story__no');
        if (no) no.textContent = i + 1;
        n.style.setProperty('--i', Math.min(i - from, 20));
      });

      if (tailBox) {
        tailBox.textContent = '';
        var lo = Math.max(0, from - live.length), hi = Math.min(to - live.length, rest.length);
        for (var k = lo; k < hi; k++) {
          var node = card(rest[k], live.length + k + 1);
          node.style.setProperty('--i', Math.min(live.length + k - from, 20));
          tailBox.appendChild(node);
        }
        tailBox.hidden = !tailBox.firstChild;
      }

      var shown = Math.min(to, found) - from;
      if (counter) counter.textContent = narrowed ? found + ' из ' + total : '';
      if (none) none.hidden = found > 0;
      if (pagers.length) {
        nums(pages);
        var stat = shown > 0
          ? (from + 1) + '–' + (from + shown) + ' из ' + found
          : 'страница пуста';
        eachPager(function (box) {
          box.hidden = pages < 2;
          Array.prototype.forEach.call(box.querySelectorAll('[data-pg]'), function (b) {
            var p = page + (+b.dataset.pg);
            b.disabled = p < 0 || p >= pages;
          });
          var st = box.querySelector('[data-pg-stat]');
          if (st) st.textContent = stat;
        });
      }
      drawn = true;
    };

    // Переворот страницы — движением, а не подменой кадра: строки приезжают
    // с той стороны, в которую листаешь. Восстанавливать класс через reflow
    // приходится потому, что анимация без смены имени сама по себе не
    // перезапускается.
    var animate = function () {
      if (calm.matches) return;
      [feedBox, tailBox].forEach(function (b) {
        if (!b) return;
        b.dataset.turn = turn > 0 ? 'fwd' : 'back';
        b.classList.remove('is-turn');
        void b.offsetWidth;
        b.classList.add('is-turn');
      });
    };

    var go = function (p) {
      ensure();
      turn = p > page ? 1 : -1;
      page = p;
      // Раскрытая середина — лестница, по которой сюда дошли, а не выбор
      // читателя: он его уже сделал. Дальше она только загромождает строку,
      // поэтому пропуски схлопываются обратно, как только страница сменилась.
      opened = {};
      draw();
      animate();
      var y = feedBox.getBoundingClientRect().top + scrollY - 72;
      scrollTo({ top: Math.max(0, y), behavior: calm.matches ? 'auto' : 'smooth' });
    };

    // Фильтр всегда возвращает на первую страницу: остаться на седьмой
    // странице выборки из трёх строк нельзя, а молча переехать — значит
    // соврать про то, что нажатие сделало.
    apply = function () {
      ensure();
      turn = 1;
      page = 0;
      draw();
      if (drawn) animate();
    };

    if (word) word.addEventListener('input', apply);

    // Архив тянется по первому же нажатию в ленте — но раскрытая панель это и
    // есть нажатие: цифры дат и тем открывают затем, чтобы на них смотреть, а
    // не чтобы сначала прочитать сотню, ткнуть наугад и увидеть другие числа.
    // Открытие — самый ранний надёжный признак интереса; грузить пять
    // мегабайт корпуса всем подряд ради панели, которую откроет не каждый,
    // незачем.
    if (toolsBox) toolsBox.addEventListener('toggle', function () {
      if (toolsBox.open) ensure();
    });
    eachPager(function (box) {
      Array.prototype.forEach.call(box.querySelectorAll('[data-pg]'), function (b) {
        b.addEventListener('click', function () { go(page + (+b.dataset.pg)); });
      });
    });
    sizeSels.forEach(function (sel) {
      sel.addEventListener('change', function () {
        var was = page * size;
        size = +sel.value;
        // вторая листалка — та же настройка, и показывать она должна то же
        sizeSels.forEach(function (o) { o.value = size; });
        try { localStorage.setItem('pgsize', size); } catch (e) {}
        go(Math.floor(was / size));
      });
    });

    draw();

    // Стрелки листают ленту, когда набирать некуда: ← и → у страницы, к
    // которой и так пришли читать подряд.
    document.addEventListener('keydown', function (e) {
      if (e.metaKey || e.ctrlKey || e.altKey || !pagers.length || pagers[0].hidden) return;
      var tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      if (root.classList.contains('is-find') || root.classList.contains('is-atlas')) return;
      if (e.key === 'ArrowRight') { e.preventDefault(); go(page + 1); }
      else if (e.key === 'ArrowLeft' && page > 0) { e.preventDefault(); go(page - 1); }
    });
  }

  // ── Подсказки фильтра: словарь этой страницы, а не всего корпуса ───────
  // Поиск по всему архиву (.find) подсказывает объектами всего корпуса — здесь
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
        // Издание сюжета: в разметке ленты оно больше не подписано в выходных
        // данных, а стоит первым в общем списке источников (или один, если
        // источник единственный). См. макрос sources в _story.html.
        add((n.querySelector('.story__sources a, .story__solo a') || {}).textContent);
        Array.prototype.forEach.call(n.querySelectorAll('.ents button'),
          function (b) { add(b.textContent); });
      });
      return Object.keys(count)
        .sort(function (a, b) { return count[b] - count[a] || a.localeCompare(b); })
        .map(function (k) { return { label: k, n: count[k] }; });
    };

    // Не draw: весь файл — одна функция, var в ней один на все блоки, и это
    // имя уже занято рисовальщиком ленты. Совпади они — листалка молча
    // перестала бы листать.
    var drawSugg = function () {
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
          drawSugg();
          word.focus();
        });
        sugg.appendChild(b);
      });
      sugg.hidden = !rows.length;
    };

    word.addEventListener('focus', drawSugg);
    word.addEventListener('input', drawSugg);
    word.addEventListener('blur', function () { sugg.hidden = true; });
    word.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !sugg.hidden) { e.stopPropagation(); sugg.hidden = true; }
    });
  }

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
    all('.story, .chips__item, .who__list li, .atlas__region')
      .forEach(function (n) { eye.observe(n); });
  }

  // ── Тема ──────────────────────────────────────────────────────────────
  // Два состояния, а не три. Было «как в системе → светлая → тёмная», и у
  // читателя со светлой системой первое нажатие ничего не меняло: кнопка
  // казалась тугой, хотя честно отрабатывала. Теперь нажатие всегда даёт
  // противоположное тому, что человек видит сейчас; «как в системе» осталось
  // умолчанием до первого нажатия.
  var button = q$('.theme');
  if (button) {
    var night = matchMedia('(prefers-color-scheme: dark)');
    var shown = function () {
      var t = root.dataset.theme;
      return t === 'light' || t === 'dark' ? t : (night.matches ? 'dark' : 'light');
    };
    var sync = function () {
      var dark = shown() === 'dark';
      button.title = dark ? 'Тема: тёмная — включить светлую'
                          : 'Тема: светлая — включить тёмную';
      button.setAttribute('aria-label', button.title);
      button.setAttribute('aria-pressed', dark ? 'true' : 'false');
    };
    var flip = function () {
      var next = shown() === 'dark' ? 'light' : 'dark';
      root.dataset.theme = next;
      try { localStorage.setItem('theme', next); } catch (e) {}
      sync();
    };
    button.addEventListener('click', function () {
      // Пока смена идёт, второе нажатие не считается. Замер: снимок кадра
      // берётся 170 мс на ПК, а на телефоне заметно дольше, и всё это время
      // на экране стоит замёрзший старый кадр — перемены не видно. Человек
      // нажимает снова, и тема возвращается туда, откуда её только что
      // переключили: кнопка выглядит сломанной, хотя отработала дважды.
      if (root.classList.contains('is-theming')) return;
      // смена гаммы — самый резкий кадр на сайте; там, где браузер умеет,
      // она переливается, а не моргает
      //
      // Кроме телефона. Замер на Pixel: снимок кадра до начала круга берётся
      // больше секунды, и всё это время на экране стоит старая тема — кнопка
      // выглядит неисправной, по ней жмут второй раз, и защита выше глотает
      // нажатие. Полторы секунды ожидания ради полусекунды перелива — цена, за
      // которую тут ничего не покупается: на телефоне тема меняется кадром.
      if (document.startViewTransition &&
          !matchMedia('(prefers-reduced-motion: reduce)').matches &&
          !matchMedia('(hover: none)').matches) {
        // Новая гамма расходится кругом от самой кнопки, а не подменяет кадр
        // целиком: перемена получает источник, и её видно откуда. Радиус —
        // до самого дальнего угла экрана от центра кнопки, иначе круг
        // остановится, не дойдя до края, и последний кадр моргнёт.
        var b = button.getBoundingClientRect();
        var x = b.left + b.width / 2;
        var y = b.top + b.height / 2;
        root.style.setProperty('--tx', x + 'px');
        root.style.setProperty('--ty', y + 'px');
        root.style.setProperty('--tr', Math.hypot(
          Math.max(x, innerWidth - x), Math.max(y, innerHeight - y)) + 'px');
        var off = function () { root.classList.remove('is-theming'); };
        root.classList.add('is-theming');
        var vt = document.startViewTransition(flip);
        // then(off, off), а не finally: нажатие в первую секунду после
        // перехода между страницами прерывает переход, finished отклоняется, и
        // на <html> оставался класс — он снимает имена перехода с шапки и
        // заголовка, то есть портил бы СЛЕДУЮЩИЙ морф страны. Страховка
        // временем — на случай, когда переход не разрешается вовсе; её надо
        // снимать, иначе она срабатывала посреди живого перехода. Замер:
        // finished наступает через ~2.2 с (снимок кадра плюс 0.62 с круга), а
        // страховка стояла на 1.2 с — то есть класс снимался, пока круг ещё
        // шёл, и правила is-theming исчезали из-под работающей анимации.
        var guard = setTimeout(off, 4000);
        var done = function () { clearTimeout(guard); off(); };
        vt.finished.then(done, done);
      } else {
        flip();
      }
    });
    night.addEventListener('change', sync);
    sync();
  }
})();

// ── Пеленг: морф страны и развёртка ─────────────────────────────────────
// Две вещи, которые CSS не может знать сам: по какой именно стране кликнули и
// под каким углом от центра карты лежит каждая страна. Всё остальное в
// style.css.
//
// Направление перехода живёт не здесь, а в head (base.html): pagereveal
// случается до первой отрисовки нового документа, то есть до запуска
// отложенного скрипта, и отсюда его было не поймать.
(function () {
  'use strict';

  var root = document.documentElement;
  var calm = matchMedia('(prefers-reduced-motion: reduce)');

  // ── Морф страны ───────────────────────────────────────────────────────
  // Имя страны — в ленте, в атласе, в списке региона или на карте — не
  // исчезает вместе со страницей, а долетает до заголовка своей страницы.
  // Имя перехода одно ('cty', см. ::view-transition-group в style.css), и
  // висеть оно должно ровно на одном видимом узле, поэтому ставится по
  // клику, а не разметкой: иначе на странице региона их было бы сорок.
  //
  // Снимается на pagehide — иначе имя останется на узле в кэше «назад» и
  // при возврате столкнётся с заголовком, который тоже 'cty'.
  // Морфить нечего там, где перехода между документами нет: на телефоне он
  // выключен (@media (hover: none) в style.css), и имя 'cty' повисало бы на
  // узле без всякого действия.
  var touch = matchMedia('(hover: none)');
  var tagged = null;
  document.addEventListener('click', function (e) {
    if (calm.matches || touch.matches || !document.startViewTransition) return;
    var a = e.target.closest && e.target.closest('a[href*="/c/"]');
    if (!a || a.target || e.metaKey || e.ctrlKey || e.shiftKey) return;
    if (tagged) tagged.style.viewTransitionName = '';
    tagged = a.querySelector('.atlas__cname') || a;
    tagged.style.viewTransitionName = 'cty';
    // На странице страны имя 'cty' уже висит на её заголовке (style.css), а два
    // видимых узла с одним именем браузер не сшивает — он отменяет переход
    // целиком, вместе с перелистыванием. Класс снимает имя с заголовка на время
    // ухода; ставится по клику, то есть до снимка.
    root.classList.add('is-morph');
  }, true);
  addEventListener('pagehide', function () {
    root.classList.remove('is-morph');
    if (tagged) { tagged.style.viewTransitionName = ''; tagged = null; }
  });
})();
