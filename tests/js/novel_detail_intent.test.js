'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../templates/novel_detail.html'), 'utf8');
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
