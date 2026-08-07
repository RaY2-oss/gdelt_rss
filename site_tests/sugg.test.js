// Подсказки поиска берут издание из общего списка источников: в выходных
// данных сюжета его больше нет (см. макрос sources в _story.html).
const fs = require('fs');
const { JSDOM } = require('jsdom');
const OUT = process.argv[2] || __dirname + '/site';
const html = fs.readFileSync(OUT + '/index.html', 'utf8');
const dom = new JSDOM(html);
const doc = dom.window.document;
const nodes = [...doc.querySelectorAll('.story')];
let found = 0;
for (const n of nodes) {
  const a = n.querySelector('.story__sources a, .story__solo a');
  if (a && /\w+\.\w+/.test(a.textContent)) found++;
}
console.log(`издание найдено у ${found} из ${nodes.length} сюжетов`);
if (found < nodes.length) { console.error('ПРОВАЛ: у части сюжетов издание не читается'); process.exit(1); }
console.log('подсказки: ок');
