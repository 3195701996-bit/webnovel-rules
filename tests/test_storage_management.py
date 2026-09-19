# -*- coding: utf-8 -*-
"""0.57.0 回归（离线）：存储占用可见 + 选择性清理（路线 §7 P1-2）。

路线原文："存储管理：按源/作品/章节查看占用，**选择性清除错误缓存**，SAF 导入导出，
完整备份范围明确。"（SAF/备份范围不在本文件范围）

本文件锁住的契约：
1. `usage()` 的数字全部来自磁盘实扫：分类小计 = 各项之和，章节缓存数 = 真实缓存文件数；
2. `clear({"scope":"regen"})` 清可再生数据（搜索/目录缓存、日志），并**同步失效内存缓存**
   ——否则"清了但搜索依旧秒回旧结果"，用户会以为没清掉；
3. `clear({"scope":"manga_cache"})` 只删临时缓存，**manga/downloads 下的离线图片一张不动**；
4. `clear({"scope":"trash"})` 只清回收站，不碰正文与图片；
5. `clear({"scope":"book_chapters"})` 只能按**明确章节**或 `only=failed` 删，
   且删完必须把 `completed`/`failed` 里的对应条目剔除（状态不许撒谎，
   下次"继续下载"会重抓）——这是 0.55.0 那个"标已下载却读不出"缺陷的另一面；
6. 参数不合法（未知 scope、没有 only/chapters、key 带路径穿越、书不存在）一律**拒绝且不删**；
7. 删不掉的东西进 `kept` 并说明原因，`freed_bytes` 只算**真的删掉**的字节（不谎报）。
"""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.app_utils import cache_key_of  # noqa: E402


@pytest.fixture()
def store(monkeypatch, tmp_path):
    """隔离出一个假的 data 目录，并把 server.storage 的路径全部指过去"""
    import server.storage as S

    root = tmp_path / "data"
    books = root / "books"
    manga = root / "manga"
    dl = manga / "downloads"
    cache = manga / "_cache"
    trash = root / "trash"
    for d in (books, dl, cache, trash):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(S, "BOOKS_DIR", str(books), raising=False)
    monkeypatch.setattr(S, "MANGA_DOWNLOADS_DIR", str(dl), raising=False)
    monkeypatch.setattr(S, "MANGA_CACHE_DIR", str(cache), raising=False)
    monkeypatch.setattr(S, "TRASH_DIR", str(trash), raising=False)
    monkeypatch.setattr(S, "DATA_DIR", str(root), raising=False)
    monkeypatch.setattr(S, "SEARCH_CACHE_FILE", str(root / "search_cache.json"), raising=False)
    monkeypatch.setattr(S, "TOC_CACHE_FILE", str(root / "toc_cache.json"), raising=False)
    monkeypatch.setattr(S, "MANGA_LIBRARY_FILE", str(root / "manga_library.json"), raising=False)
    return S, root, books, dl, cache, trash


def _write(p, n=100):
    os.makedirs(os.path.dirname(str(p)), exist_ok=True)
    with open(str(p), "wb") as f:
        f.write(b"x" * n)
    return n


def _mk_book(books, key, chapters=3, cached=(1,), failed=(), corrupt=(), name="测试书"):
    d = books / key
    d.mkdir(parents=True, exist_ok=True)
    chs = [{"name": f"第{i}章", "url": f"https://t.example.com/{key}/{i}"} for i in range(1, chapters + 1)]
    with open(str(d / "_state.json"), "w", encoding="utf-8") as f:
        json.dump({"book": {"name": name, "author": "作者", "source_uid": key.split("_")[0]},
                   "chapters": chs,
                   "completed": [chs[i - 1]["url"] for i in cached],
                   "failed": {chs[i - 1]["url"]: "抓取失败" for i in failed}}, f, ensure_ascii=False)
    for i in cached:
        _write(d / (cache_key_of(chs[i - 1]["url"]) + ".cache"), 500)
    for i in corrupt:
        _write(d / (cache_key_of(chs[i - 1]["url"]) + ".cache"), 0)
    return d, chs


def _mk_manga(dl, cache, source="jm", cid="100", images=3, temp_works=2, temp_files=2):
    for i in range(images):
        _write(dl / source / cid / f"{i:04d}.jpg", 1000)
    for w in range(temp_works):
        for i in range(temp_files):
            _write(cache / source / f"tmp{w}" / f"{i}.bin", 300)


