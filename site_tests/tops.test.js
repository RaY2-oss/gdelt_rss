// Полоса подтем на настоящей собранной странице.
//
// Проверяем ровно то, что может разъехаться молча: маска в разметке и маска в
// корпусе — разные источники одного числа, и если они разойдутся, фильтр начнёт
// врать не падая. Плюс два правила, ради которых полоса и сделана иначе, чем
// полоса дат: выключены все = показаны все, и тема без сюжетов кнопки не имеет.
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
  w.fetch = () => Promise.resolve({ json: () => Promise.resolve(JSON.parse(CORPUS)) });
  w.eval(APP);
  return { w, d: w.document };
}

const vis = (d, sel) => [...d.querySelectorAll(sel)].filter((n) => !n.hidden);
// Видимых карточек всегда ровно страница, поэтому считать надо не их, а счётчик
// отбора: «N из M». Пусто — отбор не сужен, значит найдено всё.
const found = (d) => {
  const t = d.querySelector('[data-filter-count]').textContent;
  return t ? +t.split(' ')[0] : +d.querySelector('[data-feed]').dataset.total;
};
const wait = () => new Promise((r) => setTimeout(r, 0));
// Корпус приезжает через промис, а листалка после него ещё раз перерисовывает
// ленту — одного тика мало.
const settle = async () => { for (let i = 0; i < 6; i++) await wait(); };

(async () => {
  const j = JSON.parse(CORPUS);

  // Маска темы у сюжета должна быть хоть у кого-то: пустая колонка означала бы,
  // что тему никто не проставил, и все проверки ниже прошли бы впустую.
  const tagged = j.s.filter((s) => s[9]).length;
  assert.ok(tagged > 0, 'ни одного сюжета с темой в корпусе');

  // «Прочее» — последний бит маски (см. topics.mask). Его доля стережёт
  // классификатор: без sklearn или без обучающей выборки topics.refine молча
  // откатывается на один строгий заход словаря, сборка проходит, а «Прочее»
  // прыгает с одной восьмой ленты до четверти. Потолок взят между замерами:
  // 11.5 % со слоем и не меньше 23 % без него (08.08.2026).
  const nt = JSON.parse(fs.readFileSync('/opt/gdelt_rss/topics.json', 'utf8')).length;
  const other = 1 << (nt - 1);
  const dark = j.s.filter((s) => s[9] === other).length;
  assert.ok(dark / j.s.length < 0.18,
    'сюжетов без темы четверть — похоже, классификатор не отработал: ' + dark);
  // Мультиметка: тем у сюжета бывает несколько, и если это перестало быть
  // правдой, кто-то по дороге срезал список до первой.
  const many = j.s.filter((s) => s[9] && (s[9] & (s[9] - 1))).length;
  assert.ok(many / j.s.length > 0.1, 'у сюжетов ровно по одной теме: ' + many);
  console.log('  сюжетов с темой: ' + tagged + ' из ' + j.s.length +
    ', «Прочее» ' + (100 * dark / j.s.length).toFixed(1) + ' %' +
    ', с несколькими темами ' + (100 * many / j.s.length).toFixed(0) + ' %');

  {
    const { d } = boot('index.html');
    const strip = d.querySelector('[data-tops]');
    assert.ok(strip && !strip.hidden, 'полоса подтем не показалась');

    const btns = vis(d, '.tops__btn');
    assert.ok(btns.length >= 3, 'кнопок подтем слишком мало: ' + btns.length);

    // Пустых кнопок быть не должно: тема без сюжетов уходит целиком.
    btns.forEach((b) => {
      const n = +b.querySelector('.tops__n').textContent;
      assert.ok(n > 0, 'кнопка «' + b.textContent + '» без сюжетов не должна висеть');
    });

    // Ни одна не нажата — в отборе весь архив.
    const whole = found(d);
    assert.ok(whole > 0, 'лента пуста');
    assert.ok(d.querySelector('[data-tops-reset]').hidden, 'сброс висит без выбора');

    // Нажатие темы: осталось только то, у чего в маске стоит её бит.
    const bit = +btns[0].dataset.bit;
    btns[0].click();
    await settle();
    const one = found(d);
    const shown = vis(d, '.story');
    assert.ok(shown.length > 0, 'после выбора темы лента опустела');
    assert.ok(one > 0 && one < whole, 'фильтр по теме ничего не отрезал: ' + one + '/' + whole);
    shown.forEach((n) => {
      // Хвостовые карточки листалки маску в разметку не пишут — их отбирает
      // тот же бит ещё до отрисовки, проверяем только строки самой ленты.
      if (n.dataset.tp === undefined) return;
      assert.ok(+n.dataset.tp & bit,
        'в ленту темы попал чужой сюжет: ' + n.dataset.tp + ' & ' + bit);
    });
    assert.ok(!d.querySelector('[data-tops-reset]').hidden, 'сброс не показался');

    // Вторая тема расширяет выборку, а не сужает: темы складываются по «или».
    const two = +btns[1].dataset.bit;
    btns[1].click();
    await settle();
    assert.ok(found(d) > one, 'вторая тема не расширила выборку');
    vis(d, '.story').forEach((n) => {
      if (n.dataset.tp === undefined) return;
      assert.ok(+n.dataset.tp & (bit | two), 'чужой сюжет при двух темах');
    });

    // Сброс возвращает всю ленту.
    d.querySelector('[data-tops-reset]').click();
    await settle();
    assert.equal(found(d), whole, 'сброс не вернул ленту');
    assert.ok(d.querySelector('[data-tops-reset]').hidden, 'сброс остался после сброса');
  }

  // Страна: тем там меньше, и это как раз тот случай, ради которого пустые
  // кнопки скрываются. Полосы может не быть вовсе — если у страны ни одной
  // размеченной новости, и это не поломка.
  {
    const c = fs.readdirSync(path.join(OUT, 'c')).find((f) => f.endsWith('.html'));
    const { d } = boot(path.join('c', c));
    const strip = d.querySelector('[data-tops]');
    const btns = vis(d, '.tops__btn');
    if (strip.hidden) {
      assert.equal(btns.length, 0, 'полоса спрятана, а кнопки видны');
    } else {
      assert.ok(btns.length > 0, 'полоса открыта без единой кнопки');
      const whole = found(d);
      btns[0].click();
      await settle();
      assert.ok(found(d) <= whole, 'фильтр страны расширил ленту');
    }
    console.log('  ' + c + ': кнопок подтем ' + btns.length);
  }

  console.log('подтемы: ок');
})().catch((e) => { console.error(e); process.exit(1); });
