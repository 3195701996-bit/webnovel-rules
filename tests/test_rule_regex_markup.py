# -*- coding: utf-8 -*-
"""R79 回归：`@正则:` 规则必须能匹配**带标签**的写法（实测缺陷，2026-09-18）。

真实现象：6 个源（81zw / kcshu / qb5200 / quanben8 / quanbenw / quanbenxiaoshuo）
的 `nextContentUrl` 都是 `<a[^>]*href="([^"]+)"[^>]*>\\s*下一章` —— 而规则引擎
只把**元素的纯文本**（标签已被剥掉）喂给正则，于是这类规则**永远返回空**。

失效是**静默的**：分页当场停在第 1 页，多页章节被截断。实测 quanben8 第1113章
（站点自己标着 `第(1/3)页`）只取到 **1234 字**，而整章 3 页共 **4156 字**。
修好后同一章取到 3 页、结尾与旧缓存一致。

判据两条：
  1. 带标签的正则要能在**元素 HTML** 上命中；
  2. 纯文本能命中的规则**行为不变**（不能因为回退到 HTML 而取到别的东西）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.crawler import parse_html              # noqa: E402
from engine.rules import _select_regex             # noqa: E402

NEXT_RULE = r'<a[^>]*href="([^"]+)"[^>]*>\s*下一章'
# 实测 markup（quanben8 章节页，注意 href 与 data 属性、以及标签前的两个空格）
PAGE = ('<div class="col-3">'
        '<a id="next1" href="/book/0/102/126743_2.html"  '
        'data="/book/0/102/126743_2.html"  >下一章</a></div>')


def test_markup_regex_matches_element_html():
    doc = parse_html(PAGE)
    assert _select_regex(NEXT_RULE, doc) == ["/book/0/102/126743_2.html"]


def test_markup_regex_on_element_not_just_document():
    """取到的可能不是整篇文档而是某个元素（cssselect 结果），同样要能命中。"""
    doc = parse_html(f"<html><body>{PAGE}</body></html>")
    el = doc.cssselect("div.col-3")[0]
    assert _select_regex(NEXT_RULE, el) == ["/book/0/102/126743_2.html"]


def test_plain_text_regex_still_works():
    """纯文本规则行为不变。"""
    doc = parse_html("<html><body><p>正文第一段</p><p>正文第二段</p></body></html>")
    assert _select_regex("正文(第.)段", doc) == ["第一"]
    assert _select_regex("没有这段", doc) == []


def test_regex_on_plain_string_unchanged():
    assert _select_regex("第一章", "这是第一章的正文") == ["第一章"]


def test_no_match_returns_empty_not_error():
    doc = parse_html(PAGE)
    assert _select_regex(r'<a[^>]*href="([^"]+)"[^>]*>\s*上一章', doc) == []
    # 非法正则不能抛异常
    assert _select_regex("([", doc) == []
