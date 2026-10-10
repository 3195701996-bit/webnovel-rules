# -*- coding: utf-8 -*-
"""0.63.1 回归：**阅读进度不能因为"详情少一个字段"而丢失**（用户现场反馈）。

用户原话（2026-09-16）："现在运行的系统的书库的阅读记录出问题了，明明记录了实际阅读进度，
但阅读时又会重新从第一话开始"，并追问"是否因为清理缓存导致的"。

实测结论（先测再改）：
  · **不是清理缓存**：`_history.json` / `_library.json` 直接位于 `data/manga/`，
    不落在 `_cache/`、`downloads/` 或 `trash/` 之内；重建图片缓存只删图片与处理标记。
    本文件用"跑一遍所有清理路径 → 两份文件字节不变"把这条**锁成回归**。
  · **真因是详情接口的身份字段缺失**：`GET /api/manga/<source>/<comic_id>` 的
    quick 路径（书库里有本地数据时走它——正是用户最常读的那些）只返回 `id`，
    **没有 `comic_id`**；客户端拿 `(source, comic_id)` 去历史里找进度，
    `comicId` 读成空串 → 匹配失败 → 续读落点退回"第一个已下载话/第 1 话"。
    表现就是"进度记着，但每次从第一话开始"。

本文件锁三件事：
  1. 详情接口的**每条返回路径**都必须带 `source` 与 `comic_id`（quick/离线/缓存/回源）；
  2. 历史条目与详情能对得上：用历史里的 (source, comic_id) 取详情，字段必须一致
     ——这正是客户端匹配进度的依据；
  3. 清理与重建**不得**动 `_history.json` / `_library.json`（用户怀疑的那条，用证据回答）。
"""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CID = "progtest_comic"
SRC = "copymanga_web"


@pytest.fixture(scope="module")
def client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


