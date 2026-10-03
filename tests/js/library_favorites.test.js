'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'templates/library.html'), 'utf8');
const styles = html.match(/<style>([\s\S]*?)<\/style>/)?.[1] || '';

function functionSource(name) {
  const match = html.match(new RegExp(`function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`));
  assert.ok(match, `${name} must exist as a testable bookshelf behavior`);
  return match[0];
}

const helperNames = [
  'favoriteMangaDetailUrl',
  'favoriteMangaCardHTML',
  'favoriteMangaLocalStates',
  'mangaLibraryCardVisible',
  'mangaLibraryCardCanOpen',
];
const helpers = [...helperNames, 'favoriteMangaIdentityKey'].map(functionSource).join('\n');
const sandbox = vm.createContext({
  URLSearchParams, Math, Number, String, Set,
  esc(value) {
    return String(value).replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[ch]);
  },
});
vm.runInContext(`${helpers}\nglobalThis.__url = favoriteMangaDetailUrl;\n`
  + 'globalThis.__card = favoriteMangaCardHTML;\n'
  + 'globalThis.__locals = favoriteMangaLocalStates;\n'
  + 'globalThis.__libraryVisible = mangaLibraryCardVisible;\n'
  + 'globalThis.__libraryCanOpen = mangaLibraryCardCanOpen;', sandbox);

const comic = {
  source: 'copymanga_web', comic_id: 'story-1', title: '作品 <script>',
  cover: 'https://example.test/cover.jpg', unread_count: 6,
  source_name: '拷贝漫画', update_time: '2026-10-02',
};
const localUrl = new URL(sandbox.__url(comic, true), 'https://app.test');
assert.equal(localUrl.searchParams.get('catalog'), 'local',
  'a favorite with verified local image data must open the strict local catalog');
assert.equal(localUrl.searchParams.get('source'), 'copymanga_web');
assert.equal(localUrl.searchParams.get('id'), 'story-1');
assert.equal(localUrl.searchParams.get('title'), '作品 <script>');

const onlineUrl = new URL(sandbox.__url(comic, false), 'https://app.test');
assert.equal(onlineUrl.searchParams.has('catalog'), false,
  'a favorite without local pages must open the full online catalog');

const localStates = sandbox.__locals([
  {source: 'copymanga', comic_id: 'story-1', local_downloaded_chapters: 3},
  {source: 'mangadex', comic_id: 'empty', local_downloaded_chapters: 0,
    local_catalog_pending: false},
  {source: 'mangadex', comic_id: 'pending', local_downloaded_chapters: null,
    local_catalog_pending: true},
]);
assert.equal(localStates.get('copymanga_web:story-1'), 'local',
  'CopyManga source aliases must resolve to the locally downloaded identity');
assert.equal(localStates.get('mangadex:empty'), 'online',
  'an online-only favorite must keep its full catalog');
assert.equal(localStates.get('mangadex:pending'), 'pending',
  'unknown local state must never be guessed as online or locally readable');

const pendingLegacy = {
  source: 'copymanga', comic_id: 'legacy-book', images: 28,
  local_images: 0, local_scan_pending: true,
};
assert.equal(sandbox.__libraryVisible(pendingLegacy), true,
  'legacy records awaiting the first real-media scan must remain visible');
assert.equal(sandbox.__libraryCanOpen(pendingLegacy), false,
  'an unverified record must not open an empty local reader');
assert.equal(sandbox.__libraryVisible({...pendingLegacy, local_scan_pending: false,
  status: 'done', local_images: 12}), true,
'a verified local manga stays in the shelf');
assert.equal(sandbox.__libraryCanOpen({...pendingLegacy, local_scan_pending: false,
  status: 'done', local_images: 12}), true,
  'a successful scan restores the local reading entry');
const shelfLoader = html.match(/async function loadMangaLibrary\(\) \{[\s\S]*?\n\}\nasync function loadBooks/)?.[0];
assert.ok(shelfLoader, 'manga bookshelf must keep a testable loading flow');
assert.match(shelfLoader, /\.filter\(mangaLibraryCardVisible\)/,
  'pending legacy records must flow through the shelf renderer instead of being filtered out');
assert.match(shelfLoader, /if \(!mangaLibraryCardCanOpen\(c\)\)/,
  'the shelf click handler must enforce the unverified-local-state guard');

