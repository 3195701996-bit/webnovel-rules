# -*- coding: utf-8 -*-
"""R78 回归：目录"没取全"必须能被上层读到，不能让检查更新谎称"已是最新"。

实测背景（2026-09-18）：
  · `_run_check_update` 拉新章用的是 `get_toc(book, max_pages=30)`，而规则引擎
    路径**按该上限截断**（只在日志里喊一声）。新章通常挂在目录**末页**——
    对分页目录 >30 页的源，新章检测会**永远发现不了**，用户界面却显示
    "已是最新（无新增/失败章节）"。
  · 同一个坑在"目录页"路径上已经吃过一次（精华书阁 66 页被 30 页上限截成 46%）。

本轮改法：
  1. `SourceCrawler.toc_truncated`：撞上限时置位（不再只打日志）；
  2. 检查更新的目录上限改用 `TOC_PAGE_CAP`（与目录页同一口径，不再写死 30）；
  3. 目录撞上限 / 源站不可达时，`message` 里**如实带出**，不再说"已是最新"。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.crawler import SourceCrawler      # noqa: E402


SRC = {
    "uid": "tocpage_test",
    "bookSourceName": "分页目录测试源",
    "bookSourceUrl": "https://toc.test",
    "ruleToc": {
        "chapterList": "ul#list li a",
        "chapterName": "text",
        "chapterUrl": "href",
        # 用"下一页"链接驱动分页（规则引擎的 nextTocUrl）
        "nextTocUrl": "a#next@href",
    },
}


def _toc_html(page, per_page=2, total_pages=5):
    items = "".join(
        f'<li><a href="/read/1/{page * 100 + i}.html">第{page * per_page + i}章</a></li>'
        for i in range(per_page))
    nxt = (f'<a id="next" href="/book/1/index_{page + 1}.html">下一页</a>'
           if page < total_pages else '')
    return f'<html><ul id="list">{items}</ul>{nxt}</html>'


class _Book:
    book_url = "https://toc.test/book/1/"
    toc_url = ""


def _crawler(pages_by_url):
    c = SourceCrawler(SRC)
    c.fetcher.get = lambda url, **kw: pages_by_url[url]     # 只替换网络层
    return c


def test_cap_hit_sets_flag_and_logs(capsys):
    pages = {f"https://toc.test/book/1/index_{p}.html": _toc_html(p)
             for p in range(1, 6)}
    pages["https://toc.test/book/1/"] = _toc_html(1)
    c = _crawler(pages)
    chs = c.get_toc(_Book(), max_pages=2)
    assert len(chs) == 4, f"应只取到 2 页 = 4 章，实际 {len(chs)}"
    assert c.toc_truncated is True, "撞上限必须置位（否则上层会谎称已是最新）"
    assert "达到上限" in capsys.readouterr().out, "日志也要保留"


def test_full_fetch_clears_flag():
    """取全时不得置位——否则"检查更新"会对每本书都提示"可能未取全" """
    pages = {f"https://toc.test/book/1/index_{p}.html": _toc_html(p)
             for p in range(1, 6)}
    pages["https://toc.test/book/1/"] = _toc_html(1)
    c = _crawler(pages)
    chs = c.get_toc(_Book(), max_pages=10)
    assert len(chs) == 10, f"应取到 5 页 = 10 章，实际 {len(chs)}"
    assert c.toc_truncated is False


def test_flag_resets_between_calls():
    """同一实例复用（任务/检查各调一次）时，上一次的截断不能污染下一次"""
    pages = {f"https://toc.test/book/1/index_{p}.html": _toc_html(p)
             for p in range(1, 6)}
    pages["https://toc.test/book/1/"] = _toc_html(1)
    c = _crawler(pages)
    c.get_toc(_Book(), max_pages=1)
    assert c.toc_truncated is True
    c.get_toc(_Book(), max_pages=10)
    assert c.toc_truncated is False, "第二次取全了，标记必须被重置"


def test_check_update_cap_is_toc_page_cap():
    """检查更新不得再写死 30 页；且必须真的读取 toc_truncated"""
    import io
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "server", "state.py")
    src = io.open(p, encoding="utf-8").read()
    i = src.index("def _run_check_update")
    body = src[i:i + 4000]
    assert "max_pages=TOC_PAGE_CAP" in body, "新章检测上限应改用 TOC_PAGE_CAP"
    assert "max_pages=30" not in body, "不得再写死 30 页"
    assert "toc_truncated" in body, "必须读取截断标记并如实告知用户"
    assert "可能仍有新章未检出" in body, "撞上限时不能只说'已是最新'"
