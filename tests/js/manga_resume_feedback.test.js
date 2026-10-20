'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../../templates/tasks.html'), 'utf8');
const body = html.match(/if \(b.dataset.manga\) \{\s*\/\/ 漫画任务：继续下载[\s\S]*?\n      \} else/)[0].replace(/\} else$/, '}');
async function run(result) {
  const messages = [], requests = [];
  const context = {b:{dataset:{manga:'1',src:'test source',cid:'id%25literal'}},
    encodeURIComponent, toast:(...args)=>messages.push(args),
    requestJSON:async p=>{requests.push(p);return result;}};
  await vm.runInNewContext('(async()=>{' + body + '})()', context);
  assert.equal(requests[0], '/api/manga/download/resume?source=test%20source&cid=id%25literal');
  return messages[0];
}
(async()=>{
  assert.deepEqual(await run({ok:true,resumed:true}), ['已请求继续下载','ok']);
  assert.match((await run({ok:true,resumed:false,reason:'no_task'}))[0], /不存在，未启动下载/);
  for (const data of [{}, {ok:true}, {ok:false,resumed:true}, {ok:true,resumed:'true'}]) {
    assert.deepEqual(await run(data), ['未确认恢复下载，请刷新任务后重试','error']);
  }
  console.log('manga resume: requires confirmed result, missing task is not success, percent identity preserved');
})().catch(e=>{console.error(e);process.exitCode=1;});
