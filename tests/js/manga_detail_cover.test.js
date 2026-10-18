'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../../templates/manga_detail.html'), 'utf8');
const helper = html.match(/function detailCoverUrl\(\) \{[\s\S]*?\n\}/)[0];
for (const local of [true, false]) {
  const ctx = vm.createContext({LOCAL_CATALOG: local, detail: {cover: 'https://example.invalid/cover.jpg'}});
  vm.runInContext(helper, ctx);
  assert.equal(ctx.detailCoverUrl(), local ? '' : ctx.detail.cover);
  ctx.detail.cover_view = '/api/manga/source/comic/cover?catalog=local';
  assert.equal(ctx.detailCoverUrl(), ctx.detail.cover_view);
}
assert.match(html, /const coverUrl = detailCoverUrl\(\)/);
assert.match(html, /function viewCover\(\) \{\s*const url = detailCoverUrl\(\);\s*if \(!url\) return;/);
console.log('manga detail cover: local display stays offline; source metadata preserved');
