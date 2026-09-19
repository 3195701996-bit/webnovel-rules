/* C05 追加 阅读器章节请求离线回归（node 直接运行，无浏览器依赖，不新增依赖）
 *
 * 被测对象 = templates/reader.html 内联脚本中的 _fetchChapterRemote
 * （用 node:vm 原样抽取该函数执行，与线上同一份代码）。
 * 用 fake fetch 验证三件事：
 *   1. 请求参数：URL 正确且带 cache:'no-cache'（强制条件重验证，绕开
 *      Cache-Control private,max-age=3600 的旧正文命中；非 no-store）
 *   2. 非 2xx：必须 reject，绝不把错误 JSON 当成功结果交给缓存/阅读器
 *   3. 2xx：正常返回解析后的章节对象
 *
 * 运行：node tests/js/reader_fetch.test.js
 */
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const READER = path.join(__dirname, '..', '..', 'templates', 'reader.html');
const src = fs.readFileSync(READER, 'utf8');
const START = 'function _fetchChapterRemote(idx) {';
const END = 'window._chCache = ChapterCache.makeChapterCache(';
const a = src.indexOf(START), b = src.indexOf(END);
assert.ok(a >= 0 && b > a, '未在 reader.html 中定位到 _fetchChapterRemote');
const BLOCK = src.slice(a, b);
assert.ok(/cache:\s*'no-cache'/.test(BLOCK), '_fetchChapterRemote 未使用 cache:\'no-cache\'');
assert.ok(/if \(!r\.ok\) throw/.test(BLOCK), '_fetchChapterRemote 未对非 2xx 抛错');

const calls = [];
const sandbox = {
  console, Promise, Error, JSON,
  KEY: 'bk1',
  fetch: (url, opts) => {
    calls.push({url, opts});
    const next = sandbox.__next;
    return Promise.resolve({
      ok: next.ok,
      status: next.status,
      json: () => Promise.resolve(next.body),
    });
  },
};
sandbox.__next = {};
vm.createContext(sandbox);
vm.runInContext(BLOCK + '\n;globalThis.__fetchChapterRemote = _fetchChapterRemote;', sandbox);
const fetchRemote = sandbox.__fetchChapterRemote;
assert.strictEqual(typeof fetchRemote, 'function', '未能从 vm 中取出 _fetchChapterRemote');

async function main() {
  // 1. 成功：参数正确 + 返回解析后的对象
  calls.length = 0;
  sandbox.__next = {ok: true, status: 200, body: {downloaded: true, content: '正文7'}};
  const ch = await fetchRemote(7);
  assert.strictEqual(calls.length, 1, '发起一次请求');
  assert.strictEqual(calls[0].url, '/api/books/bk1/chapter/7', 'URL 正确');
  // vm 跨 realm 对象原型不同，逐字段断言（等价于 {cache:'no-cache'} 且不含其它项）
  assert.strictEqual(calls[0].opts && calls[0].opts.cache, 'no-cache',
    "必须传 cache:'no-cache'（强制条件重验证，非 no-store）");
  assert.strictEqual(Object.keys(calls[0].opts || {}).length, 1,
    '请求选项应仅含 cache，不得误用 no-store 等');
  assert.strictEqual(ch.content, '正文7', '成功响应解析后返回');

  // 2. 非 2xx（如 500 返回错误 JSON）：必须 reject，不得把错误 JSON 当结果
  calls.length = 0;
  sandbox.__next = {ok: false, status: 500, body: {error: 'crawler failed'}};
  await assert.rejects(() => fetchRemote(8), /HTTP 500/,
    '非 2xx 必须抛错，错误 JSON 不得交给阅读器');

  // 3. 404（章节不存在）同样 reject
  sandbox.__next = {ok: false, status: 404, body: {error: 'not found'}};
  await assert.rejects(() => fetchRemote(9), /HTTP 404/, '404 必须抛错');
}

const WATCHDOG_MS = 5000;
const watchdog = setTimeout(() => {
  console.error('reader_fetch.test.js: 主流程未在 ' + WATCHDOG_MS +
    'ms 内完成（疑似存在未 resolve 的 Promise）——判定失败');
  process.exit(1);
}, WATCHDOG_MS);

main().then(() => {
  clearTimeout(watchdog);
  console.log('reader_fetch.test.js: 全部断言通过');
}).catch((e) => {
  clearTimeout(watchdog);
  console.error(e);
  process.exit(1);
});
