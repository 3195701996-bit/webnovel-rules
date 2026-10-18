'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('templates/manga_reader.html', 'utf8');
const current = html.match(/function currentPage\(\) \{[\s\S]*?\n\}/)?.[0];
const documentTop = html.match(/function readerPageDocumentTop\([^)]*\) \{[\s\S]*?\n\}/)?.[0];
const offset = html.match(/function readerScrollOffsetForPage\([^)]*\) \{[\s\S]*?\n\}/)?.[0];
assert.ok(current && documentTop && offset);
const window = {scrollY: 308, innerHeight: 844};
let starts = [50.1, 603.6666667, 1123.4];
let reads = 0;
const wraps = starts.map((_, i) => ({
  // Deliberately inconsistent legacy integer metrics reproduce the old bug.
  offsetTop: [50, 603, 1123][i], offsetHeight: [554, 520, 500][i],
  getBoundingClientRect() { reads++; return {top: starts[i] - window.scrollY}; },
}));
const document = {documentElement: {scrollHeight: 3000}, querySelectorAll: () => wraps};
const ctx = vm.createContext({window, document, Math});
vm.runInContext(`${current}; ${documentTop}; ${offset};`, ctx);
assert.equal(ctx.currentPage(), 2, 'fractional mobile boundary must not revert to page1');
assert.equal(ctx.readerPageDocumentTop(wraps[1]), starts[1], 'seek retains fractional document coordinates');
window.scrollY = 1000;
assert.equal(ctx.readerPageDocumentTop({getBoundingClientRect() {
  window.scrollY = 2000; // A synchronous layout flush adjusts browser scroll anchoring.
  return {top:-1500};
}}),500,'document coordinate must use scrollY after layout, not a stale pre-layout value');
for (const height of [667, 844, 845, 900, 901]) {
  window.innerHeight = height;
  for (const top of [500.1, 603.6666667, 1001.9]) {
    starts = [50.1, top, top + 900];
    window.scrollY = Math.round(ctx.readerScrollOffsetForPage(top, height));
    assert.equal(ctx.currentPage(), 2, `rounded seek must agree: ${top}/${height}`);
  }
}
window.innerHeight = 844;
starts = [50.1, 603.6666667, 1123.4];
window.scrollY = 307;
assert.equal(ctx.currentPage(), 1, 'outside half-pixel tolerance must not advance early');
window.scrollY = 0;
starts = [10, 100, 200];
assert.equal(ctx.currentPage(), 1, 'document start remains first page even with short images');
window.scrollY = 501;
document.documentElement.scrollHeight = 1345;
starts = [10, 400, 1200];
assert.equal(ctx.currentPage(), 3, 'bottom-clamped seek can reach a short final page');
window.scrollY = 1;
document.documentElement.scrollHeight = 100000;
starts = [0, 1000, 2000];
reads = 0;
assert.equal(ctx.currentPage(), 1);
assert.ok(reads <= 2, 'progress near start must not measure every later image');
window.scrollY = 4000 * 600 - window.innerHeight * .35;
document.documentElement.scrollHeight = 8192 * 600;
const longWraps = Array.from({length:8192}, (_, index) => ({getBoundingClientRect() {
  reads++; return {top: index * 600 - window.scrollY};
}}));
document.querySelectorAll = () => longWraps;
reads = 0;
assert.equal(ctx.currentPage(), 4001);
assert.ok(reads <= 13, '8192-page progress must use logarithmic layout reads');
document.querySelectorAll = () => [];
assert.equal(ctx.currentPage(), 1, 'loading/empty chapter has a safe initial page');
console.log('manga reader fractional seek: pixel rounding, short first/final pages and bounded layout reads passed');
