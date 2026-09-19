/* 前端剩余健壮性修复离线回归（node 直接运行，无浏览器依赖，不新增依赖）
 *
 * 被测对象（均用 node:vm 从模板/脚本**原样抽取真实函数**执行，与线上同一份代码）：
 *  1. sources.html   loadSources / selectedUids / batchToggle / srcAutoRefresh
 *       - 错误处理：HTTP/网络失败与业务字段缺失都判失败，绝不 toast 成功
 *       - 代际守卫：迟到的旧响应不得覆盖新列表
 *       - 刷新保留勾选：重建 DOM 时按旧选择回填 checked
 *       - 批量：如实统计成功/失败并禁用→恢复按钮
 *       - 可见性感知：后台不轮询；有勾选时不自动刷新
 *  2. manga_detail.html toggleFav：非 2xx/业务失败绝不翻转收藏 UI
 *  3. manga_reader.html renderScroll/_mountImg/showPage/gotoChapter：渲染代际 +
 *      冻结所属章 + 空预取/空列表/失败重试（"图片不加载"的两个客户端成因）
 *       - 切章 disconnect 旧 IntersectionObserver
 *       - 旧代 IO 回调 / onload / onerror / 延迟代理定时器切章后不得写 DOM
 *       - 延迟代理地址用"所属章 id"，绝不引用当前 chIdx
 *  4. library.html   mangaPollLoop/mangaShowResult/mangaPollThenFinish
 *       - 连续轮询失败返回 null → 报"获取失败"，绝不报"没有可检查的漫画"
 *  5. netip.js       poll：在途去重（不得并发）+ finally 清理 abort 定时器
 *
 * 运行：node tests/js/audit_frontend_remaining.test.js
 */
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');
const tick = () => new Promise((r) => setTimeout(r, 0));

function slice(src, start, end) {
  const a = src.indexOf(start), b = src.indexOf(end);
  assert.ok(a >= 0 && b > a, `未定位片段起始/结束：${start}`);
  return src.slice(a, b);
}

// ── 极简 DOM 替身（innerHTML='' 清空 children，贴近浏览器语义）──
function makeEl(tag) {
  const el = {
    tagName: tag || 'div', className: '', textContent: '', value: '',
    style: {}, dataset: {}, children: [], isConnected: true, checked: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { el.children.push(c); return c; },
    remove() { el._removed = true; },
    querySelector(sel) {
      if (sel === 'img') return el.children.find((c) => c.tagName === 'img') || null;
      if (sel && sel[0] === '.') {
        const cls = sel.slice(1);
        return el.children.find((c) => (c.className || '').split(' ').includes(cls)) || null;
      }
      return null;
    },
    querySelectorAll() { return []; },
    addEventListener() {}, contains() { return false; }, scrollIntoView() {},
  };
  let html = '';
  Object.defineProperty(el, 'innerHTML', {
    get() { return html; },
    // 贴近浏览器语义：任何 innerHTML 赋值都会**替换**全部子节点（旧实现只在
    // 赋空串时清空，导致跨用例的子节点累积，页数断言会假通过/假失败）
    set(v) { html = v; el.children.length = 0; },
  });
  return el;
}
const renderedHtml = (el) =>
  el.children.map((c) => c.innerHTML || c.textContent || '').join('\n');

// ═══════════ 1. sources.html ═══════════
const SRC = read('templates/sources.html');
const SRC_BLOCK = slice(SRC, 'let _srcLoadSeq = 0;',
  "const bdel = document.getElementById('batch-del');");
assert.ok(/await requestJSON\('\/api\/sources'\)/.test(SRC_BLOCK),
  'loadSources 未改用 requestJSON');
