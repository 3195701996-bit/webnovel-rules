const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const detail = fs.readFileSync(path.join(root, 'templates/manga_detail.html'), 'utf8');
const reader = fs.readFileSync(path.join(root, 'templates/manga_reader.html'), 'utf8');
const detailCss = detail.match(/<style>([\s\S]*?)<\/style>/)?.[1] || '';
const readerCss = reader.match(/<style>([\s\S]*?)<\/style>/)?.[1] || '';

for (const [selector, color] of [
  ['.op-read', 'var(--accent)'],
  ['.fn-continue', 'var(--accent)'],
  ['.fn-download', 'var(--bg-overlay)'],
  ['.chap-item.dl', 'var(--green)'],
]) {
  const rule = detailCss.match(new RegExp(`${selector.replaceAll('.', '\\.')}\\s*\\{[^}]*\\}`, 's'))?.[0] || '';
  assert.ok(rule.includes(color), `${selector} must use semantic color ${color}`);
}
assert.doesNotMatch(detailCss, /#(?:8b5cf6|e8833a|e0526e|9b7bd6|2a3140)\b/i,
  'detail controls and chapter surfaces must not reintroduce the legacy saturated palette');
assert.match(readerCss, /\.toc-item\.active\s*\{[^}]*color:var\(--accent\)/s,
  'active reader chapter must use the shared accent');
assert.match(readerCss, /\.toc-item\.dl\s*\{[^}]*var\(--green\)/s,
  'download state must use the semantic success color');

const helper = reader.match(/function isReaderCenterTap\([^)]*\)\s*\{[^}]*\}/)?.[0];
assert.ok(helper, 'reader center hotspot helper must be explicit and testable');
const context = vm.createContext({});
vm.runInContext(`${helper}; globalThis.check = isReaderCenterTap;`, context);
const check = context.check;
assert.equal(check(195, 422, 390, 844), true, 'tap in center must recall reader controls');
assert.equal(check(195 + 390 * .109, 422, 390, 844), true, 'tap just inside 22% width must trigger');
assert.equal(check(195 + 390 * .111, 422, 390, 844), false, 'near-center tap outside 22% width must not trigger');
assert.equal(check(195, 422 + 844 * .071, 390, 844), false, 'tap outside 14% height must not trigger');
const seekOffset = reader.match(/function readerScrollOffsetForPage\([^)]*\)\s*\{[^}]*\}/)?.[0];
assert.ok(seekOffset, 'reader seek must expose its viewport anchor calculation');
const seekContext = vm.createContext({Math});
vm.runInContext(`${seekOffset}; globalThis.offset = readerScrollOffsetForPage;`, seekContext);
assert.equal(seekContext.offset(5001, 850), 4703.5,
  'seeking must align the selected page with the same 35% viewport progress anchor');
assert.equal(seekContext.offset(100, 850), 0,
  'seeking near the document start must clamp the scroll position to zero');
assert.match(reader, /img\.onload\s*=\s*\(\)\s*=>\s*\{[^}]*_done\(\)/s,
  'a successfully loaded page must complete pending seek-layout callbacks');
assert.doesNotMatch(reader, /img\.onload\s*=\s*\(\)\s*=>\s*\{[^}]*_settled\s*=\s*true;\s*_done\(\)/s,
  'the image load handler must not pre-mark the settle callback as already consumed');
assert.match(reader, /existing\.dataset\.mountSettled === '1'[\s\S]{0,100}_mountWaiters/,
  'seeking must wait for an image already mounted by lazy loading');
assert.match(reader, /if \(force \|\| \(item && typeof item === 'object' && item\.local\)\)[\s\S]{0,120}img\.loading = 'eager'/,
  'images required to calculate a seek target must not remain deferred by native lazy loading');
assert.match(reader, /const forceForSeek = i >= pg - 2;[\s\S]{0,140}if \(!forceForSeek && !w\.querySelector\('img'\)\) continue;/,
  'seeking deep into a long chapter must not eagerly fetch every earlier page');
