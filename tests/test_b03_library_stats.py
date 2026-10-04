# -*- coding: utf-8 -*-
"""B03 后端：书库统计快照（图片数/章节数持久化 + 条目 revision + 增量失效）。

验收点：
1. /api/manga/library 无数据变更的重复请求不遍历图片目录（os.walk/listdir 计数为 0）；
2. 计数持久化到 _library_stats.json，下载完成/删除/修复增量更新，revision 递增；
3. 后台核对以真实文件为准校正快照（外部增删文件后 verify_once 校正）；
4. 快照缺失时请求路径不扫描（历史章节数可兜底、当前实盘图片数保持未知）并排队后台核对。
"""
import json
import os
import sys
import threading
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
def clean_stats(tmp_path, monkeypatch):
    """隔离统计快照、下载媒体、书库及收藏，绝不改写用户数据目录。"""
    # Statistics verification now includes favorites off the shelf. Never let a
    # test read or mutate the user's real favorites file: use an isolated empty
    # fixture unless a test explicitly supplies its own favorite collection.
    monkeypatch.setattr(st, "MANGA_FAV_FILE", str(tmp_path / "favorites.json"))
    manga_root = tmp_path / "manga"
    downloads_root = manga_root / "downloads"
    library_file = manga_root / "_library.json"
    stats_file = manga_root / "_library_stats.json"
    monkeypatch.setattr(st, "MANGA_DIR", str(manga_root))
    monkeypatch.setattr(st, "MANGA_DOWNLOADS_DIR", str(downloads_root))
    monkeypatch.setattr(st, "MANGA_CACHE_DIR", str(manga_root / "_cache"))
    monkeypatch.setattr(st, "MANGA_LIBRARY_FILE", str(library_file))
    monkeypatch.setattr(st, "MANGA_LIBRARY_STATS_FILE", str(stats_file))
    monkeypatch.setattr(sys.modules[__name__], "MANGA_DOWNLOADS_DIR",
                        str(downloads_root))
    monkeypatch.setattr(sys.modules[__name__], "MANGA_LIBRARY_FILE",
                        str(library_file))
    import server.manga_api as manga_api
    monkeypatch.setattr(manga_api, "MANGA_DIR", str(manga_root))
    monkeypatch.setattr(manga_api, "MANGA_DOWNLOADS_DIR", str(downloads_root))
    monkeypatch.setattr(manga_api, "MANGA_CACHE_DIR", str(manga_root / "_cache"))
    monkeypatch.setattr(manga_api, "MANGA_FAV_FILE", str(tmp_path / "favorites.json"))
    monkeypatch.setattr(manga_api, "MANGA_LIBRARY_FILE", str(library_file))
    library_file.parent.mkdir(parents=True, exist_ok=True)
    library_file.write_text("[]", encoding="utf-8")
    with open(st.MANGA_FAV_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f)
    with st._manga_stats_lock:
        st._manga_lib_stats.clear()
        st._manga_stats_pending.clear()
        st._manga_lib_rev = 0
    yield
    with st._manga_stats_lock:
        st._manga_lib_stats.clear()
        st._manga_stats_pending.clear()
        st._manga_lib_rev = 0


def _mk_comic(source, cid, chapters):
    """造本地漫画：chapters = {章节id: 图片数}"""
    base = os.path.join(MANGA_DOWNLOADS_DIR, source, cid)
    for ch, n in chapters.items():
        d = os.path.join(base, ch)
        os.makedirs(d, exist_ok=True)
        for i in range(n):
            with open(os.path.join(d, f"{i:04d}.webp"), "wb") as f:
                f.write(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"x" * 4)
    return base


def test_stats_count_real_supported_image_formats_and_ignore_fake_images(
        tmp_path, monkeypatch):
    """书库快照必须与真实下载识别共用图片魔数口径，不按扩展名猜。"""
    import server.state as state

    downloads, cache = tmp_path / "downloads", tmp_path / "cache"
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(cache))
    chapter = downloads / "statsfmt" / "comic" / "ch1"
    chapter.mkdir(parents=True)
    images = {
        "0000.webp": b"RIFF\x00\x00\x00\x00WEBPdata",
        "0001.jpeg": b"\xff\xd8\xff" + b"jpeg-data!",
        "0002.gif": b"GIF89a" + b"gif-data!",
        "0003.avif": b"\x00\x00\x00\x18ftypavif" + b"avif-data",
        "0004.jpg": b"<html>blocked</html>",
        "0005.png": b"not-an-image",
        "notes.txt": b"ignore",
    }
    for name, data in images.items():
        (chapter / name).write_bytes(data)

    assert state._manga_count_comic("statsfmt", "comic") == (4, 1)


