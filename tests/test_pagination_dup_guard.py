# -*- coding: utf-8 -*-
"""R79 回归：分页**重复页/重叠前缀**防护（实测缺陷，2026-09-18）。

真实现象（用户真实书库取证）：
  · yetianlian 第1384章 79 段里，**第 29~52 段与第 53~76 段完全相同**（重复 24 段）；
  · quanben8 第1113章重复 15 段 —— 两者都**正好是一页正文的量**，
    即分页时把同一页 append 了两遍（站点"下一页"指回已取过的页 / 同一内容换个 URL）。

本文件用**假 fetcher + 真规则引擎**离线锁死三条：
  1. 新页开头与已累积正文结尾重叠 → 只去掉那段重叠前缀，**后面的新内容照常保留**；
  2. 整页内容全是已有的 → 判为重复页，不追加并**停止翻页**（否则会在 A→B→A 环里空转到上限）；
  3. "下一页"指回**已取过的页** → 直接停。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.crawler import SourceCrawler  # noqa: E402

P1 = ["第一页第一段，足够长以便被当成正文段落处理。" * 2,
      # 末段要有 30 字以上：单段重叠去重要求它够长（见下一条用例的边界说明）
      "第一页第二段，这一段写得足够长，好让页缝重叠时能被判出来并去掉。" * 1]
P2 = ["第二页第一段，这是新内容。" * 3]
P3 = ["第三页第一段，收尾内容。" * 3]

SOURCE = {
    "bookSourceUrl": "https://page.test",
    "ruleContent": {"content": "div#c@text", "nextContentUrl": "a#n@href"},
}


class FakeFetcher:
    """按 URL 返回预设 HTML；记录取过哪些页。"""

    def __init__(self, pages):
        self.pages = pages
        self.got = []

    def get(self, url, **kw):
        self.got.append(url)
        return self.pages[url]


def _page(body, nxt):
    body_html = "".join(f"<p>{p}</p>" for p in body)
    link = f'<a id="n" href="{nxt}">下一章</a>' if nxt else ""
    return f"<html><body><div id='c'>{body_html}</div>{link}</body></html>"


def _crawler(pages):
    c = SourceCrawler(dict(SOURCE))
    c.fetcher = FakeFetcher(pages)
    return c


def test_overlap_prefix_is_trimmed_and_new_content_kept():
    """第 2 页开头重复了第 1 页的末段 → 去重后仍要保留第 2 页的新内容。"""
    base = "https://page.test/1/100.html"
    pages = {base: _page(P1, "https://page.test/1/100_2.html"),
             "https://page.test/1/100_2.html": _page([P1[-1]] + P2, None)}
    text = _crawler(pages).get_content(base)
    assert P1[0] in text and P1[1] in text
    assert P2[0] in text, "第 2 页的新内容被吃掉了"
    assert text.count(P1[-1]) == 1, f"重叠段没去干净：{text.count(P1[-1])} 次"


def test_short_single_paragraph_repeat_is_kept():
    """**边界**：只有 1 段重叠、且该段很短时**不删**。

    "“……”"这种短句在相邻两页都出现完全可能是巧合，删掉就等于删正文；
    宁可留下一次重复，也不误删。30 字以上完全相同才算"回读上一页"。
    """
    base = "https://page.test/5/500.html"
    short = "“……”"
    pages = {base: _page([P1[0], short], "https://page.test/5/500_2.html"),
             "https://page.test/5/500_2.html": _page([short] + P2, None)}
    text = _crawler(pages).get_content(base)
    assert text.count(short) == 2, "短句巧合重复被误删了"
    assert P2[0] in text


def test_duplicate_page_is_not_appended_and_stops_paging():
    """第 3 页与第 2 页完全相同 → 不追加、停止翻页（内容各出现一次）。"""
    base = "https://page.test/2/200.html"
    pages = {base: _page(P1, "https://page.test/2/200_2.html"),
             "https://page.test/2/200_2.html": _page(P2, "https://page.test/2/200_3.html"),
             "https://page.test/2/200_3.html": _page(P2, None)}   # 重复整页
    text = _crawler(pages).get_content(base)
    assert text.count(P2[0]) == 1, f"重复页被追加了：{text.count(P2[0])} 次"
    assert P1[0] in text


def test_next_link_back_to_seen_page_stops():
    """第 2 页的"下一页"指回第 1 页 → 停止，不重复取。"""
    base = "https://page.test/3/300.html"
    pages = {base: _page(P1, "https://page.test/3/300_2.html"),
             "https://page.test/3/300_2.html": _page(P2, base)}
    c = _crawler(pages)
    text = c.get_content(base)
    assert text.count(P1[0]) == 1 and text.count(P2[0]) == 1
    assert len(c.fetcher.got) == 2, f"同一页被取了多次：{c.fetcher.got}"


def test_normal_multi_page_chapter_is_untouched():
    """正常三页：内容全在，且不误伤。"""
    base = "https://page.test/4/400.html"
    pages = {base: _page(P1, "https://page.test/4/400_2.html"),
             "https://page.test/4/400_2.html": _page(P2, "https://page.test/4/400_3.html"),
             "https://page.test/4/400_3.html": _page(P3, None)}
    text = _crawler(pages).get_content(base)
    for p in P1 + P2 + P3:
        assert p in text, f"漏了内容：{p[:16]!r}"
