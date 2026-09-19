# -*- coding: utf-8 -*-
"""渲染误伤自愈回归（R76 / R77，离线、确定性）

背景（真实故障）：一键检查书库更新时并发 3 个渲染 worker 共享浏览器，
某 worker 的 Chrome 僵死/超时触发"全杀重建"，并行中的其它渲染任务会收到
"Target page, context or browser has been closed" 而被误伤。修复前受害者
直接抛错 → 整部漫画记"检查更新失败"（实测每轮 2 部，随机命中）。

两层修复：
- R76（渲染层 engine.manga.copymanga_web._run_pw）：受害者静默等 3s 重试
  一次（自身不再触发 pkill，避免连锁；重试仍失败才抛出）；
- R77（检查 worker 层 server.state._manga_check_worker._one）：整部失败
  （非风控类）重试一次，风控类保持源级短路不重试。

本文件不触网：渲染执行器与检查实现全部打桩。
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.manga.copymanga_web as CW  # noqa: E402
import server.state as ST  # noqa: E402


# ══════════════════════════════════════════════════════════════
# R76：渲染层受害者重试
# ══════════════════════════════════════════════════════════════

class _Fut:
    def __init__(self, exc=None, value=None):
        self._exc, self._value = exc, value

    def result(self, timeout=None):
        if self._exc is not None:
            raise self._exc
        return self._value


class _Exec:
    """按预设序列返回 future 的执行器替身（记录 submit 次数）"""

    def __init__(self, behaviours):
        self.behaviours = list(behaviours)
        self.submits = 0

    def submit(self, fn):
        self.submits += 1
        b = self.behaviours.pop(0) if self.behaviours else (None, "ok")
        kind, payload = b
        if kind == "raise":
            return _Fut(exc=payload)
        return _Fut(value=payload)


@pytest.fixture()
def _fast_sleep(monkeypatch):
    """把 time.sleep 置空：受害者重试的 3s 等待不应拖慢测试"""
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda *_a, **_k: None)


def _closed_err():
    return RuntimeError(
        "网页版详情失败: Page.wait_for_timeout: Target page, context or "
        "browser has been closed")


def test_victim_retries_once_without_killing_chrome(monkeypatch, _fast_sleep):
    """误伤 → 静默重试一次成功；且不触发 pkill（防连锁误伤）"""
    exec_ = _Exec([("raise", _closed_err()), (None, "rendered-ok")])
    kills = []
    monkeypatch.setattr(CW, "_pw_exec", exec_)
    monkeypatch.setattr(CW, "_kill_my_chrome", lambda: kills.append(1))
    out = CW._run_pw(lambda: "inner")
    assert out == "rendered-ok"
    assert exec_.submits == 2, "误伤应重试一次"
    assert kills == [], "受害者不得触发全杀（连锁误伤源）"


def test_victim_retry_failure_propagates(monkeypatch, _fast_sleep):
    """重试仍被误伤 → 确认真故障，抛出（不无限重试）"""
    exec_ = _Exec([("raise", _closed_err()), ("raise", _closed_err())])
    monkeypatch.setattr(CW, "_pw_exec", exec_)
    monkeypatch.setattr(CW, "_kill_my_chrome", lambda: None)
    with pytest.raises(RuntimeError) as ei:
        CW._run_pw(lambda: "inner")
    assert "has been closed" in str(ei.value)
    assert exec_.submits == 2, "受害路径最多重试一次"


def test_timeout_path_still_kills_and_retries(monkeypatch, _fast_sleep):
    """真凶（超时）仍走全杀 + 重建路径（R72d 语义不回归）"""
    import concurrent.futures as cf
    _Exec2 = _Exec
    exec_ = _Exec2([("raise", cf.TimeoutError()), (None, "after-kill")])
    kills = []
    monkeypatch.setattr(CW, "_pw_exec", exec_)
    monkeypatch.setattr(CW, "_kill_my_chrome", lambda: kills.append(1))
    out = CW._run_pw(lambda: "inner")
    assert out == "after-kill"
    assert kills, "超时（真凶）必须先杀 Chrome 释放阻塞线程"
    assert exec_.submits == 2


# ══════════════════════════════════════════════════════════════
# R77：检查 worker 层整部重试
# ══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _reset_check_state():
    with ST._manga_check_lock:
        ST._manga_check_state.update(running=False, total=0, started_at=0.0,
                                     finished_at=0.0)
        ST._manga_check_state["results"] = []
    yield
    with ST._manga_check_lock:
        ST._manga_check_state["results"] = []


def _run_worker(monkeypatch, results_seq):
    """以打桩的 _manga_check_one 运行一次检查 worker，返回 (results, calls)"""
    calls = []

    def _fake_check_one(src, cid, force_refresh=False):
        calls.append((src, cid))
        return results_seq[min(len(calls) - 1, len(results_seq) - 1)]
    monkeypatch.setattr(ST, "_manga_check_one", _fake_check_one)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    ST._manga_check_worker([("copymanga_web", "cid1", "测试漫画")])
    with ST._manga_check_lock:
        return list(ST._manga_check_state["results"]), calls


def test_check_worker_retries_once_on_failure(monkeypatch):
    """首次失败（渲染误伤）→ 整部重试一次并成功，结果无 error"""
    fail = {"ok": False, "error": "检查更新失败（详细错误见服务端日志）"}
    ok = {"ok": True, "has_update": False, "missing_count": 0,
          "latest": "第10話", "missing": []}
    res, calls = _run_worker(monkeypatch, [fail, ok])
    assert len(calls) == 2, "失败应重试一次"
    assert len(res) == 1 and not res[0]["error"]
    assert res[0]["latest"] == "第10話"


def test_check_worker_no_retry_on_rate_limit(monkeypatch):
    """风控类失败不重试：保持源级短路契约（避免加剧风控）"""
    rl = {"ok": False, "error": "源站风控冷却中，请稍后重试"}
    res, calls = _run_worker(monkeypatch, [rl])
    assert len(calls) == 1, "风控不应重试"
    assert res and res[0].get("skipped") is True
    assert "风控" in res[0]["error"]


def test_check_worker_double_failure_reports_error(monkeypatch):
    """两次都失败 → 只记一次失败结果（重试恰好一次，不无限重试）"""
    fail = {"ok": False, "error": "检查更新失败（详细错误见服务端日志）"}
    res, calls = _run_worker(monkeypatch, [fail, fail])
    assert len(calls) == 2
    assert len(res) == 1 and res[0]["error"]


def test_check_worker_success_first_try_no_retry(monkeypatch):
    ok = {"ok": True, "has_update": True, "missing_count": 2,
          "latest": "第12話", "missing": [{"id": "a", "name": "第11話"}]}
    res, calls = _run_worker(monkeypatch, [ok])
    assert len(calls) == 1
    assert res[0]["has_update"] is True and res[0]["missing_count"] == 2


def test_check_worker_exception_retried_then_reported(monkeypatch):
    """_manga_check_one 抛异常（worker 兜底路径）同样重试一次"""
    state = {"n": 0}

    def _boom(src, cid, force_refresh=False):
        state["n"] += 1
        raise RuntimeError("boom")
    monkeypatch.setattr(ST, "_manga_check_one", _boom)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    ST._manga_check_worker([("copymanga_web", "cid2", "测试漫画2")])
    with ST._manga_check_lock:
        res = list(ST._manga_check_state["results"])
    assert state["n"] == 2, "异常路径也应重试一次"
    assert len(res) == 1 and res[0]["error"]
