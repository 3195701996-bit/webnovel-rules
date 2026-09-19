# -*- coding: utf-8 -*-
"""规则写法归一与移动站 UA 判定（本轮由精华书阁榜单取数为 0 实测暴露）。"""
from engine.fetcher import Fetcher
from engine.rules import normalize_selector_rule


def test_normalize_tag_class_filter():
    """legado 常见写法 ul@class.xbk：本引擎原把它当"取 class 属性"，返回 0 个元素。"""
    assert normalize_selector_rule("ul@class.xbk") == "ul.xbk"
    assert normalize_selector_rule("div@id.main") == "div#main"


def test_normalize_leaves_other_rules_untouched():
    for r in (".xsm.0@a@text", "li.0@a@href", "img@src", "ul.xbk",
              "/search?q={{key}}", ""):
        assert normalize_selector_rule(r) == r


def test_mobile_host_gets_mobile_ua():
    """https://m.host 原正则匹配不到（m 前是 / 而不是 . 或串首）→ 用桌面 UA →
    实测该站只回 1.2KB 壳页（无书单）；改为按主机名判断后回完整页。"""
    from engine.fetcher import _UA_MOBILE
    for url in ("https://m.jhsssd.com/", "https://wap.example.com/",
                "https://mobile.example.com/", "https://example.com/m/list"):
        ua = Fetcher._pick_ua({"bookSourceUrl": url})
        assert ua in _UA_MOBILE, f"{url} 应为移动 UA，实际 {ua[:40]}"


def test_parse_header_reads_declared_json():
    """书源 header 字段（JSON 字符串或 dict）应被解析成请求头。"""
    f = Fetcher()
    assert f.parse_header({"header": '{"User-Agent": "MyCustomUA/1.0"}'}) == \
        {"User-Agent": "MyCustomUA/1.0"}
    assert f.parse_header({"header": {"X-A": "1"}}) == {"X-A": "1"}
    assert f.parse_header({}) == {}
    assert f.parse_header({"header": "不是 JSON"}) == {}


def test_source_declared_ua_wins():
    """书源声明的 UA 优先于按站点判断的默认 UA。

    为什么在这里 reload：本用例单跑通过、在全量套件里失败（拿到的是随机移动 UA），
    原因是同套件其它模块会动模块级状态/重新导入。为了不被别人的替换影响，
    本用例显式取一份干净的模块对象再断言。
    """
    import importlib
    import engine.fetcher as F
    F = importlib.reload(F)
    ua = F.Fetcher._pick_ua({"bookSourceUrl": "https://m.example.com/",
                             "header": '{"User-Agent": "MyCustomUA/1.0"}'})
    assert ua == "MyCustomUA/1.0", ua
