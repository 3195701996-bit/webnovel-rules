# -*- coding: utf-8 -*-
"""R79 回归：同一章内**整块重复**要去掉后面那一份（实测缺陷，2026-09-18）。

真实现象（用户真实书库取证）：
  · yetianlian 第1384章 79 段里，**第 29~52 段与第 53~76 段逐行完全相同**（重复 24 段）；
  · quanben8 第1113章重复 15 段 —— 正好都是一页正文的量（分页把同一页追加了两遍）。
  全库 11506 章里 6 章如此，最多的一章有 13% 是重复内容。

判据（保守且可复核）：**连续 ≥3 行**、每行 **≥10 字**、整块 **≥200 字**、逐字节相同。
本文件锁死四条：会删、只删后面那份、够不上判据的不动、正文（含重复的口头禅）不受影响。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.cleaner import clean_text, _L8_dedup_blocks  # noqa: E402

BLOCK = [
    "他站在山巅之上，望着远处翻涌的云海，心里忽然生出一种说不清道不明的滋味来，像是释然，又像是怅然。",
    "风从谷底吹上来，带着潮湿的草木气息，吹得他衣袖猎猎作响，也吹散了几分连日奔波积下的燥意。",
    "多年前他第一次来到这里时，还是个什么都不懂的少年，如今却已经能独当一面，替旁人遮风挡雨了。",
    "他抬手拂去肩上的落叶，转身朝山下走去，脚步不快，却一步比一步稳当，像是踩在自己选定的路上。",
    "山路尽头有人等着他，那是一道许久未见的身影，让他不由得加快了脚步，连呼吸都轻快了几分。",
    "走近了才看清，那人的鬓角竟已添了霜色，笑起来却还是记忆里的模样，仿佛这些年从没走远过。",
]
OTHER = [
    "第二天清晨，他早早醒来，推开窗子，外面正下着绵绵细雨，空气里满是泥土与青草混在一起的味道。",
    "他撑伞出门，沿着青石板路一直往前走，直到看见街角那家开了许多年的茶铺，才终于停下脚步。",
]


def test_duplicated_block_is_removed_once():
    """整块重复：后面那一份被删掉，前面那份保留。"""
    text = "\n".join(BLOCK + OTHER + BLOCK)
    out = clean_text(text)
    for line in BLOCK + OTHER:
        assert out.count(line) == 1, f"重复没去干净：{line[:16]!r} 出现 {out.count(line)} 次"


def test_short_repetition_is_not_touched():
    """够不上判据（整块不足 200 字）→ 一个字都不许动。

    正文里本来就会有重复的口头禅/排比；判据要求"连续 ≥3 行、整块 ≥200 字"，
    短重复一律不碰。
    """
    text = "\n".join(["好。", "好。", "好。"] * 4)
    assert clean_text(text) == text


def test_repeated_short_sentences_are_not_touched():
    """对白密集的短句重复（"“……”"）不是分页重复，不许删。"""
    text = "\n".join(["他愣了一下。", "“……”", "“……”", "“……”", "“……”",
                      "“……”", "“……”"] * 3)
    assert clean_text(text) == text


def test_dedup_is_idempotent():
    text = "\n".join(BLOCK + OTHER + BLOCK)
    once = clean_text(text)
    assert clean_text(once) == once


def test_block_dedup_keeps_all_unique_content():
    """去重后**其余内容不得减少**（只删重复的那一份）。"""
    text = "\n".join(BLOCK + OTHER + BLOCK)
    out = _L8_dedup_blocks(text)
    for line in BLOCK + OTHER:
        assert line in out
    assert len(out) < len(text) or text == out