@pytest.fixture()
def library_comic(monkeypatch, tmp_path):
    """造一部"书库里有下载数据"的漫画：详情会走 quick 路径（问题路径）"""
    import server.manga_api as mapi
    import server.state as st

    root = tmp_path / "manga"
    dl = root / "downloads"
    cache = root / "_cache"
    d = dl / SRC / CID
    d.mkdir(parents=True, exist_ok=True)
    (d / "_info.json").write_text(json.dumps({
        "title": "进度测试漫画", "cover": "",
        "chapters": [{"id": f"ch{i}", "name": f"第{i}話"} for i in range(1, 6)],
    }, ensure_ascii=False), encoding="utf-8")
    (d / "ch1").mkdir(exist_ok=True)
    (d / "ch1" / "0000.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 2000)

    monkeypatch.setattr(mapi, "MANGA_DOWNLOADS_DIR", str(dl), raising=False)
    monkeypatch.setattr(mapi, "MANGA_CACHE_DIR", str(cache), raising=False)
    monkeypatch.setattr(mapi, "MANGA_DIR", str(root), raising=False)
    monkeypatch.setattr(st, "MANGA_DOWNLOADS_DIR", str(dl), raising=False)
    monkeypatch.setattr(st, "MANGA_CACHE_DIR", str(cache), raising=False)
    monkeypatch.setattr(mapi, "_manga_adapter", lambda _source: type("Adapter", (), {"name": "测试源"})())
    monkeypatch.setattr(mapi, "_refresh_detail_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(mapi, "_api_detail_fallback", lambda *args, **kwargs: None)
    monkeypatch.setattr(mapi, "_detail_refresh_async", lambda *args, **kwargs: False)
    monkeypatch.setattr(mapi, "_integrity_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr(mapi, "_resume_payload", lambda *args, **kwargs: None)
    monkeypatch.setattr(mapi, "_downloaded_ids_for_chapters",
                        lambda _source, _comic_id, chapters: list(chapters))
    return mapi, root, dl, cache


def test_quick_path_detail_carries_identity(client, library_comic):
    """quick 路径（书库里有本地数据）必须带 comic_id —— 这就是用户踩的那条"""
    mapi, root, dl, cache = library_comic
    # Isolate module-scoped Flask state from earlier source-failure/cooldown tests.
    test_cid = CID + "_quick_identity"
    (dl / SRC / test_cid).mkdir(parents=True)
    (dl / SRC / test_cid / "_info.json").write_text(json.dumps({
        "title": "快速路径身份", "chapters": [{"id": "ch1", "name": "第1話"}],
    }, ensure_ascii=False), encoding="utf-8")
    (dl / SRC / test_cid / "ch1").mkdir()
    (dl / SRC / test_cid / "ch1" / "0000.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 2048)
    r = client.get(f"/api/manga/{SRC}/{test_cid}?catalog=local")
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d.get("quick") is True, "应走 quick 路径（本地有 _info.json）"
    assert d.get("comic_id") == test_cid, f"quick 路径必须给 comic_id，实际 {d.get('comic_id')!r}"
    assert d.get("source") == SRC
    assert d.get("id") == test_cid, "旧的 id 字段保持兼容"


def test_quick_path_never_exposes_unmapped_chapters_as_local(client, library_comic, monkeypatch):
    """章节身份无法映射到真实图片目录时，不能把 metadata 全目录误当已下载目录。"""
    mapi, _root, _dl, _cache = library_comic
    monkeypatch.setattr(mapi, "_downloaded_ids_for_chapters",
                        lambda *_args: [])
    response = client.get(f"/api/manga/{SRC}/{CID}?catalog=local")
    assert response.status_code == 200
    detail = response.get_json()
    assert detail["quick"] is True
    assert detail["downloaded"] == []
    assert detail["chapters"] == [] and detail["volumes"] == []


def test_default_detail_keeps_full_catalog_when_some_chapters_are_local(
        client, library_comic, monkeypatch):
    """搜索/历史等默认详情不可因存在下载元数据而被缩成仅本地章节。"""
    mapi, root, _dl, cache = library_comic
    monkeypatch.setattr(mapi, "_downloaded_ids_for_chapters",
                        lambda _s, _c, _rows: ["ch1"])
    full = cache / SRC / CID / "_info_full.json"
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps({"ts": __import__("time").time(), "data": {
        "source": SRC, "comic_id": CID, "title": "完整目录",
        "volumes": [{"id": "v1", "name": "第01卷"}],
        "chapters": [{"id": f"ch{i}", "name": f"第{i}話"}
                     for i in range(1, 6)],
    }}, ensure_ascii=False), encoding="utf-8")

    response = client.get(f"/api/manga/{SRC}/{CID}")
    assert response.status_code == 200
    detail = response.get_json()
    assert detail.get("quick") is not True
    assert [c["id"] for c in detail["volumes"] + detail["chapters"]] == [
        "v1", "ch1", "ch2", "ch3", "ch4", "ch5"]
    assert detail["downloaded"] == ["ch1"], \
        "完整源目录和本地已下载身份必须分别返回，不能被本地列表截断"


def test_online_detail_exposes_provable_partial_chapters_separately(
        client, library_comic, monkeypatch):
    """在线详情携带部分缓存状态，但不得把它并入完整 downloaded 集合。"""
    import time

    mapi, _root, _dl, cache = library_comic
    monkeypatch.setattr(mapi, "_downloaded_ids_for_chapters",
                        lambda *_args: {"ch1"})
    monkeypatch.setattr(mapi, "_partial_downloaded_ids_for_chapters",
                        lambda _s, _c, _rows: {"ch2"})
    full = cache / SRC / CID / "_info_full.json"
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps({"ts": time.time(), "data": {
        "source": SRC, "comic_id": CID, "title": "部分缓存状态",
        "chapters": [{"id": f"ch{i}", "name": f"第{i}話"}
                     for i in range(1, 4)],
    }}, ensure_ascii=False), encoding="utf-8")

    response = client.get(f"/api/manga/{SRC}/{CID}")
    assert response.status_code == 200
    detail = response.get_json()
    assert detail["downloaded"] == ["ch1"]
    assert detail["partial_downloaded"] == ["ch2"]


def test_local_catalog_detail_filters_to_real_downloads(client, library_comic, monkeypatch):
    """书架/本地阅读入口要求 catalog=local 时，只呈现实际已落盘章节。"""
    mapi, _root, _dl, _cache = library_comic
    monkeypatch.setattr(mapi, "_downloaded_ids_for_chapters",
                        lambda _s, _c, _rows: ["ch1"])
    response = client.get(f"/api/manga/{SRC}/{CID}?catalog=local")
    assert response.status_code == 200
    detail = response.get_json()
    assert detail["quick"] is True and detail["local_only"] is True
    assert [c["id"] for c in detail["chapters"] + detail["volumes"]] == ["ch1"]


def test_online_detail_merges_local_download_identity_without_truncating_new_catalog(
        client, library_comic, monkeypatch):
    """Venera semantics: online catalog stays complete while downloaded chapters stay local-first."""
    mapi, _root, _dl, cache = library_comic
    monkeypatch.setattr(mapi, "_downloaded_ids_for_chapters",
                        lambda _s, _c, rows: ["ch1"] if any(
                            row.get("id") == "ch1" for row in rows) else [])
    full = cache / SRC / CID / "_info_full.json"
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps({"ts": __import__("time").time(), "data": {
        "source": SRC, "comic_id": CID, "title": "部分缓存后有新话",
        "chapters": [{"id": f"ch{i}", "name": f"第{i}話"}
                     for i in range(1, 7)],
    }}, ensure_ascii=False), encoding="utf-8")

    response = client.get(f"/api/manga/{SRC}/{CID}")
    assert response.status_code == 200
    detail = response.get_json()
    assert [row["id"] for row in detail["chapters"]] == [
        "ch1", "ch2", "ch3", "ch4", "ch5", "ch6"]
    assert detail["downloaded"] == ["ch1"]
    assert detail.get("local_only") is not True


def test_legacy_chapter_route_marks_offline_images_local_only(
        client, library_comic, monkeypatch):
    """源不可用时，旧章节接口返回的本地页必须继续使用严格本地 URL。"""
    mapi, _root, _dl, _cache = library_comic
    monkeypatch.setattr(mapi, "_manga_read_adapter", lambda _source: None)
    response = client.get(f"/api/manga/{SRC}/{CID}/chapter/ch1")
    assert response.status_code == 200, response.get_data(as_text=True)
    payload = response.get_json()
    assert payload["local_only"] is True and payload["partial_local"] is True
    assert payload["images"] == [
        f"/api/manga/{SRC}/{CID}/chapter/ch1/img/0?catalog=local"]


def test_cached_catalog_keeps_volumes_before_chapters(tmp_path, monkeypatch):
    """书库续读/未读目录缓存必须计入整卷，并保持统一的卷优先顺序。"""
    import server.state as st
    comic_id = CID + "_volume_catalog"
    cache = tmp_path / "cache"
    comic_dir = cache / SRC / comic_id
    comic_dir.mkdir(parents=True)
    (comic_dir / "_info_full.json").write_text(json.dumps({"data": {
        "volumes": [{"id": "v1", "name": "第01卷"}],
        "chapters": [{"id": "c1", "name": "第01话"}],
    }}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(st, "MANGA_CACHE_DIR", str(cache))
    st._manga_catalog_invalidate(SRC, comic_id)
    assert [c["id"] for c in st._manga_cached_chapters(SRC, comic_id)] == ["v1", "c1"]
    assert st._manga_total_chapters(SRC, comic_id) == 2


def test_legacy_flat_catalog_is_normalized_to_volume_first_order(tmp_path, monkeypatch):
    """旧详情快照平铺卷/话时，书架、历史和收藏仍需得到同一阅读顺序。"""
    import server.state as st

    comic_id = CID + "_legacy_flat_catalog"
    cache = tmp_path / "cache"
    comic_dir = cache / SRC / comic_id
    comic_dir.mkdir(parents=True)
    (comic_dir / "_info_full.json").write_text(json.dumps({"data": {
        "chapters": [
            {"id": "c2", "name": "第2話"},
            {"id": "v2", "name": "第二卷"},
            {"id": "c1", "name": "第1話"},
            {"id": "v1", "name": "第01卷"},
        ],
    }}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(st, "MANGA_CACHE_DIR", str(cache))
    st._manga_chapters_cache.pop((SRC, comic_id), None)

    assert [c["id"] for c in st._manga_cached_chapters(SRC, comic_id)] == [
        "v1", "v2", "c1", "c2"]


def test_unread_identity_set_counts_skips_after_legacy_catalog_normalization(
        tmp_path, monkeypatch):
    """重读旧话不得抹掉跳读缺口；旧快照归一后新卷/新话按身份计算未读。"""
    import server.manga_api as ma
    import server.state as st

    comic_id = CID + "_legacy_unread_catalog"
    cache = tmp_path / "cache"
    comic_dir = cache / SRC / comic_id
    comic_dir.mkdir(parents=True)
    (comic_dir / "_info_full.json").write_text(json.dumps({"data": {
        "chapters": [
            {"id": "c1", "name": "第1話"},
            {"id": "v1", "name": "第01卷"},
            {"id": "c2", "name": "第2話"},
            {"id": "v2", "name": "第02卷"},
            {"id": "c3", "name": "第3話"},
        ],
    }}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(st, "MANGA_CACHE_DIR", str(cache))
    st._manga_chapters_cache.pop((SRC, comic_id), None)

    catalog = st._manga_cached_chapters(SRC, comic_id)
    read = {"read_chapter_ids": ["c1", "c3", "v1"]}
    resolved = ma._manga_read_id_set(read, catalog)
    assert [c["id"] for c in catalog] == ["v1", "v2", "c1", "c2", "c3"]
    assert sum(c["id"] not in resolved for c in catalog) == 2


def test_legacy_episode_read_does_not_migrate_to_same_numbered_volume(
        client, library_comic, monkeypatch):
    """A read episode and volume with ordinal 1 are distinct identities."""
    import server.manga_api as mapi
    import server.state as state

    _mapi, root, _downloads, cache = library_comic
    comic_id = CID + "_volume_episode_identity"
    catalog_dir = cache / SRC / comic_id
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "_favorites_catalog.json").write_text(json.dumps({
        "ts": 500,
        "data": {"volumes": [{"id": "new-volume-1", "name": "第1卷"}],
                 "chapters": []},
    }, ensure_ascii=False), encoding="utf-8")
    state._manga_chapters_cache.pop((SRC, comic_id), None)

    favorites_file = root / "_favorites_volume_episode.json"
    history_file = root / "_history_volume_episode.json"
    favorites_file.write_text(json.dumps({f"{SRC}:{comic_id}": {
        "title": "卷话序号相同但身份不同", "ts": 100,
    }}, ensure_ascii=False), encoding="utf-8")
    history_file.write_text(json.dumps({f"{SRC}:{comic_id}": {
        "chapter_id": "old-episode-1", "chapter_label": "第1话",
        "read_chapter_ids": ["old-episode-1"],
        "read_chapters": [{"id": "old-episode-1", "label": "第1话"}],
        "ts": 200,
    }}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(favorites_file), raising=False)
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history_file), raising=False)

    response = client.get("/api/manga/favorites")
    assert response.status_code == 200, response.get_data(as_text=True)
    favorite = next(row for row in response.get_json()["favorites"]
                    if row["comic_id"] == comic_id)
    assert favorite["unread_count"] == 1
    assert favorite["latest_chapter_id"] == "new-volume-1"


def test_favorites_api_keeps_unread_count_after_chapter_ids_change_and_reread(
        client, library_comic, monkeypatch):
    """旧 ID 已读集合跨源站重编号保留；回读第12话不抹去14–18的跳读缺口。"""
    import server.state as state

    mapi, root, _dl, cache = library_comic
    comic_id = CID + "_renumbered_unread"
    catalog_dir = cache / SRC / comic_id
    catalog_dir.mkdir(parents=True)
    chapters = [
        {"id": f"new-{number}", "name": f"第{number}話"}
        for number in range(1, 32)
    ]
    (catalog_dir / "_favorites_catalog.json").write_text(json.dumps({
        "ts": 500,
        "data": {"volumes": [], "chapters": chapters},
    }, ensure_ascii=False), encoding="utf-8")
    state._manga_chapters_cache.pop((SRC, comic_id), None)

    favorites_file = root / "_favorites_renumbered.json"
    history_file = root / "_history_renumbered.json"
    favorites_file.write_text(json.dumps({f"{SRC}:{comic_id}": {
        "title": "章节重编号作品", "ts": 100,
    }}, ensure_ascii=False), encoding="utf-8")
    read_numbers = [*range(1, 14), *range(19, 31)]
    history_file.write_text(json.dumps({f"{SRC}:{comic_id}": {
        "idx": 11,
        "pos": "第12話 P5",  # 回读第12话，历史集合仍应保留其余已读章节。
        "chapter_id": "old-12",
        "chapter_label": "第12話",
        "read_chapter_ids": [f"old-{number}" for number in read_numbers],
        "read_chapters": [
            {"id": f"old-{number}", "label": f"第{number}話"}
            for number in read_numbers
        ],
        "ts": 200,
    }}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(favorites_file), raising=False)
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history_file), raising=False)

    rows = client.get("/api/manga/favorites").get_json()["favorites"]
    favorite = next(row for row in rows if row["comic_id"] == comic_id)
    assert favorite["unread_count"] == 6  # 14–18 + newly added 31
    assert favorite["latest_chapter_id"] == "new-31"


def test_reader_image_resolves_legacy_id_across_cache_and_source_alias(
        client, library_comic, monkeypatch):
    """已下载统计认出的旧 CopyManga ID 必须由阅读图片接口实际命中。"""
    import server.state as st
    mapi, _root, dl, cache = library_comic
    # 去掉夹具默认的第 1 话，避免人为构成与旧目录互补的完整双页章节。
    shutil.rmtree(dl / SRC / CID)
    current = cache / SRC / CID
    current.mkdir(parents=True, exist_ok=True)
    (current / "_info_full.json").write_text(json.dumps({"data": {
        "chapters": [{"id": "current-id", "name": "第1話"}], "volumes": []
    }}, ensure_ascii=False), encoding="utf-8")
    legacy = cache / "copymanga" / CID
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "_info.json").write_text(json.dumps({"chapters": [
        {"id": "legacy-id", "name": "第1話"}
    ]}, ensure_ascii=False), encoding="utf-8")
    chapter_dir = legacy / "legacy-id"
    chapter_dir.mkdir()
    # 两个历史根目录的页码应合并；第 2 页只存在 cache/APP 别名中。
    (chapter_dir / "0001.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 2048)
    monkeypatch.setattr(mapi, "_manga_read_adapter", lambda _source: None)

    response = client.get(
        f"/api/manga/{SRC}/{CID}/chapter/current-id/img/1")
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.mimetype == "image/jpeg"
    assert response.data.startswith(b"\xff\xd8\xff")
    assert st._manga_local_page_dir(SRC, CID, "current-id", 1) == os.path.realpath(chapter_dir)
    # URL 清单也应支持源不可用时读取实际存在的缓存页，不调用网络适配器。
    urls = client.get(
        f"/api/manga/{SRC}/{CID}/chapter/current-id/urls")
    assert urls.status_code == 200, urls.get_data(as_text=True)
    payload = urls.get_json()
    assert payload["partial_local"] is True
    assert payload["images"] == [{
        "local": True,
        "url": f"/api/manga/{SRC}/{CID}/chapter/current-id/img/1",
    }]


def test_reader_does_not_guess_between_duplicate_legacy_chapter_names(
        library_comic):
    """重名特别篇存在时不把另一章图片伪装成目标章节。"""
    import server.state as st
    _mapi, _root, dl, cache = library_comic
    shutil.rmtree(dl / SRC / CID)
    current = cache / SRC / CID
    current.mkdir(parents=True, exist_ok=True)
    (current / "_info_full.json").write_text(json.dumps({"data": {
        "chapters": [{"id": "new-id", "name": "特别篇"}], "volumes": []
    }}, ensure_ascii=False), encoding="utf-8")
    legacy = cache / "copymanga" / CID
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "_info.json").write_text(json.dumps({"chapters": [
        {"id": "special-a", "name": "特别篇"},
        {"id": "special-b", "name": "特别篇"},
    ]}, ensure_ascii=False), encoding="utf-8")
    for chapter_id in ("special-a", "special-b"):
        chapter_dir = legacy / chapter_id
        chapter_dir.mkdir()
        (chapter_dir / "0000.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 2048)
    assert st._manga_local_media_dirs(SRC, CID, "new-id") == []


def test_history_entry_matches_detail_identity(client, library_comic):
    """历史里的 (source, comic_id) 取详情后必须一致——客户端就靠这个匹配进度"""
    mapi, root, dl, cache = library_comic
    import server.state as state
    # 同一模块里其他用例可能验证详情失败冷却；为本用例使用独立身份，
    # 避免单飞注册表按 (source, comic_id) 将前一用例的结果串进来。
    test_cid = CID + "_history_identity"
    info = cache / SRC / test_cid / "_info_full.json"
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text(json.dumps({"ts": __import__("time").time(), "data": {
        "title": "进度测试漫画", "source": SRC, "comic_id": test_cid,
        "chapters": [{"id": f"ch{i}", "name": f"第{i}話"}
                     for i in range(1, 6)],
    }}, ensure_ascii=False), encoding="utf-8")
    state._manga_chapters_cache.pop((SRC, test_cid), None)
    # 先写一条进度（第 3 话）
    r = client.post("/api/manga/history", json={
        "source": SRC, "comic_id": test_cid, "idx": 2, "pos": "第3話 P7",
        "title": "进度测试漫画"})
    assert r.status_code == 200 and r.get_json().get("ok") is True
    hist = client.get("/api/manga/history").get_json().get("history") or []
    mine = [h for h in hist if h.get("comic_id") == test_cid and
            h.get("identity_source") == "copymanga"]
    assert mine, f"历史里应能按 (source, comic_id) 找到刚写的进度：{hist[:2]}"
    assert mine[0]["idx"] == 2
    assert mine[0]["read_chapter_ids"] == ["ch3"], \
        "history endpoint must resolve the complete read set against cached current chapters"

    det = client.get(f"/api/manga/{SRC}/{test_cid}").get_json()
    # 客户端比较的就是这两个字段（服务端历史数组 vs 详情响应）
    assert det.get("identity_source") == mine[0]["identity_source"]
    assert SRC in det.get("source_aliases", [])
    assert str(det.get("comic_id")) == str(mine[0]["comic_id"]), \
        "详情与历史的身份字段必须一致，否则续读落点会退回第一话"


def test_history_api_resolves_all_read_ids_after_catalog_identity_changes(
        client, library_comic, monkeypatch):
    import server.state as state

    mapi, root, _dl, cache = library_comic
    comic_id = CID + "_read_set"
    catalog = cache / SRC / comic_id / "_info_full.json"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(json.dumps({"ts": 99, "data": {"volumes": [], "chapters": [
        {"id": "new-1", "name": "第1話"},
        {"id": "new-2", "name": "第2話"},
        {"id": "new-3", "name": "第3話"},
    ]}}), encoding="utf-8")
    state._manga_chapters_cache.pop((SRC, comic_id), None)
    history_file = root / "_history_read_set.json"
    history_file.write_text(json.dumps({f"{SRC}:{comic_id}": {
        "idx": 2, "pos": "第3話 P8", "chapter_id": "new-3",
        "chapter_label": "第3話", "read_chapter_ids": ["old-1", "new-3"],
        "read_chapters": [{"id": "old-1", "label": "第1話"},
                           {"id": "new-3", "label": "第3話"}],
        "ts": 10,
    }}), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history_file))
    monkeypatch.setattr(state, "MANGA_HISTORY_FILE", str(history_file), raising=False)
    rows = client.get("/api/manga/history").get_json()["history"]
    mine = next(row for row in rows if row["comic_id"] == comic_id)
    assert mine["read_chapter_ids"] == ["new-1", "new-3"]


def test_copymanga_transport_aliases_share_history_and_favorite_identity(
        client, library_comic, monkeypatch):
    """APK/Web endpoints must merge one work's progress and favorite records."""
    import server.state as state

    mapi, root, _dl, cache = library_comic
    comic_id = CID + "_cross_client"
    catalog_dir = cache / "copymanga_web" / comic_id
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "_favorites_catalog.json").write_text(json.dumps({"ts": 50, "data": {
        "volumes": [], "chapters": [
            {"id": "chapter-1", "name": "第1話"},
            {"id": "chapter-2", "name": "第2話"},
            {"id": "chapter-3", "name": "第3話"},
        ],
    }}), encoding="utf-8")
    state._manga_chapters_cache.pop(("copymanga", comic_id), None)
    state._manga_chapters_cache.pop(("copymanga_web", comic_id), None)
    history_file = root / "_history_cross_client.json"
    history_file.write_text(json.dumps({
        f"copymanga_web:{comic_id}": {"idx": 0, "pos": "第1話 P2", "title": "跨端作品",
            "chapter_id": "chapter-1", "chapter_label": "第1話",
            "read_chapter_ids": ["chapter-1"], "ts": 20},
        f"copymanga:{comic_id}": {"idx": 2, "pos": "第3話 P1", "title": "跨端作品",
            "chapter_id": "chapter-3", "chapter_label": "第3話",
            "read_chapter_ids": ["chapter-3"], "ts": 30},
    }, ensure_ascii=False), encoding="utf-8")
    favorites_file = root / "_favorites_cross_client.json"
    favorites_file.write_text(json.dumps({
        f"copymanga_web:{comic_id}": {"title": "跨端作品", "ts": 15},
        f"copymanga:{comic_id}": {"cover": "/cover.jpg", "ts": 16},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history_file), raising=False)
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(favorites_file), raising=False)
    monkeypatch.setattr(state, "MANGA_HISTORY_FILE", str(history_file), raising=False)
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(cache))

    history_rows = client.get("/api/manga/history").get_json()["history"]
    same_history = [row for row in history_rows if row["comic_id"] == comic_id]
    assert len(same_history) == 1
    assert same_history[0]["source"] == "copymanga_web"  # 原 transport route
    assert same_history[0]["identity_source"] == "copymanga"
    assert set(same_history[0]["read_chapter_ids"]) == {"chapter-1", "chapter-3"}
    favorite_rows = client.get("/api/manga/favorites").get_json()["favorites"]
    same_favorite = [row for row in favorite_rows if row["comic_id"] == comic_id]
    assert len(same_favorite) == 1
    assert same_favorite[0]["unread_count"] == 1
    assert same_favorite[0]["source"] == "copymanga_web"
    assert same_favorite[0]["identity_source"] == "copymanga"

    saved = client.post("/api/manga/history", json={
        "source": "copymanga_web", "comic_id": comic_id, "idx": 1,
        "pos": "第2話 P4", "chapter_id": "chapter-2", "chapter_label": "第2話",
    })
    assert saved.status_code == 200 and saved.get_json()["ok"]
    persisted = json.loads(history_file.read_text(encoding="utf-8"))
    assert list(persisted) == [f"copymanga:{comic_id}"]
    assert set(persisted[f"copymanga:{comic_id}"]["read_chapter_ids"]) == {
        "chapter-1", "chapter-2", "chapter-3"}
    assert persisted[f"copymanga:{comic_id}"]["transport_source"] == "copymanga_web"


def test_favorite_update_time_uses_newest_detail_snapshot_across_aliases(
        client, library_comic, monkeypatch):
    """A stale native-source cache must not override a newer web-source snapshot."""
    import server.state as state

    mapi, root, _dl, cache = library_comic
    comic_id = CID + "_newest_alias_snapshot"
    for source, timestamp, update_time in (
        ("copymanga", 100, "2025-01-01"),
        ("copymanga_web", 200, "2026-10-02"),
    ):
        folder = cache / source / comic_id
        folder.mkdir(parents=True)
        (folder / "_info_full.json").write_text(json.dumps({
            "ts": timestamp, "data": {"title": "快照排序", "update_time": update_time,
                                       "volumes": [], "chapters": []},
        }), encoding="utf-8")
    state._manga_chapters_cache.pop(("copymanga", comic_id), None)
    favorites_file = root / "_favorites_latest_alias.json"
    favorites_file.write_text(json.dumps({
        f"copymanga_web:{comic_id}": {"title": "快照排序", "ts": 10},
    }), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(favorites_file), raising=False)

    response = client.get("/api/manga/favorites")
    favorite = next(row for row in response.get_json()["favorites"]
                    if row["comic_id"] == comic_id)
    assert favorite["update_time"] == "2026-10-02"


def test_catalog_publish_updates_chapter_list_and_total_as_one_generation(
        library_comic):
    """收藏更新后的章节目录与书库总数不能继续各自命中旧 TTL 缓存。"""
    import server.state as state

    _mapi, _root, _dl, cache = library_comic
    comic_id = CID + "_catalog_generation"
    cache_dir = cache / "copymanga_web" / comic_id
    cache_dir.mkdir(parents=True)
    (cache_dir / "_info_full.json").write_text(json.dumps({
        "ts": 100, "data": {"volumes": [], "chapters": [
            {"id": "old-1", "name": "第1话"},
        ]},
    }), encoding="utf-8")
    state._manga_catalog_invalidate("copymanga", comic_id)

    assert [row["id"] for row in
            state._manga_cached_chapters("copymanga_web", comic_id)] == ["old-1"]
    assert state._manga_total_chapters("copymanga", comic_id) == 1

    state._manga_catalog_publish("copymanga_web", comic_id, [
        {"id": "volume-1", "name": "第1卷"},
        {"id": "new-1", "name": "第1话"},
        {"id": "new-2", "name": "第2话"},
    ], timestamp=200)

    catalog = state._manga_cached_chapters("copymanga", comic_id)
    assert [row["id"] for row in catalog] == ["volume-1", "new-1", "new-2"]
    assert state._manga_total_chapters("copymanga_web", comic_id) == 3


def test_parallel_catalog_and_total_reads_share_one_snapshot(
        library_comic, monkeypatch):
    """Parallel book-shelf readers must use one cached catalog generation."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import server.state as state

    _mapi, _root, _dl, cache = library_comic
    comic_id = CID + "_parallel_catalog"
    folder = cache / "copymanga_web" / comic_id
    folder.mkdir(parents=True)
    (folder / "_favorites_catalog.json").write_text(json.dumps({
        "ts": 100, "data": {"volumes": [], "chapters": [
            {"id": f"ch-{i}", "name": f"第{i}话"} for i in range(30)
        ]},
    }), encoding="utf-8")
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(cache))
    state._manga_catalog_invalidate("copymanga", comic_id)
    def read_pair(_):
        catalog = state._manga_cached_chapters("copymanga", comic_id)
        return len(catalog), state._manga_total_chapters("copymanga_web", comic_id)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(read_pair, range(64)))
    assert set(results) == {(30, 30)}


def test_library_entry_reports_read_progress(client, library_comic, monkeypatch):
    """书库列表也要带 read_idx（书架显示"读到第几话"与续读落点都靠它）"""
    import server.state as st
    mapi, root, dl, cache = library_comic
    monkeypatch.setattr(st, "MANGA_LIBRARY_FILE", str(root / "_library.json"), raising=False)
    monkeypatch.setattr(st, "MANGA_HISTORY_FILE", str(root / "_history.json"), raising=False)
    (root / "_library.json").write_text(json.dumps([{
        "source": SRC, "comic_id": CID, "title": "进度测试漫画", "status": "done",
        "images": 1, "chapters": 5}]), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_LIBRARY_FILE", str(root / "_library.json"), raising=False)
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(root / "_history.json"), raising=False)
    client.post("/api/manga/history", json={
        "source": SRC, "comic_id": CID, "idx": 3, "pos": "第4話 P2", "title": "进度测试漫画"})
    lib = client.get("/api/manga/library").get_json()
    rows = lib.get("comics") or lib.get("library") or lib.get("items") or []
    mine = [x for x in rows if x.get("comic_id") == CID]
    assert mine, f"书库应列出该漫画：{str(lib)[:200]}"
    assert mine[0].get("read_idx") == 3, f"书库要带 read_idx，实际 {mine[0].get('read_idx')}"


# ── 用户怀疑的那条：清理/重建不得动进度 ────────────────────────────
def test_clearing_never_touches_history_or_library(client, library_comic, monkeypatch):
    """把所有清理/重建路径跑一遍：`_history.json` 与 `_library.json` **字节不变**"""
    import server.storage as stg
    import server.state as st
    mapi, root, dl, cache = library_comic
    lib_p = root / "_library.json"
    hist_p = root / "_history.json"
    lib_p.write_text(json.dumps([{"source": SRC, "comic_id": CID,
                                  "title": "进度测试漫画", "status": "done",
                                  "images": 1, "chapters": 5}]), encoding="utf-8")
    hist_p.write_text(json.dumps({f"{SRC}:{CID}": {"idx": 4, "pos": "第5話 P1",
                                                   "title": "进度测试漫画", "ts": 1.0}}),
                      encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_LIBRARY_FILE", str(lib_p), raising=False)
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(hist_p), raising=False)
    monkeypatch.setattr(st, "MANGA_LIBRARY_FILE", str(lib_p), raising=False)
    monkeypatch.setattr(st, "MANGA_HISTORY_FILE", str(hist_p), raising=False)
    # 临时缓存目录里也放点东西，确保清理真的干了活
    (cache / SRC / CID).mkdir(parents=True, exist_ok=True)
    (cache / SRC / CID / "tmp.bin").write_bytes(b"x" * 100)
    before_lib = lib_p.read_bytes()
    before_hist = hist_p.read_bytes()

    # 1) 可再生数据 / 2) 漫画临时缓存 / 3) 按源重建图片缓存 / 4) 回收站
    for scope in ({"scope": "regen"}, {"scope": "manga_cache"},
                  {"scope": "manga_cache", "source": SRC}, {"scope": "trash"}):
        r = client.post("/api/storage/clear", json=scope)
        assert r.status_code in (200, 400), r.get_data(as_text=True)
    rb = client.post(f"/api/manga/{SRC}/rebuild-images")
    assert rb.status_code == 200, rb.get_data(as_text=True)

    assert lib_p.read_bytes() == before_lib, "清理不得改动书库文件（用户书架）"
    assert hist_p.read_bytes() == before_hist, "清理不得改动阅读历史（续读位置）"
    # 进度仍可读回
    hist = client.get("/api/manga/history").get_json().get("history") or []
    mine = [h for h in hist if h.get("comic_id") == CID]
    assert mine and mine[0]["idx"] == 4, f"清理后进度必须还在：{mine}"

# ── 其余三条返回路径也要带身份字段（四条路径各写一遍必然复发）────────
def test_old_cached_combined_episode_order_is_repaired_without_deleting_data(
        client, library_comic, monkeypatch):
    import time
    import server.state as state
    mapi, root, dl, cache = library_comic
    cached = cache / SRC / CID / "_info_full.json"
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps({"ts": time.time(), "data": {
        "title": "旧缓存", "source": SRC, "comic_id": CID,
        "volumes": [{"id": "v2", "name": "第2卷"}, {"id": "v1", "name": "第1卷"}],
        "chapters": [{"id": "c3", "name": "第03話"},
                     {"id": "c9", "name": "第09話"},
                     {"id": "combined", "name": "第01-02话"}],
        "resume": {"index": 4, "matched_id": "combined"},
    }}, ensure_ascii=False), encoding="utf-8")
    before = cached.read_bytes()
    expected = ["v1", "v2", "combined", "c3", "c9"]
    monkeypatch.setattr(mapi, "_downloaded_ids_for_chapters", lambda src, cid, chapters: ["ch1"])
    monkeypatch.setattr(mapi, "_partial_downloaded_ids_for_chapters", lambda src, cid, chapters: set())
    monkeypatch.setattr(mapi, "_resume_payload", lambda src, cid, chapters, downloaded:
                        {"index": 2, "matched_id": "combined"} if [c["id"] for c in chapters] == expected else None)
    response = client.get(f"/api/manga/{SRC}/{CID}")
    assert response.status_code == 200
    data = response.get_json()
    assert [c["id"] for c in data["volumes"] + data["chapters"]] == expected
    assert data["resume"]["index"] == 2
    state._manga_catalog_invalidate(SRC, CID)
    assert [c["id"] for c in state._manga_cached_chapters(SRC, CID)] == expected
    assert cached.read_bytes() == before
    assert (dl / SRC / CID / "ch1" / "0000.jpg").is_file()


def test_cached_path_detail_carries_identity(client, library_comic, monkeypatch, tmp_path):
    """缓存路径（_info_full.json 命中）→ 必须带 source/comic_id"""
    import server.manga_api as mapi
    mapi_ok, root, dl, cache = library_comic
    info_p = cache / SRC / CID / "_info_full.json"
    info_p.parent.mkdir(parents=True, exist_ok=True)
    info_p.write_text(json.dumps({"data": {
        "title": "进度测试漫画(缓存)", "chapters": [{"id": "c1", "name": "第1話"}],
        "downloaded": ["c1"], "source": SRC, "comic_id": CID}}, ensure_ascii=False),
        encoding="utf-8")
    # 让 quick 路径不生效（没有 downloads 目录里的 _info.json）
    import shutil as _sh
    _sh.rmtree(dl / SRC / CID, ignore_errors=True)
    r = client.get(f"/api/manga/{SRC}/{CID}")
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d.get("source") == SRC, d
    assert d.get("comic_id") == CID, d


def test_fetch_path_detail_carries_identity(client, library_comic, monkeypatch):
    """回源路径（无缓存、走适配器）→ 必须带 source/comic_id"""
    import server.manga_api as mapi
    mapi_ok, root, dl, cache = library_comic
    import shutil as _sh
    _sh.rmtree(dl / SRC / CID, ignore_errors=True)
    _sh.rmtree(cache / SRC / CID, ignore_errors=True)
    # 假适配器：详情接口只要能构造出结构即可（这里直接替换单飞取详情的实现）
    monkeypatch.setattr(mapi, "_detail_fetch_singleflight",
                        lambda src, cid, fn: ({
                            "title": "进度测试漫画(回源)",
                            "chapters": [{"id": "c1", "name": "第1話"}],
                            "downloaded": [],
                            # 刻意**不带** source/comic_id：由服务端兜底补齐
                        }, None), raising=False)
    r = client.get(f"/api/manga/{SRC}/{CID}")
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d.get("source") == SRC, d
    assert d.get("comic_id") == CID, d
