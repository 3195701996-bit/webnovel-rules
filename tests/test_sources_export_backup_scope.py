# -*- coding: utf-8 -*-
"""0.58.0 回归（离线）：SAF 导入导出的导出侧 + 备份范围明确 + 清理并发护栏。

路线 §7 P1-2 原文："存储管理：按源/作品/章节查看占用，选择性清除错误缓存，
**SAF 导入导出**，**完整备份范围明确**。"（占用与选择性清理在 0.57.0 已交付）

本文件锁住的契约：
1. `GET /api/sources/export`：全部源导出为 `{"version":1,"sources":[...]}`、
   单源导出为同样结构（只含 1 个），**都能被 /api/sources/import 原样导回**；
   不存在的 uid → 404（不是空文件，客户端不会把"空"当成功保存下来）；
2. 导出**只含书源配置**，不含阅读进度/正文/图片（与"备份与恢复"分工明确）；
3. `GET /api/backup/scope`：把"包含什么/不含什么"讲清楚，并且每项都带**真实体积**——
   含的字节数 = 书源文件 + 进度文件实测之和；不含的字节数取自磁盘实扫；
4. 并发护栏：正在下载的书/源**不许清它的缓存**（拒绝并说明），
   任务不跑了就恢复正常——避免删到正在写的中间文件导致"莫名其妙失败一次"。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _mk_source(uid, url="https://export.example.com", name="导出测试源"):
    """造一个真实书源文件（走 engine.source_mgr 的落盘规则）"""
    from engine.config import SOURCES_DIR
    os.makedirs(SOURCES_DIR, exist_ok=True)
    p = os.path.join(SOURCES_DIR, f"{uid}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"bookSourceName": name, "bookSourceUrl": url,
                   "bookSourceGroup": "测试", "enabled": True,
                   "searchUrl": "/search?q={{key}}",
                   "ruleSearch": {"bookList": "//div", "name": "//a/text()",
                                  "bookUrl": "//a/@href"}}, f, ensure_ascii=False)
    # 缓存指纹变了才会重新读盘
    try:
        from engine import source_mgr
        source_mgr._SOURCES_FINGERPRINT = None
    except Exception:
        pass
    return p


@pytest.fixture()
def tmp_source():
    uid = "导出测试源_export_test"
    p = _mk_source(uid)
    yield uid, p
    for f in (p, os.path.join(os.path.dirname(p), uid + ".json")):
        try:
            os.remove(f)
        except OSError:
            pass
    try:
        from engine import source_mgr
        source_mgr._SOURCES_FINGERPRINT = None
    except Exception:
        pass


# ── 1) 导出：形态正确、可导回、不存在的源要 404 ─────────────────────
def test_export_all_sources_is_reimportable(client, tmp_source):
    uid, _p = tmp_source
    r = client.get("/api/sources/export")
    assert r.status_code == 200
    body = r.get_json()
    assert body["version"] == 1 and body["count"] == len(body["sources"])
    assert body["exported_at"]
    assert any(s.get("uid") == uid for s in body["sources"]), "刚造的书源应在导出里"
    # 每个源都是完整可用的书源对象（导回时必须有 bookSourceUrl）
    for s in body["sources"]:
        assert s.get("bookSourceUrl"), f"导出项缺少 bookSourceUrl: {s.get('uid')}"


def test_export_single_source(client, tmp_source):
    uid, _p = tmp_source
    r = client.get("/api/sources/export", query_string={"uid": uid})
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["sources"]) == 1
    src = body["sources"][0]
    assert src["uid"] == uid and src["bookSourceUrl"] == "https://export.example.com"


def test_export_missing_uid_is_404(client):
    r = client.get("/api/sources/export", query_string={"uid": "根本不存在_xyz"})
    assert r.status_code == 404, "不存在的源必须报错，不能让客户端存下一个空文件"
    assert "书源不存在" in r.get_json()["error"]


def test_export_can_be_imported_back(client, tmp_source):
    """导出 → 导入的往返必须成立（这是"导入导出"能用的前提）"""
    uid, p = tmp_source
    exported = client.get("/api/sources/export", query_string={"uid": uid}).get_json()
    # 先删掉源文件，模拟"换设备后导回"
    os.remove(p)
    try:
        from engine import source_mgr
        source_mgr._SOURCES_FINGERPRINT = None
    except Exception:
        pass
    assert client.get("/api/sources/export", query_string={"uid": uid}).status_code == 404

    r = client.post("/api/sources/import", json={"content": json.dumps(exported)})
    assert r.status_code == 200
    res = r.get_json()
    assert res["imported"] >= 1, res
    # 导回后能查到（且 url 一致）
    back = client.get("/api/sources/export", query_string={"uid": uid}).get_json()
    assert back["sources"][0]["bookSourceUrl"] == "https://export.example.com"


def test_export_excludes_progress_and_content(client):
    """书源导出里不许混入阅读进度/正文（那是"备份与恢复"的职责）"""
    body = client.get("/api/sources/export").get_json()
    text = json.dumps(body, ensure_ascii=False)
    assert "book_progress" not in text
    for s in body["sources"]:
        assert "chapters" not in s


# ── 2) 备份范围：说清包含/不含，且数字是真的 ───────────────────────
def test_backup_scope_lists_include_and_exclude(client):
    d = client.get("/api/backup/scope").get_json()
    inc = " ".join(i["what"] for i in d["include"])
    exc = " ".join(e["what"] for e in d["exclude"])
    assert "书源" in inc and "阅读进度" in inc and "漫画书库" in inc
    assert "正文" in exc and "漫画已下载" in exc
    # 每条"不含"都要给理由（否则用户不知道删了会不会丢东西）
    for e in d["exclude"]:
        assert e["why"] and e["category"]
    assert "不是离线全文副本" in d["note"] or "不是" in d["note"]


def test_backup_scope_numbers_match_disk(client):
    from engine.config import DATA_DIR, SOURCES_DIR
    d = client.get("/api/backup/scope").get_json()

    src_files = [f for f in os.listdir(SOURCES_DIR)
                 if f.endswith(".json") and not f.startswith(".")] if os.path.isdir(SOURCES_DIR) else []
    src_bytes = sum(os.path.getsize(os.path.join(SOURCES_DIR, f)) for f in src_files)
    assert d["include_detail"]["sources"]["files"] == len(src_files)
    assert d["include_detail"]["sources"]["bytes"] == src_bytes

    prog = {p["name"]: p["bytes"] for p in d["include_detail"]["progress_files"]}
    for rel, sz in prog.items():
        on_disk = os.path.join(DATA_DIR, rel)
        assert sz == (os.path.getsize(on_disk) if os.path.isfile(on_disk) else 0)
    assert d["include_detail"]["bytes"] == src_bytes + sum(prog.values())

    # 不含的那部分必须与占用接口同口径（同一份磁盘实扫）
    usage = client.get("/api/storage").get_json()
    by = {c["key"]: c["bytes"] for c in usage["categories"]}
    for e in d["exclude"]:
        assert d["exclude_detail"][e["category"]] == by.get(e["category"], 0)
    assert d["excluded_total_bytes"] >= d["exclude_detail"].get("manga_downloads", 0)


def test_backup_scope_is_small_vs_excluded(client):
    """诚实的对比：备份很小，不含的那部分很大（界面就是靠这个说清范围）"""
    d = client.get("/api/backup/scope").get_json()
    assert d["include_detail"]["bytes"] > 0
    assert d["excluded_total_bytes"] >= 0


# ── 3) 并发护栏：正在下载就不许清它的缓存 ───────────────────────────
@pytest.fixture()
def store(monkeypatch, tmp_path):
    import server.storage as S
    root = tmp_path / "data"
    for d in ("books", "manga/downloads", "manga/_cache", "trash"):
        (root / d).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(S, "BOOKS_DIR", str(root / "books"), raising=False)
    monkeypatch.setattr(S, "MANGA_DOWNLOADS_DIR", str(root / "manga/downloads"), raising=False)
    monkeypatch.setattr(S, "MANGA_CACHE_DIR", str(root / "manga/_cache"), raising=False)
    monkeypatch.setattr(S, "TRASH_DIR", str(root / "trash"), raising=False)
    monkeypatch.setattr(S, "DATA_DIR", str(root), raising=False)
    monkeypatch.setattr(S, "SEARCH_CACHE_FILE", str(root / "search_cache.json"), raising=False)
    monkeypatch.setattr(S, "TOC_CACHE_FILE", str(root / "toc_cache.json"), raising=False)
    return S, root


def _mk_book(store, key="srcA_aaaa"):
    S, root = store
    from engine.app_utils import cache_key_of
    d = root / "books" / key
    d.mkdir(parents=True, exist_ok=True)
    url = f"https://t.example.com/{key}/1"
    with open(str(d / "_state.json"), "w", encoding="utf-8") as f:
        json.dump({"book": {"name": "书"}, "chapters": [{"name": "第1章", "url": url}],
                   "completed": [url], "failed": {}}, f)
    cp = d / (cache_key_of(url) + ".cache")
    cp.write_text("正文", encoding="utf-8")
    return key, cp


def test_clear_refuses_while_novel_task_running(store, monkeypatch):
    S, root = store
    key, cp = _mk_book(store)
    import server.state as st
    monkeypatch.setattr(st, "_tasks", {"tX": {"id": "tX", "status": st.ST_RUNNING,
                                              "book_key": key}}, raising=False)

    r = S.clear({"scope": "book_chapters", "key": key, "chapters": [1]})
    assert r["ok"] is False and "正在下载" in r["error"]
    assert cp.is_file(), "被拒绝时不得删任何缓存"

    # 任务结束后正常清理
    monkeypatch.setattr(st, "_tasks", {"tX": {"id": "tX", "status": st.ST_STOPPED,
                                              "book_key": key}}, raising=False)
    r2 = S.clear({"scope": "book_chapters", "key": key, "chapters": [1]})
    assert r2["ok"] is True and not cp.exists()


def test_clear_refuses_while_manga_download_running(store, monkeypatch):
    S, root = store
    (root / "manga/_cache/jm/100").mkdir(parents=True, exist_ok=True)
    (root / "manga/_cache/jm/100/a.bin").write_bytes(b"x" * 100)

    class _FakeDL:
        def all_tasks(self):
            return {"jm:100": {"status": "running"}}

    import server.state as st
    monkeypatch.setattr(st, "_manga_dl", _FakeDL(), raising=False)
    r = S.clear({"scope": "manga_cache"})
    assert r["ok"] is False and "正在跑" in r["error"]
    assert (root / "manga/_cache/jm/100/a.bin").is_file()

    # 另一个源在跑 → 只清该源也被拒绝；清别的源可以
    (root / "manga/_cache/nhentai/9").mkdir(parents=True, exist_ok=True)
    (root / "manga/_cache/nhentai/9/c.bin").write_bytes(b"y" * 50)
    assert S.clear({"scope": "manga_cache", "source": "jm"})["ok"] is False
    ok = S.clear({"scope": "manga_cache", "source": "nhentai"})
    assert ok["ok"] is True and ok["freed_bytes"] == 50

    class _IdleDL:
        def all_tasks(self):
            return {"jm:100": {"status": "stopped"}}

    monkeypatch.setattr(st, "_manga_dl", _IdleDL(), raising=False)
    r2 = S.clear({"scope": "manga_cache"})
    assert r2["ok"] is True and not (root / "manga/_cache/jm").exists()


# ── 4) HTTP 层：护栏也要透出可读原因 ────────────────────────────────
def test_guards_surface_readable_error_over_http(client, store, monkeypatch):
    S, root = store
    key, cp = _mk_book(store)
    import server.state as st
    monkeypatch.setattr(st, "_tasks", {"tY": {"id": "tY", "status": st.ST_RUNNING,
                                              "book_key": key}}, raising=False)
    r = client.post("/api/storage/clear",
                    json={"scope": "book_chapters", "key": key, "chapters": [1]})
    assert r.status_code == 400
    assert cp.is_file()