assert.ok(!/await fetch\(/.test(SRC_BLOCK), 'loadSources 仍存在裸 fetch');
assert.ok(/mySeq !== _srcLoadSeq/.test(SRC_BLOCK), 'loadSources 缺少代际守卫');

const srcToast = [];
const srcReq = [];
const srcEls = new Map();
const srcChecked = [];       // document.querySelectorAll('.sel-check:checked')
const srcAllChecks = [];     // document.querySelectorAll('.sel-check')
const srcGet = (id) => { if (!srcEls.has(id)) srcEls.set(id, makeEl('div')); return srcEls.get(id); };
const srcStub = (sel) => srcGet(String(sel).replace('#', ''));
const srcSandbox = {
  console, JSON, Promise, encodeURIComponent, decodeURIComponent,
  document: {
    hidden: false, activeElement: null,
    getElementById: srcGet,
    querySelectorAll: (sel) => (sel === '.sel-check:checked' ? srcChecked
      : sel === '.sel-check' ? srcAllChecks : []),
    createElement: (t) => makeEl(t),
    addEventListener() {},
  },
  $: srcStub,
  esc: (s) => String(s),
  toast: (m, t) => { srcToast.push({ m, t }); },
  requestJSON: (url, opts) => new Promise((resolve, reject) => srcReq.push({ url, opts, resolve, reject })),
  confirm: () => true,
  setTimeout: (fn, ms) => ({ fn, ms }),   // 不真正排期：避免 15s 轮询挂住事件循环
  clearTimeout: () => {},
};
vm.createContext(srcSandbox);
vm.runInContext(SRC_BLOCK +
  '\n;globalThis.__src = { loadSources, selectedUids, batchToggle, srcAutoRefresh };', srcSandbox);
const srcApi = srcSandbox.__src;

async function testSources() {
  const box = srcGet('source-list');
  srcReq.length = 0;  // 丢弃抽取时自动触发的首次加载（保持 pending 即可）

  // (a) HTTP/网络失败：reject → 显示加载失败，且无成功提示
  srcToast.length = 0;
  const pA = srcApi.loadSources();
  await tick();
  assert.strictEqual(srcReq.length, 1, 'loadSources 应发起一次请求');
  srcReq[0].reject(new Error('HTTP 500'));
  await pA;
  assert.ok(box.innerHTML.includes('书源加载失败'), 'HTTP 失败应显示加载失败');
  assert.ok(!srcToast.some((x) => x.t === 'ok'), '失败不得 toast 成功');

  // (b) 业务错误：resolve 但缺 sources 数组 → 仍判失败
  srcReq.length = 0;
  const pB = srcApi.loadSources();
  await tick();
  srcReq[0].resolve({ ok: true });   // 无 sources 字段
  await pB;
  assert.ok(box.innerHTML.includes('书源加载失败'), '业务字段缺失应判失败');

  // (c) 代际：迟到的旧响应不得覆盖新请求结果
  srcReq.length = 0;
  const pC = srcApi.loadSources();          // 旧（seq=1）
  await tick();
  const callC = srcReq[0];
  const pD = srcApi.loadSources();          // 新（seq=2）
  await tick();
  const callD = srcReq[1];
  callC.resolve({ sources: [{ uid: 'old', bookSourceName: 'OLD', bookSourceGroup: 'G', enabled: true }] });
  await pC;
  callD.resolve({ sources: [{ uid: 'new', bookSourceName: 'NEW', bookSourceGroup: 'G', enabled: true }] });
  await pD;
  const htmlC = renderedHtml(box);
  assert.ok(htmlC.includes('data-uid="new"'), '新列表应被渲染');
  assert.ok(!htmlC.includes('data-uid="old"'), '迟到的旧列表不得落地');

  // (d) 刷新保留勾选：重建 DOM 时按旧选择回填 checked
  srcReq.length = 0;
  srcChecked.length = 0;
  srcChecked.push({ dataset: { uid: 'u1' } });   // 模拟 u1 已勾选
  const pE = srcApi.loadSources();
  await tick();
  srcReq[srcReq.length - 1].resolve({ sources: [
    { uid: 'u1', bookSourceName: 'A', bookSourceGroup: 'G', enabled: true },
    { uid: 'u2', bookSourceName: 'B', bookSourceGroup: 'G', enabled: true },
  ] });
  await pE;
  const rows = box.children.map((c) => c.innerHTML).filter((h) => h.includes('sel-check'));
  const u1row = rows.find((h) => h.includes('data-uid="u1"'));
  const u2row = rows.find((h) => h.includes('data-uid="u2"'));
  assert.ok(u1row && u1row.includes('data-uid="u1" checked'), 'u1 勾选应被保留');
  assert.ok(u2row && !u2row.includes('data-uid="u2" checked'), 'u2 不应被勾选');

  // (e) 批量：如实统计成功/失败 + 按钮禁用→恢复
  srcReq.length = 0; srcToast.length = 0;
  const btn = makeEl('button');
  const pF = srcApi.batchToggle(['x', 'y'], true, btn);
  await tick();
  assert.strictEqual(srcReq.length, 1, '批量应串行：先发第一个请求');
  assert.strictEqual(btn.disabled, true, '批量进行中按钮应禁用');
  srcReq[0].resolve({ ok: true, enabled: true });
  await tick();
  assert.strictEqual(srcReq.length, 2, '第一个完成后才发第二个请求');
  srcReq[1].reject(new Error('HTTP 500'));
  await tick(); await tick();
  const refresh = srcReq[2];
  assert.ok(refresh, '批量结束后应刷新列表');
  refresh.resolve({ sources: [] });
  await pF;
  const msg = srcToast.find((x) => /成功 1 个，失败 1 个/.test(x.m));
  assert.ok(msg, '批量应如实报告成功 1/失败 1：' + JSON.stringify(srcToast));
  assert.strictEqual(msg.t, 'error', '有失败时应为 error 级提示');
  assert.strictEqual(btn.disabled, false, '批量结束后按钮应恢复可点');

  // (f) 可见性感知单飞：后台不轮询；有勾选时不自动刷新
  srcReq.length = 0; srcChecked.length = 0;
  srcSandbox.document.hidden = true;
  await srcApi.srcAutoRefresh();
  assert.strictEqual(srcReq.length, 0, '后台标签不应轮询');
  srcSandbox.document.hidden = false;
  srcChecked.push({ dataset: { uid: 'z' } });     // 有勾选 → 暂停自动刷新
  await srcApi.srcAutoRefresh();
  assert.strictEqual(srcReq.length, 0, '有勾选时不应自动刷新（避免丢选择）');
  srcChecked.length = 0;
  const pG = srcApi.srcAutoRefresh();
  await tick();
  assert.strictEqual(srcReq.length, 1, '无勾选且可见时应刷新');
  srcReq[0].resolve({ sources: [] });
  await pG;

  // (g) 自动入口单飞：并发两次 srcAutoRefresh（定时器 + visibilitychange）
  //     只应发出 1 个请求；在途结束后锁释放，可再次发起
  srcReq.length = 0; srcChecked.length = 0;
  srcSandbox.document.hidden = false;
  const pG1 = srcApi.srcAutoRefresh();
  const pG2 = srcApi.srcAutoRefresh();   // 第二次应被单飞锁挡下
  await tick(); await tick();
  assert.strictEqual(srcReq.length, 1, '同时调用 srcAutoRefresh 两次只应发 1 个请求');
  srcReq[0].resolve({ sources: [] });
  await pG1; await pG2;
  const pG3 = srcApi.srcAutoRefresh();
  await tick();
  assert.strictEqual(srcReq.length, 2, '在途结束后的下一次自动刷新应可再次发起');
  srcReq[1].resolve({ sources: [] });
  await pG3;

  // (h) 单飞锁只约束自动入口：自动刷新在途时，用户输入仍能发起新代请求
  srcReq.length = 0;
  const pAuto = srcApi.srcAutoRefresh();   // 自动请求挂起（在途）
  await tick();
  assert.strictEqual(srcReq.length, 1, '自动刷新应先发起 1 个在途请求');
  const pUser = srcApi.loadSources();      // 用户输入触发：不得被自动单飞锁阻塞
  await tick();
  assert.strictEqual(srcReq.length, 2, '在途自动刷新不应阻塞用户输入的新代请求');
  srcReq[0].resolve({ sources: [] });
  srcReq[1].resolve({ sources: [] });
  await pAuto; await pUser;

  // (i) 业务失败：HTTP 200 但 {ok:false} 应计为失败（requestJSON 不 reject 业务失败）
  srcReq.length = 0; srcToast.length = 0;
  const btn2 = makeEl('button');
  const pH = srcApi.batchToggle(['p', 'q'], true, btn2);
  await tick();
  srcReq[0].resolve({ ok: false, error: '被拒绝' });   // 业务失败
  await tick();
  srcReq[1].resolve({ ok: true, enabled: true });       // 业务成功
  await tick(); await tick();
  const refreshH = srcReq[2];
  assert.ok(refreshH, '批量结束后应刷新列表');
  refreshH.resolve({ sources: [] });
  await pH;
  const msgH = srcToast.find((x) => /成功 1 个，失败 1 个/.test(x.m));
  assert.ok(msgH, 'ok:false 应计为失败、ok:true 计为成功：' + JSON.stringify(srcToast));
  assert.strictEqual(msgH.t, 'error', '含业务失败时应为 error 级提示');
}

// ═══════════ 2. manga_detail.html toggleFav ═══════════
const MD = read('templates/manga_detail.html');
const MD_BLOCK = slice(MD, 'async function toggleFav() {', '\nasync function loadFavState()');
const mdBtn = makeEl('button');
mdBtn.dataset.fav = '0';
const mdAlerts = [];
let mdReq = null;
const mdSandbox = {
  console, JSON, Promise, encodeURIComponent,
  document: { querySelector: (s) => (s === '.op-fav' ? mdBtn : null), getElementById: () => null, addEventListener() {} },
  requestJSON: (url, opts) => new Promise((resolve, reject) => { mdReq = { url, opts, resolve, reject }; }),
  alert: (m) => { mdAlerts.push(m); },
  SOURCE: 'src1', CID: 'cid1',
  detail: { title: 'T', cover: 'C' },
};
vm.createContext(mdSandbox);
vm.runInContext(MD_BLOCK + '\n;globalThis.__toggleFav = toggleFav;', mdSandbox);
const toggleFav = mdSandbox.__toggleFav;

async function testMangaDetail() {
  // 收藏（POST）失败：UI 不得翻转
  mdBtn.dataset.fav = '0'; mdBtn.innerHTML = '🔖 收藏'; mdBtn.style.opacity = '.6';
  mdAlerts.length = 0;
  const p1 = toggleFav();
  await tick();
  assert.ok(mdReq && mdReq.url === '/api/manga/favorites', '应发 POST 收藏请求');
  mdReq.reject(new Error('HTTP 500'));
  await p1;
  assert.strictEqual(mdBtn.dataset.fav, '0', '失败不得把未收藏翻成已收藏');
  assert.strictEqual(mdBtn.innerHTML, '🔖 收藏', '失败不得改按钮文案');
  assert.strictEqual(mdBtn.style.opacity, '.6', '失败不得改透明度');
  assert.ok(mdAlerts.some((m) => m.includes('收藏操作失败')), '应如实提示失败');

  // 收藏成功：翻转
  mdBtn.dataset.fav = '0';
  const p2 = toggleFav();
  await tick();
  mdReq.resolve({ ok: true });
  await p2;
  assert.strictEqual(mdBtn.dataset.fav, '1', '成功后应翻转为已收藏');
  assert.ok(mdAlerts.some((m) => m.includes('已加入收藏')));

  // 取消收藏（DELETE）失败：UI 不得翻转
  mdBtn.dataset.fav = '1'; mdBtn.innerHTML = '🔖 已收藏';
  const p3 = toggleFav();
  await tick();
  assert.ok(/\/api\/manga\/favorites\/src1\/cid1$/.test(mdReq.url), 'DELETE 路径应含编码后的 source/cid');
  mdReq.reject(new Error('HTTP 404'));
  await p3;
  assert.strictEqual(mdBtn.dataset.fav, '1', '失败不得把已收藏翻成未收藏');
  assert.strictEqual(mdBtn.innerHTML, '🔖 已收藏', '失败不得改文案');
}

// ═══════════ 3. manga_reader.html 渲染代际 + 冻结所属章 ═══════════
const READER = read('templates/manga_reader.html');
// 仅抽取被测的真实函数（原样），并补上它们依赖的顶层声明
const RC_DECL = `let _renderGen = 0;
let _curGen = 0;
let _curChId = '';
let _scrollIO = null;
let _pageIdx = 0;
let _zoomLevel = 1.0;
let chapters = [];
let chIdx = 0;
let settings = {mode:'scroll', flip:'zone'};
// 0.64.0：续读落点由服务端按章节身份解析（detail.resume），这两个声明位于
// 被测片段之外，抽取时需补上
let _resume = null;
let _resumeInexact = false;
`;
const RC_RENDERSCROLL = slice(READER,
  'function renderScroll(imgs, chName, gen, chId) {', '\n// 挂载单张图片');
const RC_MOUNT = slice(READER,
  'function _mountImg(w, item, idx, imgs, force, onDone, gen, chId) {',
  "\ndocument.addEventListener('keydown'");
const RC_CHAPTER = slice(READER,
  'function renderChapter(imgs, chName) {', '\nfunction renderPaged');
const RC_SHOWPAGE = slice(READER,
  'function showPage(view, imgs, per, gen, chId) {', '\nfunction preloadPaged');
const RC_GOTO = slice(READER,
  'async function gotoChapter(i, explicit) {', "\ndocument.addEventListener('visibilitychange'");

// 关键守卫应真在抽取片段中（源码级前提，非行为断言）
assert.ok(/if \(_gen !== _curGen\) \{ io\.disconnect\(\); return; \}/.test(RC_RENDERSCROLL),
  'renderScroll 的 IO 回调缺少代际守卫/disconnect');
assert.ok(/chapter\/' \+ encodeURIComponent\(_chid\) \+ '\/proxy/.test(RC_MOUNT),
  '_mountImg 延迟代理应使用冻结的 _chid，而非当前 chIdx');
assert.ok(/chapter\/' \+ encodeURIComponent\(_chid\) \+ '\/proxy/.test(RC_SHOWPAGE),
  'showPage 延迟代理应使用冻结的 _chid，而非当前 chIdx');

const rcTimers = [];              // 假定时器：仅入队，由测试手动 flush
let rcTimerId = 0;
const rcIOs = [];                 // 记录创建的 IntersectionObserver 替身
class FakeIO {
  constructor(cb, opts) { this.cb = cb; this.opts = opts || {}; this.ob = []; this.disconnected = false; rcIOs.push(this); }
  observe(el) { this.ob.push(el); }
  unobserve(el) { this.ob = this.ob.filter((x) => x !== el); }
  disconnect() { this.disconnected = true; this.ob = []; }
}
const rcEls = new Map();
const rcGet = (sel) => { if (!rcEls.has(sel)) rcEls.set(sel, makeEl('div')); return rcEls.get(sel); };
const rcSandbox = {
  console, JSON, Promise, encodeURIComponent, decodeURIComponent, Math, Array, String, Object,
  document: {
    title: '', hidden: false, body: { scrollHeight: 1000 }, documentElement: { style: {} },
    createElement: (t) => makeEl(t),
    getElementById: (id) => rcGet('#' + id),
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener() {},
  },
  window: { scrollTo() {}, scrollBy() {}, addEventListener() {}, innerWidth: 1024, innerHeight: 800 },
  $: rcGet, esc: (s) => String(s),
  IntersectionObserver: FakeIO,
  Image: function () { return {}; },
  preloadPaged: () => {},
  saveProgress: () => {},
  setTimeout: (fn, ms) => { const id = ++rcTimerId; rcTimers.push({ id, fn, ms }); return id; },
  clearTimeout: () => {},
  SOURCE: 'src', CID: 'cid', TITLE: 'TITLE', chIdx0: 0, pg0: 1, _urlChExplicit: false, _histTimer: null,
  location: { hostname: 'localhost' },
  // gotoChapter 依赖：章节加载用 fetch（队列驱动）+ AbortController + 定位桩
  AbortController: class { constructor() { this.signal = {}; } abort() {} },
  fetch: (u, opt) => rcFetch(u, opt),
  scrollToPage: () => {},
  _locateY: -1, _userMoved: false, loading: false, _urlChExplicit2: false,
};
// gotoChapter 的 fetch 队列：每个 spec = {json} 或 {reject}
let rcFetchQueue = [];
const rcFetchLog = [];
const rcFetch = (u) => {
  rcFetchLog.push(u);
  if (!rcFetchQueue.length) {
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ images: [] }) });
  }
  const spec = rcFetchQueue.shift();
  if (spec.reject) return Promise.reject(spec.reject);
  return Promise.resolve({ ok: true, status: spec.status || 200,
    json: () => (spec.jsonReject ? Promise.reject(spec.jsonReject)
                                 : Promise.resolve(spec.json)) });
};
vm.createContext(rcSandbox);
vm.runInContext(RC_DECL + 'let loading = false;\nlet chIdx0 = 0;\nlet pg0 = 1;\n'
  + RC_RENDERSCROLL + '\n' + RC_MOUNT + '\n' + RC_CHAPTER + '\n' + RC_SHOWPAGE + '\n' + RC_GOTO +
  `\n;globalThis.__r = { renderScroll, _mountImg, showPage, renderChapter, gotoChapter,
   get curGen(){return _curGen;}, set curGen(v){_curGen=v;},
   get renderGen(){return _renderGen;}, set renderGen(v){_renderGen=v;},
   get curChId(){return _curChId;}, set curChId(v){_curChId=v;},
   get scrollIO(){return _scrollIO;}, setChapters(a){chapters=a;}, set chIdx(v){chIdx=v;},
   get chIdx(){return chIdx;}, get loading(){return loading;} };`,
  rcSandbox);
