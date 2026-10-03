const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '../..');
const css = fs.readFileSync(path.join(root, 'static/css/style.css'), 'utf8');
const manga = fs.readFileSync(path.join(root, 'templates/manga.html'), 'utf8');
const theme = fs.readFileSync(
  path.join(root, 'android/app/src/main/java/com/webnovel/mobile/WnTheme.kt'), 'utf8');
const novelDetail = fs.readFileSync(path.join(root, 'templates/novel_detail.html'), 'utf8');
const library = fs.readFileSync(path.join(root, 'templates/library.html'), 'utf8');
const reader = fs.readFileSync(path.join(root, 'templates/reader.html'), 'utf8');
const sources = fs.readFileSync(path.join(root, 'templates/sources.html'), 'utf8');
const mangaDetail = fs.readFileSync(path.join(root, 'templates/manga_detail.html'), 'utf8');
const mangaReader = fs.readFileSync(path.join(root, 'templates/manga_reader.html'), 'utf8');
const tasks = fs.readFileSync(path.join(root, 'templates/tasks.html'), 'utf8');
const mangaSearch = fs.readFileSync(path.join(root, 'templates/manga.html'), 'utf8');
const home = fs.readFileSync(path.join(root, 'templates/index.html'), 'utf8');
const login = fs.readFileSync(path.join(root, 'templates/login.html'), 'utf8');

const webTokens = {
  '--bg-base': 'bg',
  '--bg-elevated': 'surface',
  '--bg-overlay': 'surfaceVar',
  '--border': 'line',
  '--text-primary': 'ink',
  '--text-secondary': 'inkDim',
  '--accent': 'accent',
  '--accent-2': 'info',
  '--green': 'ok',
  '--red': 'danger',
};

for (const [webToken, androidToken] of Object.entries(webTokens)) {
  const web = css.match(new RegExp(`${webToken}:\\s*(#[0-9a-fA-F]{6})\\b`));
  const native = theme.match(new RegExp(`val ${androidToken} = Color\\(0xFF([0-9a-fA-F]{6})\\)`));
  assert.ok(web, `web theme must define ${webToken}`);
  assert.ok(native, `Android theme must define ${androidToken}`);
  assert.equal(web[1].toLowerCase(), `#${native[1].toLowerCase()}`,
    `${webToken} must match Android ${androidToken}`);
}

assert.match(css, /--bg-base:\s*#f5f1e9/i,
  'daily-use screens use a warm paper canvas rather than a dark control-panel background');
assert.match(theme, /lightColorScheme\(/,
  'Android and Web share the editorial light surface by default');
assert.match(css, /--accent:\s*#a84f35/i,
  'primary actions use a restrained terracotta accent');
assert.match(css, /--on-accent:\s*#fffaf5/i,
  'primary action foreground is explicit and readable on terracotta');

assert.match(css, /--fs-base:\s*15px/, 'web reading and control text must not fall back to dense 14px base');
assert.match(css, /\.topbar\s*\{[^}]*background:[^;]*var\(--bg-elevated\)/s,
  'shared navigation must use a theme surface token');
assert.match(css, /\.card\s*\{[^}]*background:\s*var\(--bg-card\)/s,
  'shared content cards must use a theme surface token');
assert.match(css, /\.btn\.primary\s*\{[^}]*background:\s*var\(--accent\)/s,
  'primary action must use the single accent color');
assert.doesNotMatch(css.match(/\.btn\.primary\s*\{[^}]*\}/s)?.[0] || '', /gradient/i,
  'primary actions must not use the old console-like gold gradient');
assert.match(manga, /\.mc-tag\s*\{[^}]*background:\s*var\(--accent-2-soft\)[^}]*color:\s*var\(--accent-2\)/s,
  'comic metadata chips must use the shared muted information palette');
