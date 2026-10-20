const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = {window: {}, document: {getElementById: () => null}};
vm.runInNewContext(fs.readFileSync('static/js/novel_explore.js', 'utf8'), context);
const {categoriesInOrder, checkedBooks} = context.window.NovelExploreData;
const first = {title: '同名', group: '排行', url: '/first%2Fpart'};
const second = {title: '同名', group: '排行', url: '/second'};
const third = {title: '<script>', group: '主题', url: '/third'};
const fourth = {title: '第四', group: '排行', url: '/fourth'};
const grouped = categoriesInOrder([first, second, third, fourth]);
assert.deepEqual(Array.from(grouped, g => g.name), ['排行', '主题', '排行']);
assert.equal(grouped[0].entries[0], first);
assert.equal(grouped[0].entries[1], second);
assert.equal(grouped[2].entries[0], fourth);
const row = {name: '100%作品', book_url: 'https://example.test/id%2Fpart', source_uid: 's'};
const other = {...row, book_url: 'https://example.test/another'};
const result = checkedBooks({page: 2, books: [row, {...row}, other]}, 's', 2);
assert.equal(result.length, 2);
assert.equal(result[0], row);
assert.equal(result[1], other, 'same name at distinct addresses must remain distinct');
for (const bad of [{}, {ok: false, page: 2, books: []}, {page: '2', books: []}, {page: 1, books: []},
  {page: 2, books: [{}]}, {page: 2, books: [{...row, source_uid: 'wrong'}]}]) {
  assert.throws(() => checkedBooks(bad, 's', 2));
}
assert.equal(checkedBooks({page: 1, books: []}, 's', 1).length, 0);
console.log('novel explore: contiguous groups, exact identities, per-page dedup and malformed responses passed');
