# -*- coding: utf-8 -*-
"""R79 回归：源站丢了段落结构时（整章一整行）必须补回段落，且**一个字符都不许动**。

真实现象（2026-09-18 用用户真实书库取证，不是想象）：
  · 爱下书《雪中悍刀行》**186/977 章**的章节页里 `<br>` 数 = 0、换行数 = 0，
    整章 1.5 万字就是一整行（同一本书的正常章 `<br>`=160）——读者看到的是一堵墙；
  · 全库 11695 章的"最长行"分布：**11107 章 < 400 字**（正常），≥1500 字只有 **142 章**。

判据三条，缺一不可：
  1. 超长行（≥1500 字）必须被切开（修完全库超长行 142 → 0）；
  2. **不增删任何字符**——`"".join(切出的段)` 必须与原行逐字节相同（全库 142 章核对过：
     全部"只差换行"）；
  3. 正常章一行都不许碰（有换行、段落正常的章必须逐字节不变）。
"""
from engine.cleaner import clean_text, WALL_LINE_MAX


def _wall(n=40, per=120):
    """造一个"一整行"的章节体：n 句、每句 per 字（无换行）。"""
    return "".join(("正文内容" + "字" * (per - 4) + "。") for _ in range(n))


def test_wall_is_split_into_paragraphs():
    body = _wall()
    out = clean_text(body)
    lines = [l for l in out.split("\n") if l.strip()]
    assert len(lines) > 5, f"没切开：{len(lines)} 段"
    assert max(len(l) for l in lines) < WALL_LINE_MAX, \
        f"仍有过长段落：{max(len(l) for l in lines)}"


def test_split_adds_no_characters():
    """**最要紧的一条**：只插换行，一个字符都不增删。"""
    body = _wall()
    out = clean_text(body)
    assert out.replace("\n", "") == body, "补段落时改动了字符"


def test_closing_quotes_stay_with_their_sentence():
    """切点必须在收尾引号**之后**，否则段落会以 ” 开头。"""
    sentence = "他说：“这一趟走得值。”"
    body = sentence * 30
    out = clean_text(body)
    for line in out.split("\n"):
        assert not line.startswith(("”", "』", "」")), f"段落以收尾引号开头：{line[:20]!r}"
    assert out.replace("\n", "") == body


def test_split_keeps_boundary_whitespace_exactly():
    """切点处的**空白也必须原样保留**（`strip()` 一下就等于删字符）。

    这条直接对 L7 做**单元级**断言：走整条管线时，更早的层（L1/L4）本来就会
    规范化行尾空白，拿 `clean_text` 的输出去比对会把"上游正常行为"当成失败。
    """
    from engine.cleaner import _L7_reparagraph
    line = ("他说：“走吧。” " * 220).strip()     # 句末标点后带空格，长度过阈值
    assert len(line) >= WALL_LINE_MAX
    out = _L7_reparagraph(line)
    assert out != line, "超长行没被切开"
    assert out.replace("\n", "") == line, "切段时改动了字符或空白"


def test_normal_chapter_is_untouched():
    """有换行、段落正常的章：逐字节不变。

    注意样本要**各段不同**：第一版让 8 段文字完全相同，正好撞上 L8"整块重复去重"
    （连续 ≥3 行、整块 ≥200 字逐字节相同即判为分页重复）—— 那是**判据该做的事**，
    不是 bug。真实的正常章段段不同，这里按真实形态写。
    """
    body = "\n\n".join(
        f"这是第{i}段正常长度的段落，写得长一些以便接近真实章节的段落长度，"
        f"内容自然各不相同，不会有整块重复。" for i in range(1, 9))
    assert clean_text(body) == body.strip()


def test_mixed_chapter_keeps_existing_line_breaks():
    """同章存在墙体行时，普通行的单换行不能被重排成双换行。"""
    wall = _wall()
    before = "普通第一行。\n普通第二行。"
    after = "\n普通第三行。\n普通第四行。"
    out = clean_text(before + "\n" + wall + after)
    # 旧实现只要切出一条墙体行就以 \n\n 重拼整章，下面两条会失败。
    assert out.startswith(before + "\n")
    assert not out.startswith(before + "\n\n")
    assert out.endswith(after)
    assert out.replace("\n", "") == (before + "\n" + wall + after).replace("\n", "")


def test_line_below_threshold_is_untouched():
    """刚好低于阈值的超长行也不许动（阈值是实测分界：11107 章 < 400 字）。"""
    body = "字" * (WALL_LINE_MAX - 1)
    assert clean_text(body) == body


def test_wall_without_sentence_punctuation_is_left_alone():
    """没有任何句末标点就切不动——原样保留，不许乱切。"""
    body = "字" * (WALL_LINE_MAX + 500)
    assert clean_text(body) == body
