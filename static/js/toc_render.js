/* toc_render.js — 目录分段渲染轻量组件（C04 抽取，详情页与阅读器共用）。
 *
 * 历史：reader.html P2-2 已有目录区间渲染（每段 200 章 + 上下扩段按钮），
 * 而 novel_detail.html 书库/搜索两种模式仍一次性 forEach 挂载全部章节，
 * 2000+ 章的书首屏要建数千个节点。本文件把该机制抽取为与页面解耦的组件：
 *
 *   - 分段挂载：任意时刻最多挂载一个连续区间（chunkSize 章/段）+ 至多两个
 *     「展开上一段/下一段」按钮，首屏只建当前章附近的节点，长目录挂载数有界；
 *   - locate(idx)：把挂载区间切到 idx（1 起）所在段（翻章/进度定位用），
 *     返回区间是否变化（调用方据此决定是否需要重绘）；
 *   - 搜索过滤：作用于全量章节目录数组（与是否已挂载无关），命中结果按同样
 *     步长分段追加（「显示更多」按钮增量挂载，不重绘已挂载部分）；
 *   - 条目外观由调用方 makeItem(ch) 提供，组件不管业务字段与跳转语义
 *     （详情页 ?ch=N 链接 / 阅读器 goto 跳章各自实现）。
 *
 * UMD 双导出：浏览器挂载 window.TocRender；node 下 module.exports，
 * 使 tests/js/toc_render.test.js 能用假 DOM 对线上同一份代码做语义断言。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TocRender = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // 分段大小：每段最多挂载 200 章（阅读器 P2-2 沿用至今的常量，两页统一）
  var TOC_CHUNK = 200;

  // 纯函数：目标章（1 起）所在分段窗口 [from, to]（1 起，含端点；total<1 时均为 0）
  function chunkWindow(targetIdx, total, chunk) {
    chunk = chunk || TOC_CHUNK;
    if (total < 1) return {from: 0, to: 0};
    var t = (targetIdx >= 1 && targetIdx <= total) ? targetIdx : 1;
    var from = Math.floor((t - 1) / chunk) * chunk + 1;
    return {from: from, to: Math.min(total, from + chunk - 1)};
  }

  // 纯函数：搜索过滤——作用于全量目录数组，未挂载章节同样可被找到
  function filterChapters(chapters, kw) {
    kw = (kw || '').trim().toLowerCase();
    if (!kw) return chapters;
    return chapters.filter(function (c) {
      return (c.name || '').toLowerCase().indexOf(kw) !== -1;
    });
  }

  /* makeTocView(opts)
   *   opts.container    挂载容器（必填）
   *   opts.makeItem(ch) 章节条目工厂（必填，返回 Element）
   *   opts.chunkSize    分段大小（默认 TOC_CHUNK）
   *   opts.doc          Document 注入（测试用；默认全局 document）
   *   opts.expandClass  展开按钮 class（默认 'toc-expand'，两页各自的 CSS 作用域限定）
   *   opts.emptyHTML    目录为空时的占位 HTML
   *   opts.noMatchHTML  搜索无命中时的占位 HTML
   *
   * 返回视图对象：
   *   setData(chapters)   设置全量目录（重置分段/过滤挂载状态）
   *   setFilter(kw)       设置搜索关键字（render 时生效）
   *   getFilter()         当前关键字
   *   locate(idx)         确保 idx 所在段纳入挂载区间；返回区间是否变化
   *   render()            按当前状态重绘（未过滤态整段重绘；过滤态重置为首段+增量）
   *   mountedCount()      当前已挂载章节条目数（不含展开按钮；测试断言有界用）
   */
  function makeTocView(opts) {
    var container = opts.container;
    var doc = opts.doc || (typeof document !== 'undefined' ? document : null);
    var chunk = opts.chunkSize || TOC_CHUNK;
    var makeItem = opts.makeItem;
    var expandClass = opts.expandClass || 'toc-expand';
    var emptyHTML = opts.emptyHTML != null ? opts.emptyHTML
      : '<div style="padding:12px;color:var(--text-dim);font-size:.8rem;">暂无章节</div>';
    var noMatchHTML = opts.noMatchHTML != null ? opts.noMatchHTML
      : '<div style="padding:12px;color:var(--text-dim);font-size:.8rem;">无匹配章节</div>';

    var chapters = [];
    var from = 0, to = 0;      // 未过滤态：已挂载区间（1 起，含端点；0 表示未定位）
    var kw = '';               // 过滤关键字（已 trim + 小写）
    var hits = [];             // 过滤态：全量数组中的命中章节
    var hitMounted = 0;        // 过滤态：已挂载命中条数

    function setData(list) {
      chapters = list || [];
      from = 0; to = 0; hits = []; hitMounted = 0;
    }
    function setFilter(k) { kw = (k || '').trim().toLowerCase(); }
    function getFilter() { return kw; }

    // 定位：确保 idx（1 起）所在分段纳入挂载区间；过滤态不干预（由用户自行滚动）。
    // 返回 true 表示区间发生变化（或首次定位），调用方应重绘。
    function locate(idx) {
      if (kw || idx < 1 || idx > chapters.length) return false;
      if (!to || idx < from || idx > to) {
        var w = chunkWindow(idx, chapters.length, chunk);
        var changed = (w.from !== from || w.to !== to);
        from = w.from; to = w.to;
        return changed;
      }
      return false;
    }

    function mountedCount() {
      if (kw) return hitMounted;
      return (from >= 1 && to >= from) ? to - from + 1 : 0;
    }

    function makeExpand(label, onClick) {
      var a = doc.createElement('a');
      a.className = expandClass;
      a.textContent = label;
      a.onclick = onClick;
      return a;
    }

    function render() {
      container.innerHTML = '';
      if (!chapters.length) { container.innerHTML = emptyHTML; return; }
      if (kw) { renderFiltered(); return; }
      if (!to) { var w = chunkWindow(1, chapters.length, chunk); from = w.from; to = w.to; }
      var total = chapters.length;
      if (from > 1) {
        container.appendChild(makeExpand('▴ 展开上一段（第 1-' + (from - 1) + ' 章）', function () {
          // 向上前置内容会下移视口：渲染后补偿滚动位移，保持列表位置稳定
          var prevH = container.scrollHeight, prevTop = container.scrollTop;
          from = Math.max(1, from - chunk);
          render();
          if (typeof container.scrollHeight === 'number' && typeof container.scrollTop === 'number') {
            container.scrollTop = prevTop + (container.scrollHeight - prevH);
          }
        }));
      }
      var frag = doc.createDocumentFragment();
      for (var i = from; i <= to; i++) frag.appendChild(makeItem(chapters[i - 1]));
      container.appendChild(frag);
      if (to < total) {
        container.appendChild(makeExpand('▾ 展开下一段（第 ' + (to + 1) + '-' +
          Math.min(total, to + chunk) + ' 章）', function () {
          to = Math.min(total, to + chunk);
          render();
        }));
      }
    }

    // 过滤态：整段重绘为首段；「显示更多」增量挂载下一段（不重绘已挂载条目）
    function renderFiltered() {
      hits = filterChapters(chapters, kw);
      hitMounted = 0;
      if (!hits.length) { container.innerHTML = noMatchHTML; return; }
      var more = makeExpand('', null);
      more.onclick = function () { mountHitChunk(more); };
      mountHitChunk(more);
    }
    function mountHitChunk(more) {
      var end = Math.min(hitMounted + chunk, hits.length);
      var frag = doc.createDocumentFragment();
      for (; hitMounted < end; hitMounted++) frag.appendChild(makeItem(hits[hitMounted]));
      container.insertBefore(frag, more.parentNode ? more : null);
      if (hitMounted < hits.length) {
        more.textContent = '▾ 显示更多（剩余 ' + (hits.length - hitMounted) + ' 章）';
        if (!more.parentNode) container.appendChild(more);
      } else if (more.parentNode) {
        more.parentNode.removeChild(more);
      }
    }

    return {
      setData: setData,
      setFilter: setFilter,
      getFilter: getFilter,
      locate: locate,
      render: render,
      mountedCount: mountedCount,
    };
  }

  return {
    TOC_CHUNK: TOC_CHUNK,
    chunkWindow: chunkWindow,
    filterChapters: filterChapters,
    makeTocView: makeTocView,
  };
});
