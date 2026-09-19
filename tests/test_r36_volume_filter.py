# -*- coding: utf-8 -*-
"""R36 整卷合集过滤回归

整卷合集（"第01卷"/"第一卷"）在源站是打包条目，chapter2 接口返回 null
不可下载。若未排除：下载任务会卡在这些条目上，检查更新则会因为
"源站有、本地没有"而永久误报有更新。

R36 修复：原正则只认阿拉伯数字，"第一卷/第二卷"等中文卷号会漏网。
同时补齐日文「巻」、Volume 全拼与"单行本"前缀。

关键约束：必须整名匹配。"01卷番外""02卷加笔"含卷号但是实际可下载内容，
误杀会导致用户永久缺章。
"""
import json
import os
import sys

import pytest

HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HUB)

from engine.manga.download_manager import _is_volume_only  # noqa: E402


# ── 必须跳过：纯整卷合集 ──────────────────────────────────────

@pytest.mark.parametrize("name", [
    # 阿拉伯数字
    "第01卷", "第1卷", "第 1 卷", "第09卷", "卷1", "卷 2", "1卷", "09卷",
    # 中文数字（R36 修复重点：用户明确要求不爬"第一卷/第二卷"）
    "第一卷", "第二卷", "第十卷", "第十二卷", "第二十卷", "卷一", "一卷",
    # 日文「巻」
    "第1巻", "第一巻", "巻一", "1巻",
    # 西文
    "Vol.1", "Vol 1", "vol.01", "VOL.12", "Volume 1", "VOLUME 12",
    # 单行本前缀
    "单行本第1卷", "單行本第一卷",
    # 首尾空白
    "第1卷 ", "  第01卷",
])
def test_volume_only_is_skipped(name):
    assert _is_volume_only(name) is True, f"整卷未被跳过: {name!r}"


# ── 必须保留：实际可下载内容 ──────────────────────────────────

@pytest.mark.parametrize("name", [
    # 卷附加内容（含卷号但有实质内容，误杀会导致永久缺章）
    "01卷番外", "01卷附錄", "02卷加笔", "02卷宣傳圖", "03卷宣传图",
    "03卷番外", "04卷加筆", "1卷番外", "第1卷番外", "Vol.1 Extra",
    # 正片
    "第1话", "第01話", "第一话", "第100话", "第1.5话", "第一章",
    # 附加内容
    "番外篇", "特别篇", "休載公告", "贺图", "動畫化決定", "後記",
    # 空值
    "", "   ",
])
def test_real_content_is_kept(name):
    assert _is_volume_only(name) is False, f"实际内容被误杀: {name!r}"


def test_none_is_safe():
    assert _is_volume_only(None) is False


# ── 三条使用路径必须共用同一实现（防逻辑分叉）──────────────────

def test_download_and_check_update_share_one_impl():
    """下载 worker 与 check-update 的过滤规则必须一致。

    不一致会导致：下载时跳过整卷 → 本地永远没有该章节 →
    检查更新认为"源站有、本地缺" → 永久误报有更新。
    """
    import inspect
    from engine.manga import download_manager as dm
    import server.state
    import server.manga_api

    # R47: 实现已拆入 server/ 包——state(_manga_check_one) 与
    # manga_api(api_manga_detail quick 路径) 都应从 download_manager 引入同一实现
    src = inspect.getsource(server.state) + inspect.getsource(server.manga_api)
    assert src.count(
        "from engine.manga.download_manager import _is_volume_only") >= 2, \
        "server 模块应从 download_manager 引入同一实现，不得自行复制正则"

    worker = inspect.getsource(dm.DownloadManager._worker)
    # 0.74.12：过滤逻辑抽成纯函数 filter_volume_only（政策可测），
    # worker 必须走它；判据本体仍由本文件管着。
    assert "filter_volume_only" in worker, "下载 worker 未做整卷过滤（应调用 filter_volume_only）"
    fn = inspect.getsource(dm.filter_volume_only)
    assert "_is_volume_only" in fn, "过滤函数必须复用同一整卷判据"
    # 两条例外（用户选择优先 / 滤完不剩则不滤）必须写在实现里
    assert "sel_chapters" in fn and "不再按整卷规则排除" in fn


# ── 真实书库数据回归 ─────────────────────────────────────────

def _library_chapter_names():
    base = os.path.join(HUB, "data", "manga", "downloads")
    names = set()
    if not os.path.isdir(base):
        return names
    for src in os.listdir(base):
        sp = os.path.join(base, src)
        if not os.path.isdir(sp):
            continue
        for cid in os.listdir(sp):
            info = os.path.join(sp, cid, "_info.json")
            if not os.path.exists(info):
                continue
            try:
                with open(info, encoding="utf-8") as f:
                    for c in json.load(f).get("chapters", []):
                        names.add(c.get("name") or "")
            except Exception:
                continue
    return names


def test_real_library_volume_entries_are_skipped():
    """真实书库中形如"第0X卷"的条目必须被跳过"""
    names = _library_chapter_names()
    if not names:
        pytest.skip("本机无已下载漫画数据")
    import re
    plain_vol = [n for n in names
                 if re.fullmatch(r"第\s*\d+\s*卷", n.strip())]
    if not plain_vol:
        pytest.skip("书库中无整卷条目")
    for n in plain_vol:
        assert _is_volume_only(n) is True, f"真实整卷未跳过: {n!r}"


def test_real_library_volume_extras_are_kept():
    """真实书库中"0X卷番外/加笔/宣传图"必须保留"""
    names = _library_chapter_names()
    if not names:
        pytest.skip("本机无已下载漫画数据")
    extras = [n for n in names
              if "卷" in n and not n.strip().replace(" ", "").endswith("卷")]
    if not extras:
        pytest.skip("书库中无卷附加内容")
    for n in extras:
        assert _is_volume_only(n) is False, f"卷附加内容被误杀: {n!r}"