const R = rcSandbox.__r;
const flushTimers = () => { while (rcTimers.length) rcTimers.shift().fn(); };
const wrapsOf = (stage) => stage.children.filter((c) => (c.className || '').includes('img-wrap'));

async function testReader() {
  const stage = rcGet('#stage');
  const imgsA = [{ url: 'http://cdn/a1.jpg' }, { url: 'http://cdn/a2.jpg' }];

  // (a) renderScroll 建 IO；切章后旧 IO 回调必须 disconnect 且不再挂图
  R.curGen = 100; R.curChId = 'CH1';
  R.renderScroll(imgsA, 'ch1', 100, 'CH1');
  assert.strictEqual(rcIOs.length, 1, 'renderScroll 应创建 IntersectionObserver');
  const ioA = rcIOs[0];
  const wrapsA = wrapsOf(stage);
  assert.strictEqual(wrapsA.length, 2, '应创建 2 个 img-wrap');
  R.curGen = 101;                                   // 模拟切章
  ioA.cb([{ isIntersecting: true, target: wrapsA[0] }]);
  assert.strictEqual(ioA.disconnected, true, '切章后旧 IO 回调应 disconnect');
  assert.ok(!wrapsA[0].querySelector('img'), '切章后旧 IO 不得再挂图');

  // (b) renderChapter 切章应 disconnect 旧的 _scrollIO 并递增代际
  R.curGen = 102; R.renderGen = 102; R.curChId = 'CH2';
  R.setChapters([{ id: 'CH2' }]); R.chIdx = 0;
  R.renderScroll(imgsA, 'ch2', 102, 'CH2');
  const ioOld = rcIOs[rcIOs.length - 1];
  assert.strictEqual(R.scrollIO, ioOld, 'renderScroll 应记录 _scrollIO');
  const genBefore = R.curGen;
  R.renderChapter(imgsA, 'ch2b');
  assert.strictEqual(ioOld.disconnected, true, 'renderChapter 应 disconnect 旧 _scrollIO');
  assert.ok(R.curGen > genBefore, 'renderChapter 应递增渲染代际');

  // (c) _mountImg：旧代 onload/onerror 不得改动 DOM、不得安排定时器
  const w1 = makeEl('div');
  const ph1 = makeEl('div'); ph1.className = 'img-placeholder'; w1.appendChild(ph1);
  rcTimers.length = 0;
  R.curGen = 200; R.curChId = 'OWN';
  R._mountImg(w1, { url: 'http://cdn/x.jpg' }, 0, [{ url: 'http://cdn/x.jpg' }], false, null, 200, 'OWN');
  const img1 = w1.children.find((c) => c.tagName === 'img');
  assert.ok(img1, '_mountImg 应挂载 img');
  assert.strictEqual(img1.src, 'http://cdn/x.jpg', '首次 src 应为源站地址');
  R.curGen = 201;                                   // 切章
  img1.onload();
  assert.notStrictEqual(ph1._removed, true, '旧代 onload 不得移除占位');
  img1.onerror();
  assert.strictEqual(rcTimers.filter((t) => t.ms < 10000).length, 0,
    '旧代 onerror 不得安排代理降级定时器');
  assert.strictEqual(img1.dataset.retried, undefined, '旧代 onerror 不得标记重试');
  assert.strictEqual(img1.src, 'http://cdn/x.jpg', '旧代 onerror 不得改写 src');

  // (d) _mountImg：延迟代理必须用“所属章 id”，绝不引用当前 chIdx
  const w2 = makeEl('div');
  const ph2 = makeEl('div'); ph2.className = 'img-placeholder'; w2.appendChild(ph2);
  rcTimers.length = 0;
  R.curGen = 300; R.curChId = 'CH_OWN';
  R._mountImg(w2, { url: 'http://cdn/y.jpg' }, 0, [{ url: 'http://cdn/y.jpg' }], false, null, 300, 'CH_OWN');
  const img2 = w2.children.find((c) => c.tagName === 'img');
  R.curChId = 'CH_CUR';                             // 当前章已变为其它章
  R.setChapters([{ id: 'CH_CUR' }]); R.chIdx = 0;
  img2.onerror();                                   // 同代错误 → 安排降级
  assert.ok(rcTimers.length >= 1, '同代 onerror 应安排代理降级定时器');
  flushTimers();
  assert.ok(img2.src.includes('/chapter/CH_OWN/proxy'), '代理地址应含所属章 CH_OWN：' + img2.src);
  assert.ok(!img2.src.includes('CH_CUR'), '代理地址不得引用当前章 CH_CUR');

  // (d2) _mountImg 预取：前视窗口 LOOKAHEAD 张须真实发起，且句柄必须保留
  //      引用——脱离 DOM 的 Image 被 GC 后浏览器会取消未完成的加载（预取白做）
  const pfCreated = [];
  rcSandbox.Image = function () { const o = {}; pfCreated.push(o); return o; };
  const imgsC = [{ url: 'http://cdn/p0.jpg' }, { url: 'http://cdn/p1.jpg' },
    { url: 'http://cdn/p2.jpg' }, { url: 'http://cdn/p3.jpg' },
    { url: 'http://cdn/p4.jpg' }, { url: 'http://cdn/p5.jpg' },
    { url: 'http://cdn/p6.jpg' }];
  const w3 = makeEl('div');
  R.curGen = 350; R.curChId = 'PF';
  R._mountImg(w3, imgsC[2], 2, imgsC, false, null, 350, 'PF');
  const pfUrls = pfCreated.map((o) => o.src).sort();
  assert.deepStrictEqual(pfUrls,
    ['http://cdn/p1.jpg', 'http://cdn/p3.jpg', 'http://cdn/p4.jpg',
     'http://cdn/p5.jpg', 'http://cdn/p6.jpg'].sort(),
    '应预取 前 1 张 + 后 4 张（LOOKAHEAD=4）:' + pfUrls.join(','));
  assert.strictEqual(imgsC._pre.keep.length, 5,
    '预取句柄必须保留引用（否则可能被 GC 取消）');
  const pfN = pfCreated.length;
  R._mountImg(makeEl('div'), imgsC[2], 2, imgsC, false, null, 350, 'PF');
  assert.strictEqual(pfCreated.length, pfN, '同一张图不得重复预取');
  assert.ok(!pfUrls.some((u) => u.includes('p0.jpg') || u.includes('p2.jpg')),
    '前视窗口之外的页不应预取:' + pfUrls.join(','));

  // (e) showPage：旧代不得改动 DOM；延迟代理同样冻结所属章
  const view = makeEl('div'); view.className = 'page-view';
  const imgsB = [{ url: 'http://cdn/p1.jpg' }, { url: 'http://cdn/p2.jpg' }];
  rcTimers.length = 0;
  R.curGen = 400; R.curChId = 'SP_OWN';
  R.showPage(view, imgsB, 1, 400, 'SP_OWN');
  const pImg = view.children.find((c) => c.tagName === 'img');
  const pPh = view.children.find((c) => (c.className || '').includes('page-ph'));
  assert.ok(pImg && pPh, 'showPage 应渲染 img + 占位');
  R.curGen = 401;                                   // 切章
  pImg.onload();
  assert.notStrictEqual(pPh._removed, true, '旧代 showPage onload 不得移除占位');
  pImg.onerror();
  assert.strictEqual(rcTimers.filter((t) => t.ms < 10000).length, 0,
    '旧代 showPage onerror 不得安排降级');

  const view2 = makeEl('div'); view2.className = 'page-view';
  R.curGen = 402; R.curChId = 'SP_CUR';
  R.showPage(view2, imgsB, 1, 402, 'SP_OWN');
  const pImg2 = view2.children.find((c) => c.tagName === 'img');
  R.curChId = 'SP_CUR';
  pImg2.onerror();
  assert.ok(rcTimers.length >= 1, 'showPage 同代 onerror 应安排降级');
  flushTimers();
  assert.ok(pImg2.src.includes('/chapter/SP_OWN/proxy'), 'showPage 代理地址应含所属章 SP_OWN：' + pImg2.src);
  assert.ok(!pImg2.src.includes('SP_CUR'), 'showPage 代理地址不得引用当前章');
}

