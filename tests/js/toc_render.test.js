/* C04 目录分段渲染组件离线单测（node 直接运行，无浏览器依赖）
 *
 * 被测对象 = 线上实际运行的同一份代码 static/js/toc_render.js（UMD）。
 * 用一个最小假 DOM（appendChild/insertBefore/removeChild/innerHTML 清空/
 * DocumentFragment 摊平）驱动组件，断言：
 *   1. 2000 章长目录首次挂载节点数有界（≤ 分段常量 + 至多 2 个展开按钮）
 *   2. locate 切段：目标章所在段被挂载，越界/过滤态安全忽略
 *   3. 展开上一段/下一段按需扩段，挂载数始终有界
 *   4. 目录内搜索能找到未挂载章节（作用于全量数组，与挂载区间无关）
 *   5. 搜索大量命中时分段追加：首段有界，「显示更多」增量挂载且不重绘旧条目
 *
 * 运行：node tests/js/toc_render.test.js
 */
'use strict';
const assert = require('node:assert');
const path = require('node:path');
const TocRender = require(path.join(__dirname, '..', '..', 'static', 'js', 'toc_render.js'));

// ── 最小假 DOM ──
function makeEl(tag) {
  const el = {
    tagName: tag, children: [], parentNode: null,
    className: '', textContent: '', title: '', onclick: null,
    dataset: {}, style: {},
    appendChild(c) { el.insertBefore(c, null); return c; },
    insertBefore(c, ref) {
      if (c.tagName === '#fragment') {   // DocumentFragment：摊平其子节点
        for (const k of c.children.slice()) el.insertBefore(k, ref);
        c.children.length = 0;
        return c;
      }
      c.parentNode = el;
      const i = ref == null ? -1 : el.children.indexOf(ref);
      if (i < 0) el.children.push(c); else el.children.splice(i, 0, c);
      return c;
    },
    removeChild(c) {
      const i = el.children.indexOf(c);
      if (i >= 0) el.children.splice(i, 1);
      c.parentNode = null;
      return c;
    },
    click() { if (el.onclick) el.onclick(); },
  };
  let html = '';
  Object.defineProperty(el, 'innerHTML', {
    set(v) { html = v; el.children.length = 0; },   // 赋值即清空子节点（与浏览器一致）
    get() { return html; },
  });
  return el;
}
const fakeDoc = {
  createElement: (tag) => makeEl(tag),
  createDocumentFragment: () => makeEl('#fragment'),
};

// 递归收集容器内所有条目文本（含嵌套）
function allText(node, acc) {
  acc = acc || [];
  if (node.textContent) acc.push(node.textContent);
  for (const c of node.children) allText(c, acc);
  return acc;
}
const itemCount = (root) => allText(root).filter(t => /^\d+\. /.test(t)).length;
const expandBtns = (root) => root.children.filter(c => c.className.indexOf('toc-expand') !== -1);

// ── 构造 2000 章长目录 ──
const TOTAL = 2000;
const chapters = Array.from({length: TOTAL}, (_, i) => ({index: i + 1, name: '第' + (i + 1) + '章 风云'}));
const container = makeEl('div');
const view = TocRender.makeTocView({
  container, doc: fakeDoc,
  makeItem: (c) => {
    const a = fakeDoc.createElement('a');
    a.dataset.chIdx = c.index;
    a.textContent = c.index + '. ' + c.name;
    return a;
  },
});

// ── 1. 首屏挂载有界 ──
view.setData(chapters);
view.render();
assert.strictEqual(view.mountedCount(), TocRender.TOC_CHUNK, '首屏只挂载一个分段');
assert.ok(itemCount(container) <= TocRender.TOC_CHUNK, '容器内章节节点数 ≤ 分段常量');
assert.ok(container.children.length <= TocRender.TOC_CHUNK + 2, '含展开按钮的总节点数有界');
assert.strictEqual(expandBtns(container).length, 1, '第一段只有「展开下一段」按钮');
assert.ok(allText(container).some(t => t.indexOf('1. 第1章') === 0), '首屏含第 1 章');

// ── 2. locate 切段 ──
assert.strictEqual(view.locate(5), false, '已在区间内：无变化');
assert.strictEqual(view.locate(1500), true, '区间外：切到目标段');
view.render();
assert.strictEqual(view.mountedCount(), TocRender.TOC_CHUNK, '切段后仍只挂载一个分段');
assert.ok(allText(container).some(t => t.indexOf('1500. 第1500章') === 0), '第 1500 章已挂载');
assert.ok(!allText(container).some(t => t.indexOf('1. 第1章') === 0), '旧分段已卸载');
assert.strictEqual(expandBtns(container).length, 2, '中间段同时有上/下展开按钮');