# ── 1) 占用明细必须是真的 ───────────────────────────────────────────
def test_usage_numbers_come_from_disk(store):
    S, root, books, dl, cache, trash = store
    _mk_book(books, "srcA_aaaa", chapters=4, cached=(1, 2), failed=(3,))
    _mk_book(books, "srcB_bbbb", chapters=2, cached=(), name="无缓存书")
    _mk_manga(dl, cache, source="jm", cid="100", images=3, temp_works=2, temp_files=2)
    _write(root / "search_cache.json", 700)
    _write(root / "toc_cache.json", 300)
    _write(root / "server.log", 50)
    _write(trash / "旧书_x", 400)

    u = S.usage()
    by = {c["key"]: c for c in u["categories"]}

    a_bytes = sum(os.path.getsize(os.path.join(str(books / "srcA_aaaa"), f))
                  for f in os.listdir(str(books / "srcA_aaaa")))
    assert by["novel_books"]["bytes"] == a_bytes + os.path.getsize(str(books / "srcB_bbbb" / "_state.json"))
    assert by["novel_books"]["count"] == 2
    assert by["manga_downloads"]["bytes"] == 3 * 1000
    assert by["manga_downloads"]["count"] == 1
    assert by["manga_cache"]["bytes"] == 2 * 2 * 300
    assert by["regen"]["bytes"] == 700 + 300 + 50
    assert by["trash"]["bytes"] == 400
    assert u["total_bytes"] == sum(c["bytes"] for c in u["categories"])

    # 逐书：章节缓存数/序号/失败数与磁盘一致
    a = next(b for b in u["novel_books"] if b["key"] == "srcA_aaaa")
    assert a["cached"] == 2 and a["cached_indexes"] == [1, 2]
    assert a["total"] == 4 and a["failed"] == 1 and a["name"] == "测试书"
    # 排序：占用大的在前
    assert u["novel_books"][0]["key"] == "srcA_aaaa"
    # 漫画已下载逐作品：图片数与标题（书库里没有就回落到 comic_id）
    md = u["manga_downloads"][0]
    assert (md["source"], md["comic_id"], md["images"]) == ("jm", "100", 3)
    assert md["title"] == "100"


def test_usage_reports_manga_title_from_library(store):
    S, root, books, dl, cache, trash = store
    _mk_manga(dl, cache, source="jm", cid="100", images=1, temp_works=0)
    with open(str(root / "manga_library.json"), "w", encoding="utf-8") as f:
        json.dump([{"source": "jm", "comic_id": "100", "title": "某漫画"}], f, ensure_ascii=False)
    u = S.usage()
    assert u["manga_downloads"][0]["title"] == "某漫画"


# ── 2) 可再生数据：删得掉、内存缓存也失效 ────────────────────────────
def test_clear_regen_removes_files_and_invalidates_memory_cache(store, monkeypatch):
    S, root, books, dl, cache, trash = store
    _write(root / "search_cache.json", 700)
    _write(root / "toc_cache.json", 300)
    _write(root / "server.log", 50)
    _mk_book(books, "srcA_aaaa", cached=(1,))
    _mk_manga(dl, cache, source="jm", cid="100", images=2, temp_works=1)

    import server.state as st
    monkeypatch.setattr(st, "_search_cache", {"q": {"payload": {}}}, raising=False)
    monkeypatch.setattr(st, "_toc_cache", {"u": {"chapters": [1]}}, raising=False)

    res = S.clear({"scope": "regen"})
    assert res["ok"] and res["freed_bytes"] == 700 + 300 + 50
    assert not os.path.exists(str(root / "search_cache.json"))
    assert not os.path.exists(str(root / "toc_cache.json"))
    assert not os.path.exists(str(root / "server.log"))
    assert st._search_cache == {} and st._toc_cache == {}, "内存缓存必须同步失效"
    # 已下载内容与临时缓存不受影响
    assert (books / "srcA_aaaa").is_dir() and (dl / "jm" / "100").is_dir()
    assert (cache / "jm").is_dir()
    assert any("未受影响" in n for n in res["notes"])


# ── 3) 漫画临时缓存：只删临时，离线图片一张不动 ──────────────────────
def test_clear_manga_cache_protects_downloads(store):
    S, root, books, dl, cache, trash = store
    _mk_manga(dl, cache, source="jm", cid="100", images=3, temp_works=2, temp_files=2)
    _mk_book(books, "srcA_aaaa", cached=(1,))

    res = S.clear({"scope": "manga_cache"})
    assert res["ok"] and res["freed_bytes"] == 2 * 2 * 300
    assert not (cache / "jm").exists()
    for i in range(3):
        assert (dl / "jm" / "100" / f"{i:04d}.jpg").is_file(), "离线图片不得被删"
    assert (books / "srcA_aaaa" / "_state.json").is_file()
    assert any("未受影响" in n for n in res["notes"])


