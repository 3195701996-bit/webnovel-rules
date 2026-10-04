"""书架的已下载身份不能被普通在线阅读缓存冒充。"""
import json


def _page(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "0000.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 128)


def test_download_scan_excludes_untracked_read_cache_but_keeps_downloads(
        tmp_path, monkeypatch):
    import server.state as state

    downloads = tmp_path / "downloads"
    cache = tmp_path / "cache"
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(cache))
    comic = "comic"
    _page(cache / "jm" / comic / "read-cache-only")
    _page(downloads / "jm" / comic / "downloaded")

    assert set(state._scan_downloaded_chapters("jm", comic)) == {"downloaded"}
    assert state._downloaded_ids_for_chapters("jm", comic, [
        {"id": "read-cache-only", "name": "第1话"},
        {"id": "downloaded", "name": "第2话"},
    ]) == {"downloaded"}


def test_download_scan_reuses_verified_pages_and_invalidates_after_change(
        tmp_path, monkeypatch):
    import server.state as state

    downloads = tmp_path / "downloads"
    cache = tmp_path / "cache"
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(cache))
    chapter = downloads / "jm" / "comic" / "chapter-1"
    _page(chapter)
    state._invalidate_manga_download_scan("jm", "comic")
    original_check = state._is_manga_image_file
    checks = 0

    def count_checks(path):
        nonlocal checks
        checks += 1
        return original_check(path)

    monkeypatch.setattr(state, "_is_manga_image_file", count_checks)

    assert state._scan_downloaded_chapters("jm", "comic") == ["chapter-1"]
    # A repeated detail/status read reuses the image-header verification.
    assert state._scan_downloaded_chapters("jm", "comic") == ["chapter-1"]
    assert checks == 1

    # Nested page changes alter the chapter directory timestamp and are detected
    # immediately after the same invalidation path used by download/delete APIs.
    (chapter / "0000.jpg").unlink()
    state._invalidate_manga_download_scan("jm", "comic")
    assert state._scan_downloaded_chapters("jm", "comic") == []


def test_download_scan_fingerprint_is_constant_cost_for_large_catalog(
        tmp_path, monkeypatch):
    """The cache-hit fingerprint must not stat/list every chapter directory."""
    import server.state as state

    downloads = tmp_path / "downloads"
    cache = tmp_path / "cache"
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(cache))
    base = downloads / "jm" / "comic"
    for index in range(1_000):
        _page(base / f"chapter-{index:04d}")
    state._invalidate_manga_download_scan("jm", "comic")

    original_scandir = state.os.scandir
    scanned = []

    def count_scandir(path):
        scanned.append(str(path))
        return original_scandir(path)

    monkeypatch.setattr(state.os, "scandir", count_scandir)
    expected = state._manga_download_scan_fingerprint("jm", "comic")
    # The only list operation in the fingerprint may be on the comic root; it
    # must not enumerate chapter roots on every request.
    assert len(expected) <= 4
    assert scanned == []

    # The actual first scan still verifies every chapter, then a repeat must
    # take the constant-cost fingerprint path and reuse those verified IDs.
    first = state._scan_downloaded_chapters("jm", "comic")
    scans_after_first = len(scanned)
    second = state._scan_downloaded_chapters("jm", "comic")
    assert len(first) == 1_000
    assert second == first
    assert len(scanned) == scans_after_first


def test_partial_and_complete_scan_share_one_verified_snapshot(tmp_path, monkeypatch):
    import server.state as state

    downloads = tmp_path / "downloads"
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(tmp_path / "cache"))
    base = downloads / "jm" / "comic"
    _page(base / "complete")
    partial = base / "partial"
    _page(partial)
    (partial / "0002.jpg").write_bytes(b"\xff\xd8\xff" + b"y" * 128)
    (base / "_info.json").write_text(json.dumps({"chapters": [
        {"id": "complete", "name": "第1话", "download_page_count": 1},
        {"id": "partial", "name": "第2话", "download_page_count": 3},
    ]}), encoding="utf-8")
    state._invalidate_manga_download_scan("jm", "comic")

    original_check = state._is_manga_image_file
    checks = 0

    def count_checks(path):
        nonlocal checks
        checks += 1
        return original_check(path)

    monkeypatch.setattr(state, "_is_manga_image_file", count_checks)
    assert state._scan_downloaded_chapters("jm", "comic") == ["complete"]
    checks_after_scan = checks
    assert state._scan_partial_downloaded_chapters("jm", "comic") == ["partial"]
    assert state._scan_downloaded_chapters("jm", "comic") == ["complete"]
    assert checks == checks_after_scan == 3, "部分状态读取应复用同一次图片校验"


