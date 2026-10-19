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
  let readFails = true;
  let deleteFails = true, reloads = 0;
  const context = {window: {}, document: {
    createElement: tag => new Node(tag), createTextNode: text => ({text}),
  }, confirm: () => confirmed, requestJSON: async (url, opts) => {
    if (url.endsWith('/local-media')) {
      if (readFails) throw new Error('清单不可用');
      return {ok:true, local_only:true, source:'demo', comic_id:'comic', count:2,
        media:[{id:'one',name:'同名章节',downloaded:true,partial:false},
          {id:'two',name:'同名章节',downloaded:false,partial:true}]};
    }
    requests++;
    assert.deepEqual(JSON.parse(opts.body), {chapter_ids: ['one', 'two'], identity_mode:'disk'});
    if (deleteFails) throw new Error('下载进行中');
    return {ok:true,deleted:2,missing:0};
  }};
  vm.runInNewContext(fs.readFileSync('static/js/manga_local_manager.js', 'utf8'), context);
  context.window.mountMangaLocalManager(host, {downloaded:['one','one'],
    partial_downloaded:['two'], chapters:[{id:'one',name:'第1话'}]}, 'demo', 'comic', () => {reloads++;});
  const [toggle, panel] = host.children;
  assert.equal(panel.hidden, true);
  await toggle.onclick();
  assert.equal(panel.hidden, false);
  const [controls, status, retry, list] = panel.children;
  const [all, remove] = controls.children;
  assert.match(status.textContent, /未使用在线目录代替/);
  assert.equal(list.children.length, 0);
  assert.equal(remove.disabled, true);
  readFails = false;
  await retry.onclick();
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
  deleteFails = false;
  await remove.onclick();
  assert.equal(requests, 2);
  assert.equal(reloads, 1);
  all.onclick();
  assert.equal(remove.textContent, '删除选中（0）');
  console.log('manga_local_manager: dedup/select/cancel/confirmation/error recovery passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
