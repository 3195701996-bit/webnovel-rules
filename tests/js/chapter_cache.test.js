/* C05 章节正文缓存离线单测（node 直接运行，无浏览器依赖）
 *
 * 被测对象 = 线上实际运行的同一份代码 static/js/chapter_cache.js（UMD）。
 * fetchChapter 用可控 deferred Promise 模拟网络，断言验收语义：
 *   1. 在途去重：预取未完成时切入该章（get）不发重复请求，二者共享同一 Promise
 *   2. 失败/取消删除 pending：失败后可再次请求（重新发起 fetch）
 *   3. 真正 LRU：按访问顺序淘汰，而非 Object.keys 整数键升序
 *   4. 乱序返回后缓存仍受限：容量约束发生在完成入库时
 *   5. 版本失效：invalidateAll 后跨代返回的旧响应丢弃不入库；invalidate(idx) 单章失效
 *   6. 未下载/内容缺失的响应不入库（下次重新请求）
 *   8. C05 残留修复：跨代在途 resolve 不回传旧响应（以新代重发，调用方拿到
 *      新代结果）；invalidateAll 同时清 pending（旧代在途不再被新 get 共享）
 *
 * 运行：node tests/js/chapter_cache.test.js
 */
'use strict';
const assert = require('node:assert');
const path = require('node:path');
const ChapterCache = require(path.join(__dirname, '..', '..', 'static', 'js', 'chapter_cache.js'));

// 可控 deferred
function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return {promise, resolve, reject};
}
const okCh = (idx) => ({downloaded: true, name: '第' + idx + '章', content: '正文' + idx});