def test_clear_manga_cache_single_work(store):
    S, root, books, dl, cache, trash = store
    _mk_manga(dl, cache, source="jm", cid="100", images=1, temp_works=0)
    _write(cache / "jm" / "100" / "a.bin", 111)
    _write(cache / "jm" / "200" / "b.bin", 222)
    _write(cache / "nhentai" / "9" / "c.bin", 333)

    res = S.clear({"scope": "manga_cache", "source": "jm", "comic_id": "100"})
    assert res["ok"] and res["freed_bytes"] == 111
    assert not (cache / "jm" / "100").exists()
    assert (cache / "jm" / "200" / "b.bin").is_file(), "同源其它作品不受影响"
    assert (cache / "nhentai" / "9" / "c.bin").is_file(), "其它源不受影响"

    res2 = S.clear({"scope": "manga_cache", "source": "jm"})
    assert res2["ok"] and res2["freed_bytes"] == 222
    assert not (cache / "jm").exists() and (cache / "nhentai").is_dir()


# ── 4) 回收站：只清回收站 ───────────────────────────────────────────
def test_clear_trash_only_touches_trash(store):
    S, root, books, dl, cache, trash = store
    _write(trash / "旧书_x", 400)
    _write(trash / "旧漫画_y" / "a.jpg", 600)
    _mk_book(books, "srcA_aaaa", cached=(1,))
    _mk_manga(dl, cache, source="jm", cid="100", images=1, temp_works=0)

    res = S.clear({"scope": "trash"})
    assert res["ok"] and res["freed_bytes"] == 1000
    assert os.path.isdir(str(trash)) and os.listdir(str(trash)) == []
    assert (books / "srcA_aaaa" / "_state.json").is_file()
    assert (dl / "jm" / "100" / "0000.jpg").is_file()


# ── 5) 章节级：按明确章节 / only=failed，且状态同步 ─────────────────
def test_clear_book_chapters_explicit_list(store):
    S, root, books, dl, cache, trash = store
    d, chs = _mk_book(books, "srcA_aaaa", chapters=3, cached=(1, 2, 3))
    p2 = d / (cache_key_of(chs[1]["url"]) + ".cache")

    res = S.clear({"scope": "book_chapters", "key": "srcA_aaaa", "chapters": [2]})
    assert res["ok"] and res["freed_bytes"] == 500
    assert not p2.exists()
    assert (d / (cache_key_of(chs[0]["url"]) + ".cache")).is_file()
    assert (d / (cache_key_of(chs[2]["url"]) + ".cache")).is_file()

    state = json.load(open(str(d / "_state.json"), encoding="utf-8"))
    assert chs[1]["url"] not in state["completed"], "completed 必须剔除已删章节"
    assert chs[0]["url"] in state["completed"] and chs[2]["url"] in state["completed"]
    # 占用与明细同步下降
    a = next(b for b in S.usage()["novel_books"] if b["key"] == "srcA_aaaa")
    assert a["cached"] == 2 and a["cached_indexes"] == [1, 3]


def test_clear_book_chapters_only_failed_includes_corrupt(store):
    S, root, books, dl, cache, trash = store
    d, chs = _mk_book(books, "srcA_aaaa", chapters=4, cached=(1, 2),
                      failed=(3,), corrupt=(4,))
    p3 = d / (cache_key_of(chs[2]["url"]) + ".cache")     # failed 章没有缓存
    p4 = d / (cache_key_of(chs[3]["url"]) + ".cache")     # 损坏（空）缓存

    res = S.clear({"scope": "book_chapters", "key": "srcA_aaaa", "only": "failed"})
    assert res["ok"], res
    assert not p4.exists(), "空/损坏缓存必须被清掉"
    assert not p3.exists()
    assert res["freed_bytes"] == 0, "空文件释放 0 字节，就报 0（不夸大）"
    assert (d / (cache_key_of(chs[0]["url"]) + ".cache")).is_file()
    state = json.load(open(str(d / "_state.json"), encoding="utf-8"))
    assert chs[3]["url"] not in state["completed"]
    assert list(state["failed"].keys()) == []


def test_clear_book_chapters_prunes_failed_entry(store):
    """删失败章节的缓存时，failed 里那条也要清（否则界面一直显示上次失败原因）"""
    S, root, books, dl, cache, trash = store
    d, chs = _mk_book(books, "srcA_aaaa", chapters=2, cached=(1,), failed=(2,))
    _write(d / (cache_key_of(chs[1]["url"]) + ".cache"), 200)   # 失败章也有半截缓存
    res = S.clear({"scope": "book_chapters", "key": "srcA_aaaa", "only": "failed"})
    assert res["ok"] and res["freed_bytes"] == 200
    state = json.load(open(str(d / "_state.json"), encoding="utf-8"))
    assert state["failed"] == {}
    assert chs[1]["url"] not in state["completed"]