// ── 3b. 章节加载：空预取/空列表/失败重试（"图片不加载"的两个客户端成因）──
async function testReaderChapterLoading() {
  const stage = rcGet('#stage');

  // (a) 空的"下一话预取"必须丢弃并重新请求 /urls
  //     —— 旧实现直接把空 images 当有效数据复用，这一章永远是"没有图"，
  //     而且客户端不会再请求一次（用户看到：切章后/刷新后页面再也不加载）
  rcSandbox.window._nextChImgs = { idx: 1, images: [] };
  R.setChapters([{ id: 'C1', name: 'ch1' }, { id: 'C2', name: 'ch2' }]);
  R.chIdx = 0;
  rcFetchLog.length = 0;
  rcFetchQueue = [{ json: { images: [{ url: '/api/manga/jm/cid/chapter/C2/img/0' },
                                     { url: '/api/manga/jm/cid/chapter/C2/img/1' }] } }];
  await R.gotoChapter(1);
  assert.ok(rcFetchLog.some((u) => u.includes('/chapter/C2/urls')),
    '空预取必须丢弃并重新请求 /urls：' + rcFetchLog.join(' , '));
  assert.ok(!rcSandbox.window._nextChImgs, '空预取用后必须清掉');
  assert.strictEqual(wrapsOf(stage).length, 2, '重新拉取后应渲染 2 页');
  assert.ok(!/章节加载失败/.test(stage.innerHTML || ''), '正常拉取不得报加载失败');
  assert.strictEqual(R.loading, false, '加载结束 loading 必须复位');

  // (b) 非空预取仍复用（不得白打一次 /urls）
  R.setChapters([{ id: 'C1', name: 'ch1' }, { id: 'C2', name: 'ch2' },
                 { id: 'C3', name: 'ch3' }]);
  R.chIdx = 1;
  rcSandbox.window._nextChImgs = { idx: 2, images: [{ url: '/api/manga/jm/cid/chapter/C3/img/0' }] };
  rcFetchLog.length = 0;
  rcFetchQueue = [{ json: { images: [] } }];      // 若真请求了会拿到空 → 走报错分支
  await R.gotoChapter(2);
  assert.ok(!rcFetchLog.some((u) => u.includes('/chapter/C3/urls')),
    '非空预取应复用，不得重复请求：' + rcFetchLog.join(' , '));
  assert.strictEqual(wrapsOf(stage).length, 1, '复用的预取应渲染 1 页');

  // (c) /urls 返回空列表 → 明确报错 + 可重试；绝不渲染"空白章节"
  R.setChapters([{ id: 'C1', name: 'ch1' }, { id: 'C2', name: 'ch2' }]);
  R.chIdx = 0;
  rcSandbox.window._nextChImgs = null;
  rcFetchQueue = [{ json: { images: [] } }, { json: { images: [] } }];
  await R.gotoChapter(1);
  const html = stage.innerHTML || '';
  assert.ok(/章节加载失败/.test(html), '空章节必须明确报错，而不是空白页：' + html.slice(0, 120));
  assert.ok(/重试/.test(html), '空章节错误必须给出重试入口');
  assert.strictEqual(wrapsOf(stage).length, 0, '空章节不得渲染空白页框');
  assert.strictEqual(R.loading, false, '异常后 loading 必须复位（否则之后再也切不了章）');

  // (d) jm 服务器通道（/api/ 相对地址）失败 → 同地址缓存击穿重试，绝不走 /proxy
  const w4 = makeEl('div');
  const ph4 = makeEl('div'); ph4.className = 'img-placeholder'; w4.appendChild(ph4);
  const imgsD = [{ url: '/api/manga/jm/cid/chapter/CHX/img/3' }];
  rcTimers.length = 0;
  R.curGen = 600; R.curChId = 'CHX';
  R._mountImg(w4, imgsD[0], 0, imgsD, false, null, 600, 'CHX');
  const imgD = w4.children.find((c) => c.tagName === 'img');
  imgD.onerror();
  flushTimers();
  assert.ok(imgD.src.indexOf('/img/3?r=') > 0,
    'jm 失败重试必须带 cache-buster 重打同一地址：' + imgD.src);
  assert.ok(!imgD.src.includes('/proxy'),
    'jm 相对地址绝不得走 /proxy（/proxy 只收公网 http(s)，必然 400 → 永久加载失败）');

  // (e) 挂死看门狗：既不 onload 也不 onerror（源站挂住）→ 定时按同地址重试
  const w5 = makeEl('div');
  const ph5 = makeEl('div'); ph5.className = 'img-placeholder'; w5.appendChild(ph5);
  const imgsE = [{ url: '/api/manga/jm/cid/chapter/CHX/img/4' }];
  rcTimers.length = 0;
  R.curGen = 601; R.curChId = 'CHX';
  R._mountImg(w5, imgsE[0], 0, imgsE, false, null, 601, 'CHX');
  const imgE = w5.children.find((c) => c.tagName === 'img');
  const wd = rcTimers.filter((t) => t.ms >= 10000);
  assert.ok(wd.length >= 1, '应安装挂死看门狗（请求永不 settle 时占位不会一直挂着）');
  wd.forEach((t) => t.fn());
  flushTimers();
  assert.ok(imgE.src.indexOf('/img/4?r=') > 0, '挂死应触发同地址重试：' + imgE.src);
}


