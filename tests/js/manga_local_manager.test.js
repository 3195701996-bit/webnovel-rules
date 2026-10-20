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
  let malformed = false;
  let numericRows = null;
  let confirmation = '';
  const context = {window: {}, document: {
    createElement: tag => new Node(tag), createTextNode: text => ({text}),
  }, confirm: message => { confirmation = message; return confirmed; }, requestJSON: async (url, opts) => {
    if (url.endsWith('/local-media')) {
      if (readFails) throw new Error('清单不可用');
      if (malformed) return {ok:true, local_only:false, source:'demo', comic_id:'comic', count:0, media:[]};
      if (numericRows) return {ok:true, local_only:true, source:'demo', comic_id:'comic',
        count:numericRows.length, media:numericRows};
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
    partial_downloaded:['two'], chapters:[{id:'one',name:'第1话'}]}, 'demo', 'comic', () => {
      reloads++;
      throw new Error('fixture detail refresh');
    });
  const [toggle, panel] = host.children;
  assert.equal(panel.hidden, true);
  await toggle.onclick();
  assert.equal(panel.hidden, false);
  const [search, summary, controls, status, retry, list] = panel.children;
  const [all, remove] = controls.children;
  assert.match(status.textContent, /未使用在线目录代替/);
  assert.equal(list.children.length, 0);
  assert.equal(remove.disabled, true);
  readFails = false;
  await retry.onclick();
  assert.equal(list.children.length, 2);
  assert.equal(remove.disabled, true);
  search.value = '同名 ONE';
  search.input();
  assert.equal(list.children.length, 1);
  all.onclick();
  assert.equal(remove.textContent, '删除选中（1）');
  search.value = 'two';
  search.input();
  assert.match(summary.textContent, /筛选外已选 1 项/);
  await remove.onclick();
  assert.match(confirmation, /其中 1 项位于当前筛选之外/);
  assert.equal(requests, 0);
  all.onclick();
  assert.equal(remove.textContent, '删除选中（2）');
  all.onclick();
  assert.equal(remove.textContent, '删除选中（1）');
  search.value = '';
  search.input();
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
  assert.match(status.textContent, /删除完成.*详情刷新失败/);
  assert.doesNotMatch(status.textContent, /删除失败/);
  assert.equal(remove.textContent, '删除选中（0）');
  malformed = true;
  await retry.onclick();
  assert.match(status.textContent, /清单格式不正确/);
  assert.equal(remove.disabled, true);
  assert.equal(all.disabled, true);
  all.onclick();
  await remove.onclick();
  assert.equal(requests, 2, '未经校验的清单不能触发删除');
  malformed = false;
  numericRows = [
    {id:'uuid-52-old',name:'第13话',downloaded:true,partial:false},
    {id:'uuid-new',name:'第52话',downloaded:true,partial:false},
    {id:'uuid-2.2',name:'第3话',downloaded:true,partial:false},
    {id:'fractional',name:'第2.2话',downloaded:true,partial:false},
  ];
  await retry.onclick();
  for (const [query,title] of [['52','第52话'],['５２','第52话'],['ID：52','第13话'],['2.2','第2.2话']]) {
    search.value = query; search.input();
    assert.equal(list.children.length,1);
    assert.ok(list.children[0].children[1].text.includes(title),query);
  }
  search.value = 'id:'; search.input();
  assert.equal(all.disabled,true);
  assert.equal(requests,2);
  console.log('manga_local_manager: dedup/select/cancel/confirmation/error recovery passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
