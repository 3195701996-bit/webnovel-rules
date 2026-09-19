# -*- coding: utf-8 -*-
"""图片"入缓存"路径的失败策略（离线）：还原失败绝不许当成成功落盘

0.51.0 风险评估 P0-1 §2.3 第 3 条：后处理（块还原）失败必须向上返回受控错误，
不能把原始乱序图标记成功并写入缓存——否则用户看到花图，而且这份错误内容会一直被复用。

本用例锁住：
  1. 还原抛错 → 下载器抛 BadImageError（永久失败，不重试），**磁盘上没有文件**；
  2. 还原成功 → 落盘的正是**还原后**的字节，并写入处理版本标记（供选择性重建）；
  3. 非混淆源（没有 unscramble_image）→ 原样落盘、不写标记（不打扰无关源）。
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.manga.downloader as dl  # noqa: E402
from engine.manga.downloader import PROCESS_MARKER  # noqa: E402
from engine.manga.base import MangaError  # noqa: E402

# 合法 webp 头 + 填充（>1000 字节，避免被判"源站坏页"）
_IMG = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x11" * 3000
_FIXED = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x22" * 3000


class _Resp:
    status_code = 200
    headers = {"Content-Type": "image/webp"}

    def __init__(self, content):
        self.content = content


class _Scrambling:
    """带块还原的适配器替身（jm 形态）"""
    key = "jm"
    PROCESS_VERSION = 2
    concurrent = 2

    def __init__(self, fixed=_FIXED, boom=False):
        self._fixed, self._boom = fixed, boom

    def unscramble_image(self, content, ep_id, image_url):
        if self._boom:
            raise MangaError("模拟还原失败")
        return self._fixed

    def image_headers(self, url):
        return {}

    def image_url(self, url):          # 下载器会用它按当前最优域改写地址
        return url


class _Plain:
    key = "mangadex"
    concurrent = 2

    def image_headers(self, url):
        return {}

    def image_url(self, url):
        return url


def _run(adapter, monkeypatch, tmp, url="https://cdn.example/media/photos/300000/00001.jpg"):
    monkeypatch.setattr(dl, "fetch_image_checked",
                        lambda u, h, timeout=20, **kw: _Resp(_IMG))
    d = dl.ImageDownloader(adapter, tmp, min_interval=0.0)
    return d.get(url, "comic1", "ep300000", 0)


def test_unscramble_failure_is_not_written_to_cache(monkeypatch):
    tmp = tempfile.mkdtemp()
    with pytest.raises(MangaError):
        _run(_Scrambling(boom=True), monkeypatch, tmp)
    ch = os.path.join(tmp, "comic1", "ep300000")
    files = os.listdir(ch) if os.path.isdir(ch) else []
    assert files == [], f"还原失败时磁盘上不许有文件（实际 {files}）"


def test_successful_unscramble_is_written_with_version_marker(monkeypatch):
    tmp = tempfile.mkdtemp()
    content, cp = _run(_Scrambling(), monkeypatch, tmp)
    assert content == _FIXED, "返回的必须是还原后的字节"
    with open(cp, "rb") as f:
        assert f.read() == _FIXED, "落盘的必须是还原后的字节"
    marker = os.path.join(os.path.dirname(cp), PROCESS_MARKER)
    assert os.path.isfile(marker), "必须写处理版本标记（选择性重建的依据）"
    import json
    data = json.load(open(marker, encoding="utf-8"))
    assert data["jm"]["version"] == 2


def test_plain_source_writes_no_marker(monkeypatch):
    tmp = tempfile.mkdtemp()
    content, cp = _run(_Plain(), monkeypatch, tmp,
                       url="https://cdn.example/a/b/00001.webp")
    with open(cp, "rb") as f:
        assert f.read() == _IMG, "非混淆源原样落盘"
    assert not os.path.exists(os.path.join(os.path.dirname(cp), PROCESS_MARKER)), \
        "非混淆源不该被写上处理版本标记"


def test_stale_processed_chapter_is_rebuilt_before_cache_hit(monkeypatch):
    """旧算法缓存即使图片头合法，也不能直接读出花图。"""
    import json

    tmp = tempfile.mkdtemp()
    chapter = os.path.join(tmp, "ep300000")
    os.makedirs(chapter)
    with open(os.path.join(chapter, "0000.jpg"), "wb") as f:
        f.write(_IMG)
    with open(os.path.join(chapter, "0001.jpg"), "wb") as f:
        f.write(_IMG)
    with open(os.path.join(chapter, PROCESS_MARKER), "w", encoding="utf-8") as f:
        json.dump({"jm": {"version": 1}}, f)

    content, cp = _run(_Scrambling(), monkeypatch, tmp)

    assert content == _FIXED
    with open(cp, "rb") as f:
        assert f.read() == _FIXED
    assert not os.path.exists(os.path.join(chapter, "0001.jpg")), \
        "更新 marker 前必须清掉同章其余旧算法图片"
    with open(os.path.join(chapter, PROCESS_MARKER), encoding="utf-8") as f:
        assert json.load(f)["jm"]["version"] == 2


def test_stale_marker_is_rejected_by_local_image_gateway(tmp_path):
    """HTTP 直出路径也不能绕过旧算法缓存版本闸门。"""
    import json
    import server.manga_api as ma

    class _Ad:
        key = "jm"
        PROCESS_VERSION = 2

    page = tmp_path / "0000.jpg"
    page.write_bytes(b"\xff\xd8" + b"x" * 1200)
    (tmp_path / PROCESS_MARKER).write_text(
        json.dumps({"jm": {"version": 1}}), encoding="utf-8")

    assert ma._serve_local_image(str(tmp_path), 0, _Ad()) is None
