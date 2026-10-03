'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../templates/manga.html'), 'utf8');
function extract(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `missing ${name}`);
  const brace = source.indexOf('{', start);
  let depth = 0;
  for (let i = brace; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`unterminated ${name}`);
}

const stateFn = extract('parseMangaSearchState');
const saveFn = extract('saveSearchState');
const cardFn = extract('makeMangaCard');
const restoreFn = extract('initRestoreSearch');
const elements = {
  '#q': {value: '咒术回战'},
  '#src': {value: 'jm'},
  order: {value: 'tr'},
};
const urls = [];
const context = {
  URLSearchParams,
  Number,
  document: {getElementById: id => elements[id] || null},
  $: selector => elements[selector],
  history: {replaceState: (_state, _title, url) => urls.push(url)},
  SEARCH: {page: 7},
};
vm.createContext(context);
vm.runInContext(`${stateFn}\n${saveFn}\nglobalThis.parse = parseMangaSearchState; globalThis.save = saveSearchState;`, context);

const restored = context.parse('?q=%E5%92%92%E6%9C%AF%E5%9B%9E%E6%88%98&source=jm&order=tr&page=7');
assert.deepEqual(JSON.parse(JSON.stringify(restored)), {
  q: '咒术回战', src: 'jm', order: 'tr', page: 7,
});
assert.deepEqual(JSON.parse(JSON.stringify(context.parse('?q=x&page=0&order=unsafe'))), {
  q: 'x', src: '', order: 'mr', page: 1,
});
context.save();
assert.equal(urls.length, 1);
const saved = new URLSearchParams(urls[0].slice(1));
assert.equal(saved.get('q'), '咒术回战');
assert.equal(saved.get('source'), 'jm');
assert.equal(saved.get('order'), 'tr');
assert.equal(saved.get('page'), '7');

let delayedRestoreCalls = 0;
const activeSearch = vm.createContext({
  SEARCH: {q: '用户刚提交的新搜索', gen: 1},
  location: {search: '?q=旧页面关键词&source=jm'},
  parseMangaSearchState: () => { throw new Error('active search must short-circuit restore'); },
  search: () => { delayedRestoreCalls += 1; },
});
vm.runInContext(`${restoreFn}; initRestoreSearch();`, activeSearch);
assert.equal(delayedRestoreCalls, 0,
  'late source loading must not launch a restore search over a user-initiated search');
assert.equal(activeSearch.SEARCH.q, '用户刚提交的新搜索',
  'delayed restoration must preserve the active search identity');

const cacheCalls = [...source.matchAll(/cacheMangaSearch\(([^\n]+)\)/g)];
assert.ok(cacheCalls.length >= 2, 'both initial search and pagination should cache results');
assert.ok(cacheCalls.every(match => match[1].split(',').length === 5),
  'every search cache write must preserve query, source, order, page, and list');

console.log('manga_search_state.test.js: 全部断言通过');

class FakeCard {
  constructor() { this.attrs = {}; this.listeners = {}; }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  click() { this.clicked = true; this.onclick?.(); }
}
const cards = [];
const cardContext = {
  document: {createElement: () => { const card = new FakeCard(); cards.push(card); return card; }},
  esc: value => String(value ?? ''),
  saveScrollPos() {},
  location: {},
  toast() {},
  pickMangaSource() {},
  window: {},
};
vm.createContext(cardContext);
vm.runInContext(`${cardFn}\nglobalThis.make = makeMangaCard;`, cardContext);
const keyboardCard = cardContext.make({
  id: 'one', source: 'jm', title: '键盘可达作品',
  sources: [{id: 'one', source: 'jm', source_name: '禁漫'}],
});
assert.equal(keyboardCard.attrs.role, 'link');
assert.equal(keyboardCard.attrs.tabindex, '0');
assert.match(keyboardCard.attrs['aria-label'], /键盘可达作品.*打开漫画详情/);
keyboardCard.listeners.keydown({key: 'Enter', preventDefault() { this.prevented = true; }});
assert.equal(keyboardCard.clicked, true, 'Enter should activate a search result card');
assert.equal(cardContext.location.href, '/manga_detail?source=jm&id=one');
assert.match(source, /\.manga-card:focus-visible\s*\{/, 'keyboard focus must be visible');
assert.match(source, /role="dialog" aria-modal="true"/, 'source picker must expose dialog semantics');
