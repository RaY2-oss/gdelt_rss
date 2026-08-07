// Проверка листалки на настоящей собранной странице.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { JSDOM } = require('jsdom');

const OUT = process.argv[2] || path.join(__dirname, 'site');
const APP = fs.readFileSync('/opt/gdelt_rss/site_static/app.js', 'utf8');
const CORPUS = fs.readFileSync(path.join(OUT, 'search.json'), 'utf8');

function boot(page) {
  const html = fs.readFileSync(path.join(OUT, page), 'utf8');
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    url: 'https://rss.bhutyan.online/' + (page === 'index.html' ? '' : page),
    beforeParse(w) {
      // чего в jsdom нет, но что дёргают встроенные скрипты страницы
      w.performance.getEntriesByType = () => [];
      w.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
      w.scrollTo = () => {};
      w.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} };
    },
  });
  const w = dom.window;
  w.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
  w.scrollTo = () => {};
  w.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} };
  let fetched = 0;
  w.fetch = () => { fetched++; return Promise.resolve({ json: () => Promise.resolve(JSON.parse(CORPUS)) }); };
  w.eval(APP);
  return { w, d: w.document, fetches: () => fetched };
}

const vis = (d, sel) => [...d.querySelectorAll(sel)].filter((n) => !n.hidden);
const wait = () => new Promise((r) => setTimeout(r, 0));
const nos = (d) => vis(d, '.story').map((n) => +n.querySelector('.story__no').textContent);

function goto(d, p) {
  for (let guard = 0; guard < 500; guard++) {
    const hit = [...d.querySelectorAll('.pager__num')].find((b) => +b.textContent === p);
    if (hit) return hit.click();
    const here = +d.querySelector('.pager__num.is-here').textContent;
    d.querySelector('[data-pg="' + (here < p ? 1 : -1) + '"]').click();
  }
  throw new Error('не дошли до страницы ' + p);
}

