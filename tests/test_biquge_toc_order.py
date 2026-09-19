# -*- coding: utf-8 -*-
"""R78 回归：笔趣阁模板目录顺序（"最新章节倒序块 + 完整目录"两种形态）。

实测缺陷（2026-09-18, www.yuzhaiwuh.xyz《剑来》）：
  站点目录页首是"最新章节"**倒序块**（第2666节→第2655节 共 12 条），其后才是
  完整目录（第1节→第2654节）。旧逻辑对它是**双双失效**：
    · 切片分支要求章名里有"第1章"字面量，该站是"第N节" → 走不到；
    · 倒序分支用共享的 chapter_name_num（只认"阿拉伯数字+章"）→ 全为 0，
      `0 > 0` 为假 → 不反转。
  结果站点页序原样透传：目录以"第2666节"开头、12 条最新章倒序挂最前，
  阅读顺序与下载顺序全乱（实测 2666 条全部唯一，不是重复条目）。

判据：
  1. 上述形态 → 按序号升序（1..2666）；
  2. 本来就正序 1..N → 恒等（不得改动顺序）；
  3. 序号不完整（缺号/重号）→ **不重排**（判据不成立就不猜）；
  4. 含"序章/番外"等无序号条目 → 不重排（同上保守）；
  5. A 型（biquge.company：前缀与尾部重叠、有"第1章"）→ 既有切片+补尾行为不变。
"""
from engine.adapters.biquge_common import Adapter


SRC = {"uid": "www.yuzhaiwuh.xyz", "bookSourceName": "御宅屋",
       "bookSourceUrl": "https://www.yuzhaiwuh.xyz"}


class _Book:
    book_url = "https://www.yuzhaiwuh.xyz/book/99293.html"
    toc_url = ""


class _Fake(Adapter):
    def __init__(self, html):
        super().__init__(SRC, fetcher=None)
        self.html = html

    def _get(self, url, **kw):
        return self.html


def _dd(names, base="/read/99293/"):
    return "".join(f'<dd><a href="{base}{i}.html">{n}</a></dd>'
                   for i, n in enumerate(names))


def test_newest_block_then_body_is_sorted_ascending():
    """yuzhaiwuh 实测形态：前缀是**更新的**倒序章（20..9，不在正文目录里），
    其后是完整目录 1..8。两者不重叠（实测 2666 条全唯一）→ 必须升序。"""
    newest = [f"剑来 第{n}节" for n in range(20, 8, -1)]      # 20..9 倒序（12 条）
    body = [f"剑来 第{n}节" for n in range(1, 9)]             # 1..8 正序
    ad = _Fake(_dd(newest + body))
    out = ad.get_toc(_Book())
    assert [c["name"] for c in out] == [f"剑来 第{n}节" for n in range(1, 21)], \
        "最新章节倒序块没有被归位"


def test_already_ascending_is_identity():
    names = [f"剑来 第{n}节" for n in range(1, 51)]
    ad = _Fake(_dd(names))
    out = ad.get_toc(_Book())
    assert [c["name"] for c in out] == names, "正序目录被改动了"


def test_incomplete_numbering_is_not_reordered():
    """缺号：1,2,4(缺3) → 判据不成立，保持页序（不猜阅读顺序）"""
    names = ["书 第1节", "书 第2节", "书 第4节", "书 第5节"]
    ad = _Fake(_dd(names))
    out = ad.get_toc(_Book())
    assert [c["name"] for c in out] == names


def test_duplicate_numbers_is_not_reordered():
    names = ["书 第1节", "书 第2节", "书 第2节", "书 第3节"]
    ad = _Fake(_dd(names))
    out = ad.get_toc(_Book())
    assert [c["name"] for c in out] == names


def test_unnumbered_entries_block_reordering():
    """含"序章/番外"（无序号）→ 一律不重排"""
    names = ["序章", "书 第1节", "书 第2节", "番外 一"]
    ad = _Fake(_dd(names))
    out = ad.get_toc(_Book())
    assert [c["name"] for c in out] == names


