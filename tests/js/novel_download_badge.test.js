'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'templates/library.html'), 'utf8');
const helper = html.match(/function novelDownloadBadge\(b\) \{[\s\S]*?\n\}/)[0];
const context = vm.createContext({});
vm.runInContext(helper, context);
const cases = JSON.parse(fs.readFileSync(path.join(root,
  'android/app/src/test/resources/novel_download_status.json'), 'utf8'));
for (const row of cases) {
  assert.equal(context.novelDownloadBadge(row)[0], row.label);
}
assert.match(html, /const nb = novelDownloadBadge\(b\)/);
assert.doesNotMatch(html, /b\.percent >= 100 \? \['✓ 完结'/);
console.log('novel download badges: shared count-based complete/partial/failed/empty cases passed');