// ── 3c. library.html 漫画卡片封面：必须用服务器封面接口（本地优先/离线可看）──
const LIB_ALL = read('templates/library.html');
assert.ok(/c\.cover_view \|\| c\.cover/.test(LIB_ALL),
  '漫画卡片封面应优先用 cover_view（服务器本地优先接口），否则断网时封面空白');

// ── 3d. 漫画阅读器：进度同步门控（"重连后进度被重置"的根因）──
// 实测缺陷：网络重连瞬间 /api/manga/history 读取失败 → 前端回退到第 1 话
// → 用户一滚动 saveProgress() 就把 P1 写回服务器，两端进度一起被改掉。
// 契约：读到服务器历史之前绝不写；读到后按当前进度写；显式点章也要恢复
// 同话的章内页码；迟到读到时若用户未自行导航则跟随服务器进度。
const MP_DECL = `let _histReady = false;
let _fallbackChIdx = null;
let _histTimer = null;
let chIdx = 0;
let chIdx0 = 0;
let pg0 = 1;
let chapters = [];
let _urlChExplicit = false;
let settings = {mode:'scroll'};
let window = {_curImgs: [], _nextChImgs: null, scrollY: 0, innerHeight: 800};
const SOURCE = 'src', CID = 'cid', TITLE = 'T';
let _jumped = [];
function jumpChapter(i) { _jumped.push(i); chIdx = i; }
let _mpSent = [];
function _postHistory(payload, tag, retried) { _mpSent.push(payload); }
let _pageIdx = 0;
// 0.64.0：续读落点改为"服务端按章节身份解析"后的声明；以及"只写正在显示的那一话"
// 的门槛依赖的当前渲染章节 id
let _resume = null;
let _resumeInexact = false;
let _curChId = '';
const _mpBeacons = [];
const navigator = {sendBeacon: (u, b) => { _mpBeacons.push({u, b}); return _mpBeaconOk; }};
let _mpBeaconOk = true;
`;
const MP_RESTORE = slice(READER,
  'async function restoreProgress(attempt = 0) {', '\n// ── 阅读进度：记录 章节+页码');