async function main() {
  // ── 1. 在途去重：预取未完成时 get 共享同一 Promise ──
  {
    const calls = [];
    const pending = new Map();
    const cache = ChapterCache.makeChapterCache({
      capacity: 3,
      fetchChapter: (idx) => {
        calls.push(idx);
        const d = deferred();
        pending.set(idx, d);
        return d.promise;
      },
    });
    const p1 = cache.prefetch(3);          // 预取第 3 章（未完成的在途请求）
    assert.strictEqual(cache.hasPending(3), true, '预取登记 pending');
    const p2 = cache.get(3);               // 预取未完成时切入该章
    await new Promise(r => setTimeout(r, 0));   // 让 fetchChapter 微任务执行
    assert.strictEqual(calls.filter(i => i === 3).length, 1, '不发重复请求：fetch 只调用一次');
    pending.get(3).resolve(okCh(3));
    const [a, b] = await Promise.all([p1, p2]);
    assert.deepStrictEqual(a, b, '两个调用方拿到同一结果');
    assert.strictEqual(cache.hasPending(3), false, '完成后 pending 清除');
    assert.strictEqual(cache.peek(3).content, '正文3', '完成入库');
  }

  // ── 2. 失败后可再次请求（pending 已删，允许重试）──
  {
    let calls = 0, failFirst = true;
    const cache = ChapterCache.makeChapterCache({
      capacity: 3,
      fetchChapter: async (idx) => {
        calls++;
        if (failFirst) { failFirst = false; throw new Error('network down'); }
        return okCh(idx);
      },
    });
    await assert.rejects(cache.get(5), /network down/, '失败原样抛给调用方');
    assert.strictEqual(cache.hasPending(5), false, '失败后 pending 删除');
    assert.strictEqual(cache.size(), 0, '失败结果不入库');
    const ch = await cache.get(5);         // 再次请求 → 重新发起 fetch
    assert.strictEqual(calls, 2, '失败后允许重试（重新发起请求）');
    assert.strictEqual(ch.content, '正文5');
    // prefetch 失败静默但同样可重试
    failFirst = true;
    await cache.prefetch(6);
    assert.strictEqual(cache.hasPending(6), false, '预取失败后 pending 删除');
    await cache.prefetch(6);
    assert.strictEqual(calls, 4, '预取失败后可重试');
  }

  // ── 3. 真正 LRU：按访问顺序淘汰，而非整数键升序 ──
  {
    const cache = ChapterCache.makeChapterCache({
      capacity: 3, fetchChapter: async (idx) => okCh(idx),
    });
    await cache.get(1); await cache.get(2); await cache.get(3);
    cache.peek(1);                          // 访问第 1 章 → 它是最新
    await cache.get(4);                     // 入库第 4 章 → 淘汰最久未访问的第 2 章
    assert.deepStrictEqual(cache.keys(), [3, 1, 4],
      'LRU：淘汰最久未访问的第 2 章（旧实现的整数键升序会误删第 1 章）');
    assert.strictEqual(cache.size(), 3, '容量恒定');
  }

  // ── 4. 乱序返回后缓存仍受限（容量约束在完成入库时）──
  {
    const gates = new Map();
    const cache = ChapterCache.makeChapterCache({
      capacity: 3,
      fetchChapter: (idx) => {
        const d = deferred();
        gates.set(idx, d);
        return d.promise;
      },
    });
    const ps = [1, 2, 3, 4, 5].map(i => cache.get(i));   // 5 个并发在途（超过容量）
    await new Promise(r => setTimeout(r, 0));   // 让 fetchChapter 微任务执行
    gates.get(5).resolve(okCh(5));          // 乱序：最后发起的先返回
    gates.get(1).resolve(okCh(1));
    gates.get(4).resolve(okCh(4));
    gates.get(2).resolve(okCh(2));
    gates.get(3).resolve(okCh(3));
    await Promise.all(ps);
    assert.ok(cache.size() <= 3, '乱序返回后缓存仍受限（≤ 容量）');
    assert.strictEqual(cache.pendingCount(), 0, '全部完成后无残留 pending');
  }

  // ── 5. 版本失效：invalidateAll 跨代丢弃在途旧响应；invalidate 单章失效 ──
  {
    const gates = new Map();
    const cache = ChapterCache.makeChapterCache({
      capacity: 6,
      fetchChapter: (idx) => { const d = deferred(); gates.set(idx, d); return d.promise; },
    });
    const p7 = cache.get(7);                // 先入库一章（gated：下方 resolve）
    await new Promise(r => setTimeout(r, 0));   // 让 fetchChapter 微任务执行
    gates.get(7).resolve(okCh(7));
    await p7;
    const inflight = cache.get(8);          // 第 8 章在途（代际 g 在调用时捕获）
    await new Promise(r => setTimeout(r, 0));   // 让 fetchChapter 微任务执行
    cache.invalidateAll();                  // 重爬/版本变化 → 整书失效 + 清 pending + 代际 bump
    assert.strictEqual(cache.size(), 0, 'invalidateAll 清空已缓存章节');
    gates.get(8).resolve(okCh(8));          // 旧代在途请求随后返回 → 以新代重发
    await new Promise(r => setTimeout(r, 0));
    assert.strictEqual(cache.size(), 0, '跨代返回的旧响应丢弃不入库');
    // 旧响应不直接回传：inflight 转为等新代重发，重发返回后调用方拿到新内容
    gates.get(8).resolve({downloaded: true, name: '第8章', content: '新正文8'});
    const ch8 = await inflight;
    assert.strictEqual(ch8.content, '新正文8', '旧代 Promise resolve 出新代结果');
    assert.strictEqual(cache.peek(8).content, '新正文8', '入库的是新代内容');
    // 单章失效：其余章节保留（gated：先 resolve 再 await，否则在途永不落地）
    const p1 = cache.get(1), p2 = cache.get(2);
    await new Promise(r => setTimeout(r, 0));
    gates.get(1).resolve(okCh(1)); gates.get(2).resolve(okCh(2));
    await Promise.all([p1, p2]);
    cache.invalidate(1);
    assert.strictEqual(cache.peek(1), null, '单章失效');
    assert.ok(cache.peek(2), '其余章节保留');
  }

  // ── 6. 未下载/内容缺失的响应不入库 ──
  {
    let calls = 0;
    const cache = ChapterCache.makeChapterCache({
      capacity: 3,
      fetchChapter: async (idx) => { calls++; return {downloaded: false, name: '第' + idx + '章'}; },
    });
    await cache.get(9);
    assert.strictEqual(cache.size(), 0, '未下载响应不入库');
    await cache.get(9);
    assert.strictEqual(calls, 2, '未入库 → 下次重新请求');
  }

  // ── 8. C05 残留修复：跨代在途 resolve 不回传旧响应；invalidateAll 清 pending ──
  {
    let calls = 0;
    const gates = [];
    const cache = ChapterCache.makeChapterCache({
      capacity: 3,
      fetchChapter: (idx) => {
        calls++;
        const d = deferred();
        gates.push(d);
        return d.promise;
      },
    });
    const p1 = cache.get(2);                 // 旧代在途（goto 正在等它）
    await new Promise(r => setTimeout(r, 0));   // 让 fetchChapter 微任务执行
    assert.strictEqual(calls, 1);
    cache.invalidateAll();                   // 重爬刷新 → 清缓存 + 清 pending + 代际 bump
    assert.strictEqual(cache.hasPending(2), false, 'invalidateAll 清空在途登记（pending.clear）');
    const p2 = cache.get(2);                 // 立即重读：不共享旧代在途，重发新代请求
    await new Promise(r => setTimeout(r, 0));
    assert.strictEqual(calls, 2, 'invalidateAll 后 get 不共享旧代在途，重发请求');
    // 旧代请求随后 resolve（重爬前的旧正文）
    gates[0].resolve({downloaded: true, name: '旧章', content: '旧正文'});
    gates[1].resolve({downloaded: true, name: '新章', content: '新正文'});
    const [oldResult, newResult] = await Promise.all([p1, p2]);
    assert.strictEqual(oldResult.content, '新正文',
      '跨代在途 resolve 不回传旧响应——调用方拿到新代 fetch 返回值');
    assert.strictEqual(newResult.content, '新正文');
    assert.strictEqual(cache.peek(2).content, '新正文', '入库的是新代内容');
    assert.strictEqual(calls, 2, '旧代 resolve 递归共享新代在途，不发起第三次请求');
  }

  // ── 9. 跨代重发失败：沿 Promise 链抛出（自然终止，不无限递归）──
  {
    let calls = 0;
    const gates = [];
    const cache = ChapterCache.makeChapterCache({
      capacity: 3,
      fetchChapter: (idx) => {
        calls++;
        const d = deferred();
        gates.push(d);
        return d.promise;
      },
    });
    const p1 = cache.get(4);
    await new Promise(r => setTimeout(r, 0));
    cache.invalidateAll();
    gates[0].resolve(okCh(4));               // 旧代 resolve → 触发新代重发
    await new Promise(r => setTimeout(r, 0));
    assert.strictEqual(calls, 2, '跨代 resolve 后以新代重发');
    gates[1].reject(new Error('server gone'));
    await assert.rejects(p1, /server gone/, '新代重发失败原样抛给调用方（递归自然终止）');
    assert.strictEqual(cache.hasPending(4), false, '重发失败后 pending 清除，可再重试');
  }

  // ── 7. 数字属性兼容读取（Proxy：cache[idx] ≡ peek(idx)，刷新 LRU）──
  {
    const cache = ChapterCache.makeChapterCache({
      capacity: 2, fetchChapter: async (idx) => okCh(idx),
    });
    await cache.get(1); await cache.get(2);
    assert.strictEqual(cache[1].content, '正文1', '数字属性读取命中');
    assert.strictEqual(cache[9], undefined, '未命中返回 undefined（if 判断为假）');
    await cache.get(3);                     // cache[1] 刚被访问 → 淘汰第 2 章
    assert.deepStrictEqual(cache.keys(), [1, 3], '数字属性读取同样刷新 LRU 顺序');
  }

  // ── 10. 单章 invalidate 作废在途：旧响应不入库/不回传，改以重取结果 ──
  {
    const gates = new Map();
    let calls = 0;
    const cache = ChapterCache.makeChapterCache({
      capacity: 3,
      fetchChapter: (idx) => {
        calls++;
        if (idx === 1) return Promise.resolve(okCh(1));   // 预热：立即完成一次请求
        const d = deferred(); gates.set(calls, d); return d.promise;
      },
    });
    await cache.get(1);                          // calls=1（已完成）
    const inflight = cache.get(5);               // 第 5 章在途（重爬前发起的旧请求）
    await new Promise(r => setTimeout(r, 0));    // 让 fetchChapter 微任务执行（calls=2）
    assert.strictEqual(cache.hasPending(5), true, '重爬前该章在途已登记');
    cache.invalidate(5);                         // 重爬成功 → 单章失效（应连在途一起作废）
    assert.strictEqual(cache.hasPending(5), false, 'invalidate(5) 清在途登记，允许重新发起');
    gates.get(2).resolve(okCh(5));               // 重爬前的旧响应随后返回
    await new Promise(r => setTimeout(r, 0));
    assert.strictEqual(cache.peek(5), null, '被作废的在途旧响应不入库');
    assert.strictEqual(calls, 3, '旧响应触发以当前代际重取（重新请求一次）');
    gates.get(3).resolve({downloaded: true, name: '第5章', content: '重爬后正文'});
    const ch = await inflight;
    assert.strictEqual(ch.content, '重爬后正文', '调用方拿到重取后的新正文而非旧存根');
    assert.strictEqual(cache.peek(5).content, '重爬后正文', '入库的是重取后的新正文');
    assert.strictEqual(cache.hasPending(5), false, '完成后无残留 pending');
  }

}

// 异步完成守卫：主流程若因未 resolve 的 Promise 挂起，事件循环会因该 watchdog
// 定时器保持存活 → 超时即判定失败并 exit 1，杜绝“静默 exit 0 假通过”。
const WATCHDOG_MS = 5000;
const watchdog = setTimeout(() => {
  console.error('chapter_cache.test.js: 主流程未在 ' + WATCHDOG_MS +
    'ms 内完成（疑似存在未 resolve 的 Promise）——判定失败');
  process.exit(1);
}, WATCHDOG_MS);

main().then(() => {
  clearTimeout(watchdog);
  console.log('chapter_cache.test.js: 全部断言通过');   // 明确完成标记（pytest 断言）
}).catch((e) => {
  clearTimeout(watchdog);
  console.error(e);
  process.exit(1);
});
