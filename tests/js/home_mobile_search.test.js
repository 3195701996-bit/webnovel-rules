const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'templates/index.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'static/css/style.css'), 'utf8');

const options = html.match(/<details class="search-options" open>([\s\S]*?)<\/details>/);
assert.ok(options, 'advanced search controls must have a native disclosure container');
assert.match(html, /const searchOptionsViewport = window\.matchMedia\('\(max-width: 768px\)'\)[\s\S]*?searchOptions\.open = !searchOptionsViewport\.matches;/,
  'responsive disclosure must stay open on desktop and closed on mobile');
assert.match(options[1], /<summary>筛选与排序<\/summary>/,
  'advanced filters must have a clear, keyboard-accessible toggle');
assert.match(options[1], /id="search-type"/, 'type filter must retain its public DOM identity');
assert.match(options[1], /id="search-sort"/, 'sort filter must retain its public DOM identity');
assert.ok(!html.slice(html.indexOf('class="search-bar"'), html.indexOf('</div>', html.indexOf('class="search-bar"')))
  .includes('id="search-type"'), 'no search behavior may depend on filters being direct children');

assert.match(css, /\.hero \.search-options\s*\{\s*display:\s*contents;/,
  'desktop must keep the existing one-row control layout');
assert.match(css, /\.hero \.search-options\s*\{\s*display:\s*flex;\s*flex-direction:\s*column;/,
  'mobile filters must stack only when the disclosure is opened');
assert.match(css, /\.hero \.search-options:not\(\[open\]\) > select\s*\{\s*display:\s*none;/,
  'mobile advanced selects must actually leave the layout while the disclosure is closed');
assert.match(css, /\.topbar\s*\{\s*position:\s*relative;/,
  'mobile global navigation must scroll away instead of consuming a sticky viewport band');
assert.match(css, /\.topbar \.logo\s*\{\s*display:\s*none;/,
  'mobile navigation must give its destinations the available width');
assert.match(css, /\.topbar nav::-webkit-scrollbar-thumb\s*\{\s*background:\s*var\(--accent\)/,
  'horizontal navigation overflow must have a visible affordance');
assert.match(css, /\.topbar nav a\s*\{[^}]*min-height:\s*44px/s,
  'mobile nav links must remain comfortably tappable');

console.log('home_mobile_search.test.js: mobile navigation and progressive filter contracts passed');