// ── 3. 展开按钮按需扩段 ──
const btns = expandBtns(container);
btns[0].click();   // 展开上一段
assert.strictEqual(view.mountedCount(), TocRender.TOC_CHUNK * 2, '向上扩一段后挂载两段');
assert.ok(allText(container).some(t => t.indexOf('1301. 第1301章') === 0), '上一段首章已挂载');
expandBtns(container)[1].click();   // 展开下一段
assert.strictEqual(view.mountedCount(), TocRender.TOC_CHUNK * 3, '向下扩一段后挂载三段');
assert.ok(allText(container).some(t => t.indexOf('1700. 第1700章') === 0), '下一段末章已挂载');

// 切回首段验证下一段按钮语义
view.locate(1); view.render();
expandBtns(container)[0].click();   // 首段无「上一段」，[0] 是「展开下一段」
assert.ok(allText(container).some(t => t.indexOf('201. 第201章') === 0), '展开下一段挂载第 201 章起');

// ── 4. 搜索能找到未挂载章节 ──
view.locate(1); view.render();
assert.ok(!allText(container).some(t => t.indexOf('1999.') === 0), '前置：第 1999 章未挂载');
view.setFilter('第1999章');
view.render();
assert.strictEqual(view.mountedCount(), 1, '精确搜索只命中一章');
assert.ok(allText(container).some(t => t.indexOf('1999. 第1999章') === 0),
  '搜索命中未挂载章节（作用于全量数组）');

// ── 5. 搜索大量命中：分段追加 ──
view.setFilter('第1');   // 命中 第1x/1xx/1xxx 等，远超一个分段
view.render();
const hitCount = TocRender.filterChapters(chapters, '第1').length;
assert.ok(hitCount > TocRender.TOC_CHUNK, '前置：命中数大于一个分段');
assert.strictEqual(view.mountedCount(), TocRender.TOC_CHUNK, '搜索结果首段挂载有界');
const firstMounted = allText(container).filter(t => /^\d+\. /.test(t));
const moreBtn = container.children[container.children.length - 1];
assert.ok(moreBtn.className.indexOf('toc-expand') !== -1, '末尾是「显示更多」按钮');
assert.ok(moreBtn.textContent.indexOf('剩余') !== -1, '按钮标注剩余章数');
moreBtn.click();
assert.strictEqual(view.mountedCount(), Math.min(hitCount, TocRender.TOC_CHUNK * 2),
  '点击后增量挂载下一段');
const afterMounted = allText(container).filter(t => /^\d+\. /.test(t));
assert.deepStrictEqual(afterMounted.slice(0, firstMounted.length), firstMounted,
  '增量挂载不重绘/不打乱已挂载条目');

// 清空搜索恢复分段视图（保留先前扩展的区间，挂载数仍有界）
view.setFilter('');
view.render();
assert.ok(view.mountedCount() > 0 && view.mountedCount() <= TocRender.TOC_CHUNK * 2,
  '清空搜索恢复分段视图，挂载数有界');

// ── 6. 纯函数：chunkWindow 边界 ──
assert.deepStrictEqual(TocRender.chunkWindow(1, 2000, 200), {from: 1, to: 200});
assert.deepStrictEqual(TocRender.chunkWindow(200, 2000, 200), {from: 1, to: 200});
assert.deepStrictEqual(TocRender.chunkWindow(201, 2000, 200), {from: 201, to: 400});
assert.deepStrictEqual(TocRender.chunkWindow(2000, 2000, 200), {from: 1801, to: 2000}, '末段截断到 total');
assert.deepStrictEqual(TocRender.chunkWindow(0, 2000, 200), {from: 1, to: 200}, '越界回退第一段');
assert.deepStrictEqual(TocRender.chunkWindow(9999, 2000, 200), {from: 1, to: 200}, '越界回退第一段');
assert.deepStrictEqual(TocRender.chunkWindow(1, 0, 200), {from: 0, to: 0}, '空目录安全');

// ── 7. 空目录 / 无命中占位 ──
view.setData([]);
view.render();
assert.ok(container.innerHTML.indexOf('暂无章节') !== -1, '空目录占位');
view.setData(chapters);
view.setFilter('不存在的章节名');
view.render();
assert.ok(container.innerHTML.indexOf('无匹配章节') !== -1, '搜索无命中占位');

console.log('toc_render.test.js: 全部断言通过');
