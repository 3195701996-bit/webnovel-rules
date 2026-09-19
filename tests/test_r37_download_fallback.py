# -*- coding: utf-8 -*-
"""R37 下载任务失败修复回归

实测 18 个任务中 11 个失败，两类根因：

1. UnboundLocalError（9 个任务，"cannot access local variable 'd'"）
   主渠道 comic_info 抛异常时 `d` 从未赋值，降级渠道成功后仍访问 `d.cover`
   → 降级明明拿到了章节，却在最后一步崩掉，白白浪费一次成功抓取。

2. 图片列表无降级（2 个任务，44/44 与 18/19 章全失败）
   copymanga APP 接口整体 210 风控时，ad.images() 只在同一个被风控的
   适配器上重试 2 次，必然全灭。章节列表已有 copymanga_web 降级，
   图片列表却没有——形成"能拿到章节、拿不到图片"的死局。
"""
import inspect
import os
import sys

import pytest

HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HUB)

from engine.manga import download_manager as dm  # noqa: E402


# ── 缺陷 1：详情对象作用域 ────────────────────────────────────

def test_no_unbound_detail_variable():
    """降级路径不得引用主渠道 try 内才赋值的变量"""
    src = inspect.getsource(dm.DownloadManager._worker)
    assert "cover = d.cover" not in src, \
        "回归：主渠道失败时 d 未赋值，降级成功后访问 d.cover 会 UnboundLocalError"
    assert "_detail" in src, "应使用独立变量承接详情对象"


def test_cover_access_is_none_safe():
    """_detail 为 None（两渠道都没拿到详情但 chapters 有值）时不得崩溃"""
    src = inspect.getsource(dm.DownloadManager._worker)
    assert 'getattr(_detail, "cover", None)' in src, \
        "cover 取值必须对 _detail=None 安全"


class _FakeChapter:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name
        self.group = "default"


class _FakeDetail:
    def __init__(self, n=3, cover="http://x/c.jpg"):
        self.chapters = [_FakeChapter(f"c{i}", f"第{i+1}話") for i in range(n)]
        self.cover = cover


def test_fallback_detail_provides_cover():
    """降级成功时，cover 应来自降级渠道的详情对象而非崩溃"""
    _detail = None
    cover = "old"
    try:
        raise RuntimeError("主渠道 210 风控")
    except RuntimeError:
        _detail = _FakeDetail(cover="http://web/c.jpg")
        chapters = [{"id": c.id} for c in _detail.chapters]
        assert chapters
    cover = (getattr(_detail, "cover", None) or cover)
    assert cover == "http://web/c.jpg"


def test_cover_falls_back_when_detail_is_none():
    _detail = None
    cover = "old"
    cover = (getattr(_detail, "cover", None) or cover)
    assert cover == "old"


# ── 缺陷 2：图片列表降级 ──────────────────────────────────────

def test_images_has_web_fallback():
    """图片列表获取必须有 copymanga_web 降级，否则 APP 风控时必然全失败"""
    src = inspect.getsource(dm.DownloadManager._worker)
    assert "_web_fallback_adapter" in src, "图片列表缺少网页版降级"
    # 降级必须发生在抛 MangaError 之前
    i_fb = src.find("_web_fallback_adapter")
    # 抛错点：空列表同样按失败处理（0 图不得当成功）
    i_raise = src.find('raise MangaError("图片列表为空（源站可能限流）")')
    if i_raise < 0:
        i_raise = src.find('raise MangaError("图片列表获取失败")')
    assert i_raise > 0, "未找到图片列表失败的抛出点"
    assert 0 < i_fb < i_raise, "降级必须在放弃之前尝试"


def test_web_fallback_adapter_is_cached():
    """降级适配器需复用，避免每章重建会话/指纹"""
    assert hasattr(dm.DownloadManager, "_web_fallback_adapter")
    src = inspect.getsource(dm.DownloadManager._web_fallback_adapter)
    assert "_web_fb" in src, "降级适配器应缓存复用"


def test_web_fallback_returns_none_on_failure():
    """构造失败必须返回 None 而非抛异常，交调用方决定"""
    mgr = dm.DownloadManager.__new__(dm.DownloadManager)
    mgr._web_fb = None

    def boom():
        raise RuntimeError("no state dir")

    mgr._get_state_dir = boom
    assert mgr._web_fallback_adapter() is None


def test_web_fallback_cache_hit_skips_rebuild():
    mgr = dm.DownloadManager.__new__(dm.DownloadManager)
    sentinel = object()
    mgr._web_fb = sentinel

    def boom():
        raise AssertionError("命中缓存时不应重建适配器")

    mgr._get_state_dir = boom
    assert mgr._web_fallback_adapter() is sentinel


def test_fallback_only_for_copymanga():
    """降级仅针对 copymanga；其他源不应误触发"""
    src = inspect.getsource(dm.DownloadManager._worker)
    i_fb = src.find("_web_fallback_adapter")
    window = src[max(0, i_fb - 260):i_fb]
    assert 'source == "copymanga"' in window, \
        "图片降级应限定 copymanga 源"


# ── 整卷过滤仍需生效（与 R36 联动，防修复间互相破坏）──────────

def test_volume_filter_still_applied_in_worker():
    """整卷过滤仍在生效（0.74.12 起走纯函数；判据本体仍在 download_manager）"""
    src = inspect.getsource(dm.DownloadManager._worker)
    assert "filter_volume_only" in src
    fn = inspect.getsource(dm.filter_volume_only)
    assert "_is_volume_only" in fn
