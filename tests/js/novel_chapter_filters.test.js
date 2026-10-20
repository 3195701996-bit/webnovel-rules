const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = {window: {}};
vm.runInNewContext(fs.readFileSync('static/js/novel_chapter_filters.js', 'utf8'), context);
const filter = context.window.filterNovelChapters;
const chapters = [{index: 1, name: '第一章 魔法 世界', downloaded: true},
  {index: 12, name: '卷二 第一章 魔法', downloaded: false, failed: true},
  {index: 112, name: '第112章 世界', downloaded: true}];
const before = JSON.stringify(chapters);
assert.equal(filter(chapters, '００１２', 'all')[0], chapters[1]);
assert.equal(filter(chapters, '0', 'all').length, 0);
assert.equal(filter(chapters, '99999999999999999999999999', 'all').length, 0);
assert.equal(filter(chapters, '魔法　世界', 'all')[0], chapters[0]);
assert.deepEqual(Array.from(filter(chapters, '第一章', 'all'), c => c.index), [1, 12]);
assert.equal(filter(chapters, '', 'missing')[0], chapters[1]);
assert.deepEqual(Array.from(filter(chapters, '', 'downloaded'), c => c.index), [1, 112]);
assert.equal(filter(chapters, '12', 'downloaded').length, 0);
assert.equal(filter(chapters, '  ', 'all'), chapters, 'unfiltered startup must reuse the original directory');
assert.equal(JSON.stringify(chapters), before);
console.log('novel chapter filters: Android-equivalent exact index, NFKC, words, state, identity and default fast path passed');
