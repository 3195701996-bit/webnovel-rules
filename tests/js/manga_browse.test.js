const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Node {
  constructor() { this.children = []; this.listeners = {}; this.textContent = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, action) { this.listeners[name] = action; }
  setAttribute() {}
  click() { this.listeners.click(); }
}
const elements = new Map();
const get = id => { if (!elements.has(id)) elements.set(id, new Node()); return elements.get(id); };
const stored = new Map();
const requests = [];
let fail = false;
const context = {
  window: {}, AbortController, URLSearchParams, console,
  document: { getElementById: get, createElement: () => new Node() },
  sessionStorage: { getItem: k => stored.get(k), setItem: (k, v) => stored.set(k, v) },
  makeMangaCard: row => Object.assign(new Node(), { row }),
  fetch: async url => {
    requests.push(url);
    if (url === '/api/explore/sources') return { ok: true, json: async () => ({ manga: { sources: [
      { key: 'copymanga', name: '拷贝漫画', categories: [{ key: 'theme=aiqing', name: '爱情', group: '主题' }] },
      { key: 'mangadex', name: 'MangaDex', categories: [{ key: 'hot', name: '热门', group: '排行' }] },
    ] } }) };
    if (fail) return { ok: false, json: async () => ({ error: '网络失败' }) };
    const page = Number(new URLSearchParams(url.split('?')[1]).get('page'));
    return { ok: true, json: async () => ({ results: page === 1 ? [{ id: 'a', source: 'copymanga' }] :
      page === 2 ? [{ id: 'a', source: 'copymanga' }, { id: 'b', source: 'copymanga' }] : [] }) };
  },
};
const script = fs.readFileSync('static/js/manga_browse.js', 'utf8');
vm.createContext(context); vm.runInContext(script, context);
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await context.window.MangaBrowse.open();
  assert.equal(get('browse-sources').children.length, 2);
  assert.equal(get('browse-categories').children[1].children[0].textContent, '主题');
  get('browse-categories').children[1].children[1].children[0].click(); await tick();
  assert.equal(get('browse-results').children.length, 1);
  fail = true; get('browse-more').click(); await tick();
  assert.equal(get('browse-results').children.length, 1, '错误不得丢失已加载结果');
  assert.match(get('browse-status').textContent, /网络失败/);
  fail = false; get('browse-more').click(); await tick();
  assert.equal(get('browse-results').children.length, 2, '跨页重复作品应去重');
  assert.match(requests.at(-1), /page=2/, '失败重试不得跳页');
  get('browse-more').click(); await tick();
  assert.equal(get('browse-more').hidden, true);
  get('browse-categories').children[1].click();
  assert.equal(get('browse-results').children.length, 0);
  get('browse-sources').children[1].click();
  assert.equal(get('browse-categories').children[0].textContent, 'MangaDex');
  assert.equal(get('browse-categories').children[1].children[0].textContent, '排行');
  console.log('manga_browse.test.js: groups, source switch, pagination, error retry and dedup passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