assert.doesNotMatch(manga, /rgba\(90,120,255|#8ab4ff/i,
  'comic search cards must not retain the old saturated-blue debug palette');
assert.match(css, /\.status-badge--done[\s\S]*?var\(--green-soft\)/,
  'library completion badges must use shared semantic status tokens');
assert.doesNotMatch(css, /\.status-badge[^\n]*#(?:2563eb|dc2626|2d7d46)/i,
  'library status colors must not revert to hard-coded dashboard colors');
assert.match(novelDetail, /\.fn-continue\s*\{[^}]*color:#fffaf5/s,
  'novel detail primary action must retain readable text on the new terracotta surface');
assert.match(novelDetail, /\.chap-sec\s*\{[^}]*border:1px solid var\(--border\)/s,
  'novel detail chapter list must use the shared paper surface and border');
assert.doesNotMatch(novelDetail, /#(?:1c2230|9b7bd6)|rgba\(232,179,75/i,
  'novel detail must not retain the former dark-card/purple/gold palette');
assert.match(novelDetail, /@media\s*\(max-width:768px\)[\s\S]*?\.op-btn\s*\{[^}]*min-height:44px/s,
  'novel detail secondary actions must remain comfortably tappable on mobile');
assert.match(library, /\.manga-library-card\s*\{[^}]*background:var\(--bg-elevated\)/s,
  'downloaded manga cards must use the shared paper surface, not the old dark console surface');
assert.match(library, /\.manga-library-action\s*\{[^}]*min-height:44px/s,
  'manga shelf actions must remain touch-sized and usable on mobile');
assert.match(library, /document\.createElement\('button'\)[\s\S]*?manga-library-open/,
  'continue-reading entry must be a native keyboard-operable button');
const mangaShelfLoader = library.match(/async function loadMangaLibrary\(\) \{[\s\S]*?\n\}\nasync function loadBooks/)?.[0] || '';
assert.doesNotMatch(mangaShelfLoader, /#(?:1c2230|0d1117|e8833a|d6b86a|34a853|8ab4ff|e8b34b)/i,
  'manga shelf must not retain its former hard-coded dark/debug palette');
assert.match(reader, /html\[data-theme="dark"\]\s*\{[^}]*--bg-base:[^}]*--text-primary:/s,
  'night reading theme must change the shared semantic surface and text tokens together');
assert.match(reader, /html\[data-theme="sepia"\]\s*\{[^}]*--bg-elevated:[^}]*--text-secondary:/s,
  'eye-care theme must theme both elevated surfaces and secondary text');
assert.match(reader, /let settings = \{ fs: 17, lh: 1\.9, theme: 'light' \}/,
  'new reading preferences must default to the shared paper theme');
assert.match(reader, /id="desktop-settings"[\s\S]*?aria-controls="m-sheet"/,
  'desktop reader must expose the same accessible reading settings as mobile');
assert.match(reader, /document\.addEventListener\('keydown',[\s\S]*?event\.key === 'Escape'/,
  'reading settings can be dismissed with Escape');
assert.match(reader, /m-theme-group button'[\s\S]*?\n  \}\);\n  document\.querySelectorAll\('#m-lh-group button'/,
  'theme and line-height handlers must initialize independently');
assert.match(sources, /class="src-filter"[\s\S]*?<\/select>\s*<\/div>\s*<div class="batch-bar"/,
  'source filters and batch actions must be separate toolbars');
assert.match(sources, /<div class="container">[\s\S]*?<div id="source-list"[\s\S]*?<\/div>\s*<\/div>\s*<script/s,
  'source list must remain inside the responsive page container');
assert.match(sources, /class="source-check-status" role="status" aria-live="polite"/,
  'source self-check progress must be announced to assistive technology');
assert.match(sources, /<button class="btn small source-retry" type="button"/,
  'source load recovery must use a native button');
assert.doesNotMatch(sources, /onclick="loadSources\(\)"|#(?:34a853|ff9d5c|f66)\b|#1c2230/i,
  'source management must not retain inline retry handlers or debug palette colors');
assert.match(mangaDetail, /\.op-read\s*\{[^}]*color:var\(--on-accent\)/s,
  'manga detail primary action must use the shared accessible accent foreground');
assert.match(mangaDetail, /\.history-tip\s*\{[^}]*background:var\(--accent-soft\)/s,
  'reading-history feedback must use the shared accent surface');
assert.match(mangaReader, /\.toc-item\.active\s*\{[^}]*background:var\(--accent-soft\)/s,
  'reader selection must stay in the shared editorial palette');
for (const [name, source] of Object.entries({mangaDetail, mangaReader, tasks, library})) {
  assert.doesNotMatch(source, /#(?:8ab4ff|e8b34b|e8833a|34a853|0d1117|f66)\b|rgba\(242,170,76|rgba\(232,179,75/,
    `${name} must not retain hard-coded former debug palette colors`);
}
for (const [name, source] of Object.entries({home, mangaSearch, login, css})) {
  assert.doesNotMatch(source, /#(?:0f1216|10151b|171e26|1c2230|242e3b)\b/i,
    `${name} must not carry a stale dark-theme fallback that leaks into themed surfaces`);
}

console.log('visual_system.test.js: shared Web/Android color and hierarchy contracts passed');