def test_stats_count_unique_readable_pages_not_duplicate_file_formats(
        tmp_path, monkeypatch):
    """A JPG/WebP copy with one page index is one readable page, not two."""
    import server.state as state

    downloads, cache = tmp_path / "downloads", tmp_path / "cache"
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(cache))
    base = downloads / "statsfmt" / "comic"
    chapter = base / "chapter-1"
    chapter.mkdir(parents=True)
    (chapter / "0000.jpg").write_bytes(b"\xff\xd8\xff" + b"a" * 128)
    (chapter / "0000.webp").write_bytes(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"a" * 128)
    (chapter / "0001.webp").write_bytes(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"b" * 128)
    (base / "_info.json").write_text(json.dumps({"chapters": [
        {"id": "chapter-1", "name": "第1话", "download_page_count": 2},
    ]}), encoding="utf-8")

    assert state._manga_scan_comic("statsfmt", "comic") == (2, 1, 1)


def test_download_manifest_page_count_precedes_stale_cache_manifest(
        tmp_path, monkeypatch):
    """A stale cache count cannot make a complete download disappear from shelf stats."""
    import server.state as state

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fixture_path = os.path.join(
        root, "android/app/src/test/resources/"
        "manga_download_manifest_precedence.json")
    with open(fixture_path, encoding="utf-8") as handle:
        cases = json.load(handle)["cases"]
    assert cases, "shared download-manifest fixture must not be empty"

    downloads, cache = tmp_path / "downloads", tmp_path / "cache"
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(cache))
    for case in cases:
        source, comic_id, chapter_id = (
            case["source"], case["comic_id"], case["chapter_id"])
        download_comic = downloads / source / comic_id
        chapter_dir = download_comic / chapter_id
        chapter_dir.mkdir(parents=True)
        for index in case["page_indices"]:
            (chapter_dir / f"{index:04d}.jpg").write_bytes(
                b"\xff\xd8\xff" + bytes([index]) * 128)
        (download_comic / "_info.json").write_text(json.dumps({"chapters": [{
            "id": chapter_id, "name": case["chapter_name"],
            "download_page_count": case["downloads_expected_page_count"],
        }]}), encoding="utf-8")
        cache_comic = cache / source / comic_id
        cache_comic.mkdir(parents=True)
        (cache_comic / "_info.json").write_text(json.dumps({"chapters": [{
            "id": chapter_id, "name": case["chapter_name"],
            "download_page_count": case["cache_expected_page_count"],
        }]}), encoding="utf-8")

        assert state._scan_downloaded_chapters(source, comic_id) == [chapter_id]
        assert state._manga_scan_comic(source, comic_id) == (2, 1, 1)