def test_chinese_numeral_chapters_sorted():
    """中文数字也参与判据：第一章..第三章 全序 → 升序（页序被打乱时能纠正）"""
    names = ["书 第三章", "书 第一章", "书 第二章"]
    ad = _Fake(_dd(names))
    out = ad.get_toc(_Book())
    assert [c["name"] for c in out] == ["书 第一章", "书 第二章", "书 第三章"]


def test_type_a_prefix_overlap_is_repaired_to_ascending():
    """A 型（biquge.company）：前缀与完整目录**尾部重叠**（同 URL）。
    解析时按 URL 去重 → 列表成了 [5,4,3(前缀),1,2]，旧逻辑切片后补尾得到
    [1,2,5,4,3]（顺序仍错）。本轮的"完整 1..N ⇒ 升序"把它修正为 1..5。"""
    # 前缀 3 条与其后正文的 3/4/5 用**同一批 URL**（真实的重叠形态）
    html = ('<dd><a href="/read/99293/5.html">书 第5章</a></dd>'
            '<dd><a href="/read/99293/4.html">书 第4章</a></dd>'
            '<dd><a href="/read/99293/3.html">书 第3章</a></dd>'
            + "".join(f'<dd><a href="/read/99293/{i}.html">书 第{i}章</a></dd>'
                      for i in range(1, 6)))
    ad = _Fake(html)
    out = ad.get_toc(_Book())
    names = [c["name"] for c in out]
    assert names == [f"书 第{i}章" for i in range(1, 6)], names


def test_type_b_latest_chapters_land_in_reading_order():
    """B 型（R42：前缀是最新 N 章，比主体新）→ 一个都不能丢，且按阅读顺序。
    旧逻辑切片后把最新章**倒序**补到末尾（…,6,8,7）→ 阅读顺序仍错；
    本轮升序判据把它修正为 1..8（含最新章，无缺失）。"""
    prefix = ["书 第8章", "书 第7章", "书 第6章"]
    body = ["书 第1章", "书 第2章", "书 第3章", "书 第4章", "书 第5章"]
    ad = _Fake(_dd(prefix + body))
    out = ad.get_toc(_Book())
    names = [c["name"] for c in out]
    assert names == [f"书 第{i}章" for i in range(1, 9)], names
    assert len(out) == 8, "最新章被丢了"


def test_site_typo_in_chapter_numbers_must_not_reorder():
    """**跨源实测证据（2026-09-18）**：章名序号是站点/原著自己的文本，含错号与重号，
    而且多个彼此独立的源完全一致——

      · idx≈212: '第两百二十三章 憧憬'(223) 插在 212 与 214 之间；
      · idx≈452: '第四五百五十二章 单骑南下'（原文错字，实为第452章）排在 453 之前；
      · 全书还有 89~106 个"重号"（如'第一章 惊蛰'与后文'第1章 少年游'）。

    而各源的 **URL 页序始终连续正确**（p213→p214→p215…）。所以"按章名排序"会
    毁掉正确顺序——本文件的"完整 1..N 才排序"判据正是为此设的闸门：序号不唯一
    或不连续时**一律不动**。此处用真实形态锁死这条边界。
    """
    names = ["书 第两百一十四章 风雨夜行", "书 第两百二十三章 憧憬",
             "书 第两百一十五章 画眉", "书 第四五百五十二章 单骑南下",
             "书 第四百五十三章 吾心安处打个盹儿"]
    ad = _Fake(_dd(names))
    out = ad.get_toc(_Book())
    assert [c["name"] for c in out] == names, \
        "章名序号不可靠时不得重排（否则会按错号排序，毁掉站点正确页序）"


def test_duplicate_numbering_across_parts_must_not_reorder():
    """重号形态：'第一章 惊蛰'与后文'第1章 少年游'（编号从头再来）→ 不重排"""
    names = ["书 第一章 惊蛰", "书 第二章 稗草",
             "书 第1章 少年游", "书 第2章 山水郎"]
    ad = _Fake(_dd(names))
    out = ad.get_toc(_Book())
    assert [c["name"] for c in out] == names
