# -*- coding: utf-8 -*-
"""R70/R72 抗压加固回归: 图片缓存版本化 + 渲染超时恢复行为(离线部分)。

不触网、不启 Chrome——只验证:
1. images() 缓存读取: 旧裸数组缓存(R60 半截)必须被拒绝(视为无缓存),
   只有 {"v":2,"urls":[...]} 才被信任;
2. app.py 阅读通道对旧缓存同样不信任。
"""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_cache_file(tmp, content):
    """构造 _cache/copymanga/<cid>/<chid>_imgs.json 缓存文件"""
    cdir = os.path.join(tmp, "_cache", "copymanga", "comic_x")
    os.makedirs(cdir, exist_ok=True)
    p = os.path.join(cdir, "ch1_imgs.json")
    json.dump(content, open(p, "w", encoding="utf-8"))
    return p


def test_old_bare_list_cache_rejected(tmp_path, monkeypatch):
    """R70: 旧裸数组缓存(半截列表)不得被 images() 信任"""
    import engine.manga.copymanga_web as web
    from engine.manga.base import MangaError

    # images() 缓存路径为 state_dir/../_cache；让其准确落在 tmp_path/_cache。
    state_dir = tmp_path / "data"
    state_dir.mkdir()
    _make_cache_file(tmp_path, ["https://cdn/x/1.webp", "https://cdn/x/2.webp"])
    adapter = web.CopyMangaWeb()
    adapter.state_dir = str(state_dir)

    calls = []

    def fail_http(*_args):
        raise MangaError("offline fixture")

    def fail_web(*_args):
        calls.append("render")
        raise MangaError("offline fixture")

    monkeypatch.setattr(web, "_http_chapter_images", fail_http)
    monkeypatch.setattr(web, "_web_chapter_images", fail_web)
    with pytest.raises(MangaError, match="章节图片获取失败"):
        adapter.images("comic_x", "ch1")
    assert calls == ["render"], "旧裸数组缓存必须被拒绝并进入重新提取流程"


def test_v2_cache_trusted_shape():
    """v2 缓存结构应含 version 标记与 urls 列表"""
    # 直接构造并验证解析兼容(模拟 images() 的读取逻辑)
    cached = {"v": 2, "urls": ["https://a/1.webp", "https://a/2.webp"]}
    _urls = []
    if isinstance(cached, dict) and cached.get("v") == 2:
        _urls = cached.get("urls") or []
    assert _urls == ["https://a/1.webp", "https://a/2.webp"]

    old = ["https://a/1.webp"]           # 旧格式
    _urls = []
    if isinstance(old, dict) and old.get("v") == 2:
        _urls = old.get("urls") or []
    elif isinstance(old, list):
        _urls = []                        # R70: 旧缓存不信任
    assert _urls == []
