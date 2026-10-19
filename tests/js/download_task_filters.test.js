const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const context = {window: {}};
vm.runInNewContext(fs.readFileSync('static/js/download_task_filters.js', 'utf8'), context);
const filter = context.window.filterDownloadTasks;
const tasks = [{id:'old',title:'旧作品',status:'done'},
  {id:'error',title:'第４２话',status:'error',manga_key:'copymanga:opaque-id'},
  {id:'active',title:'活动作品',status:'error',running:true},
  {id:'paused',title:'暂停作品',status:'paused'}];
const before = JSON.stringify(tasks);
assert.equal(filter(tasks, '42 COPYMANGA', 'failed')[0], tasks[1]);
assert.deepEqual(Array.from(filter(tasks, '', 'recoverable'), t=>t.id), ['error','paused']);
assert.deepEqual(Array.from(filter(tasks, '', 'active'), t=>t.id), ['active']);
assert.deepEqual(Array.from(filter(tasks, '', 'finished'), t=>t.id), ['old']);
assert.equal(filter(tasks, 'opaque-id', 'all').length, 0);
assert.equal(filter(tasks, '', 'all').length, 4);
assert.equal(filter([{status:'running',title:'running'}], '', 'active').length, 1);
assert.equal(before, JSON.stringify(tasks));
assert.equal(filter([{type:'novel',title:'旧名',book_key:'key',progress:{book_name:'实际书名'}}], '实际书名', 'all').length, 1);
console.log('download filters: display-only, identity/order, NFKC, source/name search and active precedence passed');
