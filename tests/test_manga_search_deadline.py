# -*- coding: utf-8 -*-
"""漫画聚合搜索的**期限必须真的是期限**（0.74.1 真机事故的放大器）。

事故链（用户反馈"0.74.1 的所有源都超时了"）：
  1. 拷贝漫画网页通道在手机上不可达 → 失败；
  2. 回落到 APP 通道，而那是"7 个域 × 重试 3 次 × 15 秒"的长链；
  3. 这个 worker 卡住不返回；
  4. 聚合搜索本有 20/25 秒期限，但实现用了
     `with ThreadPoolExecutor(...)` —— 退出时等价于 `shutdown(wait=True)`，
     把期限作废，**必须等那个卡死的 worker**；
  5. 前端 30 秒超时先炸 → 用户看到"所有源都超时"，其它源明明早就返回了。

本文件锁死第 4 条：期限到点，响应就必须回来，且如实标注未完成的源。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import manga_api as MA  # noqa: E402


class _SlowAdapter(object):
    """卡住的源：比期限长得多（模拟"域池 × 重试"长链）"""

    key = "slowpoke"
    name = "卡死的源"
    concurrent = 1

    def __init__(self, sleep_s):
        self._sleep = sleep_s

    def search(self, q, page=1, order=""):
        time.sleep(self._sleep)
        return []


class _FastAdapter(object):
    key = "quick"
    name = "快源"
    concurrent = 1

    def search(self, q, page=1, order=""):
        from engine.manga.base import Comic
        return [Comic(id="x1", title="找到的书", source_key="quick")]


@pytest.fixture
def _short_deadlines(monkeypatch):
    """把期限压到 1 秒，测试才能秒级跑完（断言的是"是否真的按期限返回"）"""
    monkeypatch.setattr(MA, "_MANGA_FAST_DEADLINE", 1)
    monkeypatch.setattr(MA, "_MANGA_SLOW_DEADLINE", 1)
    monkeypatch.setattr(MA, "_MANGA_PATIENCE", 0.5)


def test_deadline_returns_even_if_worker_hangs(monkeypatch, _short_deadlines):
    monkeypatch.setattr(MA, "_manga_search_adapters",
                        lambda source: [_SlowAdapter(8.0)])
    t0 = time.time()
    out = MA._do_manga_search("剑来", "", 1)
    dt = time.time() - t0
    assert dt < 4, ("期限到点必须返回，不能被卡死的 worker 拖住"
                    "（with ThreadPoolExecutor 的 __exit__ 会 wait=True）实际 %.1fs" % dt)
    assert out["results"] == []
    assert "slowpoke" in out["errors"], out["errors"]
    assert "超时" in out["errors"]["slowpoke"], out["errors"]


def test_other_sources_still_return_when_one_hangs(monkeypatch, _short_deadlines):
    """一个源卡死，其它源的结果必须照常拿到（这正是用户看到"全部超时"时缺的）"""
    monkeypatch.setattr(MA, "_manga_search_adapters",
                        lambda source: [_FastAdapter(), _SlowAdapter(8.0)])
    t0 = time.time()
    out = MA._do_manga_search("剑来", "", 1)
    dt = time.time() - t0
    assert dt < 4, f"有源返回后不该再等卡死的源：{dt:.1f}s"
    titles = [g.get("title") for g in out["results"]]
    assert "找到的书" in titles, out["results"]
    assert "slowpoke" in out["errors"]
