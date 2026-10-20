const assert = require('assert'); const fs = require('fs'); const vm = require('vm');
class Element {
  constructor(tag) { this.children = []; this.events = {}; this.tag = tag; this.open = false; }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children = []; }
  setAttribute() {}
  addEventListener(name, fn) { this.events[name] = fn; }
  showModal() { this.open = true; }
  close() { this.open = false; this.events.close?.(); }
  focus() { this.focused = true; }
}
(async () => {
  const body = new Element('body'); let writableCalls = 0, fetches = 0;
  const root = {showSaveFilePicker: async () => ({createWritable: async () => { writableCalls++; return {}; }}),
    fetch: async () => { fetches++; return {ok: false, status: 409, json: async () => ({error: '预览过期'})}; }};
  const context = {window: root, document: {body, createElement: tag => new Element(tag)},
    AbortController, setTimeout: () => 1, clearTimeout: () => {}};
  vm.runInNewContext(fs.readFileSync('static/js/reader_export.js', 'utf8'), context);
  const button = new Element('button'); let requests = 0;
  root.mountReaderExport({button, key: '书/1', name: () => '书名', request: async () => {
    requests++;
    return {ok: true, revision: 'a'.repeat(64), clean: true, omit_promotions: false,
      exported_count: 1, omitted_count: 0, missing_count: 0, omitted: [], missing: [],
      chapters: [{heading: '第1章 测试', source_index: 1}]};
  }});
  const dialog = body.children[0]; const [, , controls, content, status, actions] = dialog.children;
  const [close, preview, save] = actions.children;
  button.onclick(); assert(save.disabled);
  await preview.onclick(); assert(!save.disabled && requests === 1);
  assert(content.children.some(p => p.textContent.includes('第1章 测试')));
  close.onclick(); assert(!dialog.open && button.focused && fetches === 0);
  button.onclick(); await preview.onclick(); await save.onclick();
  assert(fetches === 1 && writableCalls === 0 && save.disabled);
  assert(status.textContent.includes('409'));
  await preview.onclick();
  root.fetch = async () => ({ok: true, headers: {get: () => 'text/html'}});
  await save.onclick(); assert(writableCalls === 0 && status.textContent.includes('不是TXT'));
  controls.children[0].children[0].checked = false;
  controls.children[0].children[0].onchange(); assert(save.disabled);
  console.log('reader export: preview/cancel/options invalidation/stale409/no target truncation/content-type guard passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
