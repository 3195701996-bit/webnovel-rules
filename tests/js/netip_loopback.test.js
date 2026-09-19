/* netip.js 回环误判回归（方向基线 P0-C / §3.2）
 *
 * 故障现场：手机 App 里打开本机页面（127.0.0.1）时，地址栏脚本把
 * "当前数字 IP 不在服务端局域网 IP 列表里"判为失效 → 页面上挂着
 * 「当前页面地址（127.0.0.1）已失效，请用上方新地址重新打开」。
 * /api/net-ips 明确排除 loopback，所以这条判定在手机上是**必然误报**，
 * 而且它会让用户以为应用坏了（截图中就是这一条）。
 *
 * 被测对象是**真实文件**（node:vm 里跑整段 IIFE，与线上同一份代码）。
 * 运行：node tests/js/netip_loopback.test.js
 */
'use strict';
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..');
const NETIP = fs.readFileSync(path.join(ROOT, 'static/js/netip.js'), 'utf8');

function makeBar() {
  return { style: {}, dataset: {}, innerHTML: '', textContent: '' };
}

/** 在 vm 里跑一遍 netip.js，返回渲染后的地址栏 */
async function run(hostname, payload) {
  const bar = makeBar();
  const sandbox = {
    console, JSON, Promise, Date, Math, String, Object,
    esc: (s) => String(s),
    location: { hostname },
    document: {
      hidden: false,
      getElementById: () => bar,
      addEventListener() {},
    },
    sessionStorage: { getItem: () => null, setItem() {} },
    AbortController: globalThis.AbortController,
    setTimeout: () => 1,
    clearTimeout() {},
    setInterval: () => 1,
    fetch: async () => ({ ok: true, status: 200, json: async () => payload }),
  };
  vm.createContext(sandbox);
  vm.runInContext(NETIP, sandbox);
  await new Promise((r) => setTimeout(r, 0));   // 让内部 await 走完
  await new Promise((r) => setTimeout(r, 0));
  return bar;
}

const LAN = { ips: ['192.168.1.5'], host_local: 'my-mac.local', port: 8766 };

async function main() {
  // 1) 回环页面：不显示任何局域网提示，更不能判"已失效"
  {
    const bar = await run('127.0.0.1', LAN);
    assert.ok(!/已失效/.test(bar.innerHTML), '回环页面不得出现"已失效"提示：' + bar.innerHTML);
    assert.ok(!/当前 IP 地址|固定地址|换网不掉线/.test(bar.innerHTML),
      '回环页面不应显示局域网地址信息：' + bar.innerHTML);
    assert.strictEqual(bar.style.display, 'none', '回环页面地址栏应隐藏');
    console.log('  ✓ 127.0.0.1：地址栏隐藏，无"已失效"误报');
  }
  // 2) localhost 同样
  {
    const bar = await run('localhost', LAN);
    assert.ok(!/已失效/.test(bar.innerHTML), 'localhost 页面不得出现"已失效"');
    assert.strictEqual(bar.style.display, 'none', 'localhost 页面地址栏应隐藏');
    console.log('  ✓ localhost：地址栏隐藏');
  }
  // 3) 局域网 IP 且**在**列表里：正常显示，不报失效
  {
    const bar = await run('192.168.1.5', LAN);
    assert.ok(!/已失效/.test(bar.innerHTML), '地址仍有效时不得报失效：' + bar.innerHTML);
    assert.ok(/当前 IP 地址/.test(bar.innerHTML), '应显示当前可用地址');
    assert.notStrictEqual(bar.style.display, 'none', '有内容时地址栏应显示');
    console.log('  ✓ 192.168.1.5（在列表内）：正常显示，不报失效');
  }
  // 4) 局域网 IP 且**不在**列表里：这才是真正需要提示的场景，必须保留
  {
    const bar = await run('192.168.1.99', LAN);
    assert.ok(/已失效/.test(bar.innerHTML),
      '换了网、旧 IP 不再可用时仍必须提示（不能因为修回环把这个功能修没）：' + bar.innerHTML);
    console.log('  ✓ 192.168.1.99（不在列表内）：仍然提示已失效');
  }
  console.log('netip 回环回归：全部通过');
}

main().catch((e) => { console.error(e); process.exit(1); });
