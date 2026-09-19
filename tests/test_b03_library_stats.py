# -*- coding: utf-8 -*-
"""B03 后端：书库统计快照（图片数/章节数持久化 + 条目 revision + 增量失效）。

验收点：
1. /api/manga/library 无数据变更的重复请求不遍历图片目录（os.walk/listdir 计数为 0）；
2. 计数持久化到 _library_stats.json，下载完成/删除/修复增量更新，revision 递增；
3. 后台核对以真实文件为准校正快照（外部增删文件后 verify_once 校正）；
4. 快照缺失时请求路径不扫描（下载记录兜底 + 排队后台核对），不误标缺失。
"""
import json
import os
import time

import pytest

import server.state as st
from engine.app_utils import atomic_write
from engine.config import MANGA_LIBRARY_FILE, MANGA_DOWNLOADS_DIR

_SRC = "b03src"          # 专用源名，避免与同会话其他用例互相污染
_CID = "b03comic1"


@pytest.fixture()
def client():
    import app
    return app.app.test_client()


@pytest.fixture()
def clean_stats():
    """重置统计快照全局态 + 保存/恢复书库记录文件（测试间隔离）"""
    with st._manga_stats_lock:
        st._manga_lib_stats.clear()
        st._manga_stats_pending.clear()
        st._manga_lib_rev = 0
    try:
        os.remove(st.MANGA_LIBRARY_STATS_FILE)
    except OSError:
        pass
    old_lib = None
    if os.path.exists(MANGA_LIBRARY_FILE):
        with open(MANGA_LIBRARY_FILE, encoding="utf-8") as f:
            old_lib = f.read()
    yield
    with st._manga_stats_lock:
        st._manga_lib_stats.clear()
        st._manga_stats_pending.clear()
        st._manga_lib_rev = 0
    try:
        os.remove(st.MANGA_LIBRARY_STATS_FILE)
    except OSError:
        pass
    if old_lib is None:
        try:
            os.remove(MANGA_LIBRARY_FILE)
        except OSError:
            pass
    else:
        atomic_write(MANGA_LIBRARY_FILE, json.loads(old_lib))
    import shutil
    shutil.rmtree(os.path.join(MANGA_DOWNLOADS_DIR, _SRC), ignore_errors=True)


def _mk_comic(source, cid, chapters):
    """造本地漫画：chapters = {章节id: 图片数}"""
    base = os.path.join(MANGA_DOWNLOADS_DIR, source, cid)
    for ch, n in chapters.items():
        d = os.path.join(base, ch)
        os.makedirs(d, exist_ok=True)
        for i in range(n):
            with open(os.path.join(d, f"{i:04d}.webp"), "wb") as f:
                f.write(b"x" * 16)
    return base


def _lib_entry(images=5, chapters=2, status="done"):
    return [{"source": _SRC, "comic_id": _CID, "title": "B03测试漫",
             "cover": "", "chapters": chapters, "images": images,
             "status": status, "downloaded_at": "2026-01-01 00:00:00",
             "source_name": "B03源"}]


class TestStatsSnapshot:
    def test_note_change_builds_snapshot_and_persists(self, clean_stats):
        _mk_comic(_SRC, _CID, {"ch1": 3, "ch2": 2})
        st._manga_stats_note_change(_SRC, _CID)
        ent = st._manga_stats_get(_SRC, _CID)
        assert ent["images"] == 5
        assert ent["chapters"] == 2
        assert ent["rev"] == 1
        assert st.manga_library_revision() == 1
        # 持久化：磁盘快照与内存一致
        with open(st.MANGA_LIBRARY_STATS_FILE, encoding="utf-8") as f:
            disk = json.load(f)
        assert disk["rev"] == 1
        assert disk["entries"][st._manga_stats_skey(_SRC, _CID)]["images"] == 5
        # 再次变更 → 条目 rev 与全局 rev 均递增
        st._manga_stats_note_change(_SRC, _CID)
        assert st._manga_stats_get(_SRC, _CID)["rev"] == 2
        assert st.manga_library_revision() == 2

    def test_removed_drops_entry(self, clean_stats):
        _mk_comic(_SRC, _CID, {"ch1": 1})
        st._manga_stats_note_change(_SRC, _CID)
        st._manga_stats_note_change(_SRC, _CID, removed=True)
        assert st._manga_stats_get(_SRC, _CID) is None
        assert st.manga_library_revision() == 2

    def test_verify_corrects_external_changes(self, clean_stats):
        """后台核对以真实文件为准：外部加图后 verify_once 校正计数"""
        base = _mk_comic(_SRC, _CID, {"ch1": 2})
        atomic_write(MANGA_LIBRARY_FILE, _lib_entry(images=2, chapters=1))
        st._manga_stats_note_change(_SRC, _CID)
        assert st._manga_stats_get(_SRC, _CID)["images"] == 2
        # 绕过钩子直接改磁盘（模拟外部/历史遗留变更）
        with open(os.path.join(base, "ch1", "0009.webp"), "wb") as f:
            f.write(b"x" * 16)
        # 核对需要条目"不新鲜"——把 ts 拨旧
        with st._manga_stats_lock:
            st._manga_lib_stats[st._manga_stats_skey(_SRC, _CID)]["ts"] = \
                time.time() - 2 * st._MANGA_VERIFY_INTERVAL
        changed = st._manga_stats_verify_once()
        assert changed >= 1
        assert st._manga_stats_get(_SRC, _CID)["images"] == 3

    def test_verify_prunes_orphan_entries(self, clean_stats):
        """书库记录被外部删除的条目，核同步摘除"""
        _mk_comic(_SRC, _CID, {"ch1": 1})
        st._manga_stats_note_change(_SRC, _CID)
        atomic_write(MANGA_LIBRARY_FILE, [])   # 外部清空书库
        st._manga_stats_verify_once()
        assert st._manga_stats_get(_SRC, _CID) is None