// Exercise the real shelf loader across the key asynchronous state transition:
// a download can become done between two polls without changing comic identity.
const loader = shelfLoader.slice(0, shelfLoader.lastIndexOf('\nasync function loadBooks'));
const shelfRenderedCards = [];
let shelfHtml = '';
const shelfBox = {
  get innerHTML() { return shelfHtml; },
  set innerHTML(value) {
    shelfHtml = value;
    if (value === '') shelfRenderedCards.length = 0;
  },
  appendChild(node) { shelfRenderedCards.push(node); },
};
const shelfReplies = [
  {comics: [{source: 'mangadex', comic_id: 'download-transition',
    title: '状态跃迁测试', source_name: 'MangaDex', status: 'downloading',
    dl_done: 1, dl_total: 2, dl_images: 4, local_images: 4,
    downloaded_at: '2026-10-03 10:00:00'}]},
  {comics: [{source: 'mangadex', comic_id: 'download-transition',
    title: '状态跃迁测试', source_name: 'MangaDex', status: 'done',
    dl_done: 2, dl_total: 2, dl_images: 8, local_images: 8,
    downloaded_at: '2026-10-03 10:00:00'}]},
  {comics: [{source: 'mangadex', comic_id: 'missing-pages',
    title: '缺页状态测试', source_name: 'MangaDex', status: 'done',
    missing_images: true, local_images: 3,
    downloaded_at: '2026-10-03 10:00:00'}]},
];
const shelfContext = vm.createContext({
  document: {
    getElementById(id) {
      if (id === 'manga-books') return shelfBox;
      return {value: ''};
    },
    createElement() {
      return {style: {}, children: [], classList: {add() {}, remove() {}},
        setAttribute() {}, appendChild(node) { this.children.push(node); }};
    },
  },
  fetch: async () => ({ok: true, json: async () => shelfReplies.shift()}),
  encodeURIComponent, URL, URLSearchParams, JSON, Date, Math, Number, String,
  AbortController, setTimeout, clearTimeout,
  esc(value) {
    return String(value).replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[ch]);
  },
  alert() {}, confirm() { return false; }, toast() {}, location: {href: ''},
});
vm.runInContext(`
  let mangaSort = 'downloaded';
  let _mangaSig = null;
  function mangaLibraryCardVisible(c) {
    return c.local_scan_pending === true || c.status === 'done' || c.status === 'downloading';
  }
  function mangaLibraryCardCanOpen(c) {
    return c.local_scan_pending !== true && c.status !== 'downloading' && !c.missing_images;
  }
  function sortList(list, _mode, getDownloaded) {
    return list.slice().sort((a, b) => getDownloaded(b) - getDownloaded(a));
  }
  function tsOf(s) { const t = Date.parse(String(s || '').replace(' ', 'T')); return isNaN(t) ? 0 : t / 1000; }
  ${loader}
  globalThis.__loadMangaLibrary = loadMangaLibrary;
`, shelfContext);

const localCard = sandbox.__card(comic, 'local');
assert.match(localCard, /未读 6 话/, 'cover badge must show the actual unread chapter count');
assert.match(localCard, /已缓存 · 仅显示本地目录/);
assert.match(localCard, /作品 &lt;script&gt;/, 'favorite title must be HTML escaped');
assert.match(localCard, /favorite-unread-badge/);
const onlineCard = sandbox.__card({...comic, unread_count: 0}, 'online');
assert.doesNotMatch(onlineCard, /未读 0 话/, 'zero unread must not create a misleading alert badge');
assert.match(onlineCard, /仅收藏 · 查看完整在线目录/);
const pendingCard = sandbox.__card(comic, 'pending');
assert.match(pendingCard, /role="status"[^>]*本地内容核验中/);
assert.match(pendingCard, /正在核验本地内容/);
assert.doesNotMatch(pendingCard, /<a href=/,
  'pending local identity must not send users into an empty local or online catalog');
assert.match(styles, /\.favorite-cover-wrap img\[hidden\][^}]*display:\s*none/,
  'a failed cover image must respect its hidden state');
assert.match(styles, /\.favorite-cover-wrap \.cover-placeholder\[hidden\][^}]*display:\s*none/,
  'the fallback placeholder must stay hidden until the cover actually fails');

assert.match(html, /<div id="favorite-manga-books" class="favorite-manga-grid"/,
  'the user bookshelf must contain a dedicated favorites shelf');
assert.match(html, /fetch\('\/api\/manga\/favorites'\)/,
  'favorites must be loaded from the shared manga favorites API');
assert.match(html, /local_downloaded_chapters/,
  'favorite navigation must use the same verified complete-chapter count as local catalogs');
assert.match(html, /local_catalog_pending/,
  'legacy/unscanned snapshots must be rendered as pending instead of guessed');
assert.match(html, /c\.local_scan_pending/,
  'the bookshelf must not present stale index image totals as verified local files');
assert.match(html, /正在核验本地文件/,
  'an unverified download record must have an explicit pending state');
