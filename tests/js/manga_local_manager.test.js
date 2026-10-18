'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.hidden = false; this.disabled = false; }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children = []; }
  setAttribute() {}
  addEventListener(name, fn) { this[name] = fn; }
}
(async () => {
  const host = new Node('host');
  let confirmed = false, requests = 0;
  const context = {window: {}, document: {
    createElement: tag => new Node(tag), createTextNode: text => ({text}),
  }, confirm: () => confirmed, requestJSON: async (url, opts) => {
    requests++;
    assert.deepEqual(JSON.parse(opts.body), {chapter_ids: ['one', 'two']});
    throw new Error('下载进行中');
  }};
  vm.runInNewContext(fs.readFileSync('static/js/manga_local_manager.js', 'utf8'), context);
  context.window.mountMangaLocalManager(host, {downloaded:['one','one'],
    partial_downloaded:['two'], chapters:[{id:'one',name:'第1话'}]}, 'demo', 'comic', () => {});
  const [toggle, panel] = host.children;
  assert.equal(panel.hidden, true);
  toggle.onclick();
  assert.equal(panel.hidden, false);
  const [controls, status, list] = panel.children;
  const [all, remove] = controls.children;
  assert.equal(list.children.length, 2);
  assert.equal(remove.disabled, true);
  all.onclick();
  assert.equal(remove.textContent, '删除选中（2）');
  await remove.onclick();
  assert.equal(requests, 0, '取消确认不应发出删除请求');
  confirmed = true;
  await remove.onclick();
  assert.equal(requests, 1);
  assert.match(status.textContent, /下载进行中/);
  assert.equal(remove.disabled, false);
  assert.equal(remove.textContent, '删除选中（2）');
  all.onclick();
  assert.equal(remove.textContent, '删除选中（0）');
  console.log('manga_local_manager: dedup/select/cancel/confirmation/error recovery passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
