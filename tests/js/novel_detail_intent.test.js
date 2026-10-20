'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../templates/novel_detail.html'), 'utf8');
const detailCss = source.match(/<style>([\s\S]*?)<\/style>/)[1];
for (const rule of detailCss.matchAll(/\.chap-list\s*\{([^}]*)\}/g)) {
  assert.doesNotMatch(rule[1], /max-height|overflow-y/, 'detail TOC must scroll with the document, not inside a short viewport');
}
assert.doesNotMatch(detailCss.match(/\.chap-head\s*\{([^}]*)\}/)[1], /position:\s*(?:sticky|fixed)/);
const start = source.indexOf('let detailToc = null;');
const end = source.indexOf('// ═══════ 书库模式', start);
assert(start >= 0 && end > start);
for (const event of ['pointerdown', 'keydown', 'scroll', 'wheel', 'touchmove', 'input']) {
  const handlers = {};
  const search = {value: '100'};
  const context = vm.createContext({
    $: selector => selector === '#chap-search' ? search : {
      addEventListener: (type, callback) => { handlers[type] = callback; },
    },
    window: {addEventListener: (type, callback) => { handlers[type] = callback; }},
  });
  vm.runInContext(source.slice(start, end), context);
  assert.equal(vm.runInContext('detailTocInteracted', context), false);
  if (event === 'input') search.oninput();
  else handlers[event]();
  assert.equal(vm.runInContext('detailTocInteracted', context), true, event);
}
assert.match(source, /if \(idx < 1 \|\| idx > chs.length \|\| !detailToc \|\| detailTocInteracted\) return;/);
console.log('novel detail intent: late history is gated after directory/search/scroll interaction');
