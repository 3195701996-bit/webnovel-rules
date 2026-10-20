'use strict';
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('templates/manga_detail.html', 'utf8');
const code = html.slice(html.indexOf('function showMore()'), html.indexOf('function searchTag('));
const status = {textContent:'old'};
let opened = 0, denied = false;
const copies = [];
const context = {
  detail: {title:'100%标题'},
  $: selector => selector === '#detail-more-status' ? status : {showModal(){opened++;}},
  navigator: {clipboard:{async writeText(value){if(denied) throw new Error('denied'); copies.push(value);}}},
  location:{href:'http://127.0.0.1/manga_detail?id=literal%252Fid'},
};
vm.createContext(context);
vm.runInContext(code, context);
(async () => {
  context.showMore();
  assert.equal(opened, 1);
  assert.equal(status.textContent, '');
  await context.copyDetailValue('title');
  assert.equal(copies[0], '100%标题');
  assert.equal(status.textContent, '标题已复制');
  await context.copyDetailValue('link');
  assert.equal(copies[1], context.location.href);
  denied = true;
  await context.copyDetailValue('title');
  assert.match(status.textContent, /复制失败/);
  context.detail = null;
  context.showMore();
  assert.equal(opened, 1);
  assert(!code.includes('prompt('));
  assert.match(html, /<dialog[^>]+aria-labelledby="detail-more-title"/);
  assert.match(html, /margin:auto; max-height:calc\(100dvh - 32px\)/);
  console.log('manga detail more: named actions, pending guard, exact copy and permission failure passed');
})().catch(error => {console.error(error); process.exitCode=1;});
