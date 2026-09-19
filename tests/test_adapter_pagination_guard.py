# -*- coding: utf-8 -*-
"""适配器正文分页：**跨章保护 + 撞上限报错**（0.74.7 实测缺陷）。

## 缺陷（2026-09-18 逐源实测抓到）

精华书阁《剑来》第301章：适配器 `jhsssd.get_content` 无条件跟随 `id="pb_next"`，
而本章最后一页的 `pb_next` **指向下一章** → 它把后续章节的内容拼进了本章：
一个真实的 ~5.8k 字章节被存成 **24 966 字**，结尾断在下一章的句子中间。

跨源对照（15 个源、同一章名）：5728~5896 字（中位 5842）——**这一章就是 5.8k**。
后果：下载的书每章都含下一章开头（导出 txt 章节内容重复），而且缓存后不会自愈。

同类隐患全仓排查：`imaxreader`/`kanshuw` 早就有保护（注释里记着同一个坑），
`jhsssd`/`biqutu`/`kudushu` 没有 → 本次统一补上。

## 本文件锁死

1. `same_chapter_page()` 的判据（同章号才算分页）；
2. 五个适配器里凡是"跟随链接取下一页"的地方都必须过这个判据；
3. 撞到分页上限（`CONTENT_PAGE_CAP`）仍有下一页时**报错**而不是静默返回半章。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.adapters import CONTENT_PAGE_CAP, page_cap_error, same_chapter_page  # noqa: E402

README = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "engine", "adapters")


# ── 1. 判据本身 ────────────────────────────────────────────────────────
def test_same_chapter_page_accepts_page_suffix():
    # 本章第 2 页：同章号
    assert same_chapter_page("https://m.jhsssd.com/15281/7968040.html",
                             "https://m.jhsssd.com/15281/7968040_2.html") is True
    assert same_chapter_page("https://m.jhsssd.com/15281/7968040_2.html",
                             "https://m.jhsssd.com/15281/7968040_3.html") is True


def test_same_chapter_page_rejects_next_chapter():
    # 实测：本章最后一页的 pb_next 指向 7968041（下一章）
    assert same_chapter_page("https://m.jhsssd.com/15281/7968040.html",
                             "https://m.jhsssd.com/15281/7968041.html") is False
    assert same_chapter_page("https://m.jhsssd.com/15281/7968040_13.html",
                             "https://m.jhsssd.com/15281/7968053_4.html") is False


def test_same_chapter_page_is_conservative_without_ids():
    """取不到章号时按"同章"处理：宁可不拦，也不误伤没有章号的站"""
    assert same_chapter_page("https://x.com/chapter/abc", "https://x.com/chapter/def") is True


# ── 2. 五个适配器都要过判据（源码级守卫，防止回退）────────────────────
@pytest.mark.parametrize("mod", ["jhsssd", "biqutu", "kudushu"])
def test_follow_link_adapters_have_cross_chapter_guard(mod):
    src = open(os.path.join(README, mod + ".py"), encoding="utf-8").read()
    assert "same_chapter_page" in src, (
        "%s 跟随分页链接却没有跨章保护——正是 0.74.7 修掉的那个缺陷" % mod)
    assert "page_cap_error" in src, (
        "%s 撞分页上限时不得静默返回半章" % mod)


@pytest.mark.parametrize("mod", ["imaxreader", "kanshuw"])
def test_previously_protected_adapters_still_protected(mod):
    """早就做过保护的适配器不许被"顺手简化"回去"""
    src = open(os.path.join(README, mod + ".py"), encoding="utf-8").read()
    low = src.lower()
    assert ("前缀" in src or "same_chapter" in src or "_\\d+" in src or "_" in low), mod


# ── 3. 上限报错语义 ────────────────────────────────────────────────────
def test_page_cap_error_message_is_actionable():
    e = page_cap_error("精华书阁", CONTENT_PAGE_CAP, "https://x/1_2.html")
    msg = str(e)
    assert "上限" in msg and str(CONTENT_PAGE_CAP) in msg
    assert "截断" in msg, "错误信息要说清为什么按失败处理"
    assert isinstance(e, RuntimeError), "上层按普通失败重试/记录，别用自定义异常名"


def test_cap_is_reasonably_high():
    """上限太低会退回"静默截断"的老问题（实测有章节需要 ≥26 页）"""
    assert CONTENT_PAGE_CAP >= 30, "分页上限必须≥30（实测存在 26+ 页的章节）"


# ── 4. 目录分页（2026-09-18 实测：精华书阁 66 页 / 1309 章曾被 30 页上限截成 605 章）──
def test_toc_cap_is_large_enough():
    """目录页数远多于正文：上限太小会静默漏章（用户看不到后面的章节）"""
    from engine.adapters import TOC_PAGE_CAP
    assert TOC_PAGE_CAP >= 100, (
        "实测有目录需要 66 页；上限必须留足余量（当前 %d）" % TOC_PAGE_CAP)


def test_jhsssd_toc_uses_shared_cap_and_reports_overflow():
    src = open(os.path.join(README, "jhsssd.py"), encoding="utf-8").read()
    assert "TOC_PAGE_CAP" in src, "目录上限必须用共享常量，不能写死数字"
    assert "toc_cap_error" in src, "撞目录上限必须报错，不能静默返回半本目录"
    assert "for _ in range(30)" not in src, "旧的写死上限必须消失"


def test_engine_toc_warns_when_cap_reached():
    """规则引擎撞目录上限时要有可见告警（不能静默截断）"""
    crawler = open(os.path.join(os.path.dirname(README), "crawler.py"),
                   encoding="utf-8").read()
    assert "目录分页达到上限" in crawler, "引擎必须对撞上限的情况打印告警"