const MP_SAVE = slice(READER,
  'function saveProgress() {', '\n// 定位锚点：scrollToPage 期间');
const MP_BEACON = slice(READER,
  'function saveProgressBeacon() {', '\nfunction saveProgress() {');
const MP_PAGE = slice(READER,
  'function currentPage() {', '\nfunction _postHistory');

const mpPosts = [];
const mpStage = makeEl('div');
for (let i = 0; i < 30; i++) { const w = makeEl('div'); w.className = 'img-wrap'; w.offsetTop = i * 1000; w.offsetHeight = 1000; mpStage.appendChild(w); }
const mpSandbox = {
  // Blob：sendBeacon 的载荷要带 application/json 类型（缺失会让 beacon 抛错被静默吞掉）
  Blob,
  console, JSON, Math, Array, String, Object, Promise, encodeURIComponent, Date,
  setTimeout: (fn) => { fn(); return 0; },     // 立即执行重试，便于断言
  clearTimeout: () => {},
  fetch: (u, opt) => mpFetch(u, opt),
  document: { querySelector: () => null, querySelectorAll: () => mpStage.children, addEventListener() {} },
  window: { _curImgs: [], scrollY: 0, innerHeight: 800 },
  $: () => makeEl('div'),
};
let mpQueue = [];
function mpFetch(u, opt) {
  const method = (opt && opt.method) || 'GET';
  if (method === 'POST') { mpPosts.push({ u, body: (opt && opt.body) || '' }); return Promise.resolve({ json: () => Promise.resolve({ ok: true }) }); }
  const spec = mpQueue.shift();
  if (!spec) return Promise.resolve({ json: () => Promise.resolve({ history: [] }) });
  if (spec.reject) return Promise.reject(spec.reject);
  return Promise.resolve({ json: () => Promise.resolve(spec.json) });
}
vm.createContext(mpSandbox);
vm.runInContext(MP_DECL + '\n' + MP_RESTORE + '\n' + MP_PAGE + '\n' + MP_BEACON + '\n' + MP_SAVE +
  `\n;globalThis.__mp = { restoreProgress, saveProgress, saveProgressBeacon,
     beacons(){return _mpBeacons;}, clearBeacons(){_mpBeacons.length = 0;},
     set beaconOk(v){_mpBeaconOk = v;},
     get ready(){return _histReady;}, set ready(v){_histReady=v;},
     get chIdx(){return chIdx;}, set chIdx(v){chIdx=v;},
     get chIdx0(){return chIdx0;}, set chIdx0(v){chIdx0=v;},
     get pg0(){return pg0;}, set pg0(v){pg0=v;},
     get fallback(){return _fallbackChIdx;}, set fallback(v){_fallbackChIdx=v;},
     get explicit(){return _urlChExplicit;}, set explicit(v){_urlChExplicit=v;},
     setChapters(a){chapters=a;}, jumped(){return _jumped;}, clearJumped(){_jumped=[];},
     // 0.64.0：写进度前要求"当前渲染的章节 == 要写的章节"，沙箱要能设置它
     get curChId(){return _curChId;}, set curChId(v){_curChId=v;},
     sent(){return _mpSent;}, clearSent(){_mpSent=[];} };`,
  mpSandbox);
const MP = mpSandbox.__mp;

