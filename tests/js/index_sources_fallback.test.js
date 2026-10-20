'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'templates/index.html'), 'utf8');
const start = html.indexOf('async function loadSources() {');
const end = html.indexOf('\n}\n\n// A03:', start);
assert.ok(start >= 0 && end > start, 'index.html loadSources function not found');
const block = html.slice(start, end + 2);

function harness({response, error, query = ''} = {}) {
  const select = {value: 'keep', innerHTML: '', disabled: false};
  const hint = {textContent: '', title: ''};
  const ctx = {
    URLSearchParams,
    location: {search: query},
    $: selector => selector === '#search-source' ? select : hint,
    esc: s => String(s).replace(/[&<>"']/g, c =>
      ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])),
    fetchTimeout: async () => {
      if (error) throw error;
      return response;
    },
  };
  vm.createContext(ctx);
  vm.runInContext(block + '\nglobalThis.run = loadSources;', ctx);
  return {ctx, select, hint};
}

async function main() {
  for (const failure of [
    {error: new Error('offline')},
    {response: {ok: false, status: 503}},
    {response: {ok: true, json: async () => ({sources: null})}},
  ]) {
    const h = harness(failure);
    assert.equal(await h.ctx.run(), false, 'source failure should be handled, not rejected');
    assert.equal(h.hint.textContent, '内容来源暂不可用，搜索时可重试');
    assert.equal(h.select.disabled, false, 'failed source loading must not lock search');
  }

  const h = harness({
    query: '?source=source-2',
    response: {ok: true, json: async () => ({sources: [
      {uid: 'source-1', bookSourceName: 'Old', enabled: true, searchUrl: '/search'},
      {uid: 'source-2', bookSourceName: '<Trusted>', enabled: true, searchUrl: '/search'},
      {uid: 'disabled', bookSourceName: 'Disabled', enabled: false, searchUrl: '/search'},
    ]})},
  });
  assert.equal(await h.ctx.run(), true);
  assert.equal(h.select.value, 'source-2', 'URL source filter must survive async option loading');
  assert.match(h.select.innerHTML, /&lt;Trusted&gt;/, 'source names must remain HTML escaped');
  assert.doesNotMatch(h.select.innerHTML, /value="disabled"/);
  assert.equal(h.hint.textContent, '2 个可用内容来源');

  assert.match(html, /loadSources\(\)\.then\(\(\) => setTimeout\(initRestoreSearch, 50\)\)/,
    'search restore should continue after loadSources converts failures to a resolved result');
  console.log('index_sources_fallback.test.js: all assertions passed');
}

main().catch(error => { console.error(error); process.exit(1); });
