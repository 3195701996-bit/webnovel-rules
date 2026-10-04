'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '../..');
const reader = fs.readFileSync(path.join(root, 'templates/manga_reader.html'), 'utf8');
const detail = fs.readFileSync(path.join(root, 'templates/manga_detail.html'), 'utf8');
const download = fs.readFileSync(path.join(root, 'templates/manga_download.html'), 'utf8');
const styles = fs.readFileSync(path.join(root, 'static/css/style.css'), 'utf8');
const library = fs.readFileSync(path.join(root, 'templates/library.html'), 'utf8');
const mangaPage = fs.readFileSync(path.join(root, 'templates/manga.html'), 'utf8');
const libraryPage = fs.readFileSync(path.join(root, 'templates/library.html'), 'utf8');
const androidMain = fs.readFileSync(path.join(root,
  'android/app/src/main/java/com/webnovel/mobile/MainActivity.kt'), 'utf8');
const androidManga = fs.readFileSync(path.join(root,
  'android/app/src/main/java/com/webnovel/mobile/MangaScreens.kt'), 'utf8');
const androidSearch = fs.readFileSync(path.join(root,
  'android/app/src/main/java/com/webnovel/mobile/SearchScreens.kt'), 'utf8');

const optionalInfoRenderer = detail.match(/function renderOptionalInfo\([^)]*\)\s*\{[\s\S]*?\n\}/)?.[0];
assert.ok(optionalInfoRenderer,
  'optional manga metadata must use a focused, testable renderer');
const optionalInfoContext = vm.createContext({
  esc: value => String(value).replace(/[&<>"']/g, char =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char])),
});
vm.runInContext(`${optionalInfoRenderer}; globalThis.renderOptionalInfo = renderOptionalInfo;`, optionalInfoContext);
function renderOptionalInfo(value, label, labelClass) {
  const row = {hidden: false, innerHTML: 'stale'};
  optionalInfoContext.renderOptionalInfo(row, value, label, labelClass);
  return row;
}
assert.deepEqual({...renderOptionalInfo('', '浏览量', 'lbl-views')},
  {hidden: true, innerHTML: ''}, 'empty optional metadata must not leave an empty outlined row');
assert.deepEqual({...renderOptionalInfo('   ', '更新时间', 'lbl-time')},
  {hidden: true, innerHTML: ''}, 'whitespace-only optional metadata must be hidden');
assert.deepEqual({...renderOptionalInfo('<script>', '浏览量', 'lbl-views')},
  {hidden: false, innerHTML: '<span class="info-label lbl-views">浏览量</span><span class="info-val">&lt;script&gt;</span>'},
  'present optional metadata must render escaped content');

const mangaCardFactory = mangaPage.match(/function makeMangaCard\(c\) \{[\s\S]*?\n\}/)?.[0];
assert.ok(mangaCardFactory, 'manga search must keep a dedicated card renderer');
assert.match(styles, /\.manga-card \.cover-wrap\.cover-missing::after\s*\{\s*opacity:\s*1;/,
  'missing or failed search covers must reveal the cover placeholder layer');
assert.doesNotMatch(styles, /\.cover-wrap img\.error \+\s*::after/,
  'cover placeholder must not use a pseudo-element sibling selector that can never match');
function renderMangaCard(cover) {
  const card = {attributes: {}, setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener() {}};
  const sandbox = {
    document: {createElement: () => card},
    esc: value => String(value || ''),
    window: {}, saveScrollPos() {}, pickMangaSource() {}, toast() {}, location: {},
  };
  vm.createContext(sandbox);
  vm.runInContext(mangaCardFactory + `; globalThis.card = makeMangaCard({title:"封面测试", cover:${JSON.stringify(cover)}, id:"c1", source:"mangadex", source_name:"MangaDex"});`,
    sandbox);
  return sandbox.card.innerHTML;
}
const noCoverCard = renderMangaCard('');
assert.match(noCoverCard, /class="cover-wrap cover-missing"/,
  'a result without a cover URL must activate the placeholder state');
assert.doesNotMatch(noCoverCard, /<img\b/,
  'a result without a cover URL must not issue an empty src request');
assert.match(renderMangaCard('https://covers.invalid/broken.jpg'),
  /onerror="[^"]*parentElement\.classList\.add\('cover-missing'\)/,
  'a failed cover request must activate the same visible placeholder state');

const catalogActions = detail.match(/function catalogActionState\([^)]*\)\s*\{[\s\S]*?\n\}/)?.[0];
const onlineCatalogUrl = detail.match(/function onlineCatalogUrl\([^)]*\)\s*\{[^}]*\}/)?.[0];
assert.ok(catalogActions && onlineCatalogUrl,
  'detail must define testable catalog-specific actions and online escape route');
