# -*- coding: utf-8 -*-
"""小说源验证也必须有硬性时限 + 精确选书（0.74.5）。

与漫画侧同一个缺陷（`engine/manga/verify` 已修）：
  1. 适配器自己的 timeout 不可靠——多地址域会逐个试，等待被地址数量乘出来；
  2. 搜索失败时还会**换 4 个关键词重试**，等于把同一段超时等 4 遍
     （漫画侧实测：nhentai 单源 120 秒，用户按一次"验证"干等两分钟）；
  3. 选书取"第一条"：源站模糊匹配会把同名前缀的书排前面，
     实测 ixdzs8 对「剑来」第一条是《青冥等剑来》89 章，《剑来》1278 章掉到第 6 条
     —— 按第一条判定就会把它误报成"目录退化"。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import source_verify as SV  # noqa: E402
from engine.app_utils import pick_title_match, StageTimeout  # noqa: E402


def test_pick_title_match_prefers_exact():
    rows = [{"name": "青冥等剑来", "book_url": "a"},
            {"name": "剑来同人", "book_url": "b"},
            {"name": "剑来", "book_url": "c"}]
    assert pick_title_match(rows, "剑来")["book_url"] == "c"
    # 没有精确匹配 → 退"包含关键词"
    assert pick_title_match([{"name": "青冥等剑来", "book_url": "a"}],
                            "剑来")["book_url"] == "a"
    # 都不匹配 → 第一条（并如实返回，不抛）
    assert pick_title_match([{"name": "别的书", "book_url": "z"}],
                            "剑来")["book_url"] == "z"
    assert pick_title_match([], "剑来") == {}


def test_verify_search_is_bounded_and_does_not_retry_keywords(monkeypatch):
    """搜索卡住时：必须在预算内收尾，且**不再换关键词**（换词只会重复等超时）"""

    class _HangCrawler(object):
        calls = []

        def __init__(self, *a, **k):
            pass

        def search(self, kw, page=1):
            _HangCrawler.calls.append(kw)
            time.sleep(30)
            return []

    import engine.crawler as _cr
    monkeypatch.setattr(_cr, "SourceCrawler", _HangCrawler, raising=False)
    monkeypatch.setattr(SV, "needs_js", lambda src: (False, ""))
    t0 = time.time()
    out = SV.verify_one({"uid": "u1", "bookSourceName": "慢源"},
                        keyword="剑来", budget={"search": 2, "book": 2,
                                                "toc": 2, "content": 2})
    dt = time.time() - t0
    assert dt < 6, f"单源验证必须限时收尾，实际 {dt:.1f}s"
    assert len(_HangCrawler.calls) == 1, (
        "连接/超时类失败不得换关键词重试：%s" % _HangCrawler.calls)
    assert out["status"] == "failed"
    assert "硬性时限" in out["stages"]["search"]["detail"], out["stages"]["search"]


def test_verify_picks_exact_title_not_first_hit(monkeypatch):
    """搜索结果里《剑来》在第 3 条时，验证必须选它（不是第一条）"""
    picked = {}

    class _Crawler(object):
        def __init__(self, *a, **k):
            pass

        def search(self, kw, page=1):
            return [{"name": "青冥等剑来", "book_url": "https://x/a"},
                    {"name": "剑来同人", "book_url": "https://x/b"},
                    {"name": "剑来", "book_url": "https://x/c"}]

        def get_book(self, url, fast=False, deadline=None):
            picked["book"] = url
            raise RuntimeError("到此为止：本用例只关心选哪本")

    import engine.crawler as _cr2
    monkeypatch.setattr(_cr2, "SourceCrawler", _Crawler, raising=False)
    monkeypatch.setattr(SV, "needs_js", lambda src: (False, ""))
    out = SV.verify_one({"uid": "u2", "bookSourceName": "选书"},
                        keyword="剑来", budget={"search": 3, "book": 2,
                                                "toc": 2, "content": 2})
    assert picked.get("book") == "https://x/c", picked
    assert out["stages"]["search"]["sample"]["book_url"] == "https://x/c"
