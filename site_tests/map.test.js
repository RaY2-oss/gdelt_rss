// Приближение карты на настоящей собранной странице.
//
// Проверяем то, ради чего приближение и сделано: имена стран проявляются по
// мере приближения, а не мельчают вместе с картой. Отдельно — снятие атрибута
// hidden: у <text> нет свойства hidden (оно живёт на HTMLElement, а не на
// SVGElement), и запись t.hidden = false молча заводит на элементе обычное
// поле, оставляя разметку как была. Подписи тогда не появляются, а любая
// проверка через t.hidden отвечает ровно то, что ей записали, — поломка,
// которая сама себя и прячет.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { JSDOM } = require('jsdom');

const OUT = process.argv[2] || path.join(__dirname, 'site');
const APP = fs.readFileSync('/opt/gdelt_rss/site_static/app.js', 'utf8');

const dom = new JSDOM(fs.readFileSync(path.join(OUT, 'index.html'), 'utf8'), {
  runScripts: 'dangerously',
  url: 'https://rss.bhutyan.online/',
  beforeParse(w) {
    w.performance.getEntriesByType = () => [];
    w.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
    w.scrollTo = () => {};
    w.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} };
    w.fetch = () => Promise.resolve({ json: () => Promise.resolve({ s: [], c: {} }) });
  },
});
const w = dom.window;
const d = w.document;
w.eval(APP);

const svg = d.querySelector('svg.rmap__svg');
const labs = [...d.querySelectorAll('.rmap__lab')];
const shown = () => labs.filter((t) => !t.hasAttribute('hidden'));
const lab = () => parseFloat(svg.style.getPropertyValue('--lab'));
const view = () => svg.getAttribute('viewBox').split(' ').map(Number);
const press = (v) => d.querySelector('[data-zoom="' + v + '"]').click();

assert.ok(labs.length > 10, 'подписей на карте почти нет: ' + labs.length);

// Пульт показывается только скриптом: без него кнопки были бы мёртвыми.
const pad = d.querySelector('[data-map-zoom]');
assert.ok(!pad.hidden, 'пульт приближения не показался');
assert.ok(d.querySelector('.rmap__reset').hidden, 'сброс висит на целом кадре');

// В покое видно только то, что влезает в границы как есть.
const rest = shown().length;
assert.ok(rest > 0 && rest < labs.length,
  'в покое видно всё или ничего: ' + rest + ' из ' + labs.length);
shown().forEach((t) => assert.ok(+t.dataset.fit <= 1, 'подпись не влезла: ' + t.textContent));

const [, , w0] = view();
const em0 = lab();

press(1);
const [, , w1] = view();
assert.ok(w1 < w0, 'кадр не сузился: ' + w1 + ' против ' + w0);
// Кегль идёт навстречу приближению — иначе имя росло бы вместе со страной и
// внутрь не поместилось бы ни одно новое.
assert.ok(Math.abs(em0 / lab() - w0 / w1) < 0.01,
  'кегль не поспевает за кадром: ' + em0 + '→' + lab());
assert.ok(shown().length > rest, 'приближение не проявило ни одной подписи');

// Пять нажатий подряд упираются в потолок, а не улетают в бесконечность.
for (let i = 0; i < 8; i++) press(1);
assert.equal(view()[2], w0 / 6, 'предел приближения не держится: ' + view()[2]);
assert.ok(d.querySelector('[data-zoom="1"]').disabled, 'кнопка на пределе жива');
const far = shown().length;
assert.ok(far > rest, 'на пределе подписей не больше, чем в покое');

// Кадр не уезжает за геометрию: показывать нечего, и раньше там было пусто.
const [x, y, vw, vh] = view();
assert.ok(x >= 0 && y >= 0 && x + vw <= w0 + 0.5, 'кадр вылез за границы карты');
assert.ok(vh > 0);

press(0);
assert.equal(view()[2], w0, 'сброс не вернул весь кадр');
assert.equal(shown().length, rest, 'сброс не убрал проявленные подписи');
assert.ok(d.querySelector('.rmap__reset').hidden, 'сброс остался после сброса');

console.log('  подписей: ' + rest + ' в покое, ' + far + ' на пределе, всего ' + labs.length);
console.log('карта: ок');
