# -*- coding: utf-8 -*-
"""B03 前端轮询治理的轻量验证（沿用项目"文本断言 + node 可执行断言"模式）。

验证方式两层：
1. 文本断言：library/tasks/task 三页固定 setInterval 轮询已移除，改走
   common.js 的 createPoller（setTimeout 链 + 可见性暂停 + 指数退避 +
   任务状态调速 + 手动刷新入口）。
2. 可执行断言（有 node 时）：把 createPoller 原样抠出交给 node，
   用假定时器验证：首刷立即、请求完成后才排下一次（慢请求不重叠）、
   隐藏页不发常规轮询、回前台立即补刷、失败指数退避并封顶、成功复位。
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMMON = (ROOT / "static" / "js" / "common.js").read_text(encoding="utf-8")
LIBRARY = (ROOT / "templates" / "library.html").read_text(encoding="utf-8")
TASKS = (ROOT / "templates" / "tasks.html").read_text(encoding="utf-8")
TASK = (ROOT / "templates" / "task.html").read_text(encoding="utf-8")
MANGA_DL = (ROOT / "templates" / "manga_download.html").read_text(encoding="utf-8")


# ═══════════ 文本断言 ═══════════

class TestCreatePollerContract:
    def test_poller_exported(self):
        assert "function createPoller(fetchFn, opts)" in COMMON
        assert "window.createPoller = createPoller;" in COMMON

    def test_poller_core_mechanisms(self):
        # setTimeout 链 + 超时 AbortController + 退避上限 + 可见性
        assert "timer = setTimeout(() => { timer = null; tick(); }, ms);" in COMMON
        assert "const ctrl = new AbortController();" in COMMON
        assert "setTimeout(() => ctrl.abort(), timeoutMs)" in COMMON
        assert "Math.min(maxMs, ms * Math.pow(2, fails))" in COMMON
        assert "document.addEventListener('visibilitychange'" in COMMON
        # 慢请求不重叠守卫
        assert "if (stopped || inFlight) return;" in COMMON

    def test_no_setinterval_for_polled_pages(self):
        # common.js 与三个页面均无 setInterval 调用（注释提及不算）
        assert not re.search(r"^[^/\n]*\bsetInterval\s*\(", COMMON, re.M)
        for name, src in (("library.html", LIBRARY), ("tasks.html", TASKS),
                          ("task.html", TASK)):
            assert not re.search(r"setInterval\(\s*load", src), \
                f"{name} 仍有固定 setInterval 轮询"

    def test_pages_use_poller_with_state_driven_interval(self):
        assert "createPoller(loadMangaLibrary" in LIBRARY
        assert "createPoller(loadBooks" in LIBRARY
        # 漫画有下载中任务时高频、静止后低频
        assert "c.status === 'downloading'" in LIBRARY
        # 任务页按 running/queued 调速
        assert "createPoller(loadTasks" in TASKS
        assert "t.status === 'running' || t.status === 'queued'" in TASKS
        # 任务详情页按 running/paused/终态调速
        assert "createPoller(load," in TASK
        assert "st === 'running'" in TASK and "st === 'paused'" in TASK

    def test_manual_refresh_entries(self):
        assert "btn-books-refresh" in LIBRARY and "bookPoller.kick()" in LIBRARY
        assert "btn-manga-refresh" in LIBRARY and "mangaPoller.kick()" in LIBRARY
        assert "refresh-btn" in TASKS and "tasksPoller.kick()" in TASKS
        assert "btn-refresh" in TASK and "taskPoller.kick()" in TASK

    def test_loaders_return_data_for_scheduler(self):
        # 轮询器靠返回值调速/判失败：三个加载函数都必须回传数据或 null
        assert re.search(r"async function loadMangaLibrary[\s\S]*?return list;",
                         LIBRARY)
        assert re.search(r"async function loadTasks[\s\S]*?return tasks;", TASKS)
        assert re.search(r"async function load\(\)[\s\S]*?return st;", TASK)


# ═══════════ manga_download.html 迁移 createPoller（残留修复）═══════════

class TestMangaDownloadPolling:
    def test_no_bare_setinterval(self):
        # 裸 setInterval 固定轮询必须移除（状态接口慢于 1.5s 会叠请求）
        assert not re.search(r"setInterval\(\s*poll", MANGA_DL)
        assert "clearInterval(timer)" not in MANGA_DL

    def test_uses_create_poller_with_state_driven_interval(self):
        assert "createPoller(poll," in MANGA_DL
        # running 1.5s 高频；queued/cancel 降频；其余低频
        assert re.search(r"st\.status === 'running' \? 1500", MANGA_DL)
        assert "'queued' || st.status === 'cancel'" in MANGA_DL

    def test_terminal_states_stop_poller(self):
        # done/error 终态停轮；stop 记录锁存，续传重启时重建轮询器
        assert re.search(r"d\.status === 'done'\) \{\s*poller\.stop\(\);", MANGA_DL)
        assert re.search(r"d\.status === 'error'\) \{\s*poller\.stop\(\);", MANGA_DL)
        assert "pollerStopped" in MANGA_DL

    def test_poll_returns_data_and_failure_null(self):
        # 轮询器靠返回值调速/判失败：成功回传数据、失败回传 null 触发退避
        assert re.search(r"async function poll\(\)[\s\S]*?return null;", MANGA_DL)
        assert re.search(r"async function poll\(\)[\s\S]*?return d;", MANGA_DL)

    def test_c01_error_recovery_semantics_kept(self):
        # C01 不回退：pollErrors 计数、连续失败上限、手动恢复入口（kick 立即重试）
        assert "let pollErrors = 0;" in MANGA_DL
        assert "if (pollErrors >= 10)" in MANGA_DL
        assert "function resumePoll()" in MANGA_DL
        assert re.search(r"function resumePoll\(\)[\s\S]*?poller\.kick\(\);", MANGA_DL)


# ═══════════ 可执行断言：node 运行抠出的 createPoller ═══════════

def _extract_poller():
    m = re.search(r"(  function createPoller\(fetchFn, opts\) \{.*?\n  \})",
                  COMMON, re.S)
    assert m, "createPoller 未找到"
    return m.group(1)


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node 不可用，跳过可执行断言")
def test_create_poller_semantics_with_node():
    script = """