def test_old_physical_file_count_snapshot_is_invalidated_for_background_rebuild(
        tmp_path, monkeypatch):
    """Never serve a legacy file-count snapshot as the new logical page count."""
    import server.state as state

    stats_file = tmp_path / "legacy-stats.json"
    monkeypatch.setattr(state, "MANGA_LIBRARY_STATS_FILE", str(stats_file))
    key = state._manga_stats_skey("copymanga", "comic")
    stats_file.write_text(json.dumps({"rev": 17, "entries": {
        key: {"images": 34, "chapters": 1, "verified_chapters": 1, "ts": 9},
    }}), encoding="utf-8")
    with state._manga_stats_lock:
        state._manga_lib_stats.clear()
        state._manga_stats_pending.clear()
        state._manga_lib_rev = 0

    state._manga_stats_load()

    assert state._manga_lib_stats == {}
    assert state._manga_stats_pending == {key}
    assert state._manga_lib_rev == 17
    with state._manga_stats_lock:
        state._manga_stats_save_locked()
    saved = json.loads(stats_file.read_text(encoding="utf-8"))
    assert saved["image_count_semantics"] == "unique-readable-pages-v1"


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
            f.write(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"x" * 4)
        # 核对需要条目"不新鲜"——把 ts 拨旧
        with st._manga_stats_lock:
            st._manga_lib_stats[st._manga_stats_skey(_SRC, _CID)]["ts"] = \
                time.time() - 2 * st._MANGA_VERIFY_INTERVAL
        changed = st._manga_stats_verify_once()
        assert changed >= 1
        assert st._manga_stats_get(_SRC, _CID)["images"] == 3

    def test_stale_background_scan_cannot_overwrite_download_completion_snapshot(
            self, clean_stats, monkeypatch):
        """A verifier started before download completion must not publish last."""
        base = _mk_comic(_SRC, _CID, {"ch1": 1})
        atomic_write(MANGA_LIBRARY_FILE, _lib_entry(images=1, chapters=1))
        st._manga_stats_note_change(_SRC, _CID)
        with st._manga_stats_lock:
            st._manga_lib_stats[st._manga_stats_skey(_SRC, _CID)]["ts"] = (
                time.time() - 2 * st._MANGA_VERIFY_INTERVAL)

        scan_started = threading.Event()
        release_scan = threading.Event()
        real_compute = st._manga_stats_compute_entry
        stale_snapshot = {"images": 1, "chapters": 1,
                          "verified_chapters": 1, "ts": time.time()}

        def delayed_compute(source, comic_id):
            if threading.current_thread().name == "stale-library-verifier":
                scan_started.set()
                assert release_scan.wait(5), "test did not release stale scan"
                return dict(stale_snapshot)
            return real_compute(source, comic_id)

        monkeypatch.setattr(st, "_manga_stats_compute_entry", delayed_compute)
        verifier = threading.Thread(target=st._manga_stats_verify_once,
                                    name="stale-library-verifier")
        verifier.start()
        assert scan_started.wait(5), "background verifier did not reach disk scan"

        with open(os.path.join(base, "ch1", "0001.webp"), "wb") as image:
            image.write(b"RIFF\x00\x00\x00\x00WEBP" + b"new!" * 32)
        st._manga_stats_note_change(_SRC, _CID)
        assert st._manga_stats_get(_SRC, _CID)["images"] == 2

        release_scan.set()
        verifier.join(5)
        assert not verifier.is_alive(), "background verifier failed to finish"
        assert st._manga_stats_get(_SRC, _CID)["images"] == 2, (
            "a scan begun before download completion must not roll back fresh stats")

    def test_verify_prunes_orphan_entries(self, clean_stats):
        """书库记录被外部删除的条目，核同步摘除"""
        _mk_comic(_SRC, _CID, {"ch1": 1})
        st._manga_stats_note_change(_SRC, _CID)
        atomic_write(MANGA_LIBRARY_FILE, [])   # 外部清空书库
        st._manga_stats_verify_once()
        assert st._manga_stats_get(_SRC, _CID) is None

    def test_verified_chapter_count_excludes_partial_media_but_counts_complete_pages(
            self, tmp_path, monkeypatch):
        """图片/目录数不是本地阅读可用数：必须按下载页数校验完整章节。"""
        downloads, cache = tmp_path / "downloads", tmp_path / "cache"
        monkeypatch.setattr(st, "MANGA_DOWNLOADS_DIR", str(downloads))
        monkeypatch.setattr(st, "MANGA_CACHE_DIR", str(cache))
        base = downloads / "verified" / "comic"
        chapter = base / "chapter-1"
        chapter.mkdir(parents=True)
        (chapter / "0000.jpg").write_bytes(b"\xff\xd8\xff" + b"a" * 128)
        (base / "_info.json").write_text(json.dumps({"chapters": [
            {"id": "chapter-1", "name": "第1话", "download_page_count": 2},
        ]}), encoding="utf-8")

        assert st._manga_scan_comic("verified", "comic") == (1, 1, 0)
        (chapter / "0001.jpg").write_bytes(b"\xff\xd8\xff" + b"b" * 128)
        assert st._manga_scan_comic("verified", "comic") == (2, 1, 1)

    def test_verified_count_requires_download_manifest_for_legacy_cache(
            self, tmp_path, monkeypatch):
        downloads, cache = tmp_path / "downloads", tmp_path / "cache"
        monkeypatch.setattr(st, "MANGA_DOWNLOADS_DIR", str(downloads))
        monkeypatch.setattr(st, "MANGA_CACHE_DIR", str(cache))
        chapter = cache / "verified" / "comic" / "chapter-1"
        chapter.mkdir(parents=True)
        (chapter / "0000.jpg").write_bytes(b"\xff\xd8\xff" + b"a" * 128)
        # An ordinary reader cache is visible in occupancy totals but is not a
        # locally downloaded reading unit without a legacy download manifest.
        assert st._manga_scan_comic("verified", "comic") == (1, 1, 0)
        (chapter.parent / "_info.json").write_text(json.dumps({"chapters": [
            {"id": "chapter-1", "name": "第1话"},
        ]}), encoding="utf-8")
        assert st._manga_scan_comic("verified", "comic") == (1, 1, 1)

    def test_scan_merges_unicode_equivalent_chapters_across_copy_aliases(
            self, tmp_path, monkeypatch):
        """Shelf stats must share detail identity normalization for legacy aliases."""
        downloads, cache = tmp_path / "downloads", tmp_path / "cache"
        monkeypatch.setattr(st, "MANGA_DOWNLOADS_DIR", str(downloads))
        monkeypatch.setattr(st, "MANGA_CACHE_DIR", str(cache))
        comic = "comic"
        download_base = downloads / "copymanga" / comic
        cache_base = cache / "copymanga_web" / comic
        for base, chapter_id, title, marker in (
                (download_base, "app-id", "第１话　开 始", b"a"),
                (cache_base, "web-id", "第1话 开 始", b"b")):
            chapter = base / chapter_id
            chapter.mkdir(parents=True)
            (chapter / "0000.jpg").write_bytes(b"\xff\xd8\xff" + marker * 128)
            (base / "_info.json").write_text(json.dumps({"chapters": [
                {"id": chapter_id, "name": title, "download_page_count": 1},
            ]}), encoding="utf-8")

        # The cached chapter is recognized as a legacy downloaded chapter by its
        # manifest; equivalent titles and page paths denote one logical chapter.
        assert st._manga_scan_comic("copymanga", comic) == (1, 1, 1)

    def test_scan_keeps_unicode_normalization_ambiguity_conservative(
            self, tmp_path, monkeypatch):
        """Normalization must not collapse genuinely duplicated chapter titles."""
        downloads = tmp_path / "downloads"
        monkeypatch.setattr(st, "MANGA_DOWNLOADS_DIR", str(downloads))
        monkeypatch.setattr(st, "MANGA_CACHE_DIR", str(tmp_path / "cache"))
        base = downloads / "jm" / "comic"
        rows = []
        for chapter_id, title in (("one", "第１话"), ("two", "第1话")):
            chapter = base / chapter_id
            chapter.mkdir(parents=True)
            (chapter / "0000.jpg").write_bytes(b"\xff\xd8\xff" + chapter_id.encode() * 64)
            rows.append({"id": chapter_id, "name": title, "download_page_count": 1})
        (base / "_info.json").write_text(json.dumps({"chapters": rows}), encoding="utf-8")

        # Ambiguous labels fall back to source-qualified IDs; count both rather
        # than silently deduplicating separate real chapters.
        assert st._manga_scan_comic("jm", "comic") == (2, 2, 2)

    def test_stats_verifier_keeps_favorites_after_shelf_removal(
            self, tmp_path, monkeypatch):
        """Favorite media remains in the snapshot set when the shelf link is removed."""
        downloads, cache = tmp_path / "downloads", tmp_path / "cache"
        library_file = tmp_path / "library.json"
        favorites_file = tmp_path / "favorites.json"
        stats_file = tmp_path / "stats.json"
        monkeypatch.setattr(st, "MANGA_DOWNLOADS_DIR", str(downloads))
        monkeypatch.setattr(st, "MANGA_CACHE_DIR", str(cache))
        monkeypatch.setattr(st, "MANGA_LIBRARY_FILE", str(library_file))
        monkeypatch.setattr(st, "MANGA_FAV_FILE", str(favorites_file))
        monkeypatch.setattr(st, "MANGA_LIBRARY_STATS_FILE", str(stats_file))
        library_file.write_text("[]", encoding="utf-8")
        favorites_file.write_text(json.dumps({"verified:comic": {"title": "保留收藏"}}),
                                  encoding="utf-8")
        chapter = downloads / "verified" / "comic" / "chapter-1"
        chapter.mkdir(parents=True)
        (chapter / "0000.jpg").write_bytes(b"\xff\xd8\xff" + b"a" * 128)
        (chapter.parent / "_info.json").write_text(json.dumps({"chapters": [
            {"id": "chapter-1", "name": "第1话", "download_page_count": 1},
        ]}), encoding="utf-8")

        with st._manga_stats_lock:
            old_stats = dict(st._manga_lib_stats)
            old_pending = set(st._manga_stats_pending)
            old_revision = st._manga_lib_rev
            st._manga_lib_stats.clear()
            st._manga_stats_pending.clear()
            st._manga_lib_rev = 0
        try:
            assert st._manga_stats_verify_once() == 1
            snapshot = st._manga_stats_get("verified", "comic")
            assert snapshot["verified_chapters"] == 1
            assert json.loads(library_file.read_text(encoding="utf-8")) == []
        finally:
            with st._manga_stats_lock:
                st._manga_lib_stats.clear()
                st._manga_lib_stats.update(old_stats)
                st._manga_stats_pending.clear()
                st._manga_stats_pending.update(old_pending)
                st._manga_lib_rev = old_revision