class TestLibraryEndpoint:
    def test_repeated_requests_do_not_walk_image_dirs(self, client, clean_stats,
                                                      monkeypatch):
        """验收核心：快照已建后，重复列表请求零 os.walk / 零图片目录 listdir"""
        _mk_comic(_SRC, _CID, {"ch1": 3, "ch2": 2})
        atomic_write(MANGA_LIBRARY_FILE, _lib_entry())
        st._manga_stats_note_change(_SRC, _CID)
        real_walk, real_listdir = os.walk, os.listdir
        calls = {"walk": 0, "listdir": 0}

        def counting_walk(*a, **kw):
            calls["walk"] += 1
            return real_walk(*a, **kw)

        def counting_listdir(*a, **kw):
            calls["listdir"] += 1
            return real_listdir(*a, **kw)

        monkeypatch.setattr(os, "walk", counting_walk)
        monkeypatch.setattr(os, "listdir", counting_listdir)
        for _ in range(3):
            r = client.get("/api/manga/library")
            assert r.status_code == 200
            d = r.get_json()
            ent = next(c for c in d["comics"] if c["comic_id"] == _CID)
            assert ent["local_images"] == 5
            assert ent["chapters"] == 2
            assert "rev" in d   # 轻量快照附全局 revision
        assert calls["walk"] == 0, f"请求路径仍在 os.walk: {calls}"
        assert calls["listdir"] == 0, f"请求路径仍在 listdir: {calls}"

    def test_missing_snapshot_falls_back_without_scan(self, client, clean_stats,
                                                      monkeypatch):
        """快照未建：请求路径不扫描，用下载记录兜底且不误标缺失，并排队核对"""
        _mk_comic(_SRC, _CID, {"ch1": 3, "ch2": 2})
        atomic_write(MANGA_LIBRARY_FILE, _lib_entry())
        real_walk = os.walk
        calls = {"walk": 0}

        def counting_walk(*a, **kw):
            calls["walk"] += 1
            return real_walk(*a, **kw)

        monkeypatch.setattr(os, "walk", counting_walk)
        r = client.get("/api/manga/library")
        assert r.status_code == 200
        d = r.get_json()
        ent = next(c for c in d["comics"] if c["comic_id"] == _CID)
        assert ent["local_images"] == 5      # 记录值兜底
        assert "missing_images" not in ent   # 未核对不误标缺失
        assert calls["walk"] == 0
        with st._manga_stats_lock:
            assert st._manga_stats_skey(_SRC, _CID) in st._manga_stats_pending

    def test_verified_empty_marks_missing_images(self, client, clean_stats):
        """快照已核对且确实无图 → done 记录标记 missing_images（语义保持）"""
        _mk_comic(_SRC, _CID, {"ch1": 1})
        atomic_write(MANGA_LIBRARY_FILE, _lib_entry())
        st._manga_stats_note_change(_SRC, _CID)
        import shutil
        shutil.rmtree(os.path.join(MANGA_DOWNLOADS_DIR, _SRC, _CID))
        st._manga_stats_note_change(_SRC, _CID)   # 重建快照（0 图 0 章）
        r = client.get("/api/manga/library")
        ent = next(c for c in r.get_json()["comics"] if c["comic_id"] == _CID)
        assert ent["missing_images"] is True

    def test_delete_invalidates_stats(self, client, clean_stats):
        _mk_comic(_SRC, _CID, {"ch1": 2})
        atomic_write(MANGA_LIBRARY_FILE, _lib_entry())
        st._manga_stats_note_change(_SRC, _CID)
        rev0 = st.manga_library_revision()
        r = client.delete(f"/api/manga/library/{_SRC}/{_CID}")
        assert r.status_code == 200
        assert st._manga_stats_get(_SRC, _CID) is None
        assert st.manga_library_revision() > rev0


class TestInvalidationHooks:
    """文本断言：各变更点都接了增量失效/更新钩子（代码审查的机器化）"""

    def test_download_completion_hook(self):
        src = (os.path.join(os.path.dirname(st.__file__), os.pardir,
                            "engine", "manga", "download_manager.py"))
        with open(src, encoding="utf-8") as f:
            code = f.read()
        assert "library_change_hook = None" in code
        assert "_hook(source, comic_id)" in code
        # state.py 注入钩子
        with open(st.__file__, encoding="utf-8") as f:
            stcode = f.read()
        assert "_dm_mod.library_change_hook = _manga_stats_note_change" in stcode

    def test_repair_hooks(self):
        import server.manga_api as mapi
        with open(mapi.__file__, encoding="utf-8") as f:
            code = f.read()
        # A01 覆盖修复完成点 + 补页完成点都更新快照
        assert code.count("_manga_stats_note_change(source, comic_id)") >= 2