'use strict';
// ── 假定时器/假 document：确定性验证调度语义 ──
const timers = [];
global.setTimeout = (fn, ms) => { const t = {fn, ms, cleared: false, fired: false}; timers.push(t); return t; };
global.clearTimeout = (t) => { if (t) t.cleared = true; };
const docListeners = {};
global.document = { hidden: false, addEventListener: (ev, fn) => { (docListeners[ev] = docListeners[ev] || []).push(fn); } };
function pend(ms) { return timers.filter(t => !t.cleared && !t.fired && (ms === undefined || t.ms === ms)); }
function fire(t) { t.fired = true; t.fn(); }
const flush = async () => { for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r)); };
function assert(cond, msg) { if (!cond) { console.error('FAIL: ' + msg); process.exit(1); } }

%s

(async () => {
  let calls = 0;
  let mode = 'ok';
  let gate = null;
  const fn = (sig) => {
    calls++;
    if (mode === 'block') return new Promise(res => { gate = res; });
    if (mode === 'fail') return Promise.reject(new Error('boom'));
    return Promise.resolve({n: calls});
  };
  const p = createPoller(fn, {baseMs: 100, maxMs: 1000, timeoutMs: 9000});
  await flush();

  // 1) 首刷立即 + 请求完成后才排下一次（setTimeout 链）
  assert(calls === 1, '首刷应立即执行, got ' + calls);
  assert(pend(100).length === 1, '完成后应排 100ms 的下一次, pend=' + JSON.stringify(pend().map(t=>t.ms)));

  // 2) 到点触发第二次
  fire(pend(100)[0]);
  await flush();
  assert(calls === 2, '第二次轮询未发生');
  assert(pend(100).length === 1, '第二次完成后应再次排期');

  // 3) 慢请求不重叠：阻塞在途期间 kick 不并发
  mode = 'block';
  fire(pend(100)[0]);
  await flush();
  assert(calls === 3, '第三次应已开始');
  p.kick();
  await flush();
  assert(calls === 3, '在途请求未完结时 kick 不得并发, got ' + calls);
  gate({n: 3});
  await flush();
  assert(pend(100).length === 1, '慢请求完成后应恢复排期');

  // 4) 页面隐藏：到点不请求、不再排期（常规轮询停止）
  mode = 'ok';
  document.hidden = true;
  fire(pend(100)[0]);
  await flush();
  assert(calls === 3, '隐藏页不得发送常规轮询, got ' + calls);
  assert(pend(100).length === 0, '隐藏页不得再排下一次常规轮询');

  // 5) 回前台：visibilitychange 立即补刷并恢复轮询
  document.hidden = false;
  (docListeners['visibilitychange'] || []).forEach(f => f());
  await flush();
  assert(calls === 4, '回前台应立即补刷, got ' + calls);
  assert(pend(100).length === 1, '补刷后应恢复排期');

  // 6) 失败指数退避并封顶
  mode = 'fail';
  fire(pend(100)[0]); await flush();
  assert(pend(200).length === 1, '失败 1 次 → 200ms, pend=' + JSON.stringify(pend().map(t=>t.ms)));
  fire(pend(200)[0]); await flush();
  assert(pend(400).length === 1, '失败 2 次 → 400ms');
  fire(pend(400)[0]); await flush();
  assert(pend(800).length === 1, '失败 3 次 → 800ms');
  fire(pend(800)[0]); await flush();
  assert(pend(1000).length === 1, '退避应封顶 maxMs=1000');

  // 7) 成功后退避复位
  mode = 'ok';
  fire(pend(1000)[0]); await flush();
  assert(pend(100).length === 1, '成功后退避应复位 100ms');

  console.log('createPoller: all cases pass');
  process.exit(0);
})().catch(e => { console.error('FAIL: ' + (e && e.stack || e)); process.exit(1); });
""" % _extract_poller()
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                       timeout=30)
    assert r.returncode == 0, r.stderr or r.stdout
