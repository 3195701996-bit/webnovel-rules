'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('templates/manga_reader.html', 'utf8');
const extract = name => html.match(new RegExp('function ' + name + '\\([^)]*\\) \\{[\\s\\S]*?\\n\\}'))?.[0];
const helper = extract('readerControlOwnsKey');
const handler = extract('handleReaderNavigationKey');
assert.ok(helper && handler);
const moves = [];
const ctx = vm.createContext({Boolean, settings: {mode:'scroll'}, chIdx:1, chapters:[{},{},{}],
  window: {innerHeight:844, scrollBy: value => moves.push(['scroll',value.top]),
    scrollTo: (...args) => moves.push(['top',...args])},
  document: {querySelector:()=>({classList:{contains:()=>true}}), body:{scrollHeight:2000}},
  jumpChapter: i => moves.push(['chapter',i]), flipPage: delta => moves.push(['flip',delta]),
});
vm.runInContext(`${helper}; ${handler};`,ctx);
function key(name, overrides={}) {
  let prevented=0;
  const e={key:name,target:{closest:()=>null},preventDefault:()=>prevented++, ...overrides};
  ctx.handleReaderNavigationKey(e);
  return prevented;
}
for (const name of ['ArrowRight','ArrowLeft','PageDown','PageUp','Home','End']) {
  assert.equal(key(name,{target:{closest:()=>({})}}),0,'native controls retain their own navigation');
  assert.equal(key(name,{target:{isContentEditable:true}}),0,'editable content retains navigation');
}
assert.deepEqual(moves,[],'range Home/End must not switch chapters or scroll the canvas');
for (const flag of ['ctrlKey','metaKey','altKey','defaultPrevented']) {
  assert.equal(key('PageDown',{[flag]:true}),0);
}
assert.deepEqual(moves,[]);
assert.equal(key('PageDown'),1);
assert.deepEqual(moves.pop(),['scroll',844*.8]);
assert.equal(key('Home'),1);
assert.deepEqual(moves.pop(),['chapter',0]);
assert.equal(key('End'),1);
assert.deepEqual(moves.pop(),['chapter',2]);
ctx.settings.mode='double';
assert.equal(key('ArrowRight'),1);
assert.deepEqual(moves.pop(),['flip',2]);
console.log('reader keyboard scope: range/editor/modifier keys stay local; canvas keys navigate without native double-scroll');
