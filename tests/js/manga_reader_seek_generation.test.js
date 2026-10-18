'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('templates/manga_reader.html', 'utf8');
const extract = name => html.match(new RegExp('function ' + name + '\\([^)]*\\) \\{[\\s\\S]*?\\n\\}'))?.[0];
const scroll = extract('scrollToPage');
assert.ok(scroll);
function environment() {
  const window = {scrollY: 0, innerHeight: 844, _curImgs: [{}, {}, {}]};
  const tops = [50.1, 603.6666667, 1123.4];
  const wraps = tops.map(top => ({isConnected: true, querySelector: () => null,
    getBoundingClientRect: () => ({top: top - window.scrollY})}));
  const callbacks = [], timers = [], positions = [];
  const ctx = vm.createContext({window, Math, _curGen: 1, _curChId: 'chapter',
    _locateY: -1, _userMoved: false, _seekGeneration: 0,
    document: {querySelectorAll: () => wraps},
    scrollReaderTo(y) { positions.push(y); window.scrollY = Math.round(y); },
    _mountImg(...args) { callbacks.push(args[5]); },
    setTimeout(fn) { timers.push(fn); },
  });
  vm.runInContext(`${extract('readerPageDocumentTop')}; ${extract('readerScrollOffsetForPage')}; ${scroll}`, ctx);
  return {ctx, callbacks, timers, positions};
}
{
  const {ctx, callbacks, timers, positions} = environment();
  ctx.scrollToPage(3);
  const firstCallbacks = callbacks.splice(0);
  ctx.scrollToPage(1);
  const homeCallbacks = callbacks.splice(0);
  ctx.scrollToPage(2);
  const before = positions.length;
  [...firstCallbacks, ...homeCallbacks].forEach(fn => fn());
  assert.equal(positions.length, before, 'late loads from older same-chapter seeks must not steal position');
  timers.slice(0,2).forEach(fn => fn());
  assert.equal(positions.length, before, 'old 3.5s correction timers must be inert');
  callbacks.forEach(fn => fn());
  timers[2]();
  assert.equal(positions.at(-1), 603.6666667 - 844 * .35);
}
for (const cancel of ['user-scroll', 'chapter-change']) {
  const {ctx, callbacks, timers, positions} = environment();
  ctx.scrollToPage(2);
  const before = positions.length;
  if (cancel === 'user-scroll') ctx._userMoved = true;
  else ctx._curGen++;
  callbacks.forEach(fn => fn());
  timers.forEach(fn => fn());
  assert.equal(positions.length, before, `${cancel} must prevent all late position corrections`);
}
console.log('manga reader seek generation: rapid last/home/page2 and obsolete layout/timer callbacks passed');
