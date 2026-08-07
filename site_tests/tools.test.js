// Приборы на настоящей собранной странице: закладки, похожие сюжеты, клавиши.
// Проверяется то, что ломается молча, — хранилище переживает перезагрузку,
// соседи берутся из корпуса, а строка опознаётся по data-url: заголовок
// больше не ссылка, и адрес живёт в самом <article>.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { JSDOM } = require('jsdom');

const OUT = process.argv[2] || path.join(__dirname, 'site');
const APP = fs.readFileSync('/opt/gdelt_rss/site_static/app.js', 'utf8');
const CORPUS = fs.readFileSync(path.join(OUT, 'search.json'), 'utf8');

// Одно хранилище на весь прогон: закладки обязаны переживать переход между
// страницами, а в jsdom это как раз разные окна.
function store() {
  const box = {};
  return {
    getItem: (k) => (k in box ? box[k] : null),
    setItem: (k, v) => { box[k] = String(v); },
    removeItem: (k) => { delete box[k]; },
    clear: () => { for (const k in box) delete box[k]; },
    _box: box,
  };
}

function boot(page, mem) {
  const html = fs.readFileSync(path.join(OUT, page), 'utf8');
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    url: 'https://rss.bhutyan.online/' + (page === 'index.html' ? '' : page),
    beforeParse(w) {
      w.performance.getEntriesByType = () => [];
      w.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = () => {};   // в jsdom его просто нет
      w.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} };
    },
  });
  const w = dom.window;
  w.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
  w.scrollTo = () => {};
  w.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} };
  if (mem) Object.defineProperty(w, 'localStorage', { value: mem, configurable: true });
  w.fetch = () => Promise.resolve({ json: () => Promise.resolve(JSON.parse(CORPUS)) });
  w.eval(APP);
  return { w, d: w.document };
}

const wait = () => new Promise((r) => setTimeout(r, 0));
const key = (w, k, target) => {
  const e = new w.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true });
  (target || w.document.body).dispatchEvent(e);
  return e;
};

