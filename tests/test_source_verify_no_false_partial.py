# -*- coding: utf-8 -*-
"""R78 回归：逐源验证不得用"人为小上限"把**能用的源**判成 partial。

实测缺陷（2026-09-18，手机口径 15 个启用源）：
  · 正文阶段写死 `max_pages=1`，而 R78 起"撞上限仍有下一页 = 整章失败
    （不静默截断）" → **任何章节跨页的源**在验证里必然失败，被标成
    `android_partial`，用户书源页看到"正文阶段失败：分页数达到上限 1 页"。
    实测顶点(m版) 就被这样误判，而同章另测正文 2982 字完全正常；
  · 目录阶段写死 `max_pages=3`，规则引擎源的分页目录会被截成 3 页
    （适配器源本来就忽略该参数，所以问题只在规则引擎源上暴露）。

判据（两条都要成立）：
  1. 验证调用正文/目录时**不传**人为小上限（边界交给 deadline）；
  2. 跨页章节的源在验证里必须判 **verified**（用假爬虫复现"首章两页"形态）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import source_verify            # noqa: E402
from engine.crawler import SourceCrawler    # noqa: E402


SRC = {"uid": "verify_paging_test", "bookSourceName": "跨页章节测试源",
       "bookSourceUrl": "https://verify.test"}


class _Book:
    book_url = "https://verify.test/book/1/"
    toc_url = ""
    name = "剑来"


def test_verification_has_no_artificial_page_caps():
    """静态判据：验证代码里不得再出现 max_pages=1 / max_pages=3"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "engine", "source_verify.py")
    import io
    # 只看**代码行**：说明这件事的注释里本来就会出现这些字面量
    code = [ln for ln in io.open(path, encoding="utf-8").read().split("\n")
            if not ln.lstrip().startswith("#")]
    blob = "\n".join(code)
    assert "max_pages=1" not in blob, "正文阶段又设了 1 页上限（跨页章节会被误判 partial）"
    assert "max_pages=3" not in blob, "目录阶段又设了 3 页上限（分页目录会被截断）"


def test_paginated_source_verifies(monkeypatch):
    """行为判据：首章跨两页的源必须判 verified（旧实现给 partial）"""
    seen = {}

    def fake_search(self, kw, page=1):
        return [{"name": kw, "book_url": "https://verify.test/book/1/"}]

    def fake_get_book(self, url, fast=False, deadline=None):
        return _Book()

    def fake_get_toc(self, book, **kw):
        seen["toc_kw"] = kw
        return [{"name": f"第{i}章", "url": f"https://verify.test/book/1/{i}.html"}
                for i in range(1, 6)]

    def fake_get_content(self, url, *a, **kw):
        seen["content_kw"] = kw
        # 关键：如果调用方传了 max_pages=1，就复现"撞上限=整章失败"
        if kw.get("max_pages") == 1:
            from engine.fetcher import DeadlineExceeded
            raise DeadlineExceeded("分页数达到上限 1 页（已取 1 页/1128 字），"
                                   "为避免静默截断按整章失败处理")
        return "陈平安笑了笑，继续往前走。" * 60

    monkeypatch.setattr(SourceCrawler, "search", fake_search)
    monkeypatch.setattr(SourceCrawler, "get_book", fake_get_book)
    monkeypatch.setattr(SourceCrawler, "get_toc", fake_get_toc)
    monkeypatch.setattr(SourceCrawler, "get_content", fake_get_content)

    out = source_verify.verify_one(SRC, keyword="剑来")
    assert out["status"] == "verified", out
    assert out["stages"]["content"]["ok"] is True, out["stages"]["content"]
    assert seen["content_kw"].get("max_pages") is None, \
        f"正文阶段不应传人为页数上限：{seen['content_kw']}"
    assert seen["toc_kw"].get("max_pages") is None, \
        f"目录阶段不应传人为页数上限：{seen['toc_kw']}"
    # 边界仍必须在：deadline 传给下游（不是"取消一切限制"）
    assert seen["content_kw"].get("deadline"), "正文阶段必须仍有 deadline 兜底"
    assert seen["toc_kw"].get("deadline"), "目录阶段必须仍有 deadline 兜底"
