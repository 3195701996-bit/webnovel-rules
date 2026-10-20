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
  vm.runInNewContext(fs.readFileSync('static/js/novel_cleaning.js', 'utf8'), context);
  const button = new Element('button');
  let response; let fail = false; let reloads = 0; const requests = [];
  context.window.mountNovelCleaning({button, key: '书/1', reload: async () => { reloads++; },
    request: async (path, opts) => { requests.push({path, opts}); if (fail) throw new Error('离线'); return response; }});
  const dialog = body.children[0];
  const [, content, status, actions] = dialog.children;
  const [cancel, confirm] = actions.children;
  response = {ok: true, scanned: 2, changed: 1, removed_lines: 1, removed_chars: 20,
    samples: ['<img onerror=attack()>'], review_count: 1,
    review_items: [{index: 2, name: '推书《测试》'}], failed: []};
  await button.onclick();
  assert(dialog.open && !confirm.disabled);
  assert(requests[0].path.includes('%E4%B9%A6%2F1/reclean?dry_run=1'));
  assert(content.children.some(p => p.textContent === '样例：<img onerror=attack()>'));
  assert(content.children.some(p => p.textContent.includes('不会自动删除')));
  cancel.onclick();
  assert(!dialog.open && button.focused && requests.length === 1);
  fail = true; await button.onclick();
  assert(confirm.disabled && status.textContent.includes('预览失败'));
  cancel.onclick(); fail = false; await button.onclick();
  response = {ok: true, changed: 1, txt_rebuilt: false, failed: [{reason: '全文重建失败'}]};
  await confirm.onclick();
  assert(requests.at(-1).opts.body.dry_run === false);
  assert(status.textContent.includes('TXT未重建'));
  assert(content.children.some(p => p.textContent.includes('全文重建失败')));
  assert(confirm.disabled && !cancel.disabled && reloads === 1);
  cancel.onclick();
  console.log('novel cleaning: preview/cancel/text safety/error recovery/exact write/export failure passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
