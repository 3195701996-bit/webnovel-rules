/* chapter_cache.js — 小说章节正文缓存（C05 抽取，阅读器使用）。
 *
 * 修复 reader.html 原预取缓存的三处缺陷：
 *   1. 无在途去重：预取请求未返回时用户切入该章会再发一次相同请求。
 *      → pending Map：同一 idx 的 get/prefetch 共享同一 Promise；
 *        失败/取消从 pending 删除，允许后续重试。
 *   2. 淘汰时机错误：原实现先检查容量、网络完成后才写入，并发预取可越过上限。
 *      → 完成入库（put）时循环约束容量，乱序返回也不会超限。
 *   3. 假 LRU：Object.keys 对整数键按升序枚举，"删除首项"删掉的是最小编号章
 *      而非最久未访问章。
 *      → Map 插入序即访问序：每次命中（get/peek）delete+set 重排到最新，
 *        淘汰删 Map 首键 = 最久未访问章。
 *   4. 版本失效：invalidateAll() 清空缓存与 pending 并 bump 代际——重爬/目录
 *      刷新后在途旧代请求 resolve 时既不入库也不回传旧响应，而是以新代重发
 *      get(idx)（命中新代在途则共享，否则重新请求；重发失败沿 Promise 链
 *      自然终止，不会无限递归）；invalidate(idx) 单章失效（重爬本章后用）。
 *
 * UMD 双导出：浏览器挂载 window.ChapterCache；node 下 module.exports，
 * 使 tests/js/chapter_cache.test.js 能对线上同一份代码做语义断言。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ChapterCache = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // 默认容量：最多缓存 6 章正文（沿用原预取缓存上限）
  var DEFAULT_CAPACITY = 6;

  /* makeChapterCache(opts)
   *   opts.fetchChapter(idx) -> Promise<chapter>   实际网络请求的唯一出口
   *   opts.capacity          容量（章数），默认 6
   *   opts.shouldCache(ch)   入库判定，默认 ch.downloaded 且 content 为字符串
   *                          （未下载/内容缺失的响应不入库，下次重新请求）
   *
   * 返回缓存对象：
   *   get(idx)         -> Promise<chapter>  命中（含在途共享）或发起请求
   *   prefetch(idx)    预取：同 get 但吞掉错误（失败自动清 pending 可重试）
   *   peek(idx)        同步命中返回章节否则 null；命中重排 LRU 顺序
   *   hasPending(idx)  该章是否有在途请求
   *   invalidate(idx)  单章失效（重爬本章成功后调用）；同时作废该章在途请求
   *                    （在途旧响应不入库、不回传，改以重取结果）
   *   invalidateAll()  整书失效 + 代际 bump（书籍/正文版本变化，如重爬刷新后）
   *   size() / keys() / pendingCount()   观测（测试与调试用）
   *
   * 兼容读取：返回对象经 Proxy 包装，cache[idx]（数字属性）等价于 peek(idx)
   * ——阅读器缓存命中分支沿用 window._chCache[idx] 的既有写法且自动刷新 LRU。
   */
  function makeChapterCache(opts) {
    opts = opts || {};
    var capacity = opts.capacity || DEFAULT_CAPACITY;
    var shouldCache = opts.shouldCache || function (ch) {
      return !!(ch && ch.downloaded && typeof ch.content === 'string');
    };
    if (typeof opts.fetchChapter !== 'function') throw new TypeError('fetchChapter 必填');
    var fetchChapter = opts.fetchChapter;

    var cache = new Map();     // idx -> chapter；Map 插入序即访问序（LRU）
    var pending = new Map();   // idx -> Promise（在途去重）
    var gen = 0;               // 代际：invalidateAll 后 +1，在途旧代结果丢弃
                               // （invalidate(idx) 以 pending 身份守卫作废单章在途）

    // 完成入库：此时才按访问序循环淘汰，乱序返回也不会越过容量上限
    function put(idx, ch) {
      if (cache.has(idx)) cache.delete(idx);
      cache.set(idx, ch);
      while (cache.size > capacity) cache.delete(cache.keys().next().value);
    }

    function get(idx) {
      if (cache.has(idx)) {
        var hit = cache.get(idx);
        cache.delete(idx); cache.set(idx, hit);   // LRU：命中重排到最新
        return Promise.resolve(hit);
      }
      if (pending.has(idx)) return pending.get(idx);   // 在途去重：共享同一 Promise
      var g = gen;
      var p = Promise.resolve()
        .then(function () { return fetchChapter(idx); })
        .then(function (ch) {
          // 跨代（invalidateAll 后旧请求才返回）：旧响应不入库也不回传给
          // 调用方——以新代重新 get（pending 已由 invalidateAll 清空或为新代
          // 登记，命中新代在途则共享，否则重发请求；重发失败沿链自然终止）
          if (g !== gen) return get(idx);
          // C05 残留修复：单章 invalidate(idx) 后旧请求才返回——pending 已被
          // 清除（本请求不再是该 idx 的当前在途），同样丢弃旧响应并回传当前
          // 代际重取结果，绝不把重爬前的旧正文入库或回传给阅读器。
          if (pending.get(idx) !== p) return get(idx);
          pending.delete(idx);
          // 不合规内容（未下载/内容缺失）不入库，下次重新请求
          if (shouldCache(ch)) put(idx, ch);
          return ch;
        }, function (err) {
          if (pending.get(idx) === p) pending.delete(idx);   // 失败/取消：删除 pending，允许重试
          throw err;
        });
      pending.set(idx, p);
      return p;
    }

    function prefetch(idx) {
      return get(idx).catch(function () {});   // 预取失败静默；pending 已清，可重试
    }

    function peek(idx) {
      if (!cache.has(idx)) return null;
      var v = cache.get(idx);
      cache.delete(idx); cache.set(idx, v);     // LRU：命中重排到最新
      return v;
    }

    function hasPending(idx) { return pending.has(idx); }
    function invalidate(idx) { cache.delete(idx); pending.delete(idx); }
    function invalidateAll() { cache.clear(); pending.clear(); gen++; }
    function size() { return cache.size; }
    function keys() { return Array.from(cache.keys()); }
    function pendingCount() { return pending.size; }

    var api = {
      get: get, prefetch: prefetch, peek: peek, hasPending: hasPending,
      invalidate: invalidate, invalidateAll: invalidateAll,
      size: size, keys: keys, pendingCount: pendingCount,
    };

    // 数字属性访问 → peek（兼容 window._chCache[idx] 的既有读取写法）
    if (typeof Proxy === 'function') {
      return new Proxy(api, {
        get: function (t, prop) {
          if (typeof prop === 'string' && /^\d+$/.test(prop)) {
            var v = t.peek(parseInt(prop, 10));
            return v === null ? undefined : v;
          }
          return t[prop];
        },
      });
    }
    return api;
  }

  return {
    DEFAULT_CAPACITY: DEFAULT_CAPACITY,
    makeChapterCache: makeChapterCache,
  };
});
