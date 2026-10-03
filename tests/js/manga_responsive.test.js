const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const css = fs.readFileSync(path.join(__dirname, '../../static/css/style.css'), 'utf8');
const detail = fs.readFileSync(path.join(__dirname, '../../templates/manga_detail.html'), 'utf8');
const reader = fs.readFileSync(path.join(__dirname, '../../templates/manga_reader.html'), 'utf8');
const mangaSearch = fs.readFileSync(path.join(__dirname, '../../templates/manga.html'), 'utf8');
for (const page of ['manga.html', 'manga_detail.html', 'manga_download.html', 'manga_reader.html']) {
  const html = fs.readFileSync(path.join(__dirname, '../../templates', page), 'utf8');
  assert.match(html, /<link\s+rel="icon"\s+href="data:,">/,
    `${page} must explicitly suppress a missing favicon network request`);
}

assert.match(css, /#manga-search-row \.input\s*\{[^}]*min-height:\s*44px/s,
  'manga search controls must retain accessible touch height');
assert.match(mangaSearch, /<body\s+class="manga-page"/,
  'manga search must opt into content-first navigation scrolling');
assert.match(css, /html:has\(body\.manga-page\),\s*body\.manga-page\s*\{\s*overflow-x:\s*clip;\s*\}/,
  'manga page must clip horizontal overflow without making the root a scroll container');
assert.match(css, /\.manga-page\s+\.topbar\s*\{\s*position:\s*relative;\s*\}/,
  'manga top navigation must scroll away with long search results on desktop too');
assert.match(css, /@media\s*\(max-width:\s*768px\)[\s\S]*?#manga-search-row\s*\{[^}]*display:\s*grid\s*!important/s,
  'mobile manga search controls must use the compact grid');
assert.match(css, /grid-template-columns:\s*minmax\(0,\s*1fr\)\s+minmax\(0,\s*1fr\)/,
  'mobile manga search controls must fit two columns without intrinsic overflow');
assert.match(css, /#manga-search-row #q\s*\{[^}]*grid-column:\s*1\s*\/\s*-1/s,
  'manga query input must span both mobile columns');
assert.match(detail, /\.op-btn\s*\{[^}]*min-height:\s*44px/s,
  'manga detail secondary actions must have a 44px touch target');
assert.match(detail, /\.chap-item\s*\{[^}]*min-height:\s*44px/s,
  'manga chapter rows must have a 44px touch target');
assert.match(detail, /id="dl-btn"[^>]*>↓ 下载</,
  'primary download action must use a widely supported arrow glyph');
assert.match(detail, /class="batch-dl"[^>]*>↓ 批量下载</,
  'batch download action must not use a font-dependent specialty symbol');
assert.doesNotMatch(detail, /[\u2b07\u2b73]/,
  'download labels must avoid down-arrow glyphs that render as tofu in common browser fonts');
assert.doesNotMatch(reader, /[\u2b07\u2b73]/,
  'reader download controls must avoid down-arrow glyphs that render as tofu in common browser fonts');
assert.match(css, /@media\s*\(max-width:\s*380px\)[\s\S]*?\.reader-top \.toc-btn\s*\{[^}]*white-space:\s*nowrap/s,
  'reader toolbar controls must stay on one line on narrow screens');
assert.match(reader, /\.reader-top \.back,\s*\.reader-top \.toc-btn,\s*\.reader-top \.nav-btn\s*\{[^}]*min-height:\s*44px/s,
  'mobile reader controls must retain an accessible 44px touch target');
assert.match(reader, /\.reader-top \.toc-btn\s*\{[^}]*white-space:\s*nowrap/s,
  'reader directory button must not wrap its label');
assert.match(reader, /#reader-chapter-label\s*\{[^}]*overflow:\s*hidden;[^}]*text-overflow:\s*ellipsis/s,
  'long chapter names in the floating progress bar must not force horizontal overflow');
assert.match(reader, /@media\s*\(max-width:\s*360px\)[\s\S]*?#reader-chapter-label\s*\{[^}]*display:\s*none/s,
  'very narrow phones must prioritize the seek slider over a duplicate chapter label');

console.log('manga_responsive.test.js: ALL ASSERTIONS PASSED');