def test_old_cache_root_download_requires_saved_download_manifest(
        tmp_path, monkeypatch):
    import server.state as state

    downloads = tmp_path / "downloads"
    cache = tmp_path / "cache"
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(cache))
    base = cache / "copymanga" / "comic"
    _page(base / "legacy-downloaded")
    _page(base / "online-cache")
    (base / "_info.json").write_text(json.dumps({"chapters": [
        {"id": "legacy-downloaded", "name": "第1話"},
    ]}), encoding="utf-8")

    assert state._scan_downloaded_chapters("copymanga", "comic") == [
        "legacy-downloaded"]
    local_dirs = state._manga_local_media_dirs(
        "copymanga", "comic", "legacy-downloaded", downloaded_only=True)
    assert local_dirs and all("legacy-downloaded" in path for path in local_dirs)
    assert all("online-cache" not in path for path in local_dirs)


def test_local_media_lookup_can_exclude_online_read_cache(tmp_path, monkeypatch):
    import server.state as state

    downloads = tmp_path / "downloads"
    cache = tmp_path / "cache"
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(cache))
    _page(cache / "jm" / "comic" / "chapter")

    assert state._manga_local_page_dir(
        "jm", "comic", "chapter", 0, downloaded_only=True) is None
    assert state._manga_local_page_dir("jm", "comic", "chapter", 0) is not None


def test_download_scan_ignores_empty_directories_and_unreadable_image_files(
        tmp_path, monkeypatch):
    """存在目录/图片扩展名不足以证明可阅读；坏文件不能报成已下载。"""
    import server.state as state

    downloads = tmp_path / "downloads"
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(tmp_path / "cache"))
    (downloads / "jm" / "comic" / "empty-chapter").mkdir(parents=True)
    corrupt = downloads / "jm" / "comic" / "corrupt-chapter"
    corrupt.mkdir(parents=True)
    (corrupt / "0000.jpg").write_bytes(b"<html>rate limited</html>")
    assert state._scan_downloaded_chapters("jm", "comic") == []


def test_download_scan_requires_complete_page_sequence_when_manifest_has_images(
        tmp_path, monkeypatch):
    """清单记录图片总数时，磁盘缺页不能被标作完整已下载。"""
    import server.state as state

    downloads = tmp_path / "downloads"
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(tmp_path / "cache"))
    base = downloads / "jm" / "comic"
    chapter = base / "chapter"
    _page(chapter)
    (chapter / "0001.jpg").write_bytes(b"\xff\xd8\xff" + b"y" * 128)
    (base / "_info.json").write_text(json.dumps({"chapters": [
        {"id": "chapter", "name": "第1话", "download_page_count": 3},
    ]}), encoding="utf-8")
    assert state._scan_downloaded_chapters("jm", "comic") == []

    info = {"chapters": [{"id": "chapter", "name": "第1话",
                           "download_page_count": 2}]}
    (base / "_info.json").write_text(json.dumps(info), encoding="utf-8")
    state._invalidate_manga_download_scan("jm", "comic")
    (chapter / "0001.jpg").rename(chapter / "0002.jpg")
    assert state._scan_downloaded_chapters("jm", "comic") == []
    (chapter / "0002.jpg").rename(chapter / "0001.jpg")
    state._invalidate_manga_download_scan("jm", "comic")
    assert state._scan_downloaded_chapters("jm", "comic") == ["chapter"]