(async () => {
  // ── Главная ───────────────────────────────────────────────────────────
  {
    const { w, d, fetches } = boot('index.html');
    const pagers = [...d.querySelectorAll('[data-pager]')];
    const pager = pagers[0];
    const stat = d.querySelector('[data-pg-stat]');
    const total = +d.querySelector('[data-feed]').dataset.total;
    const pages = Math.ceil(total / 20);

    assert.equal(fetches(), 0, 'корпус не должен грузиться сам по себе');
    assert.equal(pagers.length, 2, 'листалок должно быть две — над лентой и под ней');
    assert.ok(pagers.every((p) => !p.hidden), 'листалка показалась');
    assert.equal(vis(d, '.story').length, 20, 'на первой странице 20 строк');
    // Лида отдельной вёрсткой больше нет: первый сюжет — обычная первая строка
    // ленты, и надпись над ним ставит feed().
    assert.equal(d.querySelector('.story--lead'), null, 'лид снова отдельной вёрсткой');
    assert.equal(d.querySelector('.story__eyebrow').textContent.trim(), 'Верх двух недель');
    assert.equal(stat.textContent, '1–20 из ' + total, 'глубина известна без корпуса');
    assert.deepEqual(nos(d), [...Array(20)].map((_, i) => i + 1), 'номера идут подряд с единицы');

    // ── страница 2: строки ещё из разметки, но корпус уже поехал ────────
    d.querySelector('[data-pg="1"]').click();
    assert.equal(fetches(), 1, 'корпус запрошен на первом же перелистывании');
    assert.equal(vis(d, '.story').length, 20);
    assert.equal(nos(d)[0], 21);
    assert.equal(stat.textContent, '21–40 из ' + total);
    assert.equal(d.querySelector('[data-feed]').dataset.turn, 'fwd', 'страница перевернулась вперёд');

    await wait();
    assert.equal(fetches(), 1, 'корпус берётся один раз');

    // ── глубоко в архив: строки приходят из корпуса ─────────────────────
    const lastBtn = [...d.querySelectorAll('.pager__num')].pop();
    assert.equal(+lastBtn.textContent, pages, 'число страниц считается по всему архиву');
    lastBtn.click();
    await wait();
    const tailBox = d.querySelector('[data-feed-tail]');
    const tail = vis(d, '[data-feed-tail] .story');
    assert.ok(!tailBox.hidden, 'хвост архива показан');
    assert.equal(tail.length, total - (pages - 1) * 20, 'на последней странице остаток');
    assert.equal(vis(d, '[data-feed] .story').length, 0, 'из разметки на последней странице ничего');
    // Заголовок больше не ссылка — адрес живёт в data-url, а наружу ведёт
    // подпись издания под текстом. И текст у карточки хвоста быть обязан:
    // без него вторая страница выглядела как лента без новостей.
    assert.ok(tail[0].dataset.url, 'у строки хвоста нет адреса');
    assert.equal(tail[0].querySelector('.story__title a'), null, 'заголовок снова ссылка');
    assert.ok(tail[0].querySelector('.story__solo a').href, 'у строки хвоста нет выхода наружу');
    assert.ok(tail[0].querySelector('.story__snippet').textContent.length > 20,
      'у строки хвоста нет текста');
    assert.ok(tail[0].querySelector('.story__country'), 'на главной страна показана');
    assert.equal(+tail[0].querySelector('.story__no').textContent, (pages - 1) * 20 + 1);
    assert.equal(tailBox.dataset.turn, 'fwd');

    // ── ни один сюжет не показан дважды на стыке разметки и архива ──────
    const seen = new Set();
    for (const p of [1, 2, 5, 6, 7, 50, pages]) {
      goto(d, p);
      const here = vis(d, '.story');
      assert.equal(here.length, p === pages ? total - (pages - 1) * 20 : 20, 'страница ' + p);
      assert.deepEqual(nos(d), [...Array(here.length)].map((_, i) => (p - 1) * 20 + i + 1),
        'нумерация на странице ' + p);
      here.forEach((n) => {
        const href = n.dataset.url;
        assert.ok(!seen.has(href), 'сюжет показан дважды: ' + href);
        seen.add(href);
      });
    }

    // ── обе листалки говорят одно и то же ──────────────────────────────
    assert.deepEqual([...d.querySelectorAll('[data-pg-stat]')].map((n) => n.textContent),
      [stat.textContent, stat.textContent], 'верхняя и нижняя листалки разошлись');

    // ── многоточие раскрывает середину пропуска ────────────────────────
    goto(d, 1);
    // Номера считаем по одной листалке: их две, и обе несут одни и те же кнопки.
    const numsOf = () => [...pager.querySelectorAll('.pager__num')].map((b) => +b.textContent);
    const gapBtn = pager.querySelector('.pager__gap');
    assert.ok(gapBtn, 'пропуска в номерах нет — проверять нечего');
    assert.equal(gapBtn.tagName, 'BUTTON', 'многоточие всё ещё подпись, а не кнопка');
    const was = numsOf();
    gapBtn.click();
    const now = numsOf();
    assert.equal(now.length, was.length + 1, 'страница посередине не появилась');
    const mid = now.find((n) => was.indexOf(n) < 0);
    assert.ok(mid > 2 && mid < pages, 'раскрылась не середина, а край: ' + mid);
    assert.equal(+d.querySelector('.pager__num.is-here').textContent, 1,
      'нажатие на многоточие само перелистнуло страницу');

    // ...и схлопывает её обратно, как только читатель ушёл на страницу.
    [...pager.querySelectorAll('.pager__num')].find((b) => +b.textContent === mid).click();
    assert.equal(+d.querySelector('.pager__num.is-here').textContent, mid,
      'кнопка раскрытой страницы никуда не ведёт');
    // В строке остаются только края и окрестность текущей страницы — всё, по
    // чему сюда шли, свою работу сделало.
    const after = numsOf();
    assert.ok(after.every((n) => n === 1 || n === pages || Math.abs(n - mid) <= 1),
      'в строке остались страницы, к текущей отношения не имеющие: ' + after.join(','));

    // ── размер страницы ────────────────────────────────────────────────
    goto(d, 1);
    const sel = d.querySelector('[data-pg-size]');
    sel.value = '100';
    sel.dispatchEvent(new w.Event('change'));
    assert.equal(vis(d, '.story').length, 100, 'сотня на страницу');
    assert.equal(w.localStorage.getItem('pgsize'), '100', 'выбор запомнился');
    assert.equal(stat.textContent, '1–100 из ' + total);

    // читателя не уносит: со страницы 5 по 20 он попадает на страницу 1 по 100
    goto(d, 5);
    assert.equal(nos(d)[0], 401);
    sel.value = '20';
    sel.dispatchEvent(new w.Event('change'));
    assert.equal(nos(d)[0], 401, 'при смене размера читатель остаётся на месте');

    // ── фильтр по слову: считает по всей глубине, а не по сотне ─────────
    goto(d, 1);
    const word = d.querySelector('[data-filter]');
    word.value = 'индия';
    word.dispatchEvent(new w.Event('input'));
    const cnt = d.querySelector('[data-filter-count]').textContent;
    const m = cnt.match(/^(\d+) из (\d+)$/);
    assert.ok(m, 'счётчик фильтра: ' + cnt);
    assert.ok(+m[1] > 100, 'под фильтром видна вся глубина, а не сотня: ' + cnt);
    assert.equal(+m[2], total);
    assert.equal(stat.textContent.split(' из ')[1], m[1], 'листалка и счётчик сходятся');
    assert.equal(nos(d)[0], 1, 'фильтр вернул на первую страницу');
    vis(d, '.story').forEach((n) => {
      const t = n.textContent.toLowerCase();
      assert.ok(t.includes('инди'), 'строка не по фильтру: ' + t.slice(0, 60));
    });

    // ── пустая выборка ─────────────────────────────────────────────────
    word.value = 'зафырчалохрюкнуло';
    word.dispatchEvent(new w.Event('input'));
    assert.equal(vis(d, '.story').length, 0);
    assert.equal(d.querySelector('.feed__none').hidden, false, 'сказано, что ничего не найдено');
    assert.ok(pager.hidden, 'листалка на одной пустой странице не нужна');

    word.value = '';
    word.dispatchEvent(new w.Event('input'));
    assert.equal(vis(d, '.story').length, 20, 'снятие фильтра вернуло ленту');
    assert.equal(stat.textContent, '1–20 из ' + total);
    assert.ok(!pager.hidden);

    // ── стрелки клавиатуры ─────────────────────────────────────────────
    d.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    assert.equal(nos(d)[0], 21, '→ листает вперёд');
    d.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
    assert.equal(nos(d)[0], 1, '← листает назад');
    d.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
    assert.equal(nos(d)[0], 1, 'с первой страницы назад некуда');

    console.log('главная: ок —', total, 'сюжетов,', pages, 'страниц');
  }

  // ── Страна ────────────────────────────────────────────────────────────
  {
    const { w, d } = boot('c/india.html');
    const j = JSON.parse(CORPUS);
    const total = +d.querySelector('[data-feed]').dataset.total;
    const pages = Math.ceil(total / 20);
    const ci = j.c.findIndex((c) => c[0] === 'india');
    assert.equal(j.s.filter((s) => s[1] === ci).length, total, 'глубина страны сходится с корпусом');
    assert.equal(nos(d)[0], 1, 'у страны лида нет — счёт с первой строки');

    goto(d, pages);
    await wait();
    goto(d, pages);
    const tail = vis(d, '[data-feed-tail] .story');
    assert.ok(tail.length > 0, 'у страны тоже есть хвост');
    assert.equal(tail[0].querySelector('.story__country'), null,
      'на странице страны страна в строке не повторяется');
    assert.equal(+tail[0].querySelector('.story__no').textContent, (pages - 1) * 20 + 1);
    console.log('страна: ок —', total, 'сюжетов');
  }

  // ── Регион ────────────────────────────────────────────────────────────
  {
    const { d } = boot('r/south_asia.html');
    const j = JSON.parse(CORPUS);
    const total = +d.querySelector('[data-feed]').dataset.total;
    const g = j.g.find((x) => x[0] === 'south_asia');
    assert.equal(j.s.filter((s) => g[2].indexOf(s[1]) >= 0).length, total, 'глубина региона сходится');
    goto(d, Math.ceil(total / 20));
    await wait();
    goto(d, Math.ceil(total / 20));
    assert.ok(vis(d, '[data-feed-tail] .story').length > 0, 'у региона тоже есть хвост');
    assert.ok(vis(d, '[data-feed-tail] .story')[0].querySelector('.story__country'),
      'в регионе страна у строки нужна');
    console.log('регион: ок —', total, 'сюжетов');
  }

  console.log('листалка: все проверки прошли');
})().catch((e) => { console.error(e.message || e); process.exit(1); });