assert.match(html, /manga-favorite-update-check:' \+ instanceId/,
  'favorite updates must be checked once per engine instance in a Web session');
assert.match(html, /loadFavoriteManga\(\)\.then\(favorites =>/,
  'opening the bookshelf must load favorites and start the update check');
const updateCheck = functionSource('checkFavoriteMangaUpdates');
assert.ok(updateCheck.indexOf('currentCheck.running') < updateCheck.indexOf('sessionStorage.getItem(attemptKey)'),
  'opening the bookshelf must resume a running check before applying the per-session dedupe key');
assert.match(html, /本地下载和阅读记录会保留/,
  'removing a favorite must not imply deleting downloaded pages or reading history');

// A favorites API failure must remain actionable and never render a fake empty collection.
const loadSource = html.match(/async function loadFavoriteManga\([^)]*\) \{[\s\S]*?\n\}/)?.[0];
assert.ok(loadSource, 'loadFavoriteManga must remain a testable async behavior');
const loadBox = {innerHTML: ''};
const failureContext = vm.createContext({
  URLSearchParams, Math, Number, String, Set,
  document: {getElementById: id => id === 'favorite-manga-books' ? loadBox : null},
  fetch: async () => ({ok: false, status: 503,
    json: async () => ({error: '收藏暂不可读取'})}),
  esc(value) {
    return String(value).replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[ch]);
  },
});
vm.runInContext(`let _favoriteMangaSig = ''; let _favoriteMangaLoadSeq = 0;
  let _favoriteMangaPendingTimer = 0; let _favoriteMangaPendingRetries = 0;
  ${helpers}\n${loadSource}\nglobalThis.load = loadFavoriteManga;`, failureContext);
failureContext.load().then(async () => {
  await shelfContext.__loadMangaLibrary();
  assert.equal(shelfRenderedCards.length, 1);
  assert.match(shelfRenderedCards[0].children[0].innerHTML, /下载中 1\/2 话/,
    'the first bookshelf poll must display the active download state');
  await shelfContext.__loadMangaLibrary();
  assert.equal(shelfRenderedCards.length, 1);
  assert.match(shelfRenderedCards[0].children[0].innerHTML, /已下载 · 8 图/,
    'the next poll must redraw the same comic as downloaded after the index refresh');
  await shelfContext.__loadMangaLibrary();
  assert.match(shelfRenderedCards[0].children[0].innerHTML, /⚠ 缺页/,
    'an incomplete local comic must not simultaneously carry the complete-download badge');
  assert.match(shelfRenderedCards[0].children[0].innerHTML, /本地图片缺失/,
    'the missing-page badge must agree with the card recovery message');

  assert.match(loadBox.innerHTML, /收藏暂不可读取/,
    'an unavailable favorites API must produce an actionable error');
  assert.match(loadBox.innerHTML, /onclick="loadFavoriteManga\(\)"/,
    'the favorites error must offer a retry');

  const pending = [];
  const raceBox = {innerHTML: '', children: [], appendChild(node) { this.children.push(node); }};
  const raceContext = vm.createContext({
    URLSearchParams, Math, Number, String, Set,
    document: {
      getElementById: id => id === 'favorite-manga-books' ? raceBox : null,
      createElement: () => ({
        className: '', innerHTML: '',
        querySelector: () => ({addEventListener() {}}),
      }),
    },
    fetch: url => new Promise(resolve => pending.push({url, resolve})),
    esc: failureContext.esc,
  });
  vm.runInContext(`let _favoriteMangaSig = ''; let _favoriteMangaLoadSeq = 0;
    let _favoriteMangaPendingTimer = 0; let _favoriteMangaPendingRetries = 0;
    ${helpers}\n${loadSource}\nglobalThis.load = loadFavoriteManga;`, raceContext);
  const oldLoad = raceContext.load();
  const newLoad = raceContext.load();
  const okResponse = data => ({ok: true, status: 200, json: async () => data});
  pending[1].resolve(okResponse({favorites: [{...comic, title: '新快照',
    local_downloaded_chapters: 0, local_catalog_pending: false}]}));
  await newLoad;
  pending[0].resolve(okResponse({favorites: [{...comic, title: '过期旧快照',
    local_downloaded_chapters: 0, local_catalog_pending: false}]}));
  await oldLoad;
  const renderedCards = raceBox.children.map(card => card.innerHTML).join('\n');
  assert.match(renderedCards, /新快照/,
    'a slower stale favorite response must not replace the latest refresh');
  assert.doesNotMatch(renderedCards, /过期旧快照/,
    'out-of-order concurrent loads must be ignored after a newer snapshot renders');
  console.log('library_favorites.test.js: bookshelf favorites, unread badge, local/online catalog split passed');
}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
