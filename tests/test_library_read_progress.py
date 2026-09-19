# -*- coding: utf-8 -*-
"""书库阅读进度标记（新功能回归，离线）

需求：书库里为**每个漫画与小说**标注阅读进度（此前只有下载状态；漫画只有
"最近阅读时间"没有进度）。

契约：
  小说 /api/books 每条记录：
    read_idx   读到第几章（1 起，0=未读）
    read_pct   该章章内滚动百分比
    read_name  该章章节名
    read_ratio 章级进度百分比（round(read_idx/total*100)）
    percent    仍是**下载**进度（两者不可混淆）
  漫画 /api/manga/library 每条记录：
    read_idx / read_pos / read_page / read_title / read_total / read_ratio
    且 read_total 未知（无详情缓存）时为 0、read_ratio 为 0（前端不画进度条）
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CID = "1471972"
SRC = "jm"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import server.state as st
    import server.manga_api as ma

    data = tmp_path / "data"
    cache = data / "manga" / "_cache"
    dl = data / "manga" / "downloads"
    (cache / SRC / CID).mkdir(parents=True, exist_ok=True)
    (dl / SRC / CID).mkdir(parents=True, exist_ok=True)
    lib = data / "manga" / "_library.json"
    hist = data / "manga" / "_history.json"
    prog = data / "book_progress.json"
    monkeypatch.setattr(ma, "MANGA_CACHE_DIR", str(cache), raising=False)
    monkeypatch.setattr(ma, "MANGA_DOWNLOADS_DIR", str(dl), raising=False)
    monkeypatch.setattr(ma, "MANGA_LIBRARY_FILE", str(lib), raising=False)
    monkeypatch.setattr(ma, "MANGA_HISTORY_FILE", str(hist), raising=False)
    monkeypatch.setattr(st, "MANGA_CACHE_DIR", str(cache))
    monkeypatch.setattr(st, "MANGA_DOWNLOADS_DIR", str(dl))
    monkeypatch.setattr(st, "MANGA_LIBRARY_FILE", str(lib), raising=False)
    monkeypatch.setattr(st, "MANGA_HISTORY_FILE", str(hist), raising=False)
    monkeypatch.setattr(st, "BOOK_PROGRESS_FILE", str(prog), raising=False)
    monkeypatch.setattr(st, "MANGA_TOTAL_TTL", 300, raising=False)
    try:
        st._manga_total_cache.clear()      # 总话数短 TTL 记忆：用例间清干净
    except Exception:
        pass
    return {"st": st, "ma": ma, "cache": cache, "lib": lib, "hist": hist,
            "prog": prog, "tmp": tmp_path}


def _write(p, obj):
    with open(str(p), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


# ── 漫画：书库带阅读进度 ─────────────────────────────────────
def test_manga_library_marks_read_progress(env):
    _write(env["lib"], [{"source": SRC, "comic_id": CID, "title": "T",
                         "cover": "", "images": 100, "chapters": 159}])
    _write(env["hist"], {f"{SRC}:{CID}": {
        "idx": 34, "pos": "第35話 P12", "title": "T", "ts": 1700000000.0}})
    # 详情缓存给总话数
    d = env["cache"] / SRC / CID
    _write(d / "_info_full.json",
           {"data": {"chapters": [{"id": str(i)} for i in range(40)]}})

    import app
    app.app.config["TESTING"] = True
    c = app.app.test_client()
    j = c.get("/api/manga/library").get_json()
    rec = next(x for x in j["comics"] if str(x["comic_id"]) == CID)
    assert rec["read_idx"] == 34
    assert rec["read_pos"] == "第35話 P12"
    assert rec["read_page"] == 12
    assert rec["read_total"] == 40
    assert rec["read_ratio"] == round(35 / 40 * 100)      # 88
    assert rec["last_read_ts"] == 1700000000.0


def test_manga_library_unread_and_unknown_total(env):
    _write(env["lib"], [
        {"source": SRC, "comic_id": CID, "title": "未读", "cover": "", "images": 1},
        {"source": SRC, "comic_id": "888", "title": "读了但无详情缓存",
         "cover": "", "images": 1},
    ])
    _write(env["hist"], {f"{SRC}:888": {"idx": 3, "pos": "第4話 P7",
                                        "title": "x", "ts": 1.0}})
    import app
    app.app.config["TESTING"] = True
    j = app.app.test_client().get("/api/manga/library").get_json()
    by = {str(x["comic_id"]): x for x in j["comics"]}
    assert by[CID]["read_idx"] == 0 and by[CID]["read_pos"] == ""
    assert by[CID]["read_total"] == 0 and by[CID]["read_ratio"] == 0
    # 有阅读记录但没有详情缓存 → 不给百分比（前端据此不画条），但保留 pos
    assert by["888"]["read_pos"] == "第4話 P7"
    assert by["888"]["read_total"] == 0 and by["888"]["read_ratio"] == 0


def test_manga_total_chapters_reads_detail_cache_and_is_cached(env):
    st = env["st"]
    d = env["cache"] / SRC / CID
    _write(d / "_info_full.json",
           {"data": {"chapters": [{"id": "1"}, {"id": "2"}, {"id": "3"}]}})
    assert st._manga_total_chapters(SRC, CID) == 3
    # 二次调用走短 TTL 记忆：删掉文件仍然返回旧值（5 分钟内）
    os.remove(str(d / "_info_full.json"))
    assert st._manga_total_chapters(SRC, CID) == 3
    assert st._manga_total_chapters(SRC, "no_such") == 0


# ── 小说：书库带阅读进度（与下载 percent 分开） ──────────────
def test_novel_library_marks_read_progress(env, monkeypatch):
    import server.state as st
    books_dir = env["tmp"] / "data" / "books"
    bd = books_dir / "src_key"
    bd.mkdir(parents=True, exist_ok=True)
    _write(bd / "_state.json", {
        "book": {"name": "测试书", "author": "作者", "source_uid": "src"},
        "chapters": [{"url": f"u{i}"} for i in range(10)],
        "completed": ["u0", "u1"], "failed": {}, "updated_at": "2026-09-13",
    })
    # 0.55.0：下载进度只看**磁盘缓存**（completed 只是"爬取时成功过"的历史记录）。
    # 因此这里必须真的写出前两章的 .cache，才代表"下载了 2 章"。
    from engine.app_utils import cache_key_of
    for u in ("u0", "u1"):
        (bd / (cache_key_of(u) + ".cache")).write_text("正文", encoding="utf-8")
    monkeypatch.setattr(st, "BOOKS_DIR", str(books_dir), raising=False)
    _write(env["prog"], {"src_key": {"idx": 7, "pct": 42,
                                     "name": "第七章 库洛魔法使",
                                     "ts": 1788964193.0}})
    books = st._scan_books()
    b = next(x for x in books if x["key"] == "src_key")
    assert b["read_idx"] == 7
    assert b["read_pct"] == 42
    assert b["read_name"] == "第七章 库洛魔法使"
    assert b["read_ratio"] == 70              # 7/10
    assert b["percent"] == 20                 # 下载进度仍是 2/10（两者独立）
    assert b["last_read_ts"] == 1788964193.0


def test_novel_library_unread_book(env, monkeypatch):
    import server.state as st
    books_dir = env["tmp"] / "data" / "books"
    bd = books_dir / "src_key2"
    bd.mkdir(parents=True, exist_ok=True)
    _write(bd / "_state.json", {"book": {"name": "没读过", "source_uid": "src"},
                                "chapters": [{"url": "u0"}], "completed": [],
                                "failed": {}, "updated_at": ""})
    monkeypatch.setattr(st, "BOOKS_DIR", str(books_dir), raising=False)
    _write(env["prog"], {})
    b = next(x for x in st._scan_books() if x["key"] == "src_key2")
    assert b["read_idx"] == 0 and b["read_ratio"] == 0 and b["read_name"] == ""
    assert b["last_read_ts"] == 0.0


# ── 书库页面：两种卡片都要标注进度 ───────────────────────────
def test_library_template_marks_progress():
    html = open("templates/library.html", encoding="utf-8").read()
    assert "c.read_pos" in html and "c.read_ratio" in html, "漫画卡片未标记阅读进度"
    assert "b.read_idx" in html and "b.read_ratio" in html, "小说卡片未标记阅读进度"
    assert "read-line" in html and "bar read" in html, "缺少进度行/阅读进度条样式"
    assert "未读" in html, "未读状态需要显式标记"


def test_manga_library_continue_does_not_force_first_chapter():
    """书库卡片是续读入口，不能通过 ch=0 覆盖服务端 resume。"""
    html = open("templates/library.html", encoding="utf-8").read()
    assert "&ch=0&title=" not in html
    assert "书库卡片是“继续阅读”" in html
    for field in ("c.read_pos", "c.read_page", "c.read_idx", "c.read_ratio", "c.last_read_ts"):
        assert field in html, f"阅读进度字段 {field} 未纳入书库刷新指纹"


def test_manga_history_continue_does_not_force_stale_index():
    """历史列表同样交给服务端按章节身份解析，不能把旧 idx 拼成 ch。"""
    html = open("templates/manga.html", encoding="utf-8").read()
    start = html.index("box.querySelectorAll('.hist-item')")
    block = html[start:html.index("} catch(e)", start)]
    assert "&ch=" not in block
    assert "历史项也是“继续阅读”" in block


def test_manga_task_title_continue_does_not_force_first_chapter():
    """任务页标题同样是续读入口，不能以 ch=0 覆盖 resume。"""
    html = open("templates/tasks.html", encoding="utf-8").read()
    start = html.index("const nameHtml = isManga")
    block = html[start:html.index("let novelSpeedHtml", start)]
    assert "&ch=0" not in block
    assert "任务列表的漫画标题是“继续阅读”" in block
