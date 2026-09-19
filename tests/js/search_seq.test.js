/* C07 搜索代际守卫离线回归（node 直接运行，无浏览器依赖，不新增依赖）
 *
 * 被测对象 = templates/index.html 内联脚本中的 `_searchSeq` 声明 + doNovelSearch
 * （用 node:vm 原样抽取该片段执行，与线上同一份代码）。
 * 可控 fetch + 可控流 reader 交错驱动两次搜索：
 *   1. 搜索 A 停在 await reader.read()（读流挂起）
 *   2. 启动搜索 B（abort A + 代际 +1，B 重置结果盒/状态栏）
 *   3. A 挂起的 read 此刻才 resolve 一批旧增量事件（abort 只能在下个 await 观察）
 * 断言：A 的迟到旧响应不得渲染卡片、不得改动结果盒、不得覆盖 B 的状态栏；
 *       同时 B 自身事件仍正常落地（守卫不过度拦截）。
 *
 * 行为覆盖范围（准确）：覆盖两处 await 交错——① 旧搜索停在 reader.read() 后
 * 被取代（场景 1-4）；② 旧搜索停在 fetch() 后、其 fetch 迟到 resolve 被取代
 * （场景 5）。旧搜索的 catch/finally 分支未构造独立交错（abort 在 fake 流下不
 * 会 reject read，catch 不可达）；其守卫与 read 守卫同用 mySeq，语义一致。
 *
 * 运行：node tests/js/search_seq.test.js
 */
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const INDEX = path.join(__dirname, '..', '..', 'templates', 'index.html');
const src = fs.readFileSync(INDEX, 'utf8');
const START = 'let _searchSeq = 0;';
const END = "$('#search-q').addEventListener('keydown'";
const a = src.indexOf(START), b = src.indexOf(END);
assert.ok(a >= 0 && b > a, '未在 index.html 中定位到搜索代际片段');
const BLOCK = src.slice(a, b);
// 守卫应真在抽取片段中（读流 await 后 + fetch await 后）
assert.ok((BLOCK.match(/if \(mySeq !== _searchSeq\) return;/g) || []).length >= 2,
  'doNovelSearch 缺少读流/取流后的代际守卫');

const enc = new TextEncoder();
const sse = (obj) => 'data: ' + JSON.stringify(obj) + '\n\n';
const tick = () => new Promise((r) => setTimeout(r, 0));

function makeReader() {
  const queue = [], waiters = [];
  return {
    read() {
      if (queue.length) return Promise.resolve(queue.shift());
      return new Promise((res) => waiters.push(res));
    },
    push(text) {
      const c = {done: false, value: enc.encode(text)};
      if (waiters.length) waiters.shift()(c); else queue.push(c);
    },
    end() {
      const c = {done: true, value: undefined};
      if (waiters.length) waiters.shift()(c); else queue.push(c);
    },
  };
}

const readers = [];
const renderGroupCalls = [];
const statusCalls = [];
const els = new Map();
function makeEl(id) {
  return {
    id, innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
    children: [], isConnected: true,
    appendChild(c) { this.children.push(c); return c; },
    contains() { return false; },
  };
}
const el = (sel) => { if (!els.has(sel)) els.set(sel, makeEl(sel)); return els.get(sel); };

const sandbox = {
  console, setTimeout, clearTimeout, JSON, Promise, Map, Set,
  TextEncoder, TextDecoder, AbortController, encodeURIComponent, decodeURIComponent,
  document: { hidden: false, activeElement: null, addEventListener() {} },
  window: {},
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  GroupMerge: {
    groupKey: (g) => g && g.name,
    mergeGroup: (oldg, g) => g,
    groupFingerprint: (g) => JSON.stringify(g),
  },
  _rowIndex: new Map(),
  $: el,
  esc: (s) => String(s),
  setSearchStatus: (st, spinner, text) => { statusCalls.push(text); },
  renderGroup: (g, box) => { renderGroupCalls.push(g); box.appendChild(makeEl('card')); },
  updateGroupRow() {},
  resetTocObserver() {},
  autoLoadToc() {},
  saveSearchState() {},
  saveSearchHistory() {},
  cacheNovelSearch() {},
  getCachedNovelSearch: () => null,
  mergeGroups: (a2, b2) => b2,
  renderSearchEmpty() {},
  renderSearchSourceFail() {},
  renderSkeletons: () => 'SKELETONS',
  toast() {},
  fetch: (url, opts) => {
    // reader 仅在代码真正调用 getReader() 时才登记：据此可判定被测代码是否
    // 走到“建流”一步（fetch 迟到返回后被取代者应在守卫处直接 return）。
    const deliver = () => {
      const r = makeReader();
      return {ok: true, body: {getReader: () => { readers.push(r); return r; }}};
    };
    // __hold：让 fetch 停在 await（场景 5 用，放行前不得建流）
    if (sandbox.__hold) return new Promise((res) => { sandbox.__release = () => res(deliver()); });
    return Promise.resolve(deliver());
  },
};
sandbox.__hold = false;
sandbox.__release = null;
vm.createContext(sandbox);
vm.runInContext(BLOCK + '\n;globalThis.__search = doNovelSearch;', sandbox);
const doNovelSearch = sandbox.__search;
assert.strictEqual(typeof doNovelSearch, 'function', '未能从 vm 中取出 doNovelSearch');