const actionContext = vm.createContext({});
vm.runInContext(`${catalogActions}; ${onlineCatalogUrl}; globalThis.actionState = catalogActionState; globalThis.href = onlineCatalogUrl;`, actionContext);
assert.deepEqual({...actionContext.actionState(true, 2)}, {
  canRead: true, canDownload: false, showOnlineCatalog: true, emptyLocal: false,
}, 'partial local catalog must remain readable, local-only, and expose the full online catalog');
assert.deepEqual({...actionContext.actionState(true, 0)}, {
  canRead: false, canDownload: false, showOnlineCatalog: true, emptyLocal: true,
}, 'empty local catalog must not offer broken reading/download actions');
assert.deepEqual({...actionContext.actionState(false, 2)}, {
  canRead: true, canDownload: true, showOnlineCatalog: false, emptyLocal: false,
}, 'online detail must retain its normal read and download actions');
assert.equal(actionContext.href('copymanga/web', 'comic id/1'),
  '/manga_detail?source=copymanga%2Fweb&id=comic%20id%2F1',
  'online catalog link must preserve and encode exact source/comic identity');
class ActionNode {
  constructor() { this.hidden = false; this.disabled = false; this.attributes = {}; }
  setAttribute(name, value) { this.attributes[name] = value; }
}
const actionNodes = Object.fromEntries([
  'start-btn', 'continue-btn', 'dl-btn', 'local-catalog-note',
  'online-catalog-link', 'catalog-empty',
].map(id => [id, new ActionNode()]));
const batchButton = new ActionNode();
const domContext = vm.createContext({
  LOCAL_CATALOG: true, SOURCE: 'copymanga/web', CID: 'comic id/1',
  catalogActionState: actionContext.actionState,
  onlineCatalogUrl: actionContext.href,
  document: {
    getElementById: id => actionNodes[id],
    querySelector: selector => selector === '.batch-dl' ? batchButton : null,
  },
});
vm.runInContext(detail.match(/function updateCatalogActions\([^)]*\)\s*\{[\s\S]*?\n\}/)[0], domContext);
domContext.updateCatalogActions(0, '作品');
assert.equal(actionNodes['start-btn'].disabled, true);
assert.equal(actionNodes['continue-btn'].disabled, true);
assert.equal(actionNodes['dl-btn'].hidden, true);
assert.equal(batchButton.hidden, true);
assert.equal(actionNodes['local-catalog-note'].hidden, false);
assert.equal(actionNodes['catalog-empty'].hidden, false);
assert.equal(actionNodes['online-catalog-link'].href,
  '/manga_detail?source=copymanga%2Fweb&id=comic%20id%2F1');
assert.equal(actionNodes['online-catalog-link'].attributes['aria-label'], '查看作品的在线完整目录');
domContext.updateCatalogActions(2, '作品');
assert.equal(actionNodes['start-btn'].disabled, false,
  'partially downloaded local catalogs must retain local reading');
assert.equal(actionNodes['dl-btn'].hidden, true,
  'local details must not expose online download operations');
assert.equal(actionNodes['catalog-empty'].hidden, true);
domContext.LOCAL_CATALOG = false;
domContext.updateCatalogActions(3, '作品');
assert.equal(actionNodes['dl-btn'].hidden, false,
  'full online details must restore download actions');
assert.equal(actionNodes['local-catalog-note'].hidden, true);
assert.match(detail, /updateCatalogActions\(readingUnits\.length, detail\.title\)/,
  'catalog actions must derive from the same full or local units rendered in the TOC');
assert.match(detail, /continueBtn\.disabled = chapters\.length === 0/,
  'a history response must not re-enable continuation for an empty local catalog');
assert.match(detail, /if \(LOCAL_CATALOG \|\| !chapters\.length\) return;/,
  'local bookshelf detail must never start an online download action');
assert.match(detail, /查看在线完整目录/,
  'local bookshelf details need a direct route to the full catalog without deleting media');

