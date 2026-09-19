# -*- coding: utf-8 -*-
"""漫画下载：**用户选的话不能被"整卷规则"吃掉**（0.74.12 实测缺陷）。

## 缺陷

worker 里有一条"纯整卷合集不下载"的规则（名字整名匹配"第N卷/Vol.N"），
它的前提是"APP 的 chapter2 接口对整卷返回 null"。实测《巨人》(jurenmeiman)
的 5 章**全都叫"第01卷…第05卷"**，于是：

- **整本下载**：5 章全被滤掉 → total=0 → 任务直接 `error`；
- **用户明确选了某一卷**：也被滤掉 → 同样 error，用户完全无从理解；
- 报的还是**错误的归因**："源可能正在风控或该漫画已下架"——
  其实是我们自己按规则跳过的（指南明令禁止这种误报）。

修法（两处）：
1. **用户明确选的话不做排除**（他点了就是要下）；
2. 滤完一章不剩时，**不再谎称风控/下架**，而是说清真因并给出下一步。

## 本文件锁死这两条（用假适配器 + 假图片下载器，全离线）
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.manga.base import Chapter, ComicDetails  # noqa: E402


class _VolAdapter(object):
    """章节名全为"第0N卷"的源（复刻 jurenmeiman 的真实形态）"""
    key = "demovol"
    name = "演示卷源"
    concurrent = 2

    def __init__(self, *a, **k):
        pass

    def _chs(self):
        return [Chapter(id="v1", name="第01卷", group="默認"),
                Chapter(id="v2", name="第02卷", group="默認")]

    def comic_info(self, cid):
        return ComicDetails(id=cid, title="卷型漫画", cover="", author="作者",
                            chapters=self._chs())

    def chapters(self, cid):
        return self._chs()

    def fetch_chapters(self, cid):
        return self._chs()

    def images(self, cid, chid):
        return ["https://img.example/%s/1.jpg" % chid]

    def image_headers(self, url):
        return {}

    def image_url(self, url):
        return url


class _FakeImageDownloader(object):
    """假图片下载器：写一个合法 JPEG 头的小文件，返回 (bytes, path)"""

    def __init__(self, adapter, cache_root, *a, **k):
        self.cache_root = cache_root

    def get(self, image_url, comic_id, chapter_id, idx, timeout=20):
        d = os.path.join(self.cache_root, comic_id, chapter_id)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "%04d.jpg" % idx)
        data = b"\xff\xd8\xff\xe0" + b"x" * 2000
        with open(p, "wb") as f:
            f.write(data)
        return data, p


# ── 决策函数（政策本身，纯函数、全离线）──────────────────────────────
def _ch(name, cid="x"):
    return {"id": cid, "name": name}


def test_user_selection_wins_over_volume_rule():
    """**用户明确选了话 → 不剔除**（旧行为：选了也被滤掉 → total=0 → 任务 error）"""
    from engine.manga.download_manager import filter_volume_only
    chs = [_ch("第01卷", "v1"), _ch("第02卷", "v2")]
    kept, note = filter_volume_only(chs, sel_chapters=["v1"])
    assert [c["id"] for c in kept] == ["v1", "v2"], kept
    assert "用户已明确选择" in note


def test_all_volumes_are_kept_not_emptied():
    """整本都是整卷条目 → **一章都不能少**（否则该作品完全下载不了）"""
    from engine.manga.download_manager import filter_volume_only
    chs = [_ch("第01卷", "v1"), _ch("第二卷", "v2"), _ch("Vol.3", "v3")]
    kept, note = filter_volume_only(chs, sel_chapters=None)
    assert len(kept) == 3, kept
    assert "不再按整卷规则排除" in note, note
    assert "风控" not in note and "下架" not in note, (
        "绝不能把'我们自己跳过'说成'源在风控/已下架'：%s" % note)


def test_mixed_list_still_drops_collection_stubs():
    """R36 的初衷仍要保住：混合列表里把整卷打包条目剔除"""
    from engine.manga.download_manager import filter_volume_only
    chs = [_ch("第01卷", "v1"), _ch("第1话 开端", "c1"), _ch("01卷番外", "e1")]
    kept, note = filter_volume_only(chs, sel_chapters=None)
    ids = [c["id"] for c in kept]
    assert "v1" not in ids, "整卷打包条目应被剔除：%s" % ids
    assert "c1" in ids and "e1" in ids, "正文与卷附加内容必须保留：%s" % ids
    assert "跳过 1 个整卷" in note, note


def test_empty_list_is_untouched():
    from engine.manga.download_manager import filter_volume_only
    kept, note = filter_volume_only([], sel_chapters=None)
    assert kept == [] and note == ""


def test_volume_rule_still_matches_its_own_cases():
    """判据本身不变（R36 已有的用例继续管着它）"""
    from engine.manga.download_manager import _is_volume_only
    assert _is_volume_only("第01卷") is True
    assert _is_volume_only("第一卷") is True
    assert _is_volume_only("01卷番外") is False
    assert _is_volume_only("第1话 开端") is False
