# -*- coding: utf-8 -*-
"""0.71.0 回归（离线）：诊断里的**阅读链路分段测速**。

为什么要有它：提速与"源站风控"必须在**目标机**上量才有结论。用户反馈"禁漫很慢"时，
我们要的是"哪一段慢、走的是哪条传输"，不是猜。所以把一次有界、只读的分段测量
放进诊断（可导出、可在应用内一键跑）。

本用例锁住：
  1. 测速**不抛异常**：源站不可用时每一步都记 ok=false + 原因，整体仍然返回；
  2. 每步都有 ms；一次性图片域探测**单独成一步**（否则会污染"搜索耗时"，数字没法解释）；
  3. 报出传输类型（curl_cffi / requests）与预热/缓存参数，便于对照；
  4. 端点 `/api/diagnostics/latency` 返回 {ok, data, text}，且**只测一次**
     （text 复用同一份测量结果，不再打第二遍源站）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import diag  # noqa: E402


class _Boom:
    """每步都失败、且没有 is/ensure 方法的适配器（模拟源站不可用）"""

    key = "boom"

    def search(self, *a, **k):
        raise RuntimeError("源站不可达")

    def comic_info(self, *a, **k):
        raise RuntimeError("源站不可达")

    def images(self, *a, **k):
        raise RuntimeError("源站不可达")


def test_measure_latency_records_failures_without_raising(monkeypatch, tmp_path):
    import server.state as st
    monkeypatch.setattr(st, "_manga_read_adapter", lambda k: _Boom(), raising=False)
    monkeypatch.setattr(st, "_manga_adapter", lambda k: _Boom(), raising=False)

    d = diag.measure_latency("boom")
    assert isinstance(d, dict) and d.get("source") == "boom"
    steps = d.get("steps") or {}
    assert "search" in steps, steps
    assert steps["search"]["ok"] is False, steps["search"]
    assert "RuntimeError" in steps["search"]["detail"], steps["search"]
    assert isinstance(steps["search"]["ms"], int)
    # 传输类型必须给出（否则用户发的数字无法解读）
    assert d.get("transport") in ("curl_cffi", "requests", "unknown", "no-adapter")


def test_domain_probe_is_its_own_step(monkeypatch):
    """一次性图片域探测必须单独计，不能混进搜索耗时。"""
    import server.state as st

    class _Ad:
        key = "jm"
        _img_domain = "https://img.example"

        def _ensure_img_domain(self):
            return None

        def search(self, *a, **k):
            return []

    monkeypatch.setattr(st, "_manga_read_adapter", lambda k: _Ad(), raising=False)
    monkeypatch.setattr(st, "_manga_adapter", lambda k: _Ad(), raising=False)
    d = diag.measure_latency("jm")
    steps = d.get("steps") or {}
    assert "domain_probe" in steps, steps
    assert steps["domain_probe"]["ok"] is True
    assert "img.example" in steps["domain_probe"]["detail"]


def test_latency_lines_reuse_measurement(monkeypatch):
    """latency_lines(data=...) 必须复用结果，不能再测一次（否则翻倍打源站）。"""
    calls = {"n": 0}
    real = diag.measure_latency

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(diag, "measure_latency", counting, raising=False)
    data = {"source": "jm", "transport": "requests",
            "steps": {"search": {"ok": True, "ms": 12, "detail": "命中 1 条"}}}
    lines = diag.latency_lines("jm", "", data=data)
    assert calls["n"] == 0, "传入 data 时不应再测一次"
    assert any("search" in ln and "12" in ln for ln in lines), lines
    assert any("requests" in ln for ln in lines), lines


def test_latency_endpoint_shape(monkeypatch):
    import app
    app.app.config["TESTING"] = True
    c = app.app.test_client()
    monkeypatch.setattr(diag, "measure_latency",
                        lambda *a, **k: {"source": "jm", "transport": "requests",
                                         "steps": {"search": {"ok": True, "ms": 5,
                                                              "detail": "命中 2 条"}}},
                        raising=False)
    r = c.get("/api/diagnostics/latency?source=jm")
    assert r.status_code == 200, r.data[:200]
    j = r.get_json()
    assert j["ok"] is True and isinstance(j["data"], dict) and j["text"]
    assert "search" in j["text"]
