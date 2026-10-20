const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.disabled = false; this.open = false; }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children = []; }
  setAttribute() {}
  addEventListener(name, fn) { this.events[name] = fn; }
  showModal() { this.open = true; }
  close() { this.open = false; this.events.close?.(); }
  focus() { this.focused = true; }
}
(async () => {
  const body = new Element('body');
  const context = {window: {}, document: {body, createElement: tag => new Element(tag)}};
  vm.runInNewContext(fs.readFileSync('static/js/novel_recovery.js', 'utf8'), context);
  const button = new Element('button'); const id = 'a'.repeat(64), conflict = 'b'.repeat(64);
  const calls = []; let result; let mismatch = false;
  const listing = {ok: true, count: 2, items: [{id, label: '<img onerror=attack()>', restorable: true}, {id: conflict, restorable: false}]};
  context.window.mountNovelRecovery({button, key: '书/1', reload: async () => {}, request: async (path, options) => {
    calls.push({path, options});
    if (path.endsWith('/recovery')) return listing;
    return result || {ok: true, dry_run: true, items: [{id: mismatch ? conflict : id, ok: true}]};
  }});
  const dialog = body.children[0]; const [, , tools, list, status, actions] = dialog.children;
  const [, all] = tools.children; const [close, confirm] = actions.children;
  await button.onclick();
  assert(list.children[0].children[1].textContent.includes('<img onerror=attack()>'));
  assert(dialog.className.includes('novel-recovery'));
  assert(!list.children[0].children[1].textContent.includes(id.slice(0, 8)));
  assert(list.children[1].children[0].disabled);
  all.onclick(); await confirm.onclick();
  assert(calls.at(-1).options.body.dry_run === true);
  assert(calls.at(-1).options.body.ids.length === 1);
  assert(confirm.textContent === '确认恢复');
  close.onclick(); assert(!dialog.open && button.focused);
  assert(calls.filter(c => c.options?.body?.dry_run === false).length === 0);
  await button.onclick(); all.onclick(); mismatch = true; await confirm.onclick();
  assert(status.textContent.includes('身份不一致'));
  assert(confirm.textContent.includes('预览恢复'));
  mismatch = false; await confirm.onclick();
  result = {ok: true, dry_run: false, restored: 0, txt_rebuilt: false, export_error: '', items: [{id, ok: false, error: '后续修改'}]};
  await confirm.onclick();
  assert(calls.some(c => c.options?.body?.dry_run === false));
  assert(status.textContent.includes('已恢复 0 项') && status.textContent.includes('后续修改'));
  assert(confirm.disabled);
  // The server owns catalog ordering. The client must not re-sort opaque IDs.
  listing.items = [{id: conflict, label: '第1项 · 重置章号', restorable: true},
    {id, label: '第2项 · 重置章号', restorable: true}];
  close.onclick(); await button.onclick();
  assert(list.children[0].children[1].textContent.startsWith('第1项'));
  assert(list.children[1].children[1].textContent.startsWith('第2项'));
  all.onclick(); mismatch = false; result = {ok: true, dry_run: true,
    items: [{id: conflict, ok: true}, {id, ok: true}]};
  await confirm.onclick();
  assert.deepStrictEqual(Array.from(calls.at(-1).options.body.ids), [conflict, id]);
  close.onclick();
  console.log('novel recovery: safe labels/conflict disable/scoped select/preview cancel/identity guard/partial failure passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