(async function () {
  // ── Закладки ─────────────────────────────────────────────────────────────
  {
    const mem = store();
    const { w, d } = boot('index.html', mem);
    const art = d.querySelector('.story');
    const url = art.dataset.url;

    assert.equal(d.querySelector('.kept-btn').hidden, true, 'пустой счётчик виден');
    art.querySelector('[data-keep]').click();

    const saved = JSON.parse(mem.getItem('kept'));
    assert.equal(saved.length, 1, 'закладка не записалась');
    assert.equal(saved[0].u, url, 'записан не тот адрес');
    assert.ok(saved[0].t && saved[0].t.length > 10, 'заголовок не сохранён: ' + saved[0].t);
    assert.equal(art.classList.contains('is-kept'), true, 'карточка не помечена');
    assert.equal(art.querySelector('[data-keep]').getAttribute('aria-pressed'), 'true');

    const btn = d.querySelector('.kept-btn');
    assert.equal(btn.hidden, false, 'счётчик остался скрытым');
    assert.equal(btn.querySelector('[data-kept-n]').textContent, '1');

    // Снятие возвращает всё как было.
    art.querySelector('[data-keep]').click();
    assert.deepEqual(JSON.parse(mem.getItem('kept')), [], 'закладка не снялась');
    assert.equal(art.classList.contains('is-kept'), false, 'пометка осталась');
    assert.equal(d.querySelector('.kept-btn').hidden, true, 'счётчик не спрятался');

    art.querySelector('[data-keep]').click();
    w.close();

    // Другая страница, то же хранилище: список должен доехать и нарисоваться.
    const two = boot('r/east_asia.html', mem);
    const list = two.d.querySelectorAll('.kept__item');
    assert.equal(list.length, 1, 'закладка не пережила переход: ' + list.length);
    assert.equal(list[0].querySelector('a').href, url, 'в списке не тот адрес');
    assert.equal(two.d.querySelector('.kept-btn').hidden, false);

    // Удаление из самого списка и «очистить».
    list[0].querySelector('[data-kept-off]').click();
    assert.deepEqual(JSON.parse(mem.getItem('kept')), [], 'крестик не удалил');
    assert.equal(two.d.querySelectorAll('.kept__item').length, 0);
    assert.equal(two.d.querySelector('[data-kept-empty]').hidden, false, 'нет заглушки');
    two.w.close();
    console.log('ok  закладки: запись, снятие, перенос между страницами, удаление');
  }

  // ── Похожие сюжеты ───────────────────────────────────────────────────────
  {
    const { w, d } = boot('index.html', store());
    const j = JSON.parse(CORPUS);

    const det = d.querySelector('.akin');
    assert.ok(det, 'ни у одного сюжета в разметке нет блока похожих');
    // Подписано как источники: «N похожих сюжетов», число первым словом.
    const want = +det.querySelector('.akin__sum').textContent.trim().split(' ')[0];
    assert.ok(want > 0, 'счётчик соседей пуст: ' + det.querySelector('.akin__sum').textContent);
    assert.equal(det.querySelectorAll('.akin__link').length, 0, 'список набит заранее');

    // toggle не всплывает — обработчик ловит его на перехвате; здесь это
    // значит, что событие надо слать самому.
    det.open = true;
    det.dispatchEvent(new w.Event('toggle'));
    await wait(); await wait();

    const rows = det.querySelectorAll('.akin__link');
    assert.equal(rows.length, want, 'нарисовано ' + rows.length + ' из ' + want);

    // Ровно те соседи, что посчитал build, и в том же порядке.
    const url = det.closest('.story').dataset.url;
    const me = j.s.findIndex((s) => s[4] === url);
    assert.ok(me >= 0, 'сюжет не нашёлся в корпусе');
    assert.deepEqual([...rows].map((a) => a.href), j.k[me].map((i) => j.s[i][4]),
      'соседи не те или не в том порядке');
    assert.ok([...rows].every((a) => a.textContent.trim().length > 10), 'пустой заголовок');
    assert.equal(det.querySelectorAll('.akin__wait').length, 0, '«Ищем…» не убралось');

    // Второе открытие не удваивает список.
    det.dispatchEvent(new w.Event('toggle'));
    await wait();
    assert.equal(det.querySelectorAll('.akin__link').length, want, 'список удвоился');
    w.close();
    console.log('ok  похожие: счётчик из разметки, список из корпуса, порядок, идемпотентность');
  }

  // ── Клавиши ──────────────────────────────────────────────────────────────
  // Их осталось две: Esc и стрелки. Буквы (j/k/s/o/a/t/e/?) сняты вместе со
  // справкой о них — каждая дублировала кнопку, которая и так на экране, —
  // поэтому проверяем ровно обратное прежнему: что буква НЕ делает ничего.
  {
    const mem = store();
    const { w, d } = boot('index.html', mem);
    const arts = [...d.querySelectorAll('.story')];

    assert.equal(d.getElementById('help'), null, 'справка о клавишах осталась в разметке');

    ['j', 'k', 's', 'o', 'a', 't', 'e', '?'].forEach((k) => {
      const ev = key(w, k, arts[0]);
      assert.equal(ev.defaultPrevented, false, 'буква ' + k + ' всё ещё перехвачена');
    });
    assert.equal(mem.getItem('kept'), null, 'буква что-то отложила');
    assert.equal(d.activeElement, d.body, 'буква увела фокус');

    // Esc закрывает открытое. Открываем список отложенного кнопкой в шапке —
    // единственным оставшимся способом.
    arts[0].querySelector('[data-keep]').click();
    d.querySelector('.kept-btn').click();
    assert.equal(d.getElementById('kept').hidden, false, 'кнопка не открыла отложенное');
    key(w, 'Escape');
    assert.equal(d.getElementById('kept').hidden, true, 'Esc не закрыл отложенное');

    // Стрелки листают ленту — и только вне поля ввода.
    const pg = d.querySelector('[data-pager]');
    if (pg && !pg.hidden) {
      assert.equal(key(w, 'ArrowRight').defaultPrevented, true, '→ не листает');
      const inp = d.querySelector('input[type=search], input[type=text]');
      assert.ok(inp, 'на странице нет поля ввода — проверять нечего');
      assert.equal(key(w, 'ArrowRight', inp).defaultPrevented, false,
        '→ перехвачена в поле ввода');
    }
    // Стрелка запускает подтягивание архива; окно нельзя закрывать, пока
    // обещание не отработало, — иначе оно доедет уже в мёртвый документ.
    await wait(); await wait();
    w.close();
    console.log('ok  клавиши: буквы сняты, Esc закрывает, стрелки листают');
  }

  console.log('\ntools ok');
})().catch((e) => { console.error(e); process.exit(1); });