def test_legacy_manifest_with_known_page_gap_is_not_marked_complete(
        tmp_path, monkeypatch):
    """旧清单没有期望页数，但可证明的中间缺页不能冒充完整章节。"""
    import server.state as state

    downloads = tmp_path / "downloads"
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(tmp_path / "cache"))
    base = downloads / "old-source" / "old-comic"
    chapter = base / "old-chapter"
    chapter.mkdir(parents=True)
    for index in (0, 2):
        (chapter / f"{index:04d}.jpg").write_bytes(
            b"\xff\xd8\xff" + bytes([index]) * 128)
    (base / "_info.json").write_text(json.dumps({"chapters": [
        {"id": "old-chapter", "name": "第1话"},
    ]}), encoding="utf-8")

    assert state._scan_downloaded_chapters("old-source", "old-comic") == []
    assert state._manga_scan_comic("old-source", "old-comic") == (2, 1, 0)

    (chapter / "0001.jpg").write_bytes(b"\xff\xd8\xff" + b"y" * 128)
    state._invalidate_manga_download_scan("old-source", "old-comic")
    assert state._scan_downloaded_chapters("old-source", "old-comic") == [
        "old-chapter"]
    assert state._manga_scan_comic("old-source", "old-comic") == (3, 1, 1)


def test_online_read_backfills_source_page_count_for_legacy_downloads(
        tmp_path, monkeypatch):
    """Full online read repairs old page-count metadata; local-only read stays offline."""
    import app
    import server.manga_api as manga_api
    import server.state as state

    source, comic_id, chapter_id = "legacytest", "legacy-comic", "legacy-chapter"
    downloads = tmp_path / "downloads"
    cache = tmp_path / "cache"
    manga_root = tmp_path / "manga"
    comic_dir = downloads / source / comic_id
    chapter_dir = comic_dir / chapter_id
    chapter_dir.mkdir(parents=True)
    for index in (0, 2):
        (chapter_dir / f"{index:04d}.jpg").write_bytes(
            b"\xff\xd8\xff\xe0" + bytes([index]) * 2048)
    info_path = comic_dir / "_info.json"
    info_path.write_text(json.dumps({"chapters": [
        {"id": chapter_id, "name": "第1话"},
    ]}), encoding="utf-8")

    for module in (manga_api, state):
        monkeypatch.setattr(module, "MANGA_DOWNLOADS_DIR", str(downloads), raising=False)
        monkeypatch.setattr(module, "MANGA_CACHE_DIR", str(cache), raising=False)
    monkeypatch.setattr(manga_api, "MANGA_DIR", str(manga_root), raising=False)
    monkeypatch.setattr(manga_api, "_known_chapter_pages", lambda *_args: None)
    source_fetches = []

    class _Adapter:
        PROCESS_VERSION = 0

    adapter = _Adapter()
    monkeypatch.setattr(manga_api, "_manga_read_adapter", lambda _source: adapter)

    def source_images(*_args):
        source_fetches.append(True)
        return adapter, ["https://img.test/0.jpg", "https://img.test/1.jpg",
                         "https://img.test/2.jpg"]

    monkeypatch.setattr(manga_api, "_manga_read_images", source_images)
    stats_refresh = []
    monkeypatch.setattr(manga_api, "_manga_stats_request",
                        lambda src, cid: stats_refresh.append((src, cid)))
    app.app.config["TESTING"] = True
    client = app.app.test_client()

    # The shelf's local-only entry must not call the source or mutate old metadata.
    local = client.get(
        f"/api/manga/{source}/{comic_id}/chapter/{chapter_id}/urls?catalog=local")
    assert local.status_code == 200, local.get_data(as_text=True)
    assert local.get_json()["count"] == 2
    assert source_fetches == []
    assert "download_page_count" not in json.loads(info_path.read_text())[
        "chapters"][0]

    # Full-catalog reading obtains the source's actual three-page count, persists it,
    # and the gapped local copy is no longer represented as a complete download.
    online = client.get(
        f"/api/manga/{source}/{comic_id}/chapter/{chapter_id}/urls")
    assert online.status_code == 200, online.get_data(as_text=True)
    assert online.get_json()["count"] == 3
    assert source_fetches == [True]
    assert json.loads(info_path.read_text())["chapters"][0][
        "download_page_count"] == 3
    assert stats_refresh == [(source, comic_id)]
    assert state._scan_downloaded_chapters(source, comic_id) == []