class TestLibraryEndpoint:
    def test_favorites_expose_verified_local_catalog_state(
            self, client, monkeypatch, tmp_path):
        import server.manga_api as mapi

        favorites_file = tmp_path / "favorites.json"
        history_file = tmp_path / "history.json"
        favorites_file.write_text(json.dumps({
            "copymanga:local": {"title": "本地作品", "transport_source": "copymanga_web"},
            "mangadex:online": {"title": "在线作品"},
            "jm:unknown": {"title": "核验中"},
        }), encoding="utf-8")
        history_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(favorites_file))
        monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history_file))
        monkeypatch.setattr(mapi, "MANGA_CACHE_DIR", str(tmp_path / "cache"))
        pending = []

        def stats(source, comic_id):
            return {
                ("copymanga", "local"): {"verified_chapters": 2},
                ("mangadex", "online"): {"verified_chapters": 0},
            }.get((source, comic_id))

        monkeypatch.setattr(mapi, "_manga_stats_get", stats)
        monkeypatch.setattr(mapi, "_manga_stats_request",
                            lambda source, cid: pending.append((source, cid)))

        response = client.get("/api/manga/favorites")
        assert response.status_code == 200
        favorites = {item["comic_id"]: item for item in response.get_json()["favorites"]}
        assert favorites["local"]["local_downloaded_chapters"] == 2
        assert favorites["local"]["local_catalog_pending"] is False
        assert favorites["online"]["local_downloaded_chapters"] == 0
        assert favorites["online"]["local_catalog_pending"] is False
        assert favorites["unknown"]["local_downloaded_chapters"] is None
        assert favorites["unknown"]["local_catalog_pending"] is True
        assert ("jm", "unknown") in pending

    def test_library_keeps_same_comic_id_from_unrelated_sources_separate(
            self, client, clean_stats, monkeypatch, tmp_path):
        """Source is part of manga identity; only known aliases may collapse."""
        import server.manga_api as mapi
        library_file = tmp_path / "library-identities.json"
        history_file = tmp_path / "history-identities.json"
        history_file.write_text("{}", encoding="utf-8")
        entries = [
            {"source": "source_a", "comic_id": "same-id", "title": "作品甲",
             "status": "done", "images": 2, "chapters": 1},
            {"source": "source_b", "comic_id": "same-id", "title": "作品乙",
             "status": "done", "images": 3, "chapters": 1},
            {"source": "copymanga", "comic_id": "copy-1", "title": "拷贝漫画",
             "status": "done", "images": 2, "chapters": 1},
            {"source": "copymanga_web", "comic_id": "copy-1", "title": "",
             "status": "done", "images": 5, "chapters": 2},
            {"source": "jm", "comic_id": "JM559440", "title": "禁漫",
             "status": "done", "images": 1, "chapters": 1},
            {"source": "jm", "comic_id": "559440", "title": "",
             "status": "done", "images": 4, "chapters": 2},
        ]
        atomic_write(str(library_file), entries)
        monkeypatch.setattr(mapi, "MANGA_LIBRARY_FILE", str(library_file))
        monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history_file))
        monkeypatch.setattr(mapi, "_manga_stats_get", lambda source, _cid: {
            "images": {"copymanga": 2, "copymanga_web": 5}.get(source, 1),
            "chapters": 1, "alt_source": None, "alt_images": 0,
        })
        monkeypatch.setattr(mapi, "_manga_stats_request", lambda *_: None)

        response = client.get("/api/manga/library")
        assert response.status_code == 200
        comics = response.get_json()["comics"]
        by_key = {(comic["source"], comic["comic_id"]): comic for comic in comics}
        assert len(comics) == 4
        assert ("source_a", "same-id") in by_key
        assert ("source_b", "same-id") in by_key
        assert ("copymanga_web", "copy-1") in by_key
        assert ("jm", "559440") in by_key
        assert by_key[("copymanga_web", "copy-1")]["title"] == "拷贝漫画"

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
            assert ent["local_downloaded_chapters"] == 2
            assert ent["local_catalog_pending"] is False
            assert "rev" in d   # 轻量快照附全局 revision
        assert calls["walk"] == 0, f"请求路径仍在 os.walk: {calls}"
        assert calls["listdir"] == 0, f"请求路径仍在 listdir: {calls}"

    @pytest.mark.parametrize("media_present", [True, False])
    def test_missing_snapshot_falls_back_without_scan(self, client, clean_stats,
                                                      monkeypatch, media_present):
        """快照未建：不把历史累计图片数冒充实盘，并排队核对"""
        _mk_comic(_SRC, _CID, {"ch1": 3, "ch2": 2})
        if not media_present:
            import shutil
            shutil.rmtree(os.path.join(MANGA_DOWNLOADS_DIR, _SRC, _CID))
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
        assert ent["local_images"] == 0      # 未核验不声称本地文件数
        assert ent["local_scan_pending"] is True
        assert ent["local_downloaded_chapters"] is None
        assert ent["local_catalog_pending"] is True
        assert ent["images"] == 5            # 原索引累计值仍保留作历史信息
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

    @pytest.mark.parametrize("media_present", [True, False])
    def test_legacy_library_without_status_is_classified_after_scan(
            self, client, clean_stats, media_present):
        """旧索引无 status 时实扫应推导下载态，并保留缺失内容供恢复。"""
        if media_present:
            _mk_comic(_SRC, _CID, {"ch1": 2})
        legacy = _lib_entry()[0]
        legacy.pop("status")
        atomic_write(MANGA_LIBRARY_FILE, [legacy])

        assert st._manga_stats_verify_once() == 1
        response = client.get("/api/manga/library")
        assert response.status_code == 200
        comics = response.get_json()["comics"]
        assert len(comics) == 1, "旧版书库记录不能在统计核对后被误清理"
        ent = comics[0]
        assert ent["local_scan_pending"] is False
        assert ent["status"] == "done"
        if media_present:
            assert ent["local_images"] == 2
            assert ent["local_downloaded_chapters"] == 1
            assert ent["local_catalog_pending"] is False
            assert "missing_images" not in ent
        else:
            assert ent["local_images"] == 0
            assert ent["local_downloaded_chapters"] == 0
            assert ent["local_catalog_pending"] is False
            assert ent["missing_images"] is True

    def test_shelf_removal_preserves_download_stats_for_favorites(self, client, clean_stats):
        _mk_comic(_SRC, _CID, {"ch1": 2})
        atomic_write(MANGA_LIBRARY_FILE, _lib_entry())
        st._manga_stats_note_change(_SRC, _CID)
        rev0 = st.manga_library_revision()
        r = client.delete(f"/api/manga/library/{_SRC}/{_CID}")
        assert r.status_code == 200
        snapshot = st._manga_stats_get(_SRC, _CID)
        assert snapshot is not None
        assert snapshot["verified_chapters"] == 1
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
