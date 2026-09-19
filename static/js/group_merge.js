/* group_merge.js —— 小说搜索"新增/更新"协议的纯函数核心（A03）
 *
 * 浏览器端挂全局 GroupMerge（index.html 通过 <script> 引入），
 * CommonJS 环境（node）直接 module.exports —— 供 tests/js/group_merge.test.js
 * 离线单测 require，保证被测代码与线上运行代码是同一份。
 *
 * 协议语义（与 server/novel_api.py api_search_stream 对应）：
 *   - 增量事件 groups  = 新出现的分组；updates = 已存在分组的内容更新
 *   - finished 事件 groups = 全量最终快照（校准来源/元数据/排序）
 * 合并原则：只增不减——静默刷新时缓存来源不被实时部分结果冲掉。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.GroupMerge = api;
})(typeof self !== 'undefined' ? self : globalThis, function () {
  'use strict';

  // 稳定分组 key：与服务端 sent_state、前端 DOM 索引一致
  function groupKey(g) {
    return ((g && g.name) || '') + '|' + ((g && g.author) || '');
  }

  // 内容指纹：判断已渲染卡片是否需要重绘。
  // 指纹不变 → 跳过重绘（避免闪烁、避免打断用户正在进行的选源操作）。
  function groupFingerprint(g) {
    const srcs = (g && g.sources) || [];
    return JSON.stringify([
      (g && g.name) || '', (g && g.author) || '',
      (g && g.intro) || '', (g && g.cover) || '',
      srcs.map(function (s) {
        return [
          (s && s.source_uid) || '', (s && s.book_url) || '',
          (s && s.chapter_count) || 0, (s && s.last_chapter) || '',
          (s && s.update_time) || '', (s && s.word_count) || '',
        ];
      }),
    ]);
  }

  // 来源并集：按 source_uid|book_url 去重（newSrcs 覆盖同键旧条目的元数据），
  // 结果按 chapter_count 降序（稳定排序：章节数相同/缺失时保持原相对顺序）。
  function mergeSources(oldSrcs, newSrcs) {
    const byKey = new Map();
    const order = [];
    const put = function (s) {
      if (!s) return;
      const k = ((s.source_uid) || '') + '|' + ((s.book_url) || '');
      if (!byKey.has(k)) order.push(k);
      byKey.set(k, s);
    };
    (oldSrcs || []).forEach(put);
    (newSrcs || []).forEach(put);   // 新数据覆盖同键旧元数据
    return order.map(function (k) { return byKey.get(k); })
      .map(function (s, i) { return [s, i]; })
      .sort(function (a, b) {
        return ((b[0].chapter_count || 0) - (a[0].chapter_count || 0)) || (a[1] - b[1]);
      })
      .map(function (x) { return x[0]; });
  }

  // 分组合并：newG 为权威（服务端重建的全量组），oldG 提供 newG 缺失的
  // 旧来源/简介/封面（静默刷新时缓存来源不被实时结果冲掉；同书晚到来源
  // 经 mergeSources 并入同一分组 → 一个卡片包含全部来源）。
  function mergeGroup(oldG, newG) {
    if (!oldG) return newG;
    if (!newG) return oldG;
    const out = Object.assign({}, newG);
    out.name = newG.name || oldG.name || '';
    out.author = newG.author || oldG.author || '';
    out.intro = newG.intro || oldG.intro || '';
    out.cover = newG.cover || oldG.cover || '';
    out.sources = mergeSources(oldG.sources, newG.sources);
    return out;
  }

  // 卡片更新后保持用户已选来源：按 source_uid 找回新索引，
  // 找不到（该来源在新数据中消失）时回退 fallback（通常为主源索引）。
  function reselectSourceIndex(sources, keepUid, fallback) {
    const list = sources || [];
    if (keepUid) {
      for (let i = 0; i < list.length; i++) {
        if (list[i] && list[i].source_uid === keepUid) return i;
      }
    }
    return (typeof fallback === 'number' && fallback >= 0) ? fallback : 0;
  }

  return {
    groupKey: groupKey,
    groupFingerprint: groupFingerprint,
    mergeSources: mergeSources,
    mergeGroup: mergeGroup,
    reselectSourceIndex: reselectSourceIndex,
  };
});
