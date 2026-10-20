'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor() { this.children = []; }
  append(...items) { this.children.push(...items); }
  replaceChildren() { this.children = []; }
  setAttribute() {}
}
(async () => {
  let result = {ok:true, missing_count:1, latest:'第42话',
    missing:[{id:'new-42',name:'第42话'}]};
  const submitted = [];
  let submitResult = {ok:true,status:'queued'};
  const host = new Node();
  const context = {window:{}, confirm:()=>true,
    document:{createElement:()=>new Node()},
    requestJSON:async (url, opts) => {
      if (url.endsWith('/check-update')) return result;
      submitted.push(JSON.parse(JSON.stringify(opts.body)));
      return submitResult;
    }};
  vm.runInNewContext(fs.readFileSync('static/js/manga_update_preview.js','utf8'), context);
  context.window.mountMangaUpdatePreview(host,{title:'演示'},'copy','comic');
  const [check, status, preview, download] = host.children;
  assert.equal(download.hidden,true);
  await check.onclick();
  assert.match(status.textContent,/最新：第42话/);
  assert.equal(preview.children[1].children[0].textContent,'第42话');
  await download.onclick();
  assert.deepEqual(submitted[0].chapters,['new-42']);
  assert.equal(download.disabled,true,'成功提交后不得重复提交同一清单');
  result = {ok:true,missing_count:3,missing:[],catalog_diff:{baseline_available:true,
    added_missing_count:1,existing_missing_count:2,same_name_new_id_count:1}};
  await check.onclick();
  assert.match(status.textContent,/目录新增 1 项，原目录待补 2 项/);
  assert.match(status.textContent,/同名新身份/);
  assert.match(status.textContent,/不会自动合并或删除/);
  result = {ok:true,missing_count:3,missing:[]};
  await check.onclick();
  assert.match(status.textContent,/不会自动下载整本/);
  assert.equal(download.hidden,true);
  await download.onclick();
  assert.equal(submitted.length,1);
  result = {ok:false,error:'目录失败'};
  await check.onclick();
  assert.match(status.textContent,/检查失败/);
  assert.equal(check.disabled,false);
  assert.equal(download.hidden,true);
  result = {ok:true,missing_count:1,missing:[{id:'new-43',name:'第43话'}]};
  await check.onclick();
  submitResult = {ok:false,error:'请先暂停当前任务'};
  await download.onclick();
  assert.match(status.textContent,/下载未启动.*暂停.*清单已保留/);
  assert.equal(download.disabled,false);
  submitResult = {ok:true,status:'queued'};
  await download.onclick();
  assert.deepEqual(submitted.at(-1).chapters,['new-43']);
  assert.equal(download.disabled,true);
  console.log('manga_update_preview: exact chapter submission/no whole-book fallback/error recovery passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
