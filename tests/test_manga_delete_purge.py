# -*- coding: utf-8 -*-
"""「从书库移除」是否连已下载文件一起删（离线，不触网）

背景（2026-09-15）：实测在手机/模拟器上"从书库移除"后，`manga/downloads/<源>/<id>/`
里 83 张图（14MB）仍在，而 App 里没有别的入口能删掉它们——手机存储宝贵，这是真实缺口。

口径：
  · 默认（不带 files）**保持原行为**：只移除记录，文件保留（桌面网页端一直如此，
    不能悄悄改）；
  · `?files=1`（或 body {"files": true}）→ 连 downloads 目录一起删，并如实回报释放字节数；
  · 两种情况下书库记录都要被移除。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import config as cfg  # noqa: E402


@pytest.fixture
def client():
    import app
    return app.app.test_client()


def _make_comic(source="mangadex", comic_id="cid1", n=3):
    """造一个"已下载"的漫画：书库记录 + downloads 目录 + 图片文件"""
    lib_p = cfg.MANGA_LIBRARY_FILE
    os.makedirs(os.path.dirname(lib_p), exist_ok=True)
    lib = []
    if os.path.exists(lib_p):
        try:
            lib = json.load(open(lib_p, encoding="utf-8"))
        except Exception:
            lib = []
    lib.append({"source": source, "comic_id": comic_id, "title": "自检漫画",
                "chapters": 1, "images": n, "status": "done"})
    with open(lib_p, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False)
    d = os.path.join(cfg.MANGA_DOWNLOADS_DIR, source, comic_id, "ch1")
    os.makedirs(d, exist_ok=True)
    for i in range(n):
        with open(os.path.join(d, "%04d.jpg" % i), "wb") as f:
            f.write(b"\xff\xd8\xff" + b"x" * 2048)
    return os.path.join(cfg.MANGA_DOWNLOADS_DIR, source, comic_id)


def _clean(source="mangadex", comic_id="cid1"):
    import shutil
    shutil.rmtree(os.path.join(cfg.MANGA_DOWNLOADS_DIR, source, comic_id), ignore_errors=True)
    lib_p = cfg.MANGA_LIBRARY_FILE
    if os.path.exists(lib_p):
        try:
            lib = json.load(open(lib_p, encoding="utf-8"))
            lib = [x for x in lib if not (x.get("source") == source
                                          and str(x.get("comic_id")) == comic_id)]
            with open(lib_p, "w", encoding="utf-8") as f:
                json.dump(lib, f, ensure_ascii=False)
        except Exception:
            pass


def test_default_keeps_files(client):
    """默认行为不变：只移除记录，文件保留（桌面网页端语义）"""
    d = _make_comic()
    try:
        r = client.delete("/api/manga/library/mangadex/cid1")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] and body["purged"] == []
        assert os.path.isdir(d), "默认不应删除已下载文件"
        lib = json.load(open(cfg.MANGA_LIBRARY_FILE, encoding="utf-8"))
        assert not [x for x in lib if x.get("comic_id") == "cid1"], "记录必须被移除"
    finally:
        _clean()


def test_files_flag_purges_downloads(client):
    """?files=1 → 连文件一起删，并如实回报释放的字节数"""
    d = _make_comic(n=3)
    try:
        r = client.delete("/api/manga/library/mangadex/cid1?files=1")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert "mangadex" in body["purged"], body
        assert body["freed_bytes"] > 5000, f"应回报释放字节数，实际 {body['freed_bytes']}"
        assert not os.path.isdir(d), "files=1 后 downloads 目录必须消失"
    finally:
        _clean()


def test_body_flag_also_works(client):
    """body {"files": true} 同样生效（两种调用方式一致）"""
    d = _make_comic(comic_id="cid2")
    try:
        r = client.delete("/api/manga/library/mangadex/cid2", json={"files": True})
        assert r.status_code == 200
        assert not os.path.isdir(d)
    finally:
        _clean(comic_id="cid2")


def test_purge_without_files_on_disk_is_not_an_error(client):
    """没有已下载文件时也不报错（purged 为空、freed=0）"""
    lib_p = cfg.MANGA_LIBRARY_FILE
    os.makedirs(os.path.dirname(lib_p), exist_ok=True)
    lib = []
    if os.path.exists(lib_p):
        try:
            lib = json.load(open(lib_p, encoding="utf-8"))
        except Exception:
            lib = []
    lib.append({"source": "mangadex", "comic_id": "nofiles", "title": "x"})
    with open(lib_p, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False)
    try:
        r = client.delete("/api/manga/library/mangadex/nofiles?files=1")
        assert r.status_code == 200
        body = r.get_json()
        assert body["purged"] == [] and body["freed_bytes"] == 0
    finally:
        _clean(comic_id="nofiles")
