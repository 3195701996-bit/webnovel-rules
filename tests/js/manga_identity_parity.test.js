'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '../..');
const fixturePath = path.join(root,
  'android/app/src/test/resources/manga_identity_parity.json');
const cases = JSON.parse(fs.readFileSync(fixturePath, 'utf8')).cases;

for (const template of ['manga_detail.html', 'manga_reader.html']) {
  const source = fs.readFileSync(path.join(root, 'templates', template), 'utf8');
  const declaration = source.match(/const IDENTITY_SOURCE = s => [^;]+;/)?.[0];
  assert.ok(declaration, `${template} must define canonical manga identity`);
  const canonicalSource = vm.runInNewContext(`${declaration}; IDENTITY_SOURCE`);
  for (const item of cases) {
    assert.equal(canonicalSource(item.transport), item.identity,
      `${template}/${item.name} must match the shared identity fixture`);
  }
}

console.log(`漫画身份跨端共享夹具：${cases.length} cases × 2 Web entry points passed`);