async function testMangaProgressSync() {
  // (a) 历史读取失败（模拟重连）→ 允许写前必须仍为 false；saveProgress 不得回写
  MP.clearSent(); mpQueue = [{ reject: new Error('network') }];
  MP.ready = false; MP.chIdx = 0;
  MP.setChapters([{id: 'c1', name: '第1話'}]);
  await MP.restoreProgress(0);
  assert.strictEqual(MP.ready, false, '历史读取失败时不得进入可写状态');
  MP.saveProgress();
  assert.strictEqual(MP.sent().length, 0,
    '读取失败后 saveProgress 绝不允许写服务器（否则重连会把两端进度改成第1话）');

  // (b) 历史读取成功 → 可写；saveProgress 写入当前章节与页码
  MP.clearSent(); mpQueue = [{ json: { history: [{ source: 'src', comic_id: 'cid', idx: 4, pos: '第5話 P9' }] } }];
  await MP.restoreProgress(0);
  assert.strictEqual(MP.ready, true, '读到历史后应进入可写状态');
  assert.strictEqual(MP.chIdx0, 4, '应恢复历史章节');
  assert.strictEqual(MP.pg0, 9, '应恢复历史页码');
  MP.chIdx = 4; MP.setChapters([{id: 'c1', name: '第1話'}, {id: 'c2', name: '第2話'},
                                {id: 'c3', name: '第3話'}, {id: 'c4', name: '第4話'},
                                {id: 'c5', name: '第5話'}]);
  // (b0) 0.64.0：目标话**还在加载中**（尚未渲染）时，任何自动保存都必须被拒绝。
  //      否则 chIdx 的初始值 0 会被写进服务器 → 记录变成"第1话 P1"，
  //      用户下次进来就只能从第1话开始（实测复现：第23话退出→再进跳第1话）。
  MP.curChId = '';
  MP.saveProgress();
  assert.strictEqual(MP.sent().length, 0,
    '章节尚未渲染时不得写进度（会写进 chIdx 初始值 0 → 覆盖成第1话）');
  MP.curChId = 'c5';       // 渲染完成：_curChId 与 chapters[chIdx].id 一致
  MP.saveProgress();
  assert.strictEqual(MP.sent().length, 1, '读到历史后应正常写进度');
  assert.strictEqual(MP.sent()[0].idx, 4, '写入的应是当前章节');
  assert.ok(String(MP.sent()[0].pos).startsWith('第5話 P'),
    '写入的应是当前话与页码：' + MP.sent()[0].pos);

  // (c) 显式点章（URL 带 ch）：与历史同话时必须恢复章内页码
  MP.ready = false; MP.explicit = true; MP.chIdx0 = 5; MP.pg0 = 1;
  mpQueue = [{ json: { history: [{ source: 'src', comic_id: 'cid', idx: 5, pos: '第6話 P37' }] } }];
  await MP.restoreProgress(0);
  assert.strictEqual(MP.pg0, 37, '显式点进同一话时应恢复章内页码，而不是从 P1 开始');
  assert.strictEqual(MP.chIdx0, 5, '显式章节不得被历史改写');

  // (d) 显式点章到别的话：不得套用其它话的页码
  MP.ready = false; MP.explicit = true; MP.chIdx0 = 2; MP.pg0 = 1;
  mpQueue = [{ json: { history: [{ source: 'src', comic_id: 'cid', idx: 5, pos: '第6話 P37' }] } }];
  await MP.restoreProgress(0);
  assert.strictEqual(MP.pg0, 1, '不同话不得套用历史页码');
  assert.strictEqual(MP.chIdx0, 2, '显式章节不得被历史改写');

  // (e) 迟到读到历史（重连后补读）：用户仍在回退话 → 跟随服务器进度
  MP.ready = false; MP.explicit = false; MP.clearJumped();
  MP.setChapters([{id: 'c1', name: '第1話'}, {id: 'c2', name: '第2話'},
                  {id: 'c3', name: '第3話'}, {id: 'c4', name: '第4話'},
                  {id: 'c5', name: '第5話'}]);
  MP.chIdx = 0; MP.fallback = 0;
  mpQueue = [{ json: { history: [{ source: 'src', comic_id: 'cid', idx: 4, pos: '第5話 P9' }] } }];
  await MP.restoreProgress(1);
  assert.strictEqual(MP.ready, true);
  assert.strictEqual(MP.jumped().length, 1, '用户未导航时应跟随服务器进度');
  assert.strictEqual(MP.jumped()[0], 4, '应跳到服务器进度所在话：' + MP.jumped());

  // (f) 迟到读到历史但用户已自行翻话：尊重用户位置，只恢复可写
  MP.ready = false; MP.explicit = false; MP.clearJumped();
  MP.chIdx = 2; MP.fallback = 0;
  mpQueue = [{ json: { history: [{ source: 'src', comic_id: 'cid', idx: 4, pos: '第5話 P9' }] } }];
  await MP.restoreProgress(1);
  assert.strictEqual(MP.ready, true);
  assert.strictEqual(MP.jumped().length, 0, '用户已翻话时不得抢位');

  // (g) 退出/切后台保存：必须走 sendBeacon（普通 fetch 在卸载时会被浏览器取消，
  //     实测"读到一半退出后阅读记录不更新"）
  MP.clearBeacons(); MP.ready = false; MP.chIdx = 0;
  MP.setChapters([{id: 'c1', name: '第1話'}]);
  MP.saveProgressBeacon();
  assert.strictEqual(MP.beacons().length, 0, '未读到进度时不得写服务器（beacon 同样受门控）');
  MP.ready = true;
  MP.curChId = '';                 // 目标话尚未渲染 → beacon 也必须拒绝
  MP.saveProgressBeacon();
  assert.strictEqual(MP.beacons().length, 0,
    '章节尚未渲染时 beacon 不得写（会把 chIdx 初始值 0 覆盖成第1话）');
  MP.curChId = 'c1';               // 渲染完成
  MP.saveProgressBeacon();
  assert.strictEqual(MP.beacons().length, 1, '退出时应通过 sendBeacon 保存');
  assert.ok(MP.beacons()[0].u.includes('/api/manga/history'),
    'beacon 目标应为历史接口：' + MP.beacons()[0].u);
  assert.strictEqual(MP.beacons()[0].b.type, 'application/json', 'beacon 需声明 JSON 类型');

  // (h) sendBeacon 不可用/失败 → 回落 keepalive fetch（仍保证送出）
  const mpFetchCalls = [];
  MP.beaconOk = false;
  mpQueue = [];
  const _origFetch = mpSandbox.fetch;
  mpSandbox.fetch = (u, opt) => { mpFetchCalls.push({u, opt}); return Promise.resolve({json: () => Promise.resolve({ok: true})}); };
  MP.curChId = 'c1';
  MP.saveProgressBeacon();
  assert.strictEqual(mpFetchCalls.length, 1, 'beacon 失败应回落 fetch');
  assert.strictEqual(mpFetchCalls[0].opt.keepalive, true, '回落 fetch 必须 keepalive');
  assert.strictEqual(mpFetchCalls[0].opt.method, 'POST');
  mpSandbox.fetch = _origFetch;
}


// ═══════════ 4. library.html 检查更新：失败绝不冒充“没有可检查的漫画” ═══════════
const LIB = read('templates/library.html');
const LIB_BLOCK = slice(LIB, 'async function mangaPollLoop(bar) {', '\nfunction mangaSetBtn');
assert.ok(/if \(pollFailed\)/.test(LIB_BLOCK), 'mangaShowResult 缺少 pollFailed 分支');
assert.ok(/检查状态获取失败/.test(LIB_BLOCK), '缺少“检查状态获取失败”文案');
assert.ok(/if \(s === null\)/.test(LIB_BLOCK), 'mangaPollThenFinish 未处理轮询返回 null');
assert.ok(/if \(!fd\)/.test(LIB_BLOCK), 'mangaPollThenFinish 未处理末次状态请求失败');

const libToast = [];
const libEls = new Map();
const libGet = (id) => { if (!libEls.has(id)) libEls.set(id, makeEl('div')); return libEls.get(id); };
let libFetchQueue = [];
const libNext = () => {
  const spec = libFetchQueue.shift();
  if (!spec) return Promise.reject(new Error('no more fetch'));   // 队列耗尽 → 视为网络失败
  if (spec.reject) return Promise.reject(spec.reject);
  return Promise.resolve({ ok: true, status: 200, json: () =>
    (spec.jsonReject ? Promise.reject(spec.jsonReject) : Promise.resolve(spec.json)) });
};
const libSandbox = {
  console, JSON, Promise, Date, Math, Array, String, Object, encodeURIComponent,
  document: { getElementById: libGet, createElement: (t) => makeEl(t), querySelector: () => null, addEventListener() {} },
  esc: (s) => String(s),
  toast: (m, t) => { libToast.push({ m, t }); },
  setTimeout: (fn) => { fn(); return 0; },   // 立即触发：跳过 1.2s 轮询间隔
  fetch: () => libNext(),
};
vm.createContext(libSandbox);
vm.runInContext(LIB_BLOCK +
  '\n;globalThis.__lib = { mangaPollLoop, mangaShowResult, mangaPollThenFinish };', libSandbox);
