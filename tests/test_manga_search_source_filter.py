# -*- coding: utf-8 -*-
"""分源搜索的服务端契约（离线，不触网）

来源：0.51.0 风险评估 §3 —— 服务端**早就支持** `source` 参数，但原生 App 三条请求
路径一个都没带，用户反馈"分源搜索失效"是准确的。App 侧由设备用例保证"每条路径都带
同一 source"；本用例锁住服务端这一侧的行为，避免以后改坏：

  1. 指定 `source` 时只查该源（per_source_total 的键集合 == {该源}）；
  2. 不指定时查全部已注册源；
  3. `order` 透传：支持排序的适配器会收到它，不支持的适配器不会被它影响（签名不匹配
     时不报错、也不重跑一次搜索）；
  4. 未知源 → 明确业务结果（不是 5xx，也不是"静默查全部源"）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server.manga_api as ma  # noqa: E402
from engine.manga.base import Comic  # noqa: E402
from server import state as st  # noqa: E402


class _Recorder:
    """记录收到的 (keyword, order)；order 为 None 表示该源不支持排序"""
    def __init__(self, key, name, support_order, rows=2):
        self.key, self.name = key, name
        self._support = support_order
        self._rows = rows
        self.calls = []

    def search(self, keyword, page=1):
        self.calls.append((keyword, None))
        return [Comic(id=f"{self.key}-{i}", title=f"{self.key}-{i}", author="",
                      cover="", tags=[], source_key=self.key)
                for i in range(self._rows)]


class _OrderRecorder(_Recorder):
    def search(self, keyword, page=1, order=None):
        self.calls.append((keyword, order))
        return [Comic(id=f"{self.key}-{i}", title=f"{self.key}-{i}", author="",
                      cover="", tags=[], source_key=self.key)
                for i in range(self._rows)]


@pytest.fixture(autouse=True)
def _clean():
    st._manga_search_cache.clear()
    st._manga_search_partial.clear()
    st._manga_search_fail.clear()
    ma._manga_search_latency.clear()
    yield
    st._manga_search_cache.clear()
    st._manga_search_partial.clear()
    st._manga_search_fail.clear()


def _patch(monkeypatch, adapters):
    by_key = {a.key: a for a in adapters}
    monkeypatch.setattr(ma, "_manga_search_adapters",
                        lambda source: ([by_key[source]] if source in by_key
                                        else ([] if source else adapters)))
    return by_key


def test_source_param_limits_search_to_that_source(monkeypatch):
    by = _patch(monkeypatch, [_Recorder("a", "A", False), _Recorder("b", "B", False)])
    out = ma._do_manga_search("关键词", "a", 1)
    assert set(out["per_source_total"].keys()) == {"a"}, out["per_source_total"]
    assert all(r["sources"][0]["source"] == "a" for r in out["results"])
    assert by["b"].calls == [], "指定源时不许去查别的源"


def test_no_source_queries_all_registered(monkeypatch):
    by = _patch(monkeypatch, [_Recorder("a", "A", False), _Recorder("b", "B", False)])
    out = ma._do_manga_search("关键词", "", 1)
    assert set(out["per_source_total"].keys()) == {"a", "b"}
    assert by["a"].calls and by["b"].calls


def test_order_is_passed_only_to_adapters_that_support_it(monkeypatch):
    by = _patch(monkeypatch, [_OrderRecorder("jm", "禁漫", True),
                              _Recorder("md", "MD", False)])
    ma._do_manga_search("关键词", "", 1, order="mr")
    assert by["jm"].calls == [("关键词", "mr")], by["jm"].calls
    # 不支持排序的源：按签名判断后**不传** order（也不会因此重跑一次搜索）
    assert by["md"].calls == [("关键词", None)], by["md"].calls
    assert len(by["md"].calls) == 1, "不支持 order 的源不得被查两次"


def test_unknown_source_is_a_business_result(monkeypatch):
    _patch(monkeypatch, [])
    out = ma._do_manga_search("关键词", "nosuch", 1)
    assert out["results"] == [] and out["sources"] == 0
    assert "没有可用的漫画源" in out["errors"]["_sources"]
