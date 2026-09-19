# -*- coding: utf-8 -*-
"""漫画源验证**必须有硬性时限**（0.74.2 实测：nhentai 单源耗了 120 秒）。

背景：`urllib.request.urlopen(timeout=20)` 在目标域有多个地址（IPv4/IPv6、多条
A 记录）时会**逐个**去连，每地址各等一次超时；再叠加"换关键词重试"，
用户按一次「逐源校验」就要干等两分钟。修法：
  1. 阶段调用套硬性时限（`call_bounded`），超时即放弃并如实标注；
  2. 连接类/超时类失败**不再换关键词**（换词对不可达的域毫无意义）。

实测：nhentai 120s → 20s；包子 403 仍在 1.6~2.3s 内返回。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.manga import verify as mv  # noqa: E402


def test_call_bounded_returns_value():
    assert mv.call_bounded(lambda: 42, 2) == 42


def test_call_bounded_raises_on_timeout():
    t0 = time.time()
    with pytest.raises(mv._StageTimeout):
        mv.call_bounded(lambda: time.sleep(5), 1)
    assert time.time() - t0 < 3, "超时判定必须比被调函数本身短得多"


def test_call_bounded_propagates_adapter_error():
    def _boom():
        raise ValueError("适配器自己的错")
    with pytest.raises(ValueError):
        mv.call_bounded(_boom, 2)


def test_verify_one_is_bounded_on_unreachable_source(monkeypatch):
    """不可达的源：验证必须**按预算收尾**，不能靠适配器自己的超时链拖几分钟。"""
    class _Hang:
        key = "hang"
        name = "卡死的源"

        def search(self, kw, page=1):
            time.sleep(30)
            return []

    monkeypatch.setattr(mv, "registered_keys", lambda: ["hang"])
    from engine.manga import manager as mgr
    monkeypatch.setattr(mgr, "get_adapter", lambda key, state_dir=None: _Hang())
    from server import capabilities as cap
    monkeypatch.setattr(cap, "source_status",
                        lambda key, caps=None: {"status": "supported", "reason": "",
                                                "transport": {}})
    t0 = time.time()
    out = mv.verify_one("hang", keyword="巨人", budget={"search": 2, "detail": 2,
                                                       "images": 2})
    dt = time.time() - t0
    assert dt < 8, f"单源验证必须限时收尾，实际 {dt:.1f}s"
    assert out["status"] == "failed"
    detail = out["stages"]["search"]["detail"]
    assert "硬性时限" in detail, detail
