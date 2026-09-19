# -*- coding: utf-8 -*-
"""A01 回归（离线）：漫画章节"覆盖重下"必须先验证新版本再原子切换。

原实现（server/manga_api.py api_manga_chapter_repair 的 overwrite 分支）：
先删除本地全部旧图，再向源站取清单、逐页下载。任一环失败（清单取不到 /
某页下载失败 / 写盘失败）都会让一章原本可读的内容变成残章甚至空章。

修复契约：
1. 先取得并验证图片清单（非空），清单失败原章不动；
2. 新内容写入独立暂存目录，全部页下载+写盘成功才算通过校验；
3. 校验通过才原子切换到正式目录；任何失败原章继续可读；
4. 同一漫画有下载任务进行中时拒绝覆盖（与下载单飞协调）；
5. 只有新版本完整切换后才向前端报告"修复完成"。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 合法 webp 头（RIFF....WEBP）+ 填充到 >1000 字节（阅读端的小文件判损阈值）
_WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x11" * 2000
_WEBP_NEW = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x22" * 2000

SOURCE = "copymanga"


@pytest.fixture(scope="module")
def client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _seed_chapter(comic, chapter, pages=3, content=_WEBP):
    """造一章"已下载"的本地内容，返回章节目录"""
    from engine.config import MANGA_DOWNLOADS_DIR
    d = os.path.join(MANGA_DOWNLOADS_DIR, SOURCE, comic, chapter)
    os.makedirs(d, exist_ok=True)
    for i in range(pages):
        with open(os.path.join(d, f"{i:04d}.webp"), "wb") as f:
            f.write(content)
    return d


def _dir_files(d):
    return sorted(os.listdir(d)) if os.path.isdir(d) else None


class _FakeWebAdapter:
    """copymanga_web 适配器替身：images() 行为由测试注入"""
    def __init__(self, imgs=None, exc=None):
        self._imgs = imgs
        self._exc = exc

    def images(self, comic_id, chapter_id):
        if self._exc is not None:
            raise self._exc
        return list(self._imgs or [])


def _patch_web_adapter(monkeypatch, fake):
    import engine.manga.manager as mgr
    monkeypatch.setattr(mgr, "get_adapter", lambda key, **kw: fake)


def _patch_fetch(monkeypatch, behavior):
    """behavior: callable(idx, url) -> (status_code, content) 或抛异常"""
    import engine.manga.downloader as dl

    class _Resp:
        def __init__(self, status_code, content):
            self.status_code = status_code
            self.content = content

    def _fake_fetch(url, headers=None, timeout=20, **kw):
        idx = int(url.rsplit("_", 1)[1].split(".")[0])
        return _Resp(*behavior(idx, url))

    monkeypatch.setattr(dl, "fetch_image_checked", _fake_fetch)


def _repair(client, comic, chapter, overwrite=True):
    return client.post(
        f"/api/manga/{SOURCE}/{comic}/chapter/{chapter}/repair",
        json={"overwrite": overwrite})


def _read_img(client, comic, chapter, idx):
    return client.get(
        f"/api/manga/{SOURCE}/{comic}/chapter/{chapter}/img/{idx}")


def _staging_dirs():
    from engine.config import MANGA_STATE_DIR
    root = os.path.join(MANGA_STATE_DIR, "_repair_staging")
    if not os.path.isdir(root):
        return []
    out = []
    for base, _dirs, _files in os.walk(root):
        for f in _files:
            out.append(os.path.join(base, f))
    return out


# ── 失败场景：原章必须保持可读 ─────────────────────────────

def test_overwrite_manifest_error_keeps_chapter(client, monkeypatch):
    """清单获取抛异常：不得触碰原章"""
    d = _seed_chapter("ov_manifest_err", "ch1")
    _patch_web_adapter(monkeypatch, _FakeWebAdapter(exc=RuntimeError("boom")))
    r = _repair(client, "ov_manifest_err", "ch1")
    assert r.status_code != 200 or not r.get_json().get("ok")
    assert _dir_files(d) == ["0000.webp", "0001.webp", "0002.webp"]
    assert _read_img(client, "ov_manifest_err", "ch1", 0).status_code == 200


def test_overwrite_empty_manifest_keeps_chapter(client, monkeypatch):
    """清单为空：不得触碰原章"""
    d = _seed_chapter("ov_manifest_empty", "ch1")
    _patch_web_adapter(monkeypatch, _FakeWebAdapter(imgs=[]))
    r = _repair(client, "ov_manifest_empty", "ch1")
    assert r.status_code != 200 or not r.get_json().get("ok")
    assert _dir_files(d) == ["0000.webp", "0001.webp", "0002.webp"]
    assert _read_img(client, "ov_manifest_empty", "ch1", 1).status_code == 200


def test_overwrite_page_download_failure_keeps_chapter(client, monkeypatch):
    """某页下载失败（HTTP 500）：原章一页都不能少，暂存不得残留"""
    d = _seed_chapter("ov_page_fail", "ch1")
    urls = [f"https://img.example.com/x_{i}.webp" for i in range(4)]
    _patch_web_adapter(monkeypatch, _FakeWebAdapter(imgs=urls))

    def _behavior(idx, url):
        if idx == 2:
            return 500, b""
        return 200, _WEBP_NEW
    _patch_fetch(monkeypatch, _behavior)

    r = _repair(client, "ov_page_fail", "ch1")
    body = r.get_json()
    assert r.status_code != 200 or not body.get("ok")
    assert "修复完成" not in (body.get("msg") or "")
    # 原章三页完好且内容未被改动
    assert _dir_files(d) == ["0000.webp", "0001.webp", "0002.webp"]
    for i in range(3):
        with open(os.path.join(d, f"{i:04d}.webp"), "rb") as f:
            assert f.read() == _WEBP
    assert _read_img(client, "ov_page_fail", "ch1", 2).status_code == 200
    assert _staging_dirs() == []


def test_overwrite_page_exception_keeps_chapter(client, monkeypatch):
    """某页下载抛异常（网络错误）：同上，原章完整"""
    d = _seed_chapter("ov_page_exc", "ch1")
    urls = [f"https://img.example.com/x_{i}.webp" for i in range(3)]
    _patch_web_adapter(monkeypatch, _FakeWebAdapter(imgs=urls))

    def _behavior(idx, url):
        if idx == 1:
            raise ConnectionError("net down")
        return 200, _WEBP_NEW
    _patch_fetch(monkeypatch, _behavior)

    r = _repair(client, "ov_page_exc", "ch1")
    assert r.status_code != 200 or not r.get_json().get("ok")
    assert _dir_files(d) == ["0000.webp", "0001.webp", "0002.webp"]
    assert _staging_dirs() == []


def test_overwrite_write_failure_keeps_chapter(client, monkeypatch):
    """写盘失败（磁盘满等）：原章完整，不留半章"""
    d = _seed_chapter("ov_write_fail", "ch1")
    urls = [f"https://img.example.com/x_{i}.webp" for i in range(3)]
    _patch_web_adapter(monkeypatch, _FakeWebAdapter(imgs=urls))
    _patch_fetch(monkeypatch, lambda idx, url: (200, _WEBP_NEW))

    import engine.manga.downloader as dl
    real_write = dl.atomic_write_image

    def _flaky_write(path, content):
        if "0001" in path:
            raise OSError("No space left on device")
        return real_write(path, content)
    monkeypatch.setattr(dl, "atomic_write_image", _flaky_write)

    r = _repair(client, "ov_write_fail", "ch1")
    assert r.status_code != 200 or not r.get_json().get("ok")
    assert _dir_files(d) == ["0000.webp", "0001.webp", "0002.webp"]
    for i in range(3):
        with open(os.path.join(d, f"{i:04d}.webp"), "rb") as f:
            assert f.read() == _WEBP
    assert _read_img(client, "ov_write_fail", "ch1", 0).status_code == 200
    assert _staging_dirs() == []


def test_overwrite_rejected_while_download_running(client, monkeypatch):
    """同一漫画下载任务进行中：拒绝覆盖（与下载单飞协调），原章不动"""
    d = _seed_chapter("ov_dl_running", "ch1")
    urls = [f"https://img.example.com/x_{i}.webp" for i in range(3)]
    _patch_web_adapter(monkeypatch, _FakeWebAdapter(imgs=urls))
    _patch_fetch(monkeypatch, lambda idx, url: (200, _WEBP_NEW))

    import server.manga_api as mapi
    monkeypatch.setattr(mapi._manga_dl, "status",
                        lambda key: {"status": "running"})
    r = _repair(client, "ov_dl_running", "ch1")
    assert r.status_code == 409
    assert _dir_files(d) == ["0000.webp", "0001.webp", "0002.webp"]


# ── 成功场景：完整校验 + 原子切换后才报告修复完成 ──────────

def test_overwrite_success_swaps_and_reports(client, monkeypatch):
    """全部页下载+写盘成功：原子切换为新版本，响应报告修复完成"""
    d = _seed_chapter("ov_success", "ch1", pages=3, content=_WEBP)
    urls = [f"https://img.example.com/new_{i}.webp" for i in range(4)]
    _patch_web_adapter(monkeypatch, _FakeWebAdapter(imgs=urls))
    _patch_fetch(monkeypatch, lambda idx, url: (200, _WEBP_NEW))

    r = _repair(client, "ov_success", "ch1")
    body = r.get_json()
    assert r.status_code == 200 and body.get("ok")
    assert body.get("overwrite") is True
    assert "修复完成" in (body.get("msg") or "")
    assert body.get("total") == 4
    # 新版本 4 页完整落盘，旧 3 页已被替换
    assert _dir_files(d) == ["0000.webp", "0001.webp", "0002.webp", "0003.webp"]
    for i in range(4):
        with open(os.path.join(d, f"{i:04d}.webp"), "rb") as f:
            assert f.read() == _WEBP_NEW
    assert _read_img(client, "ov_success", "ch1", 3).status_code == 200
    assert _staging_dirs() == []


def test_overwrite_no_local_chapter_creates_fresh(client, monkeypatch):
    """本地无此章 + overwrite：直接以新版本建章"""
    urls = [f"https://img.example.com/x_{i}.webp" for i in range(2)]
    _patch_web_adapter(monkeypatch, _FakeWebAdapter(imgs=urls))
    _patch_fetch(monkeypatch, lambda idx, url: (200, _WEBP_NEW))
    r = _repair(client, "ov_fresh", "ch9")
    assert r.status_code == 200 and r.get_json().get("ok")
    assert _read_img(client, "ov_fresh", "ch9", 1).status_code == 200
    assert _staging_dirs() == []


def _seed_interrupted_swap(comic, chapter):
    from engine.config import MANGA_STATE_DIR
    target = _seed_chapter(comic, chapter, pages=1)
    staging = os.path.join(MANGA_STATE_DIR, "_repair_staging",
                           SOURCE, comic, chapter)
    os.makedirs(os.path.dirname(staging), exist_ok=True)
    backup = staging + ".old"
    os.rename(target, backup)
    return target, backup


def test_interrupted_swap_restored_before_manifest_failure(client, monkeypatch):
    target, backup = _seed_interrupted_swap("ov_crash_manifest", "ch1")
    _patch_web_adapter(monkeypatch, _FakeWebAdapter(exc=RuntimeError("offline")))
    r = _repair(client, "ov_crash_manifest", "ch1")
    assert r.status_code == 500
    assert not os.path.exists(backup)
    with open(os.path.join(target, "0000.webp"), "rb") as f:
        assert f.read() == _WEBP
    assert _read_img(client, "ov_crash_manifest", "ch1", 0).status_code == 200


def test_interrupted_swap_restored_before_page_failure(client, monkeypatch):
    target, backup = _seed_interrupted_swap("ov_crash_page", "ch1")
    urls = ["https://img.example.com/x_0.webp"]
    _patch_web_adapter(monkeypatch, _FakeWebAdapter(imgs=urls))
    _patch_fetch(monkeypatch, lambda idx, url: (500, b""))
    r = _repair(client, "ov_crash_page", "ch1")
    assert r.status_code == 500
    assert not os.path.exists(backup)
    with open(os.path.join(target, "0000.webp"), "rb") as f:
        assert f.read() == _WEBP


def test_failed_backup_restore_preserves_only_copy(client, monkeypatch):
    import shutil
    import server.manga_api as mapi

    target, backup = _seed_interrupted_swap("ov_crash_restore", "ch1")
    real_rename = os.rename

    def fail_restore(src, dst):
        if src == backup:
            raise OSError("restore denied")
        return real_rename(src, dst)

    def no_manifest(*a, **kw):
        raise AssertionError("must restore before fetching")

    _patch_web_adapter(monkeypatch, _FakeWebAdapter())
    monkeypatch.setattr(_FakeWebAdapter, "images", no_manifest)
    monkeypatch.setattr(mapi.os, "rename", fail_restore)
    try:
        r = _repair(client, "ov_crash_restore", "ch1")
        assert r.status_code == 500
        assert not os.path.exists(target)
        with open(os.path.join(backup, "0000.webp"), "rb") as f:
            assert f.read() == _WEBP
    finally:
        shutil.rmtree(backup)


def test_txn_lock_lives_outside_staging(client, monkeypatch):
    """事务锁文件必须放在**会被清理的目录之外**（0.71.0 修）。

    否则：①用户私有目录里每个作品积一个 `.txn.lock`；
    ②暂存清理/rmtree 一旦把锁文件删掉，新来的锁者会另建文件 → 互斥窗口出现，
      两个修复流程可能同时改同一章。
    """
    import server.manga_api as m
    from engine.config import MANGA_STATE_DIR
    d = _seed_chapter("ov_lock_place", "ch1")
    _repair(client, "ov_lock_place", "ch1")
    locks = os.path.join(MANGA_STATE_DIR, "_repair_locks")
    assert os.path.isdir(locks), "锁文件应落在 _repair_locks/ 下"
    assert any(n.endswith(".lock") for n in os.listdir(locks)), os.listdir(locks)
    # 暂存目录必须干净（锁文件不在里面）
    assert _staging_dirs() == [], _staging_dirs()