def _legacy_download_with_manifest(base, chapter_id, chapter_name):
    page = base / chapter_id
    page.mkdir(parents=True, exist_ok=True)
    # Match the production image-serving minimum size so this fixture validates
    # the full reader path rather than being rejected as a tiny corrupt image.
    (page / "0000.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 2048)
    (base / "_info.json").write_text(json.dumps({"chapters": [
        {"id": chapter_id, "name": chapter_name},
    ]}), encoding="utf-8")


def test_downloaded_identity_matches_normalized_unique_chapter_name(
        tmp_path, monkeypatch):
    import server.state as state

    downloads = tmp_path / "downloads"
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(tmp_path / "cache"))
    _legacy_download_with_manifest(
        downloads / "copymanga" / "comic", "old-id", "  第１话　开 始  ")

    resolved = state._downloaded_ids_for_chapters("copymanga", "comic", [
        {"id": "new-id", "name": "第1话 开 始"},
        {"id": "other-id", "name": "第2话"},
    ])
    assert resolved == {"old-id", "new-id"}


def test_downloaded_identity_does_not_match_empty_or_ambiguous_names(
        tmp_path, monkeypatch):
    import server.state as state

    downloads = tmp_path / "downloads"
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(tmp_path / "cache"))
    _legacy_download_with_manifest(downloads / "jm" / "comic", "old-id", "")

    resolved = state._downloaded_ids_for_chapters("jm", "comic", [
        {"id": "blank-id", "name": ""},
        {"id": "dup-a", "name": "第1话"},
        {"id": "dup-b", "name": "第 １ 話"},
    ])
    assert resolved == {"old-id"}


def test_offline_detail_uses_verified_legacy_alias_download_metadata(
        tmp_path, monkeypatch):
    """旧缓存根/CopyManga 别名上的真下载可离线开详情，普通缓存仍不可冒充。"""
    import app
    import server.manga_api as manga_api
    import server.state as state

    downloads = tmp_path / "downloads"
    cache = tmp_path / "cache"
    manga_root = tmp_path / "manga"
    monkeypatch.setattr(manga_api, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(manga_api, "MANGA_CACHE_DIR", str(cache))
    monkeypatch.setattr(manga_api, "MANGA_DIR", str(manga_root))
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(cache))
    monkeypatch.setattr(manga_api, "_manga_adapter", lambda _source: None)
    monkeypatch.setattr(manga_api, "_manga_read_adapter", lambda _source: None)
    monkeypatch.setattr(manga_api, "_integrity_summary", lambda *_args: None)
    monkeypatch.setattr(manga_api, "_detail_refresh_async", lambda *_args, **_kwargs: False)

    old_comic = "old-comic"
    legacy_base = cache / "copymanga_web" / old_comic
    _legacy_download_with_manifest(legacy_base, "old-chapter", "第１话　开 始")

    app.app.config["TESTING"] = True
    client = app.app.test_client()
    response = client.get(f"/api/manga/copymanga/{old_comic}?catalog=local")
    assert response.status_code == 200, response.get_data(as_text=True)
    detail = response.get_json()
    assert detail["quick"] is True and detail["local_only"] is True
    assert detail["downloaded"] == ["old-chapter"]
    assert [chapter["id"] for chapter in detail["chapters"]] == ["old-chapter"]

    urls = client.get(
        f"/api/manga/copymanga/{old_comic}/chapter/old-chapter/urls?catalog=local")
    assert urls.status_code == 200, urls.get_data(as_text=True)
    assert urls.get_json()["local_only"] is True
    assert all(image["local"] for image in urls.get_json()["images"])
    # The exact same identity advertised by the local catalog must resolve each
    # returned image URL to the legacy alias/cache bytes (not fall back online).
    for image in urls.get_json()["images"]:
        page = client.get(image["url"])
        assert page.status_code == 200, page.get_data(as_text=True)
        assert page.mimetype == "image/jpeg"
        assert page.data.startswith(b"\xff\xd8\xff")

    # 仅有普通详情清单、没有任何章节图片的缓存，必须仍被视为不可离线打开。
    cached_only = cache / "copymanga_web" / "online-only"
    (cached_only / "_info.json").parent.mkdir(parents=True, exist_ok=True)
    (cached_only / "_info.json").write_text(json.dumps({
        "chapters": [{"id": "chapter", "name": "第1话"}],
    }), encoding="utf-8")
    missing = client.get(
        "/api/manga/copymanga/online-only?catalog=local")
    assert missing.status_code == 404


