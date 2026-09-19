# -*- coding: utf-8 -*-
"""漫画搜索"不再为一个慢源干等 20s"的契约回归（离线，不触网）

起因（2026-09-14 实测）：用户在实例上搜索**每次都正好 20.0s**。查明不是所有源都慢，
而是一个不可达源（nhentai）拖满了自己的 20s 期限：其它源早已返回结果，用户却要等到
期限结束才看到任何东西；而且带 errors 的结果按 R66 旧规则不缓存 → **连连续重搜、
翻页都要再等一轮 20s**。

新口径：
  1. 只要已有源返回结果，最多再等 `_MANGA_PATIENCE` 秒就带着**部分结果**返回；
  2. 未返回的源如实写进 errors，且文案区分"仍在查询（已先显示其它源结果）"与"搜索超时"；
  3. 部分结果给 60s 短缓存：连续重搜/翻页秒回，一分钟后的重试仍会真正重跑。
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server.manga_api as ma  # noqa: E402
from engine.manga.base import Comic  # noqa: E402
from server import state as st  # noqa: E402


class _FakeAdapter:
    def __init__(self, key, name, rows=1, delay=0.0, never=False):
        self.key, self.name = key, name
        self._rows, self._delay, self._never = rows, delay, never

    def search(self, keyword, page=1, order=None):
        if self._never:
            time.sleep(60)          # 模拟不可达源：永远不返回
        if self._delay:
            time.sleep(self._delay)
        return [Comic(id=f"{self.key}-{i}", title=f"{self.key}-{i}", author="",
                      cover="", tags=[], source_key=self.key)
                for i in range(self._rows)]


@pytest.fixture(autouse=True)
def _clean_caches():
    st._manga_search_cache.clear()
    st._manga_search_partial.clear()
    st._manga_search_fail.clear()
    ma._manga_search_latency.clear()
    yield
    st._manga_search_cache.clear()
    st._manga_search_partial.clear()
    st._manga_search_fail.clear()


def _patch(monkeypatch, adapters):
    monkeypatch.setattr(ma, "_manga_search_adapters", lambda source: adapters)


def test_returns_early_with_partial_results(monkeypatch):
    """一个不可达源不得把整次搜索拖满 20s"""
    _patch(monkeypatch, [_FakeAdapter("fast", "快源", rows=3),
                         _FakeAdapter("dead", "不可达源", never=True)])
    monkeypatch.setattr(ma, "_MANGA_PATIENCE", 1.0)
    t = time.time()
    out = ma._do_manga_search("测试", "", 1)
    dt = time.time() - t
    assert dt < 5, f"应带部分结果提前返回，实际等了 {dt:.1f}s"
    assert len(out["results"]) == 3, "快源结果必须返回"
    assert "dead" in out["errors"], "未返回的源必须如实进 errors"
    assert "仍在查询" in out["errors"]["dead"], out["errors"]["dead"]


def test_all_fast_sources_have_no_errors(monkeypatch):
    """没有慢源时行为不变：结果齐全、errors 为空"""
    _patch(monkeypatch, [_FakeAdapter("a", "A", rows=2), _FakeAdapter("b", "B", rows=1)])
    out = ma._do_manga_search("测试", "", 1)
    assert len(out["results"]) == 3
    assert out["errors"] == {}


def test_partial_result_is_cached_briefly(monkeypatch):
    """部分结果短缓存：连续重搜秒回，且 errors 一起带回来（界面照实显示）"""
    _patch(monkeypatch, [_FakeAdapter("fast", "快源", rows=2),
                         _FakeAdapter("dead", "不可达源", never=True)])
    monkeypatch.setattr(ma, "_MANGA_PATIENCE", 0.5)
    t = time.time()
    first, cached1 = st._manga_search_cached(("q", "uid", "mr", "manga"),
                                            lambda: ma._do_manga_search("q", "", 1))
    dt1 = time.time() - t
    assert dt1 >= 0.5 and not cached1

    t = time.time()
    again, cached2 = st._manga_search_cached(("q", "uid", "mr", "manga"),
                                            lambda: ma._do_manga_search("q", "", 1))
    dt2 = time.time() - t
    assert cached2 is True, "第二次应命中（短）缓存"
    assert dt2 < 0.2, f"连续重搜不该再等一轮，实际 {dt2:.2f}s"
    assert "dead" in again["errors"], "命中缓存也要保留 errors（不许假装全部成功）"


def test_partial_cache_expires_and_reruns(monkeypatch):
    """短缓存过期后要真正重跑失败源（R66 的本意保留）"""
    calls = []

    class _Counting(_FakeAdapter):
        def search(self, keyword, page=1, order=None):
            calls.append(self.key)
            return super().search(keyword, page)

    _patch(monkeypatch, [_Counting("fast", "快源", rows=1),
                         _Counting("dead", "不可达源", never=True)])
    monkeypatch.setattr(ma, "_MANGA_PATIENCE", 0.3)
    key = ("q2", "uid", "mr", "manga")
    st._manga_search_cached(key, lambda: ma._do_manga_search("q2", "", 1))
    n1 = len(calls)
    st._manga_search_cached(key, lambda: ma._do_manga_search("q2", "", 1))
    assert len(calls) == n1, "短缓存内不应重跑"
    # 人为把写入时间拨老（超过短 TTL）→ 必须重跑
    with st._MANGA_SEARCH_LOCK:
        ts, payload = st._manga_search_cache[key]
        st._manga_search_cache[key] = (ts - (st._MANGA_SEARCH_PARTIAL_TTL + 1), payload)
    st._manga_search_cached(key, lambda: ma._do_manga_search("q2", "", 1))
    assert len(calls) > n1, "短缓存过期后必须真的重跑失败源"


def test_patience_does_not_break_full_result_caching(monkeypatch):
    """全部源都成功时仍走完整 TTL（行为不变）"""
    _patch(monkeypatch, [_FakeAdapter("a", "A", rows=2)])
    key = ("q3", "uid", "mr", "manga")
    _, c1 = st._manga_search_cached(key, lambda: ma._do_manga_search("q3", "", 1))
    _, c2 = st._manga_search_cached(key, lambda: ma._do_manga_search("q3", "", 1))
    assert c1 is False and c2 is True
    assert key not in st._manga_search_partial, "完整结果不该被标成部分缓存"


def test_stream_endpoint_also_stops_early(monkeypatch):
    """流式端点同一口径：已有结果就不把流一直挂着（否则"搜索中"与汇总行等满 20s）"""
    import json as _json
    _patch(monkeypatch, [_FakeAdapter("fast", "快源", rows=2),
                         _FakeAdapter("dead", "不可达源", never=True)])
    monkeypatch.setattr(ma, "_MANGA_PATIENCE", 1.0)
    client = None
    import app as _app
    client = _app.app.test_client()
    t = time.time()
    r = client.get("/api/manga/search/stream?q=测试流式&page=1")
    body = r.get_data(as_text=True)
    dt = time.time() - t
    assert r.status_code == 200
    events = [ln[len("data:"):].strip() for ln in body.splitlines()
              if ln.startswith("data:")]
    assert events, "流式端点必须至少推一条事件"
    parsed = [_json.loads(e) for e in events]
    assert parsed[-1].get("finished") is True, "最后一条必须是 finished 事件"
    assert dt < 6, f"已有结果时应提前收流，实际 {dt:.1f}s"
    assert "dead" in (parsed[-1].get("errors") or {}), "未返回的源必须如实进 errors"
    assert "仍在查询" in parsed[-1]["errors"]["dead"]
