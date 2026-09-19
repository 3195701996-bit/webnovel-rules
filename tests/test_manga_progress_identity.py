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
    return mapi, root, dl, cache


def test_quick_path_detail_carries_identity(client, library_comic):
    """quick 路径（书库里有本地数据）必须带 comic_id —— 这就是用户踩的那条"""
    mapi, root, dl, cache = library_comic
    r = client.get(f"/api/manga/{SRC}/{CID}")
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d.get("quick") is True, "应走 quick 路径（本地有 _info.json）"
    assert d.get("comic_id") == CID, f"quick 路径必须给 comic_id，实际 {d.get('comic_id')!r}"
    assert d.get("source") == SRC
    assert d.get("id") == CID, "旧的 id 字段保持兼容"


def test_history_entry_matches_detail_identity(client, library_comic):
    """历史里的 (source, comic_id) 取详情后必须一致——客户端就靠这个匹配进度"""
    mapi, root, dl, cache = library_comic
    # 先写一条进度（第 3 话）
    r = client.post("/api/manga/history", json={
        "source": SRC, "comic_id": CID, "idx": 2, "pos": "第3話 P7",
        "title": "进度测试漫画"})
    assert r.status_code == 200 and r.get_json().get("ok") is True
    hist = client.get("/api/manga/history").get_json().get("history") or []
    mine = [h for h in hist if h.get("comic_id") == CID and h.get("source") == SRC]
    assert mine, f"历史里应能按 (source, comic_id) 找到刚写的进度：{hist[:2]}"
    assert mine[0]["idx"] == 2

    det = client.get(f"/api/manga/{SRC}/{CID}").get_json()
    # 客户端比较的就是这两个字段（服务端历史数组 vs 详情响应）
    assert det.get("source") == mine[0]["source"]
    assert str(det.get("comic_id")) == str(mine[0]["comic_id"]), \
        "详情与历史的身份字段必须一致，否则续读落点会退回第一话"


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
