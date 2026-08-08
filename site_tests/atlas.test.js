// Атлас охвата на настоящей собранной главной.
//
// Здесь стояла карта, и проверять у неё приходилось приближение: имена не
// влезали в страны. У сетки такой болезни нет — влезает всё, — поэтому
// проверяем то, ради чего сетка и поставлена: она показывает ВСЕ страны
// охвата, а не те, чьё имя поместилось, и заливка ячейки означает величину.
//
// Отдельно — что атлас на главной и атлас в выдвижной панели остались одной
// разметкой: их два в документе, и разойтись они могут только молча.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { JSDOM } = require('jsdom');

const OUT = process.argv[2] || path.join(__dirname, 'site');
const html = fs.readFileSync(path.join(OUT, 'index.html'), 'utf8');
const dom = new JSDOM(html, { url: 'https://rss.bhutyan.online/' });
const d = dom.window.document;

// Карты больше нет — и её геометрии в разметке тоже.
// Меряем не вес страницы (её держат сто сюжетов с обзорами), а именно
// геометрию: карта возвращается в разметку километрами путей SVG.
assert.equal(d.querySelector('svg.rmap__svg'), null, 'карта вернулась в разметку');
const geom = [...d.querySelectorAll('path[d]')]
  .reduce((n, p) => n + p.getAttribute('d').length, 0);
assert.ok(geom < 2048, 'в главной завелась готовая геометрия SVG: ' + geom + ' знаков');

const grids = [...d.querySelectorAll('.atlas__grid')];
assert.equal(grids.length, 2, 'атласов на главной не два: ' + grids.length);

const home = d.querySelector('.tools--atlas .atlas__grid');
assert.ok(home, 'на главной нет развёрнутого атласа');

const regions = [...home.querySelectorAll('.atlas__region')];
const cells = [...home.querySelectorAll('.atlas__cell')];
assert.ok(regions.length >= 8, 'регионов почти нет: ' + regions.length);
assert.ok(cells.length > 60, 'стран в атласе почти нет: ' + cells.length);

// Сетка и панель — одна разметка: разное число ячеек значит, что макрос
// развели по двум местам и одно из них отстало.
const panel = grids.find((g) => g !== home);
assert.equal(panel.querySelectorAll('.atlas__cell').length, cells.length,
  'атлас на главной и в панели разошлись');

// Каждая ячейка — ссылка на страну с именем, числом сюжетов и ступенью шкалы.
cells.forEach((li) => {
  const a = li.querySelector('a[href^="/c/"]');
  assert.ok(a, 'ячейка без ссылки на страну');
  assert.ok(a.querySelector('.atlas__cname').textContent.trim(), 'страна без имени');
  assert.ok(/^\d+$/.test(a.querySelector('.atlas__cnum').textContent.trim()),
    'у страны не число сюжетов: ' + a.textContent);
  assert.ok(/^[0-4]$/.test(li.dataset.tier), 'ступень вне шкалы: ' + li.dataset.tier);
});

// Заливка означает величину: одинаковая ступень у всех — это не шкала.
const tiers = new Set(cells.map((li) => li.dataset.tier));
assert.ok(tiers.size > 1, 'у всех стран одна ступень заливки');

// Свёрнут: 89 стран поверх экрана отодвигают новость на третий разворот.
const box = d.querySelector('details.tools--atlas');
assert.ok(box && !box.open, 'атлас на главной раскрыт по умолчанию');
assert.ok(/\d+\s+стран/.test(box.querySelector('.tools__hint').textContent),
  'в шапке плашки не сказано, сколько стран');

console.log('  атлас: ' + regions.length + ' регионов, ' + cells.length +
  ' стран, ступеней ' + tiers.size + ', главная ' +
  Math.round(html.length / 1024) + ' КБ');
console.log('атлас: ок');
