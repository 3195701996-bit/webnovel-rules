const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.children = []; this.events = {}; }
  get childElementCount() { return this.children.length; }
  append(...items) { this.children.push(...items); }
  replaceChildren() { this.children = []; }
  setAttribute() {}
  addEventListener(name, fn) { this.events[name] = fn; }
  showModal() { this.open = true; }
  close() { this.open = false; this.events.close?.(); }
  focus() { this.focused = true; }
}
(async () => {
  const body = new Element(), button = new Element(), calls = [];
  const context = {window: {}, document: {body, createElement: () => new Element()}};
  vm.runInNewContext(fs.readFileSync('static/js/novel_metadata.js', 'utf8'), context);
  let reply, failure = false, reloads = 0;
  context.window.mountNovelMetadata({button, key: '书/1', reload: async () => reloads++,
    request: async (path, options) => { calls.push({path, options}); if (failure) throw Error('409 已变化'); return reply; }});
  const dialog = body.children[0], [, content, status, actions] = dialog.children;
  const [cancel, confirm] = actions.children;
  const preview = {ok: true, read_only: true, state_revision: 'a'.repeat(64), proposed: {name: '<img src=x>'},
    changes: [{field: 'name', before: '旧名', after: '<img src=x>'}]};
  button.onclick(); assert.equal(calls.length, 0);
  reply = preview; await confirm.onclick(); assert.equal(confirm.textContent, '确认修改');
  assert.equal(content.children[0].textContent, '书名：旧名 → <img src=x>');
  cancel.onclick(); assert.equal(calls.length, 1); assert(button.focused);
  button.onclick(); await confirm.onclick(); failure = true; await confirm.onclick();
  assert.equal(confirm.textContent, '读取预览'); assert(!confirm.disabled); assert(status.textContent.includes('409'));
  assert.equal(calls.at(-1).options.body.state_revision, 'a'.repeat(64));
  assert(calls.at(-1).path.includes('%E4%B9%A6%2F1/metadata'));
  failure = false; await confirm.onclick(); reply = {ok: true, catalog_preserved: true}; await confirm.onclick();
  assert.equal(reloads, 1); assert(status.textContent.includes('已保存'));
  cancel.onclick(); button.onclick(); reply = {...preview, state_revision: 'invalid'};
  await confirm.onclick(); assert.equal(confirm.textContent, '读取预览'); assert(!confirm.disabled);
  reply = {...preview, changes: []}; await confirm.onclick(); assert(confirm.disabled);
  console.log('metadata: cancel, exact revision, stale invalidation, retry, text safety, malformed preview and no-change passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
