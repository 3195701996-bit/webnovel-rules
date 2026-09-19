# -*- coding: utf-8 -*-
"""R78 回归：ddsk(大帝书阁) 分页必须按站点自报页数取满，绝不给重复页或半章。

背景（2026-09-18 实测取证）：
  · 站点正文自带页标 `(第N/M页)`——普通章 `第1/3页`，短章 `第1/1页`；
  · 对**不存在的** `_N` 页，站点返回 **HTTP 200 + 正文容器**，内容就是第 1 页
    （软 404）。旧实现盲探 `_2.._6` 且只在取不到时 break，于是把同一页拼了
    3 遍：实测《剑来》新书感言 576 字被存成 2068 字（指纹落在第 17/570/1123 字）；
  · 分页中途抓取失败（超时/封锁）旧实现直接返回**半章**且不报错——读者以为
    读完了，任务还记成"已下载"。这比重复更危险。

判据（任一破坏都必须让本文件失败）：
  1. `第1/1页` + `_2`/`_3` 返回第 1 页副本 → 只保留 1 页（不得重复）；
  2. `第1/3页` → 三页按序拼接，且每页只在结果里出现一次；
  3. 中途页抓取失败 → **抛错**（宁可失败可重试，也不静默给半章）；
  4. 后续页自报页码与首屏不一致（跳章/换书）→ 停止，绝不跨章拼接；
  5. 无 `{a}_{b}.html` 形式的 URL → 单页取，不做任何分页猜测。
"""
import re
import pytest

from engine.adapters.ddsk import Adapter


SRC = {"uid": "大帝书阁wap_ddsk2__wap.dingdiansk.com",
       "bookSourceName": "大帝书阁wap", "bookSourceUrl": "http://wap.dingdiansk.com"}


def _page(n, total, body, next_chapter=False):
    """造一页 ddsk 正文 HTML：页标 (第n/total页) + 正文 + 下一页链接"""
    href = ("/wapbook/16022_999.html" if next_chapter
            else f"/wapbook/16022_49318899_{n + 1}.html")
    return (f'<html><div id="chaptercontent">'
            f'天才一秒记住本站地址:(大帝书阁)wap.bbqsk.info 最快更新!无广告!'
            f'<br>(第{n}/{total}页){body}<br>'
            f'<a href="{href}">下一{"章" if next_chapter else "页"}</a>'
            f'</div></html>')


class _FakeFetch(Adapter):
    """只替换网络层：pages 映射 URL→HTML 或 Exception"""
    def __init__(self, pages):
        super().__init__(SRC, fetcher=None)
        self.pages = pages
        self.calls = []

    def _get(self, url, **kw):
        self.calls.append(url)
        v = self.pages.get(url, KeyError("404"))
        if isinstance(v, Exception):
            raise v
        return v


BASE = "http://wap.dingdiansk.com/wapbook/16022_49318899.html"
P2 = "http://wap.dingdiansk.com/wapbook/16022_49318899_2.html"
P3 = "http://wap.dingdiansk.com/wapbook/16022_49318899_3.html"


def test_single_page_chapter_not_duplicated():
    """1 页章：站点对缺页返回第 1 页副本 → 必须只保留 1 遍（实测 576 字被存成 2068 字的回归）"""
    body = "短章的正文内容。" * 60
    dup = _page(1, 1, body)
    ad = _FakeFetch({BASE: dup, P2: dup, P3: dup})
    out = ad.get_content(BASE)
    assert out.count("短章的正文内容") == 60, "同一页被重复拼接了"
    assert "第1/1页" not in out and "1/1" not in out, "页标应被清掉"
    # 页标在原文里带括号，只删中间会留下空括号（实测每章标题后都挂着 "()"）
    assert "()" not in out and "（）" not in out, f"页标括号残留: {out[:80]!r}"
    # 自报 1 页 → 不该再去探 _2（省一次白请求）
    assert P2 not in ad.calls, f"自报 1 页仍去探分页: {ad.calls}"


def test_three_page_chapter_joined_in_order():
    b1, b2, b3 = "第一页开头。" * 40, "第二页中间。" * 40, "第三页结尾。" * 40
    ad = _FakeFetch({BASE: _page(1, 3, b1), P2: _page(2, 3, b2),
                     P3: _page(3, 3, b3, next_chapter=True)})
    out = ad.get_content(BASE)
    i1, i2, i3 = out.find("第一页开头"), out.find("第二页中间"), out.find("第三页结尾")
    assert -1 not in (i1, i2, i3), "三页没取全"
    assert i1 < i2 < i3, "分页顺序错乱"
    assert out.count("第一页开头") == 40 and out.count("第二页中间") == 40
    assert "16022_999" not in out, "把下一章内容拼进了本章"


def test_mid_page_failure_raises_instead_of_truncating():
    """中途页失败必须报错——静默返回半章是本轮实测纠正的缺陷"""
    b1 = "第一页正文。" * 40
    ad = _FakeFetch({BASE: _page(1, 3, b1),
                     P2: TimeoutError("timed out")})
    with pytest.raises(RuntimeError) as ei:
        ad.get_content(BASE)
    msg = str(ei.value)
    assert "分页" in msg and "半章" in msg, f"错误信息要能解释清楚: {msg}"


def test_first_page_failure_raises_original():
    ad = _FakeFetch({BASE: TimeoutError("connect timeout")})
    with pytest.raises(Exception):
        ad.get_content(BASE)


def test_page_marker_mismatch_stops_without_cross_chapter():
    """后续页自报页码与首屏口径不一致（跳章）→ 停在该页之前"""
    b1 = "本章第一页。" * 40
    ad = _FakeFetch({BASE: _page(1, 2, b1),
                     P2: _page(1, 3, "别的章内容。" * 40)})
    out = ad.get_content(BASE)
    assert "别的章内容" not in out, "跨章内容混入"
    assert out.count("本章第一页") == 40


def test_non_standard_url_single_page_no_probing():
    u = "http://wap.dingdiansk.com/wapbook/special.html"
    ad = _FakeFetch({u: _page(1, 1, "特殊页正文。" * 30)})
    out = ad.get_content(u)
    assert "特殊页正文" in out and len(ad.calls) == 1


def test_page_marker_removed_from_output():
    ad = _FakeFetch({BASE: _page(1, 1, "正文。" * 50)})
    out = ad.get_content(BASE)
    assert not re.search(r"第\s*\d+\s*/\s*\d+\s*页", out), "页标残留"
    assert "无广告" not in out, "广告行残留"