# ── 6) 参数校验：一律先拒绝、后动手 ─────────────────────────────────
def test_clear_rejects_bad_targets_without_deleting(store):
    S, root, books, dl, cache, trash = store
    _mk_book(books, "srcA_aaaa", cached=(1,))
    before = S.usage()["total_bytes"]

    assert S.clear({"scope": "everything"})["ok"] is False
    assert S.clear({"scope": ""})["ok"] is False
    assert S.clear(None)["ok"] is False
    # 章节清理不给 only/chapters → 拒绝（防"一键清空整本"）
    r = S.clear({"scope": "book_chapters", "key": "srcA_aaaa"})
    assert r["ok"] is False and "only" in r["error"]
    # 路径穿越
    r = S.clear({"scope": "book_chapters", "key": "../srcA_aaaa", "chapters": [1]})
    assert r["ok"] is False and "不合法" in r["error"]
    # 不存在的书
    r = S.clear({"scope": "book_chapters", "key": "no_such_book", "chapters": [1]})
    assert r["ok"] is False and "不存在" in r["error"]
    # 越界章节号被忽略，不会误删别的
    r = S.clear({"scope": "book_chapters", "key": "srcA_aaaa", "chapters": [99, -1, "x"]})
    assert r["ok"] is True and r["freed_bytes"] == 0

    assert S.usage()["total_bytes"] == before, "被拒绝的请求不得删掉任何东西"


# ── 7) 删不掉就照实说：freed 只算真删掉的 ───────────────────────────
def test_clear_reports_kept_when_delete_fails(store, monkeypatch):
    S, root, books, dl, cache, trash = store
    _mk_manga(dl, cache, source="jm", cid="100", images=1, temp_works=1, temp_files=1)
    _write(root / "search_cache.json", 700)

    def _boom(path, *a, **k):
        raise OSError("设备忙")

    monkeypatch.setattr(S.shutil, "rmtree", _boom)
    res = S.clear({"scope": "manga_cache"})
    assert res["ok"] and res["freed_bytes"] == 0, "删不掉就不能算释放"
    assert res["kept"] and "设备忙" in res["kept"][0]["reason"]
    assert (cache / "jm").is_dir()

    # regen 里文件删除仍走 os.remove，不受影响
    res2 = S.clear({"scope": "regen"})
    assert res2["freed_bytes"] == 700


# ── 8) HTTP 层契约 ────────────────────────────────────────────────
@pytest.fixture(scope="module")
def client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


def test_storage_api_contract(client, store):
    S, root, books, dl, cache, trash = store
    _mk_book(books, "srcA_aaaa", cached=(1,))

    r = client.get("/api/storage")
    assert r.status_code == 200
    body = r.get_json()
    assert body["total_bytes"] > 0 and any(c["key"] == "novel_books" for c in body["categories"])
    assert body["novel_books"][0]["key"] == "srcA_aaaa"

    # 未知范围 → 400 且带可读原因
    r = client.post("/api/storage/clear", json={"scope": "nope"})
    assert r.status_code == 400 and "未知的清理范围" in r.get_json()["error"]

    # 正常清理 → 200 + 实测释放量
    _write(root / "search_cache.json", 123)
    r = client.post("/api/storage/clear", json={"scope": "regen"})
    assert r.status_code == 200
    assert r.get_json()["freed_bytes"] == 123

    # 章节清理走 HTTP 也要能改状态
    d, chs = _mk_book(books, "srcC_cccc", chapters=2, cached=(1, 2))
    r = client.post("/api/storage/clear",
                    json={"scope": "book_chapters", "key": "srcC_cccc", "chapters": [1]})
    assert r.status_code == 200 and r.get_json()["freed_bytes"] == 500
    state = json.load(open(str(d / "_state.json"), encoding="utf-8"))
    assert chs[0]["url"] not in state["completed"]


def test_storage_usage_does_not_walk_twice_slowly(store):
    """占用扫描是 O(文件数) 一次遍历；这里只是防止有人退回"每本书 stat 上千次"的写法"""
    S, root, books, dl, cache, trash = store
    d, chs = _mk_book(books, "srcBig_zzzz", chapters=50, cached=tuple(range(1, 51)))
    import time
    t0 = time.monotonic()
    u = S.usage()
    dt = time.monotonic() - t0
    assert next(b for b in u["novel_books"] if b["key"] == "srcBig_zzzz")["cached"] == 50
    assert dt < 1.0, f"50 章扫描应远低于 1s（实测 {dt:.3f}s）"