const L = libSandbox.__lib;

async function testLibrary() {
  const bar = libGet('manga-check-status');

  // (a) 轮询连续失败返回 null → 报“获取失败”，绝不报“没有可检查的漫画”
  libFetchQueue = [];                              // 每次 fetch 都 reject
  libToast.length = 0; bar.textContent = '';
  const r1 = await L.mangaPollThenFinish(false);
  assert.ok(/检查状态获取失败/.test(bar.textContent), '轮询失败应报获取失败：' + bar.textContent);
  assert.ok(!/没有可检查的漫画/.test(bar.textContent), '轮询失败绝不得显示“没有可检查的漫画”');
  // 失败分支不返回更新/错误明细（生产实现 return; —— 调用方不消费返回值），
  // 但若返回了对象则必须为空数组，绝不得携带伪造结果
  if (r1 && typeof r1 === 'object') {
    assert.strictEqual((r1.upd || []).length, 0, '失败若返回对象应含空更新');
    assert.strictEqual((r1.err || []).length, 0, '失败若返回对象应含空错误');
  }
  assert.ok(libToast.some((x) => x.t === 'error'), '失败应有 error 级 toast');

  // (b) 轮询成功但末次状态请求失败 → 同样报“获取失败”
  libFetchQueue = [{ json: { running: false, results: [] } }, { jsonReject: new Error('boom') }];
  bar.textContent = '';
  await L.mangaPollThenFinish(false);
  assert.ok(/检查状态获取失败/.test(bar.textContent), '末次状态失败应报获取失败：' + bar.textContent);
  assert.ok(!/没有可检查的漫画/.test(bar.textContent), '末次状态失败不得显示“没有可检查的漫画”');

  // (c) 确无漫画（轮询+末次都成功且 results 为空）→ 才显示“没有可检查的漫画”
  libFetchQueue = [{ json: { running: false, results: [] } }, { json: { running: false, results: [] } }];
  bar.textContent = ''; bar.innerHTML = '';
  await L.mangaPollThenFinish(false);
  // 成功分支经 bar.innerHTML 写入结果（非 textContent），故在此断言 innerHTML
  assert.ok(/没有可检查的漫画/.test(bar.innerHTML), '确无漫画应如实提示：' + bar.innerHTML);

  // (d) 正常有更新 → 不得被失败分支误触
  const updResults = [{ title: 'A', has_update: true, missing_count: 2 }];
  libFetchQueue = [{ json: { running: false, results: updResults } }, { json: { running: false, results: updResults } }];
  bar.textContent = ''; bar.innerHTML = '';
  await L.mangaPollThenFinish(false);
  assert.ok(/1 部有更新/.test(bar.innerHTML), '有更新应如实展示：' + bar.innerHTML);
}

// ═══════════ 5. netip.js poll：在途去重 + finally 清理 abort 定时器 ═══════════
const NETIP = read('static/js/netip.js');
assert.ok(/_inFlight/.test(NETIP), 'netip 缺少在途去重标志');
assert.ok(/finally\s*\{[\s\S]*clearTimeout\(t\)/.test(NETIP), 'netip 未在 finally 清理 abort 定时器');

const ntTimers = [];
let ntTimerId = 0;
const ntCleared = [];
const ntFetches = [];
let visCb = null, intervalFn = null;
const ntBar = makeEl('div');
const ntSandbox = {
  console, JSON, Promise, Date, Math, String, Object,
  esc: (s) => String(s),
  location: { hostname: '127.0.0.1' },
  document: {
    hidden: false,
    getElementById: () => ntBar,
    addEventListener: (ev, fn) => { if (ev === 'visibilitychange') visCb = fn; },
  },
  sessionStorage: { getItem: () => null, setItem() {} },
  AbortController: globalThis.AbortController,
  setTimeout: (fn, ms) => { const id = ++ntTimerId; ntTimers.push({ id, fn, ms }); return id; },
  clearTimeout: (id) => { ntCleared.push(id); },
  setInterval: (fn) => { intervalFn = fn; return 1; },
  fetch: (url, opts) => new Promise((resolve, reject) => ntFetches.push({ url, opts, resolve, reject })),
};
vm.createContext(ntSandbox);
vm.runInContext(NETIP, ntSandbox);

async function testNetip() {
  // 加载即发起首次轮询（已安排 abort 定时器）
  assert.strictEqual(ntFetches.length, 1, 'netip 加载应发起一次轮询');
  assert.strictEqual(ntTimers.length, 1, '轮询应安排一个 abort 定时器');
  assert.strictEqual(typeof visCb, 'function', '应注册 visibilitychange 回调');
  assert.strictEqual(typeof intervalFn, 'function', '应以 setInterval 周期轮询');

  // (a) 在途去重：请求未回来时 visibilitychange 不得并发第二次请求
  visCb();
  await tick();
  assert.strictEqual(ntFetches.length, 1, '在途时不得并发第二次请求（去重）');

  // 成功返回 → finally 清理 abort 定时器并释放锁
  ntFetches[0].resolve({ ok: true, status: 200,
    json: () => Promise.resolve({ port: 8766, ips: ['192.168.1.9'], host_local: 'mac.local' }) });
  await tick(); await tick();
  assert.ok(ntCleared.length >= 1, '成功后必须清理 abort 定时器');

  // (b) 锁已释放：再次 visibilitychange 可发起新请求
  visCb();
  await tick();
  assert.strictEqual(ntFetches.length, 2, '锁释放后应可发起新请求');

  // (c) 失败路径同样释放锁 + 清理定时器
  const clearedBefore = ntCleared.length;
  ntFetches[1].reject(new Error('network down'));
  await tick(); await tick();
  assert.ok(ntCleared.length > clearedBefore, '失败后也必须清理 abort 定时器');
  visCb();
  await tick();
  assert.strictEqual(ntFetches.length, 3, '失败后锁应释放，允许下一轮');

  ntFetches[2].resolve({ ok: true, status: 200,
    json: () => Promise.resolve({ port: 8766, ips: [], host_local: '' }) });
  await tick();
}

// ═══════════ 主流程（watchdog + 明确完成标记，防假通过）═══════════
const WATCHDOG_MS = 5000;
const watchdog = setTimeout(() => {
  console.error('audit_frontend_remaining.test.js: 主流程未在 ' + WATCHDOG_MS +
    'ms 内完成（疑似存在未 resolve 的 Promise）——判定失败');
  process.exit(1);
}, WATCHDOG_MS);

(async () => {
  await testSources();
  await testMangaDetail();
  await testReader();
  await testReaderChapterLoading();
  await testMangaProgressSync();
  await testLibrary();
  await testNetip();
})().then(() => {
  clearTimeout(watchdog);
  console.log('audit_frontend_remaining.test.js: 全部断言通过');
}).catch((e) => {
  clearTimeout(watchdog);
  console.error(e);
  process.exit(1);
});
