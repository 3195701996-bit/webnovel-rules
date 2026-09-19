# -*- coding: utf-8 -*-
"""R79 回归：章尾**站点残片**必须清掉，正常收尾一个字符都不许动。

实测背景（2026-09-18，全库 11695 章普查）：**935 章**章尾粘着站点残片，243 种形态；
Top 形态 `r1058` 84×、`rt` 50×、`v`/`u` 各 24×、`bk` 23×、`i` 17×、`本书来自` 36×、
`无弹窗小说网` 32×、`jpg` 8×。原文照抄：`…“是两指。”小说网`、`…算得了什么？.t`、
`…双手合十。h`。

判据（只动末尾、只动句末标点之后）：紧跟句末标点的 1~8 个 ASCII 字母数字点横线，
或在实测站名表里 → 剥掉；剥后仍须 ≥200 字。

全库隔离核对：**11213 章未动 / 482 章被改 / 越界改动 0 处**
（每一处都恰好是"末尾去掉 ≤12 字"）。
"""
from engine.cleaner import clean_text, _L9_strip_tail_residue

BODY = "正文内容" * 120          # 足够长（>200 字），避免触发长度保护


def _tail(t):
    return t.rstrip()[-14:]


def test_measured_residues_are_stripped():
    for frag in ("小说网", ".t", "h", "r1058", "rt", "bk", "jpg", "本书来自", "无弹窗小说网"):
        raw = BODY + "他转身离去。" + frag
        out = clean_text(raw)
        assert frag not in _tail(out) or frag in "他转身离去。", f"{frag!r} 没被清掉：{_tail(out)!r}"
        assert out.endswith("。") or out.endswith("！") or out.endswith("”"), _tail(out)


def test_dialogue_ending_with_residue():
    """对白收尾 + 残片（实测形态）：`…“是两指。”小说网`"""
    out = _L9_strip_tail_residue(BODY + '洪洗象嘀咕道：“是两指。”小说网')
    assert out.endswith('”'), _tail(out)
    assert "小说网" not in out


def test_normal_endings_are_untouched():
    """正常收尾一律不动（含英文、括号、引号、纯标点）。"""
    for tail in ("他说完了。", "他说：“好。”", "我等你回来 forever",
                 "全书完（完）", "他说不下去了，", "THE END"):
        raw = BODY + tail
        assert clean_text(raw) == raw, f"正常收尾被改：{tail!r}"


def test_punctuation_only_residue_is_kept():
    """只剩标点的残余**不是垃圾**（那是正文自己的结尾，多半是站点侧截断）→ 不删。"""
    for tail in ("他说不下去了，", "徐凤年望着远方，“"):
        raw = BODY + tail
        assert _L9_strip_tail_residue(raw) == raw


def test_short_text_is_never_touched():
    """太短的文本不处理（防止把整章当残片）。"""
    assert _L9_strip_tail_residue("很短。h") == "很短。h"
