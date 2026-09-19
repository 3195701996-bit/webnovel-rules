# -*- coding: utf-8 -*-
"""探索/榜单（engine/explore.py）单元测试：只做有数据支撑的探索。"""
import json

import pytest

from engine import explore as ex


def _src(**kw):
    d = {"uid": "s1", "bookSourceName": "测试源", "bookSourceUrl": "https://e.com",
         "enabled": True}
    d.update(kw)
    return d


def test_parse_json_array_with_group_header():
    raw = json.dumps([
        {"title": "排行榜", "url": ""},
        {"title": "总点击", "url": "/rank/{{page}}.html"},
        {"title": "完本", "url": "/wanben_{{page}}.html"},
    ])
    cats = ex.parse_explore(_src(exploreUrl=raw))
    assert [c["title"] for c in cats] == ["总点击", "完本"]
    assert all(c["group"] == "排行榜" for c in cats)      # 组标题继承给后续条目
    assert cats[0]["url"] == "/rank/{{page}}.html"


def test_parse_js_style_json_with_trailing_comma():
    """内置书源实测就是这种写法（末条带尾随逗号），严格 JSON 会直接解析失败。"""
    raw = '[\n{"title":"A","url":"/a_{{page}}.html"},\n{"title":"B","url":"/b_{{page}}.html"},\n]'
    cats = ex.parse_explore(_src(exploreUrl=raw))
    assert [c["title"] for c in cats] == ["A", "B"]


def test_parse_line_format():
    cats = ex.parse_explore(_src(exploreUrl="分类一::/c1_{{page}}.html\n/c2_{{page}}.html"))
    assert [c["title"] for c in cats] == ["分类一", "/c2_{{page}}.html"]
    assert cats[0]["url"] == "/c1_{{page}}.html"


def test_parse_no_explore_is_empty_not_fake():
    assert ex.parse_explore(_src()) == []
    assert ex.parse_explore(_src(exploreUrl="   ")) == []


def test_parse_unparsable_raises():
    with pytest.raises(ex.ExploreError):
        ex.parse_explore(_src(exploreUrl="[这不是 JSON 也不是规则"))


def test_sources_with_explore_only_lists_real_ones():
    srcs = [_src(uid="a", bookSourceName="有榜单",
                 exploreUrl=json.dumps([{"title": "榜单", "url": "/r_{{page}}.html"}])),
            _src(uid="b", bookSourceName="没榜单")]
    items = ex.sources_with_explore(srcs)
    assert [i["uid"] for i in items] == ["a"]
    assert items[0]["categories"][0]["title"] == "榜单"


class _FakeEngine:
    def get_elements(self, rule, html):
        return ["el1", "el2"]

    def get_string(self, rule, el, base_url=""):
        if rule == "name": return "书名"
        if rule == "bookUrl": return "/book/1.html"
        if rule == "author": return "作者"
        return ""


def test_explore_builds_books_and_replaces_page(monkeypatch):
    seen = {}

    def _fake_search_one(fetcher, engine, src, url, cfg, kw, timeout):
        seen["url"] = url
        seen["kw"] = kw
        return "<html></html>", ""

    monkeypatch.setattr("engine.source_mgr._search_one", _fake_search_one)
    monkeypatch.setattr(ex, "RuleEngine", lambda source=None: _FakeEngine())
    books = ex.explore(_src(ruleExplore={"bookList": "ul@li", "name": "name",
                                         "bookUrl": "bookUrl", "author": "author"}),
                       "/rank/{{page}}.html", page=3)
    assert seen["url"] == "https://e.com/rank/3.html"      # {{page}} 已替换、相对路径已拼 base
    assert seen["kw"] == ""
    assert len(books) == 2
    assert books[0]["name"] == "书名" and books[0]["book_url"].endswith("/book/1.html")


def test_explore_page_is_bounded(monkeypatch):
    seen = {}

    def _fake_search_one(fetcher, engine, src, url, cfg, kw, timeout):
        seen["url"] = url
        return "<html/>", ""

    monkeypatch.setattr("engine.source_mgr._search_one", _fake_search_one)
    monkeypatch.setattr(ex, "RuleEngine", lambda source=None: _FakeEngine())
    ex.explore(_src(ruleExplore={"bookList": "x"}), "/r_{{page}}.html", page=999)
    assert "/r_50.html" in seen["url"]                     # MAX_PAGE 上限生效


def test_explore_without_rule_explore_raises(monkeypatch):
    monkeypatch.setattr("engine.source_mgr._search_one", lambda *a, **kw: ("<html/>", ""))
    monkeypatch.setattr(ex, "RuleEngine", lambda source=None: _FakeEngine())
    with pytest.raises(ex.ExploreError):
        ex.explore(_src(), "/r_{{page}}.html")


def test_explore_fetch_failure_is_explicit(monkeypatch):
    monkeypatch.setattr("engine.source_mgr._search_one",
                        lambda *a, **kw: (None, "目标地址被拒绝（仅允许公网 http/https 地址）"))
    monkeypatch.setattr(ex, "RuleEngine", lambda source=None: _FakeEngine())
    with pytest.raises(ex.ExploreError) as ei:
        ex.explore(_src(ruleExplore={"bookList": "x"}), "http://127.0.0.1/x")
    assert "被拒绝" in str(ei.value)


# ── @ 链式步骤与 .class.N 索引（本轮由榜单书单取不到实测暴露）──────────

def test_select_css_supports_class_index_and_negative():
    from engine.rules import RuleEngine, parse_html
    eng = RuleEngine(source={})
    html = ('<ul class="xbk"><li class="a">1</li><li class="a">2</li>'
            '<li class="a">3</li></ul>')
    assert [e.text for e in eng.get_elements(".a.0", html)] == ["1"]
    assert [e.text for e in eng.get_elements(".a.2", html)] == ["3"]
    assert [e.text for e in eng.get_elements(".a.-1", html)] == ["3"]   # 负数取倒数
    assert eng.get_elements(".a.9", html) == []                        # 越界返回空


def test_at_chain_steps_are_executed_sequentially():
    """`li.0@a@href` 这类链式步骤原先把 "li.0@a" 整串当 CSS 选择器 → 恒空。"""
    from engine.rules import RuleEngine
    eng = RuleEngine(source={})
    html = ('<ul class="xbk"><li><a href="/97310/">玄鉴仙族</a></li>'
            '<li><a href="/15281/">剑来</a></li></ul>')
    assert eng.get_string("li.0@a@href", html) in ("/97310/",)
    assert eng.get_string("li.1@a@text", html) == "剑来"
    assert len(eng.get_elements("ul.xbk@li", html)) == 2   # 选择器链同样生效


def test_explore_book_url_is_absolute(monkeypatch):
    """榜单链接是相对路径时必须归一成绝对地址，否则建任务会被 SSRF 校验拒绝。"""
    monkeypatch.setattr("engine.source_mgr._search_one", lambda *a, **kw: (
        '<ul class="xbk"><li><a href="/97310/">书</a></li></ul>', ""))
    src = _src(ruleExplore={"bookList": "ul@class.xbk", "name": "li.0@a@text",
                            "bookUrl": "li.0@a@href"})
    books = ex.explore(src, "/rank/{{page}}.html", page=1)
    assert books and books[0]["book_url"] == "https://e.com/97310/"