def test_real_download_files_drive_full_and_local_catalog_status(
        tmp_path, monkeypatch):
    """同一份真实磁盘证据必须驱动在线完整目录与书架本地目录状态。"""
    import app
    import server.manga_api as manga_api
    import server.state as state

    comic_id = "download-status-e2e"
    source = "copymanga_web"
    manga_root = tmp_path / "manga"
    downloads = manga_root / "downloads"
    cache = manga_root / "_cache"
    comic_dir = downloads / source / comic_id
    chapter_dir = comic_dir / "chapter-1"
    chapter_dir.mkdir(parents=True)
    for index in range(2):
        (chapter_dir / f"{index:04d}.jpg").write_bytes(
            b"\xff\xd8\xff\xe0" + bytes([index]) * 2048)
    partial_dir = comic_dir / "chapter-2"
    partial_dir.mkdir()
    (partial_dir / "0000.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"p" * 2048)
    legacy_gap_dir = comic_dir / "chapter-3-legacy"
    legacy_gap_dir.mkdir()
    for index in (0, 2):
        (legacy_gap_dir / f"{index:04d}.jpg").write_bytes(
            b"\xff\xd8\xff\xe0" + bytes([index]) * 2048)
    (comic_dir / "_info.json").write_text(json.dumps({
        "title": "下载状态闭环",
        "chapters": [
            {"id": "chapter-1", "name": "第1话", "download_page_count": 2},
            {"id": "chapter-2", "name": "第2话", "download_page_count": 2},
            {"id": "chapter-3-legacy", "name": "第3话"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    full_detail = cache / source / comic_id / "_info_full.json"
    full_detail.parent.mkdir(parents=True)
    full_detail.write_text(json.dumps({"ts": __import__("time").time(), "data": {
        "source": source,
        "comic_id": comic_id,
        "title": "下载状态闭环",
        "volumes": [],
        "chapters": [
            {"id": "chapter-1", "name": "第1话"},
            {"id": "chapter-2", "name": "第2话"},
            {"id": "chapter-3-legacy", "name": "第3话"},
        ],
    }}, ensure_ascii=False), encoding="utf-8")

    for module in (manga_api, state):
        monkeypatch.setattr(module, "MANGA_DOWNLOADS_DIR", str(downloads), raising=False)
        monkeypatch.setattr(module, "MANGA_CACHE_DIR", str(cache), raising=False)
    monkeypatch.setattr(manga_api, "MANGA_DIR", str(manga_root), raising=False)
    monkeypatch.setattr(manga_api, "_manga_adapter", lambda _source: type(
        "Adapter", (), {"name": "fixture", "in_cooldown": lambda _self: False})())
    monkeypatch.setattr(manga_api, "_manga_read_adapter", lambda _source: None)
    monkeypatch.setattr(manga_api, "_detail_refresh_async", lambda *_a, **_kw: False)
    monkeypatch.setattr(manga_api, "_integrity_summary", lambda *_a, **_kw: None)
    monkeypatch.setattr(manga_api, "_resume_payload", lambda *_a, **_kw: None)

    app.app.config["TESTING"] = True
    client = app.app.test_client()
    assert state._scan_downloaded_chapters(source, comic_id) == ["chapter-1"]

    full = client.get(f"/api/manga/{source}/{comic_id}")
    assert full.status_code == 200, full.get_data(as_text=True)
    full_payload = full.get_json()
    assert [row["id"] for row in full_payload["chapters"]] == [
        "chapter-1", "chapter-2", "chapter-3-legacy"]
    assert full_payload["downloaded"] == ["chapter-1"]

    local = client.get(f"/api/manga/{source}/{comic_id}?catalog=local")
    assert local.status_code == 200, local.get_data(as_text=True)
    local_payload = local.get_json()
    assert local_payload["local_only"] is True
    assert [row["id"] for row in local_payload["chapters"]] == ["chapter-1"]
    assert local_payload["downloaded"] == ["chapter-1"]

    urls = client.get(
        f"/api/manga/{source}/{comic_id}/chapter/chapter-1/urls?catalog=local")
    assert urls.status_code == 200, urls.get_data(as_text=True)
    images = urls.get_json()["images"]
    assert len(images) == 2 and all(row["local"] for row in images)
    for row in images:
        response = client.get(row["url"])
        assert response.status_code == 200
        assert response.data.startswith(b"\xff\xd8\xff")


def test_local_catalog_never_falls_back_to_full_cache_after_media_is_deleted(
        tmp_path, monkeypatch):
    """媒体被删空后，书架本地目录不能因源仍可用而泄漏回完整在线目录。"""
    import app
    import server.manga_api as manga_api
    import server.state as state

    source, comic_id = "copymanga_web", "deleted-local-media"
    manga_root = tmp_path / "manga"
    downloads = manga_root / "downloads"
    cache = manga_root / "_cache"
    detail_file = cache / source / comic_id / "_info_full.json"
    detail_file.parent.mkdir(parents=True)
    detail_file.write_text(json.dumps({"ts": __import__("time").time(), "data": {
        "source": source,
        "comic_id": comic_id,
        "title": "仍可在线访问的作品",
        "volumes": [{"id": "v1", "name": "第1卷"}],
        "chapters": [{"id": "ch1", "name": "第1话"},
                     {"id": "ch2", "name": "第2话"}],
    }}, ensure_ascii=False), encoding="utf-8")

    for module in (manga_api, state):
        monkeypatch.setattr(module, "MANGA_DOWNLOADS_DIR", str(downloads), raising=False)
        monkeypatch.setattr(module, "MANGA_CACHE_DIR", str(cache), raising=False)
    monkeypatch.setattr(manga_api, "MANGA_DIR", str(manga_root), raising=False)
    monkeypatch.setattr(manga_api, "_manga_adapter", lambda _source: type(
        "Adapter", (), {"name": "fixture", "in_cooldown": lambda _self: False})())
    monkeypatch.setattr(manga_api, "_integrity_summary", lambda *_a, **_kw: None)
    refreshes = []
    monkeypatch.setattr(manga_api, "_detail_refresh_async",
                        lambda *args, **kwargs: refreshes.append(args) or True)

    app.app.config["TESTING"] = True
    client = app.app.test_client()
    full = client.get(f"/api/manga/{source}/{comic_id}")
    assert full.status_code == 200
    assert [row["id"] for row in full.get_json()["volumes"] +
            full.get_json()["chapters"]] == ["v1", "ch1", "ch2"]

    local = client.get(f"/api/manga/{source}/{comic_id}?catalog=local")
    assert local.status_code == 200, local.get_data(as_text=True)
    payload = local.get_json()
    assert payload["local_only"] is True
    assert payload["downloaded"] == []
    assert payload["volumes"] == [] and payload["chapters"] == []
    assert refreshes == [], "本地目录请求不能启动源站详情刷新"


def test_provable_partial_download_is_reported_separately_and_maps_by_name(
        tmp_path, monkeypatch):
    """缺页章节不能冒充完整下载，但在线目录应明确识别并可供补齐。"""
    import server.state as state

    downloads = tmp_path / "downloads"
    cache = tmp_path / "cache"
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(cache))
    base = downloads / "copymanga_web" / "partial-comic"
    chapter = base / "legacy-chapter-id"
    chapter.mkdir(parents=True)
    for index in (0, 2):
        (chapter / f"{index:04d}.jpg").write_bytes(
            b"\xff\xd8\xff" + bytes([index]) * 512)
    (base / "_info.json").write_text(json.dumps({"chapters": [{
        "id": "legacy-chapter-id", "name": "第１話", "download_page_count": 3,
    }]}), encoding="utf-8")

    assert state._scan_downloaded_chapters("copymanga", "partial-comic") == []
    assert state._scan_partial_downloaded_chapters(
        "copymanga", "partial-comic") == ["legacy-chapter-id"]
    current_catalog = [{"id": "new-source-id", "name": "第1話"}]
    assert state._partial_downloaded_ids_for_chapters(
        "copymanga", "partial-comic", current_catalog) == {"new-source-id"}

    # 普通在线缓存不应被报告成持久下载的部分内容。
    online_cache = cache / "copymanga_web" / "partial-comic" / "read-cache-only"
    online_cache.mkdir(parents=True)
    (online_cache / "0000.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 512)
    assert "read-cache-only" not in state._scan_partial_downloaded_chapters(
        "copymanga", "partial-comic")
