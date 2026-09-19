'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../..');
const source = name => fs.readFileSync(path.join(root, name), 'utf8');
function extract(text, start) {
  const a = text.indexOf(start);
  const b = text.indexOf('\n}', a);
  assert.ok(a >= 0 && b > a, start);
  return text.slice(a, b + 2);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return {promise, resolve, reject};
}
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
function element() {
  return {value: '', innerHTML: '', textContent: '', children: [], style: {}, attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
    removeAttribute(k) { delete this.attrs[k]; }, remove() {}};
}
function harness() {
  const els = new Map();
  const el = name => { if (!els.has(name)) els.set(name, element()); return els.get(name); };
  const timers = new Map(), handlers = new Set(), requests = [], pages = [], drawn = [], toasts = [];
  let timerId = 0;
  const ctx = {
    console, AbortController, TextDecoder, Set, Map, encodeURIComponent,
    window: {scrollTo() {}}, location: {}, sessionStorage: {getItem() { return null; }},
    document: {hidden: false, querySelector: el, getElementById: name => el('#' + name),
      addEventListener(type, cb) { handlers.add(cb); }, removeEventListener(type, cb) { handlers.delete(cb); }},
    setTimeout(cb) { const id = ++timerId; timers.set(id, cb); return id; },
    clearTimeout(id) { timers.delete(id); },
    $: el, toast: msg => toasts.push(msg),
    fetch() { const d = deferred(); requests.push(d); return d.promise; },
    requestJSON(url, options) { const d = deferred(); pages.push({url, options, ...d}); return d.promise; },
    SEARCH: {q: 'old', src: 'source', page: 1, list: [], rendered: new Set(), gen: 0, fetching: false},
    pagerEl: null, SCROLL_KEY: 'scroll', parseJmCode: () => null,
    saveSearchState() {}, saveLastSrc() {}, saveScrollPos() {}, cacheMangaSearch() {},
    updatePager() {}, announceManga() {}, showEmpty() {},
    setSearchStatus(st, q) { st.textContent = q; },
    appendResults(items) { drawn.push(...items); },
  };
  vm.createContext(ctx);
  const manga = source('templates/manga.html');
  vm.runInContext(extract(manga, 'async function fetchPage(') + '\n' + extract(manga, 'async function search('), ctx);
  return {ctx, el, timers, handlers, requests, pages, drawn, toasts};
}
function stream() {
  const reads = [];
  let opened = 0;
  return {reads, get opened() { return opened; }, response: {ok: true, body: {getReader() {
    opened++;
    return {read() { const d = deferred(); reads.push(d); return d.promise; }};
  }}}};
}
function chunk(title) {
  return {done: false, value: new TextEncoder().encode('data: ' + JSON.stringify({
    groups: [{title}], finished: true, has_more: false,
  }) + '\n\n')};
}
async function testSearch() {
  // An old page response and its finally must not clear a new search's busy state.
  for (const fail of [false, true]) {
    const h = harness(), {ctx, el} = h;
    const old = ctx.fetchPage(2);
    assert.equal(h.pages[0].options.timeout, 30000);
    el('#q').value = 'fresh';
    const fresh = ctx.search(false);
    if (fail) h.pages[0].reject(new Error('old failure'));
    else h.pages[0].resolve({results: [{title: 'old'}], has_more: false});
    await old;
    assert.equal(h.drawn.length, 0);
    assert.equal(h.toasts.length, 0);
    assert.equal(el('#status').attrs['aria-busy'], 'true');
    const s = stream(); h.requests[0].resolve(s.response); await flush();
    s.reads[0].resolve(chunk('fresh')); await fresh;
    assert.equal(h.drawn[0].title, 'fresh');
    assert.equal(el('#status').attrs['aria-busy'], undefined);
    assert.equal(h.timers.size, 0);
  }
  // A late fetch must never open an obsolete stream.
  {
    const h = harness(); h.el('#q').value = 'old'; const old = h.ctx.search(false);
    h.el('#q').value = 'new'; const next = h.ctx.search(false);
    const s = stream(); h.requests[0].resolve(s.response); await old;
    assert.equal(s.opened, 0);
    assert.equal(h.el('#status').attrs['aria-busy'], 'true');
    const n = stream(); h.requests[1].resolve(n.response); await flush();
    n.reads[0].resolve(chunk('new')); await next;
    assert.equal(h.drawn.length, 1);
  }
  // Late stream data / catch / finally cannot undo a newer page request.
  for (const fail of [false, true]) {
    const h = harness(); h.el('#q').value = 'old'; const old = h.ctx.search(false);
    const s = stream(); h.requests[0].resolve(s.response); await flush();
    const next = h.ctx.fetchPage(2);
    if (fail) s.reads[0].reject(new Error('old stream failure'));
    else s.reads[0].resolve(chunk('old'));
    await old;
    assert.equal(h.drawn.length, 0);
    assert.equal(h.el('#status').attrs['aria-busy'], 'true');
    h.pages[0].resolve({results: [{title: 'page2'}], has_more: false}); await next;
    assert.equal(h.ctx.SEARCH.page, 2);
    assert.equal(h.drawn[0].title, 'page2');
    assert.equal(h.el('#status').attrs['aria-busy'], undefined);
  }
  // Active pagination failures are recoverable and do not clear the current list.
  const h = harness(); h.ctx.SEARCH.list = [{title: 'keep'}];
  const failed = h.ctx.fetchPage(3); h.pages[0].reject(new Error('timeout')); await failed;
  assert.equal(h.ctx.SEARCH.fetching, false);
  assert.equal(h.ctx.SEARCH.list[0].title, 'keep');
  assert.equal(h.toasts.length, 1);
}
async function testPoller() {
  const h = harness(); vm.runInContext(source('static/js/common.js'), h.ctx);
  const gate = deferred(); let calls = 0;
  const poller = h.ctx.window.createPoller(() => { calls++; return gate.promise; });
  assert.equal(h.handlers.size, 1);
  poller.kick(); [...h.handlers][0]();
  assert.equal(calls, 1);
  poller.stop(); poller.stop();
  assert.equal(h.handlers.size, 0);
  gate.resolve({ok: true}); await flush();
  assert.equal(h.timers.size, 0);
  poller.kick(); assert.equal(calls, 1);
  h.ctx.document.hidden = true;
  const p2 = h.ctx.window.createPoller(async () => { calls++; return {}; });
  assert.equal(calls, 1);
  h.ctx.document.hidden = false; [...h.handlers][0](); await flush();
  assert.equal(calls, 2); p2.stop(); assert.equal(h.handlers.size, 0);
}
async function testRendering() {
  const h = harness(); vm.runInContext(source('static/js/common.js'), h.ctx);
  h.ctx.esc = h.ctx.window.esc;
  vm.runInContext(extract(source('templates/tasks.html'), 'function _taskRowParts('), h.ctx);
  const message = '<img src=x onerror="alert(1)">';
  const parts = h.ctx._taskRowParts({id: 'task', status: 'running', progress: {message}});
  assert.ok(parts.progHtml.includes('&lt;img'));
  assert.ok(!parts.progHtml.includes(message));
  let stops = 0;
  Object.assign(h.ctx, {SOURCE: 'source', CID: 'comic', pollErrors: 0,
    pollerStopped: false, poller: {stop() { stops++; }}, requestJSON: async () => ({status: 'done', done: 1, total: 1})});
  vm.runInContext(extract(source('templates/manga_download.html'), 'async function poll('), h.ctx);
  h.el('#start-btn').disabled = true; h.el('#zip-btn').disabled = true;
  await h.ctx.poll();
  assert.equal(stops, 1);
  assert.equal(h.el('#start-btn').disabled, false);
  assert.equal(h.el('#zip-btn').disabled, false);
}
const watchdog = setTimeout(() => { console.error('unfinished assertions'); process.exit(1); }, 5000);
(async () => { await testSearch(); await testPoller(); await testRendering(); })().then(() => {
  clearTimeout(watchdog); console.log('audit_search_poller.test.js: ALL ASSERTIONS PASSED');
}).catch(e => { clearTimeout(watchdog); console.error(e); process.exit(1); });