assert.match(mangaPage, /if \(!r\.ok \|\| d\.recoverable\) throw new Error\(d\.error/,
  'favorite and history views must not render a corrupt identity store as an empty list');
assert.match(mangaPage, /收藏加载失败[\s\S]*?loadFavs\(\)/,
  'favorite load errors must include an actionable retry');
assert.match(mangaPage, /历史加载失败[\s\S]*?loadHist\(\)/,
  'history load errors must include an actionable retry');
assert.match(detail, /async function loadReadingHistory\(\)[\s\S]*?if \(!response\.ok \|\| data\.recoverable\)/,
  'detail must not treat a failed history request as an empty reading record');
assert.match(detail, /read_chapter_ids[\s\S]*?chapterReadIndices\.add\(index\)/,
  'detail TOC must mark every chapter resolved from the durable read identity set');
assert.match(detail, /async function loadFavState\(\)[\s\S]*?if \(!r\.ok \|\| d\.recoverable\)/,
  'detail must not turn a failed favorite request into a non-favorite state');
const historyLoader = detail.match(/async function loadReadingHistory\(\) \{[\s\S]*?\n\}/)?.[0];
assert.ok(historyLoader, 'detail must expose a bounded reading-history loader');
assert.match(historyLoader, /continueBtn\.disabled = true[\s\S]*?readingHistoryReady = true[\s\S]*?continueBtn\.disabled = chapters\.length === 0/,
  'continue-reading must wait for a successful history read and stay disabled for an empty catalog');
assert.match(historyLoader, /catch[\s\S]*?continueBtn\.disabled = true/,
  'continue-reading must remain disabled when reading history fails');
assert.match(reader, /if \(!r\.ok \|\| d\.recoverable\)[\s\S]*?throw new Error/,
  'reader restore must retry recoverable HTTP failures without opening an empty history');
assert.match(reader, /showHistoryLoadError[\s\S]*?重试读取历史/,
  'reader must offer a manual retry when history remains unavailable');

const searchScreenAt = androidSearch.indexOf('internal fun MangaSearchScreen(');
assert.ok(searchScreenAt >= 0, 'Android manga search screen must remain discoverable');
const mangaSearchScreen = androidSearch.slice(searchScreenAt);
assert.match(mangaSearchScreen, /Scaffold\s*\{\s*pad\s*->\s*LazyColumn\(/,
  'Android manga search header and results must share one vertically scrolling container');
assert.equal((mangaSearchScreen.match(/LazyColumn\s*\(/g) || []).length, 1,
  'Android manga search must not introduce a second nested vertical lazy list');
for (const tag of ['manga_search_back', 'manga_search_field', 'manga_search_btn',
  'manga_src_all', 'manga_search_progress', 'manga_search_pager', 'manga_search_results']) {
  assert.ok(mangaSearchScreen.includes(`testTag("${tag}")`),
    `scrolling search page must keep ${tag} inside the shared screen composition`);
}

const match = reader.match(/function setReadingCatalog\(detail\) \{[\s\S]*?\n\}/);
assert.ok(match, 'reader must centralize the server catalog into its reading order');
assert.match(reader, /const nextChapterIsReadable = \(candidate\) => !LOCAL_CATALOG \|\|[\s\S]*?window\._dlSet\.has\(candidate && candidate\.id\)[\s\S]*?if \(i < chapters\.length - 1 && !window\._nextChImgs && nextChapterIsReadable\(chapters\[i \+ 1\]\)\)/,
  'strict local reading must not prefetch any chapter outside the verified downloaded catalog');

const chapterListRule = detail.match(/\.chap-list\s*\{([^}]*)\}/);
assert.ok(chapterListRule, 'manga detail must define its chapter list layout');
assert.doesNotMatch(chapterListRule[1], /max-height|overflow-y\s*:\s*(auto|scroll)/,
  'long chapter catalogs must use the page scroll, not a nested vertical scroller');
for (const rule of styles.matchAll(/\.chap-list\s*\{([^}]*)\}/g)) {
  assert.doesNotMatch(rule[1], /max-height|overflow-y\s*:\s*(auto|scroll)/,
    'responsive chapter-list rules must not restore nested vertical scrolling');
}

const ctx = {chapters: [], window: {}};
vm.createContext(ctx);
vm.runInContext(match[0], ctx);
ctx.setReadingCatalog({
  volumes: [{id: 'v1', name: '第01卷'}],
  chapters: [{id: 'c1', name: '第01话'}],
  downloaded: ['v1'],
});
assert.deepEqual(ctx.chapters.map(row => row.id), ['v1', 'c1']);
assert.deepEqual([...ctx.window._dlSet], ['v1']);

// Cross-platform golden ordering cases are consumed directly by Python and Android;
// run the actual Web catalog adapter against the same cases instead of a one-row sample.
const orderFixture = JSON.parse(fs.readFileSync(path.join(root,
  'android/app/src/test/resources/manga_chapter_order_parity.json'), 'utf8'));
for (const testCase of orderFixture.cases) {
  const webContext = {chapters: [], window: {}};
  vm.createContext(webContext);
  vm.runInContext(match[0], webContext);
  const rowById = new Map(testCase.chapters.map(row => [row.id, row]));
  const orderedRows = testCase.expected.map(id => rowById.get(id));
  const expectedVolumes = orderedRows.filter(row => row.kind === 'volume');
  const expectedChapters = orderedRows.filter(row => row.kind !== 'volume');
  webContext.setReadingCatalog({
    // The API has already applied the canonical order within each group.
    volumes: expectedVolumes,
    chapters: expectedChapters,
    downloaded: [],
  });
  assert.deepEqual(webContext.chapters.map(row => row.id), testCase.expected,
    `Web reader order must match the shared catalog contract: ${testCase.name}`);
}

const volumesAt = detail.indexOf('...vols.map((item, sourceIndex)');
const chaptersAt = detail.indexOf('...chs.map((item, sourceIndex)', volumesAt);
assert.ok(volumesAt >= 0 && chaptersAt > volumesAt,
  'the full reader index must place actionable volumes before single chapters');
assert.match(detail, /chapters\.push\(\.\.\.units\.map\(entry => entry\.item\)\)/,
  'all catalog units must retain stable reader indices before lazy rendering');
const batchRenderer = detail.match(/function renderChapterBatch\(\) \{[\s\S]*?\n\}/);
assert.ok(batchRenderer, 'long detail catalogs must render in bounded batches');
assert.match(detail, /const CHAPTER_BATCH_SIZE = 120/,
  'the web detail DOM must have a bounded initial chapter batch');
assert.match(batchRenderer[0], /IntersectionObserver/,
  'the next chapter batch should load near the end of the page scroll');
assert.match(batchRenderer[0], /btn\.onclick = \(\) => openReader\(idx\)/,
  'lazy-rendered rows must keep their stable complete-catalog reading index');
assert.match(batchRenderer[0], /state\.more\.textContent = `继续加载目录/,
  'a manual continuation control must remain as an accessibility/browser fallback');

class FakeNode {
  constructor(tag, fragment = false) {
    this.tag = tag;
    this.isFragment = fragment;
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.classes = new Set();
    this.classList = {add: name => this.classes.add(name)};
  }
  appendChild(node) {
    if (node.isFragment) {
      for (const child of node.children) {
        child.parentNode = this;
        this.children.push(child);
      }
      node.children = [];
    } else {
      node.parentNode = this;
      this.children.push(node);
    }
    return node;
  }
  insertBefore(node, reference) {
    const at = this.children.indexOf(reference);
    const nodes = node.isFragment ? node.children.splice(0) : [node];
    for (const child of nodes) child.parentNode = this;
    this.children.splice(at < 0 ? this.children.length : at, 0, ...nodes);
  }
  setAttribute() {}
  remove() {
    if (!this.parentNode) return;
    const at = this.parentNode.children.indexOf(this);
    if (at >= 0) this.parentNode.children.splice(at, 1);
    this.parentNode = null;
  }
}

const longBox = new FakeNode('box');
const more = new FakeNode('button');
longBox.appendChild(more);
const longUnits = [
  {item: {id: 'v1', name: '第1卷'}, sourceIndex: 0, kind: 'volume'},
  ...Array.from({length: 240}, (_, i) => ({
    item: {id: `c${i + 1}`, name: `第${i + 1}话`, group: ''},
    sourceIndex: i, kind: 'chapter',
  })),
];
const opened = [];
const renderCtx = {
  CHAPTER_BATCH_SIZE: 120,
  chapterObserver: null,
  chapterReadIndices: new Set([120]),
  chapterRenderState: {box: longBox, units: longUnits, downloaded: new Set(),
    partialDownloaded: new Set(['c2']), more,
    rendered: 0, rendering: false, curGroup: ''},
  document: {
    createDocumentFragment: () => new FakeNode('fragment', true),
    createElement: tag => new FakeNode(tag),
  },
  window: {},
  esc: String,
  openReader: idx => opened.push(idx),
};
vm.createContext(renderCtx);
vm.runInContext(batchRenderer[0], renderCtx);
more.onclick = renderCtx.renderChapterBatch;
renderCtx.renderChapterBatch();
assert.equal(longBox.children.length, 121,
  'initial render must add one bounded batch and keep the continuation control');
assert.match(longBox.children[0].innerHTML, /整卷/,
  'the first rendered reader unit must be the volume');
assert.equal(longBox.children[0].dataset.chapterIndex, '0');
assert.equal(longBox.children[1].dataset.chapterIndex, '1');
assert.match(longBox.children[2].className, /partial/,
  'provably incomplete local chapter must have a distinct partial-download state');
assert.match(longBox.children[2].innerHTML, /可补齐/,
  'partial chapter must tell the reader its local images can be topped up');
longBox.children[0].onclick();
assert.deepEqual(opened, [0], 'rendered volume must open using its stable reader index');
more.onclick();
assert.ok(longBox.children.some(node => node.dataset.chapterIndex === '120'
  && node.classes.has('read')), 'later batches must retain read-state decoration');
more.onclick();
assert.equal(renderCtx.chapterRenderState.rendered, 241);
assert.ok(!longBox.children.includes(more), 'continuation control must disappear at catalog end');
assert.match(detail, /LOCAL_CATALOG\s*\?\s*'&catalog=local'/,
  'local detail must preserve catalog mode when entering the reader');
const downloadAll = detail.match(/function downloadAll\(\) \{[\s\S]*?\n\}/);
assert.ok(downloadAll, 'detail must keep an explicit download-all action');
assert.match(downloadAll[0], /sessionStorage\.setItem\(selectionKey, JSON\.stringify\(chapters\.map\(ch => String\(ch\.id\)\)\)\)/,
  'download action must persist exactly the currently visible catalog unit IDs');
assert.match(downloadAll[0], /selection=visible/,
  'download action must mark that the request is scoped to the visible catalog');
assert.match(download, /VISIBLE_SELECTION[\s\S]*VISIBLE_CHAPTER_IDS = \[\.\.\.new Set\(stored/,
  'download page must validate and deduplicate the explicit visible selection');
assert.match(download, /else if \(VISIBLE_SELECTION\)[\s\S]*chapters = VISIBLE_CHAPTER_IDS\.slice\(\)[\s\S]*不会下载整本/,
  'missing visible selection must cancel instead of falling back to a whole-work download');
assert.match(download, /body: \{title: TITLE, cover: COVER, chapters: chapters\}/,
  'download request must submit the validated explicit chapter IDs');
const downloadParams = download.slice(download.indexOf('const params = new URLSearchParams'),
  download.indexOf("document.title = TITLE + ' · 下载';"));
const startDownload = download.match(/async function startDl\(\) \{[\s\S]*?\n\}/);
assert.ok(startDownload, 'download page must expose its real start action');
async function verifyScopedDownload() {
  for (const scenario of [
    {stored: null, expectedCalls: 0, expectedStatus: '当前目录选区已丢失'},
    {stored: ['v1', 'c1', 'c1'], expectedCalls: 1, expectedIds: ['v1', 'c1']},
  ]) {
    const nodes = new Map();
    const calls = [];
    const sandbox = {
      URLSearchParams, JSON, Set, Promise, encodeURIComponent, decodeURIComponent,
      location: {search: '?source=copy&cid=work&title=作品&selection=visible'},
      sessionStorage: {getItem: () => scenario.stored === null ? null : JSON.stringify(scenario.stored)},
      document: {title: ''},
      $: selector => {
        const key = selector.replace('#', '');
        if (!nodes.has(key)) nodes.set(key, {textContent: '', innerHTML: '', disabled: false, style: {}, dataset: {}});
        return nodes.get(key);
      },
      requestJSON: async (url, options) => { calls.push({url, options}); return {status: 'queued'}; },
      createPoller: () => ({kick() {}, stop() {}}),
      poll: async () => null,
    };
    vm.createContext(sandbox);
    vm.runInContext(downloadParams + '\nlet pollErrors = 0, poller = null, pollerStopped = false;\n'
      + 'let autoFavoriteWarning = "";\n'
      + startDownload[0] + '\nglobalThis.__startDl = startDl;', sandbox);
    await sandbox.__startDl();
    assert.equal(calls.length, scenario.expectedCalls,
      'missing selection must not submit; a valid selection submits once');
    if (!scenario.expectedCalls) {
      assert.match(nodes.get('status').textContent, /当前目录选区已丢失/);
      assert.equal(nodes.get('start-btn').disabled, false);
    } else {
      assert.deepEqual(Array.from(calls[0].options.body.chapters), scenario.expectedIds,
        'the POST body must contain only deduplicated IDs from the visible directory');
    }
  }
}
assert.match(reader, /LOCAL_CATALOG\s*\?\s*'\?catalog=local'/,
  'reader must request the same catalog mode as its entry');
assert.match(library, /\+ '&title=' \+ encodeURIComponent\(c\.title\) \+ '&catalog=local'/,
  'web bookshelf must open the local-only reading catalog');
assert.match(androidMain, /Dest\.MangaDetail\(m\.source, m\.comicId,\s*localCatalog = true\)/,
  'Android bookshelf must explicitly request local-only catalog');
assert.match(androidManga, /if \(localCatalog\) "\?catalog=local"/,
  'Android detail/reader requests must preserve catalog mode');
assert.match(androidManga, /LaunchedEffect\(source, comicId, localCatalog\) \{ reload\(\) \}/,
  'Android detail must reload when switching between complete and local catalogs');
assert.match(androidManga, /LaunchedEffect\(source, comicId, localCatalog\) \{[\s\S]*?val cached = MangaReadCache\.getDetail\(source, comicId, localCatalog\)/,
  'Android reader loading identity and detail cache must include the local catalog mode');
assert.match(androidManga, /LaunchedEffect\(ch\.id, p\.count, localCatalog\)/,
  'changing catalog mode must cancel stale in-chapter image prefetch');
assert.match(androidManga, /LaunchedEffect\(ch\.id, p\.count, approachingChapterEnd, localCatalog\)/,
  'changing catalog mode must cancel stale next-chapter prefetch');
assert.match(androidManga,
  /val tocListState = rememberLazyListState\([\s\S]*?initialFirstVisibleItemIndex = chapterIndex\.coerceIn\([\s\S]*?state = tocListState/,
  'reopening the reader TOC must position the current chapter in view');
assert.match(reader, /encodeURIComponent\(chapters\[i\]\.id\)\}\/urls`\s*\n\s*\+ \(LOCAL_CATALOG \? '\?catalog=local'/,
  'web chapter URL fetches must preserve local-only mode');
assert.match(reader, /encodeURIComponent\(chapters\[_pi\]\.id\)\}\/urls`\s*\n\s*\+ \(LOCAL_CATALOG \? '\?catalog=local'/,
  'web next-chapter prefetch must preserve local-only mode');
assert.match(androidManga, /getUrls\(source: String, comicId: String, chapterId: String,\s*\n\s*localCatalog: Boolean/,
  'Android URL cache must distinguish local and complete catalogs');
assert.match(androidManga, /readerCacheMatchesCatalog\(pages: MangaChapterPages, localCatalog: Boolean\): Boolean =\s*\n\s*pages\.local == localCatalog/,
  'Android URL metadata must match the exact catalog mode that produced it');
assert.match(androidManga, /cache\[ch\.id\]\?\.takeIf \{ readerCacheMatchesCatalog\(it, localCatalog\) \}/,
  'Android reader must reject complete-catalog URL metadata on a local-only read');
assert.match(androidManga, /"\/chapter\/\$\{Uri\.encode\(ch\.id\)\}\/urls"\) \+ localSuffix/,
  'Android chapter loading must request local-only URLs when reading from the library');

const loadFavs = mangaPage.match(/async function loadFavs\(\) \{[\s\S]*?\n\}/);
assert.ok(loadFavs, 'web favorite shelf must render from the shared favorites endpoint');
assert.match(loadFavs[0], /!Array\.isArray\(d\.favorites\)[\s\S]*?响应格式异常/,
  'a malformed HTTP 200 favorites envelope must not be reported as an empty collection');
assert.match(loadFavs[0], /Number\(c\.unread_count \|\| 0\) > 0[\s\S]*fav-unread-badge/,
  'web favorite cover must show a badge only for a positive actual unread count');
assert.match(loadFavs[0], /<div class="cover-wrap">[\s\S]*fav-unread-badge/,
  'web unread badge must overlay the cover, not displace the card content');
assert.match(loadFavs[0], /class="fav-remove-btn"[^>]*>取消收藏/,
  'favorite cards must expose a visible touch- and keyboard-accessible remove action');
assert.match(loadFavs[0], /if \(!response\.ok \|\| result\.ok === false\) throw/,
  'favorite removal must not silently report success when the server rejects it');
const mangaLibraryLoader = libraryPage.match(
  /async function loadMangaLibrary\(\) \{[\s\S]*?\n\}\nasync function loadBooks/);
assert.ok(mangaLibraryLoader, 'web manga shelf loader must remain a single testable flow');
assert.match(mangaLibraryLoader[0], /if \(!r\.ok\) throw new Error/,
  'web shelf must not interpret an API corruption error as an empty library');
assert.match(mangaLibraryLoader[0], /_mangaSig = null;[\s\S]*?onclick="loadMangaLibrary\(\)"/,
  'a recovered identical payload must redraw after an error and expose retry');
assert.match(loadFavs[0], /class="fav-open-link" href="\/manga_detail\?source=/,
  'favorite detail navigation must use a native, keyboard-accessible link');
assert.match(loadFavs[0], /<button class="fav-remove-btn"[\s\S]*?card\.querySelector\('\.fav-remove-btn'\)/,
  'favorite removal must be a sibling action, not nested inside the detail link');
assert.match(styles, /\.fav-unread-badge\s*\{[^}]*position:\s*absolute;[^}]*top:\s*6px;[^}]*left:\s*6px;/,
  'web unread badge must stay small at the cover top-left corner');
assert.match(styles, /\.fav-remove-btn\s*\{[^}]*min-height:\s*44px/s,
  'favorite removal touch target must meet the 44px minimum');
assert.match(androidMain, /mangaFavoriteUnreadBadgeLabel\(f\.unreadCount\)\?\.let \{ unreadLabel ->\s*Text\(unreadLabel[\s\S]*Alignment\.TopStart/,
  'Android favorite cover must place the validated unread label in its top-left corner');
assert.equal((androidMain.match(/LazyVerticalGrid\s*\(/g) || []).length, 1,
  'the cached manga section must not nest a full-height lazy grid inside the shelf LazyColumn');
assert.match(androidMain, /items\(\(sortedManga\.size \+ 2\) \/ 3, key = \{ row ->[\s\S]*"manga-row:/,
  'cached manga cards must be emitted as stable-keyed rows by the outer lazy shelf');
assert.match(androidMain, /items\(history, key = \{ entry ->[\s\S]*novel:\$\{entry\.item\.key\}[\s\S]*manga:\$\{entry\.item\.source\}/,
  'combined reading history rows must keep stable cross-content identities');
assert.match(androidMain, /items\(favorites\.size, key = \{ i ->[\s\S]*favorites\[i\]\.source/,
  'favorite grid cards must retain source/comic identity when update counts reorder them');

const favoriteUpdateCheck = mangaPage.match(
  /async function checkFavoriteUpdates\(force = false\) \{[\s\S]*?\n\}/);
const favoriteStatusVisibility = mangaPage.match(
  /function syncFavoriteCheckStatusVisibility\(tab\) \{[\s\S]*?\n\}/);
assert.ok(favoriteUpdateCheck, 'web workspace must check favorite updates on startup');
assert.ok(favoriteStatusVisibility,
  'favorite-check status visibility must be a testable tab-scoped behavior');
const tabSwitch = mangaPage.match(/function switchTab\(tab\) \{[\s\S]*?\n\}/)?.[0] || '';
assert.match(tabSwitch, /syncFavoriteCheckStatusVisibility\(tab\)/,
  'switching tabs must re-evaluate whether a stored favorite result is relevant');
assert.match(tabSwitch, /show\('manga-search-panel', tab === 'search'\)/,
  'search-only filters and source status must not consume the favorites/history viewport');
const favoriteStatus = {hidden: false, textContent: '收藏列表暂无可检查的作品。'};
const statusContext = vm.createContext({document: {getElementById: id =>
  id === 'favorite-check-status' ? favoriteStatus : null}});
vm.runInContext(favoriteStatusVisibility[0]
  + '\nglobalThis.__syncStatus = syncFavoriteCheckStatusVisibility;', statusContext);
statusContext.__syncStatus('search');
assert.equal(favoriteStatus.hidden, true,
  'an empty-favorites message must not occupy the unrelated default search screen');
statusContext.__syncStatus('fav');
assert.equal(favoriteStatus.hidden, false,
  'the favorite-check result must be available on the favorites tab');
favoriteStatus.textContent = '';
statusContext.__syncStatus('fav');
assert.equal(favoriteStatus.hidden, true,
  'a cleared status message must not leave an empty banner visible');
assert.match(favoriteUpdateCheck[0], /runtime\.instance_id/,
  'web favorite check must be scoped to the current engine instance');
assert.match(favoriteUpdateCheck[0], /sessionStorage\.getItem\(attemptKey\)/,
  'reloading the workspace must not repeat a completed startup check in one tab session');
assert.match(favoriteUpdateCheck[0], /收藏更新检查完成：/,
  'web must show the completion and failure outcome of the background check');
assert.match(favoriteUpdateCheck[0], /你可以继续搜索和阅读/,
  'web must make clear that the background check does not block normal use');
assert.match(mangaPage, /id="refresh-favs" onclick="retryFavoriteUpdateCheck\(\)"/,
  'favorite shelf must offer a direct retry action for update checks');
assert.match(favoriteUpdateCheck[0], /!force && sessionStorage\.getItem\(attemptKey\)/,
  'explicit user retry must bypass the automatic once-per-session guard');
async function verifyFavoriteCheckIsOncePerEngineSession() {
  const values = new Map();
  let instanceId = 'engine-one';
  let starts = 0;
  let failNextStart = true;
  const sandbox = {
    fetch: async path => {
      if (path === '/api/health') {
        return {ok: true, json: async () => ({runtime: {instance_id: instanceId}})};
      }
      if (path === '/api/manga/favorites/check-updates') {
        starts += 1;
        if (failNextStart) {
          failNextStart = false;
          return {ok: false};
        }
        return {ok: true, json: async () => ({started: 2})};
      }
      if (path === '/api/manga/favorites/check-updates/status') {
        return {ok: true, json: async () => ({running: false, succeeded: 1, failed: 1})};
      }
      throw new Error(`unexpected request ${path}`);
    },
    sessionStorage: {
      getItem: key => values.get(key) || null,
      setItem: (key, value) => values.set(key, value),
    },
    document: {getElementById: id => id === 'favorite-check-status'
      ? (sandbox.status || (sandbox.status = {hidden: true, dataset: {}, textContent: ''}))
      : ({classList: {contains: () => false}})},
    setTimeout: callback => { callback(); return 0; },
    Promise, String,
  };
  vm.createContext(sandbox);
  vm.runInContext(favoriteStatusVisibility[0] + '\n' + favoriteUpdateCheck[0]
    + '\nglobalThis.__checkFavoriteUpdates = checkFavoriteUpdates;', sandbox);
  await sandbox.__checkFavoriteUpdates();
  await sandbox.__checkFavoriteUpdates();
  assert.equal(starts, 2, 'a failed start must not consume the once-per-session marker');
  assert.match(sandbox.status.textContent, /1 部成功，1 部失败/,
    'partial source failures must be visible instead of silently swallowed');
  await sandbox.__checkFavoriteUpdates();
  assert.equal(starts, 2, 'successful check must not repeat on workspace reload');
  instanceId = 'engine-two';
  await sandbox.__checkFavoriteUpdates();
  assert.equal(starts, 3, 'a restarted engine gets its own automatic favorite check');
}

async function verifyEmptyFavoriteCheckIsOncePerEngineSession() {
  const values = new Map();
  const statusBox = {hidden: true, dataset: {}, textContent: ''};
  let starts = 0;
  let forceRetry;
  const sandbox = {
    fetch: async path => {
      if (path === '/api/health') {
        return {ok: true, json: async () => ({runtime: {instance_id: 'empty-engine'}})};
      }
      if (path === '/api/manga/favorites/check-updates') {
        starts += 1;
        return {ok: true, json: async () => ({started: 0, already_running: false})};
      }
      throw new Error(`unexpected request ${path}`);
    },
    sessionStorage: {
      getItem: key => values.get(key) || null,
      setItem: (key, value) => values.set(key, value),
    },
    document: {getElementById: id => id === 'favorite-check-status'
      ? statusBox : {classList: {contains: () => false}}},
    setTimeout, Promise, String,
  };
  vm.createContext(sandbox);
  vm.runInContext(favoriteStatusVisibility[0] + '\n' + favoriteUpdateCheck[0]
    + '\nglobalThis.__checkFavoriteUpdates = checkFavoriteUpdates;', sandbox);
  await sandbox.__checkFavoriteUpdates();
  assert.equal(statusBox.hidden, true,
    'empty favorites status stays out of the default search tab');
  await sandbox.__checkFavoriteUpdates();
  assert.equal(starts, 1, 'an empty favorites shelf must consume the successful automatic check');
  assert.match(values.get('manga-favorite-update-check:empty-engine'), /1/,
    'empty-shelf result must be remembered for this engine session');
  await sandbox.__checkFavoriteUpdates(true);
  assert.equal(starts, 2, 'manual retry must still check favorites added later in the session');
}

verifyScopedDownload().then(() => {
  return verifyFavoriteCheckIsOncePerEngineSession();
}).then(() => {
  return verifyEmptyFavoriteCheckIsOncePerEngineSession();
}).then(() => {
  console.log('manga_catalog.test.js: ALL ASSERTIONS PASSED');
}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