assert.match(reader, /window\.addEventListener\('wheel',[\s\S]*?window\.addEventListener\('touchmove'/,
  'automatic browser scroll anchoring must not masquerade as manual reader scrolling');
assert.match(reader, /if \(!e\.target\.closest\('#stage'\)\) return;/,
  'toolbars may only toggle from the reading canvas');
assert.match(reader, /isReaderCenterTap\(e\.clientX, e\.clientY, window\.innerWidth, window\.innerHeight\)/,
  'the document click handler must enforce the small center hotspot');
assert.match(reader, /<div class="reader-top hidden" id="reader-top">/,
  'web reader controls must start hidden on the reading screen');
assert.match(reader, /<div class="reader-bottom hidden" id="reader-bottom"/,
  'web reader must have a matching hidden bottom progress bar');
assert.match(reader, /function readerProgressState\(\)/,
  'reader progress must be represented as a mode-aware current/total pair');
assert.match(reader, /function seekReaderProgress\(value\)/,
  'reader progress must be directly adjustable');
assert.match(reader, /let _readerResizeTimer[\s\S]*?window\.addEventListener\('resize',\s*\(\)\s*=>\s*\{[\s\S]*?pageBeforeResize[\s\S]*?seekReaderProgress\(pageBeforeResize\)/,
  'scroll reading must preserve the selected page when viewport reflow changes image heights');
const chapterRenderAt = reader.indexOf('renderChapter(imgs, chapters[i].name);');
const chapterImagesAt = reader.lastIndexOf('window._curImgs = imgs;', chapterRenderAt);
assert.ok(chapterImagesAt >= 0 && chapterImagesAt < chapterRenderAt,
  'the reader must publish the chapter image list before rendering progress controls');
assert.doesNotMatch(reader, /lastScrollY[\s\S]{0,500}classList\.remove\('hidden'\)/,
  'scrolling up must not reveal the reader controls');

const controlBlock = reader.match(/\/\/ ── 阅读器专属控件显隐[\s\S]*?(\(function\(\)\s*\{[\s\S]*?\}\)\(\);)/)?.[1];
assert.ok(controlBlock, 'reader control state machine must be isolated in the reader page');
function mockNode(hidden = false) {
  const listeners = {};
  const classes = new Set(hidden ? ['hidden'] : []);
  return {
    listeners, classes,
    value: '1', max: '1', textContent: '', disabled: false,
    classList: {
      toggle(name, value) { value ? classes.add(name) : classes.delete(name); },
      contains(name) { return classes.has(name); },
    },
    addEventListener(name, handler) { (listeners[name] ||= []).push(handler); },
    fire(name, event = {}) { for (const handler of listeners[name] || []) handler(event); },
  };
}
const nodes = {
  'reader-top': mockNode(true), 'reader-bottom': mockNode(true),
  'reader-progress': mockNode(), 'reader-page-label': mockNode(),
  'reader-prev-page': mockNode(), 'reader-next-page': mockNode(),
  'toc-panel': mockNode(), 'settings-panel': mockNode(),
};
const docListeners = {}, winListeners = {}, timers = new Map();
let nextTimer = 0;
const fakeWindow = {
  innerWidth: 390, innerHeight: 844,
  addEventListener(name, handler) { (winListeners[name] ||= []).push(handler); },
};
const fakeDocument = {
  getElementById(id) { return nodes[id] || null; },
  addEventListener(name, handler) { (docListeners[name] ||= []).push(handler); },
};
const controlContext = vm.createContext({
  window: fakeWindow, document: fakeDocument, Math,
  setTimeout(fn, ms) { const id = ++nextTimer; timers.set(id, {fn, ms, active: true}); return id; },
  clearTimeout(id) { const timer = timers.get(id); if (timer) timer.active = false; },
  seekReaderProgress(value) { seeks.push(Number(value)); }, readerProgressState() { return {current: 1, total: 3}; },
  syncReaderProgress() {},
});
const seeks = [];
vm.runInContext(`${helper}; ${controlBlock}`, controlContext);
const click = (x, y, inStage = true, inControls = false) => {
  const target = {closest(selector) {
    if (selector.includes('#reader-top') && inControls) return {};
    if (selector === '#stage' && inStage) return {};
    return null;
  }};
  for (const handler of docListeners.click || []) handler({target, clientX: x, clientY: y});
};
const controlsHidden = () => nodes['reader-top'].classes.has('hidden') && nodes['reader-bottom'].classes.has('hidden');
click(195, 422, false);
assert.equal(controlsHidden(), true, 'taps outside the reading canvas must not show controls');
click(195, 422);
assert.equal(controlsHidden(), false, 'small center tap must reveal both reader bars');
let activeTimer = [...timers.values()].findLast(timer => timer.active);
assert.equal(activeTimer?.ms, 4000, 'reader bars must auto-hide after an idle delay');
const progressTarget = {closest(selector) { return selector === '#reader-progress' ? {} : null; }};
nodes['reader-bottom'].fire('pointerdown', {target: progressTarget});
assert.ok(![...timers.values()].some(timer => timer.active),
  'dragging the progress slider must cancel the idle-hide timer');
nodes['reader-progress'].value = '3';
nodes['reader-progress'].max = '3';
nodes['reader-progress'].fire('input');
assert.equal(nodes['reader-page-label'].textContent, '3 / 3 页',
  'the page label must follow the slider while it is being dragged');
nodes['reader-progress'].fire('change');
assert.deepEqual(seeks, [3], 'releasing the slider must seek to the selected page');
assert.ok([...timers.values()].some(timer => timer.active && timer.ms === 4000),
  'after seeking, the idle-hide timer must resume');
activeTimer = [...timers.values()].findLast(timer => timer.active);
activeTimer.fn();
assert.equal(controlsHidden(), true, 'idle timeout must hide both reader bars');
for (const handler of winListeners.scroll || []) handler();
assert.equal(controlsHidden(), true, 'scrolling must not reveal the controls');
click(195, 422 + 844 * .071);
assert.equal(controlsHidden(), true, 'tap outside the narrow center zone must not reveal controls');

console.log('manga_reader_visual_controls.test.js: palette, center hotspot, idle hide, and adjustable progress controls passed');
