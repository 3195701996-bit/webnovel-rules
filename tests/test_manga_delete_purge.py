# -*- coding: utf-8 -*-
"""书架移除、下载内容删除和用户阅读数据必须彼此独立（离线回归）

背景（2026-09-15）：实测在手机/模拟器上"从书库移除"后，`manga/downloads/<源>/<id>/`
里 83 张图（14MB）仍在，而 App 里没有别的入口能删掉它们——手机存储宝贵，这是真实缺口。

口径：
  · DELETE /library 只移除书架记录，下载文件、缓存、历史和收藏不变；
  · DELETE /downloads 只删除本地媒体，阅读历史和收藏不变；书架保留可恢复条目；
  · 旧式在 /library 上传 files 参数会被拒绝，避免把两种操作重新混为一谈。
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
    for path, payload in (
            (cfg.MANGA_HISTORY_FILE, {f"{source}:{comic_id}": {
                "chapter_id": "ch1", "chapter_label": "第1话",
                "read_chapter_ids": ["ch1"]}}),
            (cfg.MANGA_FAV_FILE, {f"{source}:{comic_id}": {"title": "自检漫画"}})):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        current = {}
        if os.path.exists(path):
            try:
                current = json.load(open(path, encoding="utf-8"))
            except Exception:
                current = {}
        current.update(payload)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False)
    d = os.path.join(cfg.MANGA_DOWNLOADS_DIR, source, comic_id, "ch1")
    os.makedirs(d, exist_ok=True)
    for i in range(n):
        with open(os.path.join(d, "%04d.jpg" % i), "wb") as f:
            f.write(b"\xff\xd8\xff" + b"x" * 2048)
    return os.path.join(cfg.MANGA_DOWNLOADS_DIR, source, comic_id)


def _clean(source="mangadex", comic_id="cid1"):
    import shutil
    shutil.rmtree(os.path.join(cfg.MANGA_DOWNLOADS_DIR, source, comic_id),
                  ignore_errors=True)
    shutil.rmtree(os.path.join(cfg.MANGA_CACHE_DIR, source, comic_id),
                  ignore_errors=True)
    for path in (cfg.MANGA_HISTORY_FILE, cfg.MANGA_FAV_FILE):
        if os.path.exists(path):
            try:
                data = json.load(open(path, encoding="utf-8"))
                data.pop(f"{source}:{comic_id}", None)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
            except Exception:
                pass
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


def test_library_remove_preserves_downloads_history_and_favorite(client):
    """从书架移除不级联删除漫画内容或用户数据。"""
    d = _make_comic()
    try:
        r = client.delete("/api/manga/library/mangadex/cid1")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] and body["files_preserved"] and body["history_preserved"]
        assert os.path.isdir(d), "默认不应删除已下载文件"
        lib = json.load(open(cfg.MANGA_LIBRARY_FILE, encoding="utf-8"))
        assert not [x for x in lib if x.get("comic_id") == "cid1"], "记录必须被移除"
        history = json.load(open(cfg.MANGA_HISTORY_FILE, encoding="utf-8"))
        favorites = json.load(open(cfg.MANGA_FAV_FILE, encoding="utf-8"))
        assert "mangadex:cid1" in history
        assert "mangadex:cid1" in favorites
    finally:
        _clean()


def test_dedicated_download_delete_preserves_history_and_favorite(client):
    """专用内容删除接口释放文件，但不动历史、收藏或书架关系。"""
    d = _make_comic(n=3)
    cache = os.path.join(cfg.MANGA_CACHE_DIR, "mangadex", "cid1")
    chapter_cache = os.path.join(cache, "ch1")
    os.makedirs(chapter_cache, exist_ok=True)
    with open(os.path.join(chapter_cache, "0001.jpg"), "wb") as f:
        f.write(b"c" * 1024)
    with open(os.path.join(cache, "ch1_imgs.json"), "w", encoding="utf-8") as f:
        json.dump({"v": 2, "urls": ["https://example.test/image"]}, f)
    catalog_path = os.path.join(cache, "_info_full.json")
    with open(catalog_path, "w", encoding="utf-8") as f:
        json.dump({"data": {"title": "自检漫画"}}, f)
    try:
        r = client.delete("/api/manga/mangadex/cid1/downloads")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert "mangadex" in body["removed"], body
        assert body["freed_bytes"] > 5000, \
            f"应回报释放字节数，实际 {body['freed_bytes']}"
        assert not os.path.isdir(d), "files=1 后 downloads 目录必须消失"
        assert not os.path.exists(chapter_cache)
        assert not os.path.exists(os.path.join(cache, "ch1_imgs.json"))
        assert os.path.exists(catalog_path), "删媒体不能顺带清目录元数据"
        assert body["freed_bytes"] >= 3 * 2048 + 1024
        assert body["history_preserved"] and body["favorite_preserved"]
        history = json.load(open(cfg.MANGA_HISTORY_FILE, encoding="utf-8"))
        favorites = json.load(open(cfg.MANGA_FAV_FILE, encoding="utf-8"))
        library = json.load(open(cfg.MANGA_LIBRARY_FILE, encoding="utf-8"))
        assert "mangadex:cid1" in history and "mangadex:cid1" in favorites
        assert any(x.get("comic_id") == "cid1" for x in library)
    finally:
        _clean()


def test_legacy_files_flag_is_rejected_by_library_remove(client):
    """破坏性内容删除必须通过明确的专用接口。"""
    d = _make_comic(comic_id="cid2")
    try:
        r = client.delete("/api/manga/library/mangadex/cid2", json={"files": True})
        assert r.status_code == 400
        assert os.path.isdir(d)
        lib = json.load(open(cfg.MANGA_LIBRARY_FILE, encoding="utf-8"))
        assert any(x.get("comic_id") == "cid2" for x in lib), \
            "拒绝破坏性旧参数前不能先移除书架记录"
    finally:
        _clean(comic_id="cid2")


def test_library_remove_does_not_orphan_running_download(client, monkeypatch):
    import server.manga_api as mapi
    monkeypatch.setattr(mapi._manga_dl, "status", lambda _key: {"status": "running"})
    r = client.delete("/api/manga/library/mangadex/cid-running")
    assert r.status_code == 409
    assert "下载" in r.get_json()["error"]


def test_download_content_delete_rejects_queued_task(client, monkeypatch):
    import server.manga_api as mapi
    monkeypatch.setattr(mapi._manga_dl, "begin_media_delete", lambda _key: False)
    r = client.delete("/api/manga/mangadex/cid-queued/downloads")
    assert r.status_code == 409


def test_delete_partial_legacy_chapter_uses_same_normalized_identity_as_status(
        client, tmp_path, monkeypatch):
    """部分缓存按 NFKC 章节名映射后，管理删除也必须找到旧别名目录。"""
    import server.manga_api as mapi

    source, comic_id = "copymanga", "partial-delete-identity"
    downloads = tmp_path / "downloads"
    cache = tmp_path / "cache"
    old_base = downloads / "copymanga_web" / comic_id
    old_chapter = old_base / "legacy-chapter-id"
    old_chapter.mkdir(parents=True)
    (old_chapter / "0000.jpg").write_bytes(b"\xff\xd8\xff" + b"page" * 128)
    (old_base / "_info.json").write_text(json.dumps({"chapters": [{
        "id": "legacy-chapter-id", "name": "第１話",
    }]}), encoding="utf-8")
    current_cache = cache / source / comic_id
    current_cache.mkdir(parents=True)
    (current_cache / "_info_full.json").write_text(json.dumps({"data": {
        "chapters": [{"id": "current-chapter-id", "name": "第1話"}],
    }}), encoding="utf-8")

    monkeypatch.setattr(mapi, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(mapi, "MANGA_CACHE_DIR", str(cache))
    monkeypatch.setattr(mapi, "_manga_stats_request", lambda *_args: None)
    response = client.post(
        f"/api/manga/{source}/{comic_id}/chapters/delete",
        json={"chapter_ids": ["current-chapter-id"]})
    assert response.status_code == 200, response.get_data(as_text=True)
    result = response.get_json()
    assert result["deleted"] == 1 and result["missing"] == 0
    assert not old_chapter.exists()


def test_resume_reports_conflict_during_media_delete(client, monkeypatch):
    import server.manga_api as mapi
    monkeypatch.setattr(mapi._manga_dl, "resume_result", lambda _key: "deleting")
    r = client.post("/api/manga/download/resume?source=mangadex&cid=cid-reserved")
    assert r.status_code == 409
    assert "error" in r.get_json()


def test_resume_missing_task_is_idempotent_noop(client, monkeypatch):
    import server.manga_api as mapi
    monkeypatch.setattr(mapi._manga_dl, "resume_result", lambda _key: "no_task")
    r = client.post("/api/manga/download/resume?source=mangadex&cid=missing-task")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "resumed": False, "reason": "no_task"}


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
        r = client.delete("/api/manga/mangadex/nofiles/downloads")
        assert r.status_code == 200
        body = r.get_json()
        assert body["removed"] == [] and body["freed_bytes"] == 0
    finally:
        _clean(comic_id="nofiles")


def test_download_delete_retry_converges_after_partial_filesystem_failure(
        client, tmp_path, monkeypatch):
    import shutil
    import server.manga_api as mapi

    comic_id = "cid-partial-delete"
    download_dir = tmp_path / "downloads" / "mangadex" / comic_id
    download_chapter = download_dir / "ch1"
    download_chapter.mkdir(parents=True)
    download_page = download_chapter / "0000.jpg"
    download_page.write_bytes(b"d" * 4096)

    cache_dir = tmp_path / "cache" / "mangadex" / comic_id
    cache_chapter = cache_dir / "ch1"
    cache_chapter.mkdir(parents=True)
    cache_page = cache_chapter / "0000.jpg"
    cache_page.write_bytes(b"c" * 2048)
    cache_page_size = cache_page.stat().st_size
    image_index = cache_dir / "ch1_imgs.json"
    image_index.write_text('{"urls": []}', encoding="utf-8")
    download_page_size = download_page.stat().st_size
    image_index_size = image_index.stat().st_size
    library = [{"source": "mangadex", "comic_id": comic_id, "title": "保留书架记录"}]
    library_path = tmp_path / "_library.json"
    library_path.write_text(json.dumps(library, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(mapi, "MANGA_DOWNLOADS_DIR", str(tmp_path / "downloads"))
    monkeypatch.setattr(mapi, "MANGA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(mapi._manga_dl, "begin_media_delete", lambda _key: True)
    monkeypatch.setattr(mapi._manga_dl, "end_media_delete", lambda _key: None)
    monkeypatch.setattr(mapi._manga_dl, "delete", lambda _key: None)
    stats_updates = []
    monkeypatch.setattr(
        mapi, "_manga_stats_note_change",
        lambda _source, _comic, removed=False: stats_updates.append(removed),
    )

    real_rmtree = shutil.rmtree
    real_remove = os.remove
    failed_download_once = [True]
    failed_index_once = [True]

    def controlled_rmtree(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(download_dir) and failed_download_once[0]:
            failed_download_once[0] = False
            raise PermissionError("fixture: download folder is locked")
        return real_rmtree(path, *args, **kwargs)

    def controlled_remove(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(image_index) and failed_index_once[0]:
            failed_index_once[0] = False
            raise PermissionError("fixture: index file is locked")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", controlled_rmtree)
    monkeypatch.setattr(os, "remove", controlled_remove)

    response = client.delete(f"/api/manga/mangadex/{comic_id}/downloads")

    assert response.status_code == 500
    body = response.get_json()
    assert body["ok"] is False and body["partial"] is True
    assert body["failed_count"] == 2
    assert body["freed_bytes"] == cache_page_size
    assert "mangadex:downloads" in body["failed_scopes"]
    assert "mangadex:image-index" in body["failed_scopes"]
    assert body["history_preserved"] and body["favorite_preserved"]
    assert download_page.is_file(), "failed download deletion must preserve the file"
    assert image_index.is_file(), \
        "failed cache-index deletion must be reported and preserved"
    assert not cache_page.exists(), "successfully deleted cache pages stay deleted"
    assert stats_updates == [False, False, False], \
        "partial cleanup must rescan remaining files rather than drop stats"
    assert json.loads(library_path.read_text(encoding="utf-8")) == library

    # Retrying the same operation after the transient locks disappear must finish
    # the remaining deletions without touching the library relationship.
    retry = client.delete(f"/api/manga/mangadex/{comic_id}/downloads")
    assert retry.status_code == 200
    retry_body = retry.get_json()
    assert retry_body["ok"] is True and retry_body["removed"] == ["mangadex"]
    assert retry_body["freed_bytes"] == download_page_size + image_index_size
    assert not download_dir.exists() and not image_index.exists()
    assert json.loads(library_path.read_text(encoding="utf-8")) == library
    assert stats_updates[-3:] == [True, True, True]

    # A third retry is idempotent and reports zero additional bytes.
    repeated = client.delete(f"/api/manga/mangadex/{comic_id}/downloads")
    assert repeated.status_code == 200
    assert repeated.get_json()["freed_bytes"] == 0
    assert stats_updates[-3:] == [True, True, True]
