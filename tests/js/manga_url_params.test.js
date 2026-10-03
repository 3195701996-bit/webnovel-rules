'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '../..');
const cases = [
  {
    file: 'manga_download.html',
    end: 'const MODE =',
    query: '?source=src&cid=id%252Fsegment&title=100%25%E6%BC%AB%E7%94%BB&cover=https%3A%2F%2Fx.invalid%2Fc.jpg&ch=part%252F2&chid=chapter%252F3',
    expected: {SOURCE: 'src', CID: 'id%2Fsegment', TITLE: '100%漫画', COVER: 'https://x.invalid/c.jpg', CH_NAME: 'part%2F2', CH_ID: 'chapter%2F3'},
    vars: 'SOURCE,CID,TITLE,COVER,CH_NAME,CH_ID',
  },
  {
    file: 'manga_reader.html',
    end: '// URL 显式指定章节',
    query: '?source=src&cid=id%252Fsegment&title=100%25%E6%BC%AB%E7%94%BB',
    expected: {SOURCE: 'src', CID: 'id%2Fsegment', TITLE: '100%漫画'},
    vars: 'SOURCE,CID,TITLE',
  },
  {
    file: 'manga_detail.html',
    end: 'const chapters =',
    query: '?source=src&id=id%252Fsegment',
    expected: {SOURCE: 'src', CID: 'id%2Fsegment'},
    vars: 'SOURCE,CID',
  },
  {
    file: 'novel_detail.html',
    end: 'function fmtWords(',
    query: '?name=100%25%E5%AD%97%E7%9A%84%E4%B9%A6%25%E5%90%8D',
    expected: {NAME: '100%字的书%名'},
    vars: 'NAME',
  },
];

for (const test of cases) {
  const html = fs.readFileSync(path.join(root, 'templates', test.file), 'utf8');
  const start = html.indexOf('const params = new URLSearchParams(location.search);');
  const end = html.indexOf(test.end, start);
  assert.ok(start >= 0 && end > start, `${test.file}: parameter initialization block must exist`);
  const block = html.slice(start, end);
  assert.doesNotMatch(block, /decodeURIComponent\s*\(\s*params\.get/,
    `${test.file}: URLSearchParams values must not be decoded a second time`);
  const context = vm.createContext({
    URLSearchParams,
    location: {search: test.query},
  });
  vm.runInContext(`${block}\nglobalThis.result = {${test.vars}};`, context, {filename: test.file});
  assert.deepEqual(JSON.parse(JSON.stringify(context.result)), test.expected,
    `${test.file}: percent signs and escaped separators survive one URL decoding pass`);
}

console.log('manga_url_params.test.js: percent-containing IDs and titles round-trip across all detail/read/download routes');
