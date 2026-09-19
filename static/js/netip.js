/* netip.js — 手机/局域网访问地址栏：实时轮询刷新。
 *
 * 需求：电脑换网/WiFi 重连后局域网 IP 会变，页面上一次抓取的
 * "手机访问地址"就失效了。此脚本每 15s 轮询 /api/net-ips：
 *  1. IP 变了 → 栏内地址自动换成新 IP；
 *  2. mDNS 固定名（<主机名>.local，不随 DHCP 变）优先展示，
 *     手机浏览器（iOS Safari/Android Chrome）可直接解析访问；
 *  3. 当前页面正用旧 IP 打开（已不在新列表）→ 红字提示"已失效"；
 *  4. 轮询失败（网络已断）→ 用会话内快照提示固定地址。
 * 依赖模板中已存在 <div id="netip-bar">；无则静默跳过。
 */
(function () {
  const POLL_MS = 15000;              // 15s 一次，接口极轻（ifconfig）
  const FETCH_TIMEOUT = 8000;
  const SNAP_KEY = 'netip_snap_v1';
  const bar = () => document.getElementById('netip-bar');

  // esc 由 common.js 全局提供（加载顺序：common.js 须在本文件之前）

  function urlOf(host, port) {
    return 'http://' + host + ':' + port;
  }

  function render(d) {
    const b = bar();
    if (!b) return;
    const host = (location.hostname || '').toLowerCase();
    if (host === '127.0.0.1' || host === 'localhost' || host === '::1') {
      b.style.display = 'none';   // 本机访问：局域网地址与"换地址"提示都无意义
      return;
    }
    const port = d.port || 8766;
    const cur = (location.hostname || '').toLowerCase();
    const curIsIp = /^\d+\.\d+\.\d+\.\d+$/.test(cur);
    // 回环地址永远不算"失效"：本机就是 127.0.0.1，它本来就不在局域网 IP 列表里。
    // 不排除它的话，手机上打开本机页面会一直看到"当前页面地址已失效"的误报
    // （实测截图就是这条），把正常的本机访问说成故障。
    const curIsLoopback = cur === '127.0.0.1' || cur === 'localhost' || cur === '::1';
    // 当前页面地址是否已失效：正用某个局域网 IP 打开，但它已不在服务器私网 IP 列表
    const stale = curIsIp && !curIsLoopback && !(d.ips || []).includes(cur);

    const parts = [];
    // 固定地址（换网不失效）放最前
    if (d.host_local) {
      parts.push('🔗 固定地址（换网不掉线）: <a href="' + esc(urlOf(d.host_local, port)) +
        '" style="color:var(--accent);text-decoration:none;font-weight:600;">' +
        esc(urlOf(d.host_local, port)) + '</a>');
    }
    if (d.ips && d.ips.length) {
      const links = d.ips.map(ip =>
        '<a href="' + esc(urlOf(ip, port)) + '" style="color:var(--accent);text-decoration:none;margin-right:12px;white-space:nowrap;">' +
        esc(urlOf(ip, port)) + '</a>').join('');
      parts.push('📶 当前 IP 地址: ' + links);
    }
    if (!parts.length) return;
    if (stale) {
      parts.unshift('<span style="color:#ff8f8f;">⚠️ 当前页面地址（' + esc(cur) +
        '）已失效，请用上方新地址重新打开。</span>');
    }
    // IP 或固定名变化时才重绘（避免每 15s 闪烁打断阅读）
    const sig = (d.host_local || '') + '|' + (d.ips || []).join(',');
    if (b.dataset.sig !== sig) {
      b.innerHTML = parts.join('<br>');
      b.dataset.sig = sig;
      b.style.display = '';
      // 变化瞬间短暂高亮提示"已更新"
      b.style.transition = 'background .4s';
      b.style.background = 'rgba(255,190,60,.22)';
      setTimeout(() => { b.style.background = ''; }, 1600);
    }
    // 会话快照：断网后仍能提示固定地址
    try { sessionStorage.setItem(SNAP_KEY, JSON.stringify({host_local: d.host_local, port: port})); } catch (e) {}
  }

  let _inFlight = false;  // 在途去重：visibilitychange 补拉与定时器不得并发发起第二次请求
  async function poll() {
    if (_inFlight) return;
    const b = bar();
    if (!b) return;
    _inFlight = true;
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), FETCH_TIMEOUT);
    try {
      const r = await fetch('/api/net-ips?t=' + Date.now(), {
        signal: ctl.signal, cache: 'no-store'
      });
      if (!r.ok) throw new Error('status ' + r.status);
      render(await r.json());
    } catch (e) {
      // 连不上服务器 = 当前 IP 多半已失效；用快照给固定地址
      let snap = null;
      try { snap = JSON.parse(sessionStorage.getItem(SNAP_KEY) || 'null'); } catch (e2) {}
      if (snap && snap.host_local) {
        const u = urlOf(snap.host_local, snap.port || 8766);
        b.innerHTML = '<span style="color:#ff8f8f;">⚠️ 与服务器连接已断开（IP 可能已变化）。</span><br>' +
          '🔗 固定地址: <a href="' + esc(u) + '" style="color:var(--accent);text-decoration:none;font-weight:600;">' +
          esc(u) + '</a>';
        b.style.display = '';
      } else if (!/^(127\.|localhost|::1)/.test(location.hostname || '')) {
        b.innerHTML = '<span style="color:#ff8f8f;">⚠️ 与服务器连接中断——网络或地址已变化，请重新获取访问地址。</span>';
        b.style.display = '';
      }
      // 不更新 sig：恢复后若地址没变不重绘闪烁
    } finally {
      clearTimeout(t);     // 成功/失败/中止都清理 abort 定时器，避免遗留挂起
      _inFlight = false;   // 释放在途锁，允许下一轮轮询
    }
  }

  // 页面可见时轮询；切后台/回前台立即补一次
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) poll();
  });
  poll();
  setInterval(poll, POLL_MS);
})();