async function main() {
  const box = el('#search-results');

  // 1. 搜索 A：启动后停在 await reader.read()
  const pA = doNovelSearch('A', false);
  await tick();
  assert.strictEqual(readers.length, 1, 'A 已发起一次搜索请求');
  const readerA = readers[0];
  const statusBeforeB = statusCalls.length;

  // 2. 搜索 B：应 abort A 并把代际 +1，重置结果盒/状态栏
  const pB = doNovelSearch('B', false);
  await tick();
  assert.strictEqual(readers.length, 2, 'B 已发起搜索请求');
  const readerB = readers[1];
  const boxAfterB = box.innerHTML;
  const statusAfterB = statusCalls.length;
  assert.ok(statusAfterB > statusBeforeB, 'B 已写入自己的进行中状态');

  // 3. A 的挂起 read 此时才 resolve 一批旧增量事件
  readerA.push(sse({done: 1, total: 2, groups: [{name: 'A书'}], updates: []}));
  await tick();
  assert.strictEqual(renderGroupCalls.length, 0, '被取代的 A 不得渲染任何卡片');
  assert.strictEqual(box.innerHTML, boxAfterB, '被取代的 A 不得改动 B 的结果盒');
  assert.strictEqual(statusCalls.length, statusAfterB, '被取代的 A 不得覆盖 B 的状态栏');

  // 4. B 自身事件仍正常落地（守卫不过度拦截）
  readerB.push(sse({done: 1, total: 2, groups: [{name: 'B书'}], updates: []}));
  await tick();
  assert.strictEqual(renderGroupCalls.length, 1, 'B 的增量事件正常渲染');
  assert.ok(statusCalls[statusCalls.length - 1].includes('B'), 'B 的状态文本正常更新');

  // 收尾：结束两条流，让各自 finally 清掉 60s 超时计时器
  readerA.end();
  readerB.end();
  await tick(); await Promise.resolve();

  // 5. 旧代 fetch await 期间被取代：A2 的 fetch 迟到 resolve 后不得再建读流
  sandbox.__hold = true;
  const pA2 = doNovelSearch('A2', false);
  await tick();
  const releaseA = sandbox.__release;          // A2 的 fetch 放行器
  const readersBefore = readers.length;
  const pB2 = doNovelSearch('B2', false);      // 取代 A2（代际 +1）
  await tick();
  const releaseB = sandbox.__release;          // B2 的 fetch 放行器
  releaseA();                                  // A2 旧代 fetch 迟到返回
  await tick();
  assert.strictEqual(readers.length, readersBefore, '被取代的 A2 迟到 fetch 不得再建立读流');
  // 放行 B2 并正常结束其流（守卫不得过度拦截）
  sandbox.__hold = false;
  releaseB();
  await tick();
  readers[readers.length - 1].end();
  await tick(); await Promise.resolve();

  await Promise.allSettled([pA, pB, pA2, pB2]);
}

const WATCHDOG_MS = 5000;
const watchdog = setTimeout(() => {
  console.error('search_seq.test.js: 主流程未在 ' + WATCHDOG_MS +
    'ms 内完成（疑似存在未 resolve 的 Promise）——判定失败');
  process.exit(1);
}, WATCHDOG_MS);

main().then(() => {
  clearTimeout(watchdog);
  console.log('search_seq.test.js: 全部断言通过');
}).catch((e) => {
  clearTimeout(watchdog);
  console.error(e);
  process.exit(1);
});
