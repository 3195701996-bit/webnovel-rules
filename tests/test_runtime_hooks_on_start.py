# -*- coding: utf-8 -*-
"""start() 路径必须真的执行初始化钩子（离线，不触网）

2026-09-15 设备实测抓到的真实缺陷：
  RuntimeController.start() 先把状态置为 STARTING，再调用 initialize()；
  而 initialize() 的早退条件里把 STATE_STARTING 也当"已完成"直接返回 →
  **在这条路径上初始化钩子从不执行**。手机端（mobile_entry 走 start()）因此
  从来没跑过 recover_tasks：系统强杀/重启/升级后，_tasks.json 里的漫画下载任务
  不会被装载，界面里任务消失、也无法点"继续"。
  桌面 main() 是先 initialize() 再 start()，所以一直没暴露。

本用例锁住：start() 必须执行钩子；已初始化后重复 start() 不许重复执行。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import runtime as rt  # noqa: E402


def _controller(calls):
    def hook_a():
        calls.append("a")
        return {"ok": 1}

    def hook_b():
        calls.append("b")
        return {"ok": 2}

    return rt.RuntimeController(name="t", initialize_hooks=(hook_a, hook_b),
                                workers={}, logger=lambda *_: None)


def test_start_runs_initialize_hooks():
    calls = []
    c = _controller(calls)
    st = c.start(wsgi_app=None, start_workers=False, profile="mobile")
    assert calls == ["a", "b"], f"start() 必须执行初始化钩子，实际 {calls}"
    assert c.status().get("initialized") is True
    assert st.get("state") == rt.STATE_READY


def test_initialize_then_start_does_not_run_hooks_twice():
    calls = []
    c = _controller(calls)
    c.initialize(profile="mobile")
    assert calls == ["a", "b"]
    c.start(wsgi_app=None, start_workers=False, profile="mobile")
    assert calls == ["a", "b"], f"钩子不得重复执行，实际 {calls}"


def test_hook_failure_is_not_swallowed_on_start():
    calls = []

    def boom():
        calls.append("boom")
        raise RuntimeError("钩子故意失败")

    c = rt.RuntimeController(name="t", initialize_hooks=(boom,), workers={},
                             logger=lambda *_: None)
    st = c.start(wsgi_app=None, start_workers=False, profile="mobile")
    assert calls == ["boom"], "失败的钩子必须被执行到（不许被早退吞掉）"
    assert st.get("state") == rt.STATE_FAILED, "钩子失败必须让启动失败，不许静默继续"
