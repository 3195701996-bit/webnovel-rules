# -*- coding: utf-8 -*-
"""0.69.0 回归（离线）：搜索返回后的**后台预热**（封面 + 前几条详情）。

用户现场反馈（真机 0.67.0）：
  · "jm 源加载速度还是很慢，包括搜索结果的封面，详情页，漫画内容"。

实测（直连）分段耗时：
  · jm 搜索 /search 2.2s（热 0.8s）
  · jm 详情 /album 首 6.9s、热 0.8–1.3s
  · jm 章节 /chapter 1.8s（热 0.85s）
  · 每张图 1.6s
可见"点开才去取"是主要体感来源。因此搜索返回后由引擎**后台**预热：
  1. 本页封面的代理缓存（最多 12 张，详见 test_manga_cover_proxy.py）；
  2. **前 3 条结果的详情**（点进去就不用再等 /album）。

本用例锁住第 2 条：有界（≤3）、已有缓存跳过、且经详情单飞提交。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _enable_search_warm(monkeypatch):
    """测试环境默认 `WR_DISABLE_BACKGROUND=1`（不起后台线程），
    而本文件测的正是"后台预热是否被正确提交"，所以这里显式打开这个开关。"""
    monkeypatch.setenv("WR_BG_SEARCH_WARM", "1")


def test_search_warms_top_details_bounded(monkeypatch, tmp_path):
    import server.manga_api as m
    import server.state as st

    monkeypatch.setattr(m, "MANGA_CACHE_DIR", str(tmp_path), raising=False)
    submitted = []

    def fake_refresh(source, comic_id, builder, delay=0.0):
        submitted.append((source, comic_id))
        return True

    monkeypatch.setattr(m, "_detail_refresh_async", fake_refresh, raising=False)
    monkeypatch.setattr(m, "_refresh_detail_cache", lambda *a, **k: {}, raising=False)

    results = [{"source": "jm", "id": str(1000 + i), "cover": ""} for i in range(10)]
    n = m._warm_search_details(results, "jm", limit=3)
    assert n == 3, f"最多预热 3 条，实际 {n}"
    assert [c for _, c in submitted] == ["1000", "1001", "1002"], submitted


def test_search_warms_skip_when_detail_cached(monkeypatch, tmp_path):
    import server.manga_api as m

    monkeypatch.setattr(m, "MANGA_CACHE_DIR", str(tmp_path), raising=False)
    # 第 1 条已有详情缓存 → 应跳过，只预热后两条
    d = tmp_path / "jm" / "1000"
    d.mkdir(parents=True)
    (d / "_info_full.json").write_text(json.dumps({"ts": 1, "data": {"chapters": []}}),
                                      encoding="utf-8")
    submitted = []
    monkeypatch.setattr(m, "_detail_refresh_async",
                        lambda s, c, b, delay=0.0: submitted.append((s, c)) or True, raising=False)
    monkeypatch.setattr(m, "_refresh_detail_cache", lambda *a, **k: {}, raising=False)

    n = m._warm_search_details([{"source": "jm", "id": "1000"},
                                {"source": "jm", "id": "1001"},
                                {"source": "jm", "id": "1002"}], "jm", limit=3)
    assert n == 2, f"已有详情缓存的条目应跳过，实际预热 {n}"
    assert [c for _, c in submitted] == ["1001", "1002"], submitted
