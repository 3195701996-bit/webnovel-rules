# -*- coding: utf-8 -*-
"""阶段 B 回归：共享后端可控运行（设计 §5 / §11 阶段 B）

覆盖设计要求的检查项：
  - 显式 initialize / start / status / stop，状态机正确
  - 连续启停、重复启动（不得出现双服务/双工作线程）
  - 端口冲突、初始化失败 → FAILED 且**不留半个服务**
  - 工作线程随 stop 有界退出（可唤醒，不睡满整轮）
  - 导入模块**不启动任何后台线程**（设计阶段 B 退出条件）
  - mobile profile：独立预算 + 默认关闭源站密集后台任务，且可显式开启
  - 系统挂起识别（挂起的一轮不把任务误判为业务卡死）
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import runtime as rt                       # noqa: E402
from server.runtime import RuntimeController           # noqa: E402

WORKER_NAMES = ("prewarm", "manga-stats-verify", "auto-follow", "search-warm",
                "task-watchdog", "source-health")


def _wr_threads():
    return [t.name for t in threading.enumerate() if t.name.startswith("wr-")]


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ── 1. 工作线程开关矩阵（profile × 显式覆盖 × 全局禁用）────────
def test_worker_plan_desktop_defaults_all_on():
    plan = rt.workers_plan(profile="desktop", env={})
    assert plan == {n: True for n in WORKER_NAMES}, \
        "桌面默认必须与改造前一致（全部开启），不得静默砍掉后台能力"


def test_worker_plan_mobile_turns_off_source_heavy_workers():
    plan = rt.workers_plan(profile="mobile", env={})
    # 设计 §5：mobile 默认关闭"启动即全源健康检查"与"热门搜索预热"，保留本地能力
    assert plan["prewarm"] is False
    assert plan["source-health"] is False
    assert plan["auto-follow"] is False
    assert plan["manga-stats-verify"] is True, "纯本地目录核对应保留"
    assert plan["task-watchdog"] is True, "任务看护应保留"


def test_worker_env_override_and_global_disable():
    assert rt.worker_enabled("prewarm", "mobile", {"WR_BG_PREWARM": "1"}) is True
    assert rt.worker_enabled("task-watchdog", "desktop",
                             {"WR_BG_TASK_WATCHDOG": "0"}) is False
    plan = rt.workers_plan("desktop", {"WR_DISABLE_BACKGROUND": "1"})
    assert not any(plan.values()), "全局禁用必须关掉全部后台线程（未显式覆盖时）"
    # 优先级：显式开关 > 全局禁用（文档口径）——允许"全局关掉、只开回一个"
    plan2 = rt.workers_plan("desktop", {"WR_DISABLE_BACKGROUND": "1",
                                        "WR_BG_SEARCH_WARM": "1"})
    assert plan2["search-warm"] is True and plan2["prewarm"] is False, plan2


# ── 2. initialize：幂等、钩子、失败可重试 ────────────────────
def test_initialize_idempotent_and_records_hooks():
    calls = []

    def _h1():
        calls.append("h1")
        return {"n": 1}

    def _h2():
        calls.append("h2")

    c = RuntimeController(name="t", initialize_hooks=(("a", _h1), ("b", _h2)),
                          logger=lambda *a: None)
    s1 = c.initialize()
    assert s1["hooks"]["a"]["ok"] and s1["hooks"]["a"]["result"] == {"n": 1}
    assert s1["hooks"]["b"]["ok"]
    assert calls == ["h1", "h2"]
    s2 = c.initialize()                      # 幂等：不重复执行
    assert s2.get("already") is True
    assert calls == ["h1", "h2"], "重复 initialize 不得重复执行钩子"
    assert c.status()["initialized"] is True


def test_initialize_failure_sets_failed_and_can_retry():
    state = {"fail": True}

    def _hook():
        if state["fail"]:
            raise RuntimeError("boom")

    c = RuntimeController(name="t", initialize_hooks=(("x", _hook),),
                          logger=lambda *a: None)
    s = c.initialize()
    assert s.get("failed") is True and "boom" in s["error"]
    assert c.status()["state"] == rt.STATE_FAILED
    state["fail"] = False
    s2 = c.initialize(force=True)
    assert not s2.get("failed"), "初始化失败后必须能显式重试"
    assert c.status()["initialized"] is True


# ── 3. 工作线程：启动/停止/重复（不得出现双份）────────────────
def _dummy_worker(stop_event):
    while not stop_event.wait(0.05):
        pass


def test_workers_start_and_stop_boundedly():
    c = RuntimeController(name="t", logger=lambda *a: None)
    for n in WORKER_NAMES:
        c.register_worker(n, _dummy_worker)
    c.initialize()
    plan = c.start_workers(profile="desktop", env={})
    assert all(plan.values())
    time.sleep(0.2)
    assert sorted(c.status()["workers_alive"]) == sorted(WORKER_NAMES)
    c.stop("test")
    assert c.status()["state"] == rt.STATE_STOPPED
    assert c.status()["workers_alive"] == [], "stop 后工作线程必须全部退出"
    assert not _wr_threads(), f"仍有残留线程: {_wr_threads()}"


def test_repeated_start_stop_no_duplicate_workers():
    c = RuntimeController(name="t", logger=lambda *a: None)
    for n in WORKER_NAMES:
        c.register_worker(n, _dummy_worker)
    for i in range(5):
        c.start(start_workers=True, profile="desktop", env={})
        assert len(c.status()["workers_alive"]) == len(WORKER_NAMES), f"第{i}轮"
        c.stop(f"round{i}")
        assert c.status()["workers_alive"] == []
    assert not _wr_threads()
    # 重复 stop 幂等
    assert c.stop("again")["state"] == rt.STATE_STOPPED


def test_double_start_is_idempotent():
    c = RuntimeController(name="t", logger=lambda *a: None)
    c.register_worker("task-watchdog", _dummy_worker)
    c.start(start_workers=True, profile="desktop", env={})
    first = c.status()
    c.start(start_workers=True, profile="desktop", env={})     # 再启动一次
    second = c.status()
    assert first["instance_id"] == second["instance_id"]
    assert len(second["workers_alive"]) == 1, "重复 start 不得起第二份工作线程"
    c.stop("done")


# ── 4. HTTP 服务：真实起停 + 端口冲突 ────────────────────────
def _mk_app():
    from flask import Flask, jsonify
    a = Flask("runtime-test")

    @a.get("/health")
    def _h():
        return jsonify({"ok": True})

    return a


def test_serve_and_stop_closes_port():
    c = RuntimeController(name="t", logger=lambda *a: None)
    c.register_worker("task-watchdog", _dummy_worker)
    c.start(wsgi_app=_mk_app(), host="127.0.0.1", port=0, server="werkzeug",
            start_workers=True, profile="desktop", env={})
    st = c.status()
    assert st["state"] == rt.STATE_READY and st["port"] > 0, st
    with urllib.request.urlopen(f"http://127.0.0.1:{st['port']}/health", timeout=5) as r:
        assert r.status == 200 and b'"ok":true' in r.read()
    c.stop("test")
    with pytest.raises(Exception):
        urllib.request.urlopen(f"http://127.0.0.1:{st['port']}/health", timeout=3)


def test_port_conflict_fails_without_leaking_service():
    busy = socket.socket()
    busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    port = busy.getsockname()[1]
    try:
        c = RuntimeController(name="t", logger=lambda *a: None)
        c.register_worker("task-watchdog", _dummy_worker)
        c.start(wsgi_app=_mk_app(), host="127.0.0.1", port=port, server="werkzeug",
                start_workers=True, profile="desktop", env={})
        st = c.status()
        assert st["state"] == rt.STATE_FAILED, f"端口冲突应明确失败: {st}"
        assert st["error"], "失败必须带原因"
        assert st["workers_alive"] == [], "启动失败不得留下工作线程"
        assert not _wr_threads()
    finally:
        busy.close()


# ── 5. 系统挂起识别（挂起的一轮不算业务卡死）─────────────────
def test_begin_round_detects_suspend_gap():
    c = RuntimeController(name="t", logger=lambda *a: None)
    assert c.begin_round("w", 600) is False          # 首轮无基准
    assert c.begin_round("w", 600) is False          # 正常间隔
    with c._lock:                                    # 伪造"上轮发生在很久以前"
        c._worker_round["w"] = (time.monotonic() - 3600, 2)
    assert c.begin_round("w", 600) is True, "间隔远超预期应判定为被挂起"


# ── 6. 导入模块不得启动后台线程（阶段 B 退出条件）─────────────
def test_import_app_starts_no_background_threads():
    code = (
        "import os,sys,json,threading\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "os.environ.pop('WR_DISABLE_BACKGROUND', None)\n"
        "before = {t.name for t in threading.enumerate()}\n"
        "import app\n"
        "after = {t.name for t in threading.enumerate()}\n"
        "new = sorted(after - before)\n"
        "print(json.dumps({'new': new, 'state': app._RUNTIME.status()['state'],\n"
        "                  'initialized': app._RUNTIME.status()['initialized']}))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       timeout=120, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-800:]
    d = json.loads(r.stdout.strip().splitlines()[-1])
    bad = [n for n in d["new"]
           if n in ("prewarm", "auto-follow", "task-watchdog", "src-health",
                    "manga-stats-verify", "runtime-http") or n.startswith("wr-")]
    assert bad == [], f"导入 app 后出现了后台线程: {bad}"
    assert d["state"] == "stopped" and d["initialized"] is False, \
        "导入不得自动 initialize/start（应由宿主显式调用）"


# ── 7. mobile profile 预算（子进程，保证 env 在 config 导入前生效）──
def test_mobile_profile_budgets_and_defaults():
    code = (
        "import sys,json\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "import engine.config as c\n"
        "from server import runtime as r\n"
        "print(json.dumps({'profile': c.PROFILE, 'mobile': c.IS_MOBILE,\n"
        "  'search': c.SEARCH_MAX_WORKERS, 'crawl': c.CRAWL_DEFAULT_CONCURRENCY,\n"
        "  'host': c.FETCH_HOST_CONCURRENCY, 'enhance': c.ENHANCE_MAX_WORKERS,\n"
        "  'plan': r.workers_plan(c.PROFILE)}))\n"
    )
    env = dict(os.environ, WR_PROFILE="mobile")
    env.pop("WR_DISABLE_BACKGROUND", None)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       timeout=120, cwd=ROOT, env=env)
    assert r.returncode == 0, r.stderr[-800:]
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert d["profile"] == "mobile" and d["mobile"] is True
    assert (d["search"], d["crawl"], d["host"], d["enhance"]) == (8, 2, 2, 2), d
    assert d["plan"]["prewarm"] is False and d["plan"]["source-health"] is False
    assert d["plan"]["task-watchdog"] is True
