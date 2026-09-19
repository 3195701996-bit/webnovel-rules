/* common.js — 全站前端助手单一实现（R34b 重构抽取）。
 *
 * 历史：esc/toast/$ 曾散落在 11 份模板内联脚本中逐份复制。各模板版本
 * 经逐一比对语义完全一致（esc 均转义 & < > " ' 且 null/undefined→''；
 * toast 均为 #toast 单例 + 3200ms 自动隐藏；$ 均为 querySelector 别名），
 * 故抽取为本文件统一导出。实现以最主流模板内版本为准，未改变任何行为。
 *
 * 用法：模板须在首个内联 <script> 之前以普通（非 defer/async）方式引入：
 *   <script src="/static/js/common.js"></script>
 * netip.js 依赖本文件的全局 esc，加载顺序须在本文件之后。
 */
(function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  }

  function toast(msg, type) {
    let t = document.getElementById('toast');
    if (!t) { t = document.createElement('div'); t.id = 'toast'; document.body.appendChild(t); }
    t.textContent = msg; t.className = 'toast show ' + (type || '');
    clearTimeout(t._timer); t._timer = setTimeout(() => { t.className = 'toast'; }, 3200);
  }

  function $(s) { return document.querySelector(s); }

  /* C01: 统一请求辅助 requestJSON(url, opts)。
   *
   * 解决各模板 fetch 后不检查 r.ok / catch 后仍提示成功 / 按钮永久禁用等
   * 不一致问题。约定：
   *   - opts.method    默认 'GET'
   *   - opts.body      对象自动 JSON.stringify 并补 Content-Type；字符串原样发送
   *   - opts.timeout   毫秒，默认 15000；超时抛 TimeoutError（err.timeout === true）
   *   - opts.signal    外部 AbortSignal：外部取消时原样透传 AbortError（不包装），
   *                    调用方据此区分"主动取消"与"真实失败"
   *   - opts.button    请求期间禁用，finally 中无条件恢复——失败绝不留下永久禁用
   *                    按钮；若成功后要防重复提交，由调用方在 await 之后自行再禁用
   *   - 非 2xx         抛 HttpError（err.status / err.data，消息取服务端 error/message）
   *   - 非 JSON 响应    抛 ParseError（HTML 错误页/代理拦截页不会再被当成功）
   *   - 网络层失败      抛 NetworkError
   * 返回解析后的 JSON 对象。调用方负责 try/catch 并提示可恢复错误。
   */
  async function requestJSON(url, opts) {
    opts = opts || {};
    const timeout = opts.timeout == null ? 15000 : opts.timeout;
    const ctrl = new AbortController();
    const ext = opts.signal;
    let timedOut = false;
    const onExtAbort = () => ctrl.abort();
    if (ext) {
      if (ext.aborted) ctrl.abort();
      else ext.addEventListener('abort', onExtAbort, {once: true});
    }
    const timer = timeout > 0
      ? setTimeout(() => { timedOut = true; ctrl.abort(); }, timeout)
      : null;
    const btn = opts.button || null;
    if (btn) btn.disabled = true;
    try {
      const init = {
        method: opts.method || 'GET',
        signal: ctrl.signal,
        headers: Object.assign({}, opts.headers),
      };
      if (opts.body !== undefined && opts.body !== null) {
        if (typeof opts.body !== 'string') {
          init.headers['Content-Type'] = 'application/json';
          init.body = JSON.stringify(opts.body);
        } else {
          init.body = opts.body;
        }
      }
      let r;
      try {
        r = await fetch(url, init);
      } catch (e) {
        if (e && e.name === 'AbortError') {
          if (timedOut) {
            const te = new Error('请求超时（' + Math.round(timeout / 1000) + ' 秒）');
            te.name = 'TimeoutError'; te.timeout = true;
            throw te;
          }
          throw e;  // 外部主动取消：透传 AbortError，由调用方决定静默或提示
        }
        const ne = new Error('网络连接失败：' + ((e && e.message) || e));
        ne.name = 'NetworkError';
        throw ne;
      }
      const text = await r.text();
      let data = null;
      if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
      if (!r.ok) {
        // C06: 全局 401 → 跳登录页（登录过期不再表现为"莫名报错"）。
        // 仍抛出 HttpError 让调用方中止后续逻辑，但跳转优先发生。
        if (r.status === 401) {
          try { location.href = '/login'; } catch (e) {}
        }
        const he = new Error((data && (data.error || data.message)) || ('HTTP ' + r.status));
        he.name = 'HttpError'; he.status = r.status; he.data = data;
        throw he;
      }
      if (data === null || typeof data !== 'object') {
        const pe = new Error('服务器返回了非 JSON 响应（可能是错误页，HTTP ' + r.status + '）');
        pe.name = 'ParseError'; pe.status = r.status;
        throw pe;
      }
      return data;
    } finally {
      if (timer) clearTimeout(timer);
      if (ext) ext.removeEventListener('abort', onExtAbort);
      if (btn) btn.disabled = false;  // C01: 按钮最终一定恢复可用
    }
  }

  /* B03: 通用轮询调度器 createPoller(fetchFn, opts)。
   *
   * 替代各页面 setInterval 固定轮询，治理"非活动标签持续轮询 + 慢请求叠加"：
   *   - setTimeout 链：一次请求完成后才安排下一次，慢请求永不重叠
   *   - 请求超时：opts.timeoutMs（默认 20000）AbortController 兜底
   *   - 指数退避：连续失败按 2^n 拉长间隔（上限 opts.maxMs，默认 60000）
   *   - 页面隐藏（document.hidden）停止常规轮询；visibilitychange 回到
   *     前台时立即补刷一次并恢复计时
   *   - opts.interval(lastData)：按最近数据动态决定间隔（如任务运行中
   *     高频、全部暂停/完成后低频）
   * fetchFn(signal) 返回最新数据；返回 null/undefined 或抛错视为失败。
   * 返回 {kick, stop}：kick() 为手动刷新入口（立即拉取并重排计时）。
   */
  function createPoller(fetchFn, opts) {
    opts = opts || {};
    const baseMs = opts.baseMs || 4000;
    const maxMs = opts.maxMs || 60000;
    const timeoutMs = opts.timeoutMs || 20000;
    const intervalOf = typeof opts.interval === 'function' ? opts.interval : null;
    let timer = null, inFlight = false, fails = 0, stopped = false, lastData;
    function clearTimer() { if (timer) { clearTimeout(timer); timer = null; } }
    function schedule() {
      if (stopped || timer) return;
      if (document.hidden) return;   // 隐藏页不排常规轮询；回前台时补刷
      let ms = intervalOf ? intervalOf(lastData) : baseMs;
      if (!(ms > 0)) ms = baseMs;
      if (fails > 0) ms = Math.min(maxMs, ms * Math.pow(2, fails));
      timer = setTimeout(() => { timer = null; tick(); }, ms);
    }
    async function tick() {
      if (stopped || inFlight) return;   // 在途请求未完结 → 不再并发
      if (document.hidden) return;
      inFlight = true;
      const ctrl = new AbortController();
      const to = setTimeout(() => ctrl.abort(), timeoutMs);
      try {
        const d = await fetchFn(ctrl.signal);
        if (d === undefined || d === null) fails++;
        else { fails = 0; lastData = d; }
      } catch (e) {
        fails++;
      } finally {
        clearTimeout(to);
        inFlight = false;
      }
      schedule();
    }
    function kick() {
      if (stopped) return;
      clearTimer();
      tick();
    }
    // 具名监听并在 stop() 摘除：poller 可被反复重建（如下载页续传），
    // 匿名监听会随每次 createPoller 累积泄漏
    function onVisibility() {
      if (!document.hidden) kick();   // 回到前台：立即补刷并恢复轮询
    }
    document.addEventListener('visibilitychange', onVisibility);
    tick();   // 立即首刷（替代原先独立的首次调用）
    return {
      kick: kick,
      stop: () => {
        stopped = true;
        clearTimer();
        document.removeEventListener('visibilitychange', onVisibility);
      },
    };
  }

  window.esc = esc;
  window.escapeHtml = esc;   // 别名：task.html / reader.html 原用 escapeHtml
  window.toast = toast;
  window.$ = $;
  window.requestJSON = requestJSON;
  window.createPoller = createPoller;
})();
