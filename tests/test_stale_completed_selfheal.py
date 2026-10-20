# -*- coding: utf-8 -*-
"""0.55.0 回归（离线）：completed 与磁盘缓存不一致时必须自愈 + 界面照实呈现。

问题（P1-3 小说回归实测发现，emulator-5554）：
把书目录删掉时任务还在跑，任务线程随后用内存状态把目录**写回**——
`_state.json.completed` 里留着第 1 章，而第 1 章的 `.cache` 已经不在了。
后果链：
  · 书架/详情按 `url in completed or 有缓存` 判定 → 显示"已下载 1/1278"；
  · 读者点开第 1 章 → 单章端点返回 `downloaded=false, content=""`（空白页，无原因）；
  · 用户"继续下载" → `is_done` 命中 completed → **永远跳过这一章**，无自救路径。

修复契约：
1. 单章端点内容为空时给 `reason`：state 说已完成而缓存不在 → 明确说明缓存丢失
   且可点重试；真正的未下载 → "本章尚未下载"；
2. 详情/书库的"已下载"只看磁盘缓存（`done`/`downloaded`），并把
   `state_completed`/`stale_completed` 作为诊断字段暴露（不再谎报）；
3. 任务开爬前把 completed 与磁盘对齐：缓存丢失的章节重新进入待下载队列
   （`_sync_completed_with_disk`），进度按缓存计。
"""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOOK_URL = "http://stale.example.com/book/1"
BOOK_KEY = "stale_src_deadbeef"


def _chapters(n=3):
    return [{"name": f"第{i}章", "url": f"{BOOK_URL}/c{i}.html"}
            for i in range(1, n + 1)]


@pytest.fixture(scope="module")
def client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _mk_book(key=BOOK_KEY, completed_n=3, cache_n=2, total=3):
    """造书：state 里 completed 前 completed_n 章，磁盘上只有前 cache_n 章的缓存"""
    from server.state import BOOKS_DIR, invalidate_book_state
    from engine.app_utils import cache_key_of

    chapters = _chapters(total)
    d = os.path.join(BOOKS_DIR, key)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    sp = os.path.join(d, "_state.json")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump({
            "book": {"source_uid": "stale_src", "book_url": BOOK_URL,
                     "name": "缓存不一致书"},
            "chapters": chapters,
            "completed": [c["url"] for c in chapters[:completed_n]],
            "failed": {},
        }, f, ensure_ascii=False)
    invalidate_book_state(sp)
    for c in chapters[:cache_n]:
        with open(os.path.join(d, cache_key_of(c["url"]) + ".cache"),
                  "w", encoding="utf-8") as f:
            f.write(f"{c['name']}\n\n{c['name']}的正文，长度足够当作真实缓存。" * 10)
    return d, chapters


def _invalidate(d):
    from server.state import invalidate_book_state
    invalidate_book_state(os.path.join(d, "_state.json"))


@pytest.fixture()
def stale_book():
    """默认书：3 章，state 说全完成，磁盘上只有前 2 章缓存 → 第 3 章是"谎报"章"""
    d, chapters = _mk_book()
    yield d, chapters
    shutil.rmtree(d, ignore_errors=True)
    _invalidate(d)


# ── 1) 单章端点：空内容必须给原因，不许空白 ───────────────────────────
def test_chapter_missing_cache_reports_reason(client, stale_book):
    """state 说已完成、缓存被清掉 → downloaded=false 且 reason 说明缓存丢失"""
    d, chapters = stale_book
    # 第 3 章在 completed 里、但磁盘上没有缓存（模拟"缓存被清理/目录被重建"）
    from engine.app_utils import cache_key_of
    assert not os.path.exists(os.path.join(d, cache_key_of(chapters[2]["url"]) + ".cache"))
    _invalidate(d)

    r = client.get(f"/api/books/{BOOK_KEY}/chapter/3")
    assert r.status_code == 200
    body = r.get_json()
    assert body["downloaded"] is False and body["content"] == ""
    assert "缓存" in body.get("reason", ""), "缓存丢失必须给可读原因"
    assert "重试" in body["reason"]


def test_chapter_never_downloaded_reason(client):
    """真正没下过的章节（completed 里也没有）：原因就是"尚未下载"，不误导成缓存丢失"""
    key = "stale_src_never1"
    d, _ = _mk_book(key=key, completed_n=1, cache_n=1, total=3)
    try:
        _invalidate(d)
        r = client.get(f"/api/books/{key}/chapter/3")
        assert r.status_code == 200
        body = r.get_json()
        assert body["downloaded"] is False
        assert body.get("reason") == "本章尚未下载"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_chapter_downloaded_still_serves_content(client, stale_book):
    """正常章节不受影响：内容和 downloaded 照旧"""
    r = client.get(f"/api/books/{BOOK_KEY}/chapter/2")
    body = r.get_json()
    assert body["downloaded"] is True
    assert len(body["content"]) > 100
    assert "reason" not in body


# ── 2) 详情/书库：已下载只看磁盘缓存 ─────────────────────────────────
def test_detail_downloaded_follows_disk(client, stale_book):
    """缓存不在的章节在详情里必须显示未下载（此前靠 completed 谎报已下载）"""
    d, chapters = stale_book
    _invalidate(d)

    body = client.get(f"/api/books/{BOOK_KEY}").get_json()
    assert body["total"] == 3
    assert body["done"] == 2, f"已下载应为磁盘上真实存在的 2 章，实际 {body['done']}"
    assert body["chapters"][2]["downloaded"] is False, "缓存不在 = 未下载"
    assert body["chapters"][0]["downloaded"] is True
    assert body["state_completed"] == 3
    assert body["stale_completed"] == 1, "诊断字段要能看出 state 与磁盘差 1 章"


def test_scan_books_done_follows_disk(client, stale_book):
    """书库列表的 done 与详情同口径（不把 completed 当已下载）"""
    d, chapters = stale_book
    from engine.app_utils import cache_key_of
    os.remove(os.path.join(d, cache_key_of(chapters[0]["url"]) + ".cache"))
    _invalidate(d)

    books = client.get("/api/books").get_json()["books"]
    me = [b for b in books if b.get("key") == BOOK_KEY]
    assert me, "刚造的书必须出现在书库列表"
    assert me[0]["done"] == 1


# ── 3) 任务自愈：缓存丢了的章节要重新下载 ────────────────────────────
@pytest.mark.parametrize("total", [4999, 5000, 5035])
def test_large_book_download_count_matches_detail(client, total):
    key = f"stale_large_{total}"
    _mk_book(key=key, completed_n=total, cache_n=2, total=total)
    detail = client.get(f"/api/books/{key}").get_json()
    books = client.get("/api/books").get_json()["books"]
    summary = next(book for book in books if book["key"] == key)
    assert summary["done"] == detail["done"] == 2
    assert detail["stale_completed"] == total - 2


@pytest.mark.parametrize("cache_kind", ["empty", "directory", "whitespace", "bad_utf8"])
def test_invalid_cache_is_not_downloaded(client, cache_kind):
    from engine.app_utils import cache_key_of
    key = f"stale_invalid_{cache_kind}"
    d, chapters = _mk_book(key=key, completed_n=3, cache_n=2)
    path = os.path.join(d, cache_key_of(chapters[2]["url"]) + ".cache")
    if cache_kind == "directory":
        os.mkdir(path)
    elif cache_kind == "bad_utf8":
        with open(path, "wb") as f:
            f.write(b"valid prefix\xff\xfe")
    elif cache_kind == "whitespace":
        with open(path, "w", encoding="utf-8") as f:
            f.write(" \n\t\u3000")
    else:
        with open(path, "w", encoding="utf-8"):
            pass
    detail = client.get(f"/api/books/{key}").get_json()
    books = client.get("/api/books").get_json()["books"]
    assert next(b for b in books if b["key"] == key)["done"] == 2
    assert detail["done"] == 2
    assert detail["percent"] == 66.7
    assert detail["chapters"][2]["downloaded"] is False
    assert client.get(f"/api/books/{key}/chapter/3").get_json()["downloaded"] is False
    stat = os.stat(path)
    conditional = client.get(f"/api/books/{key}/chapter/3", headers={
        "If-None-Match": f'"3:{stat.st_mtime_ns}:{stat.st_size}"'})
    assert conditional.status_code == 200
    assert conditional.get_json()["downloaded"] is False


@pytest.mark.parametrize("offline", [False, True])
def test_check_update_detects_missing_cache_without_opening_reader(monkeypatch, offline):
    import server.state as st
    key = f"stale_check_{offline}"
    d, chapters = _mk_book(key=key, completed_n=3, cache_n=2)
    state_path = os.path.join(d, "_state.json")
    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)
    state["completed"] = [c["url"] for c in chapters]
    state["failed"] = {chapters[0]["url"]: "stale failure"}
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    _invalidate(d)

    class Crawler:
        def __init__(self, source):
            pass

        def get_book(self, url):
            if offline:
                raise OSError("offline")
            return object()

        def get_toc(self, book, **kwargs):
            return chapters

    monkeypatch.setattr(st, "get_by_uid", lambda uid: {"uid": uid})
    monkeypatch.setattr(st, "SourceCrawler", Crawler)
    st._run_check_update(key)
    assert st._check_jobs[key]["status"] == "done"
    assert st._check_jobs[key]["missing_count"] == 1
    assert st._check_jobs[key]["retry_failed"] == 0
    assert st._check_missing[key]["chapters"] == chapters[2:]


@pytest.mark.parametrize("cache_kind", ["missing", "empty", "directory", "bad_utf8", "whitespace"])
@pytest.mark.parametrize("stale_failure", [False, True])
def test_crawl_prunes_completed_without_cache(monkeypatch, cache_kind, stale_failure):
    """开爬前 completed 与磁盘对齐：丢缓存的章节重新进入待下载队列"""
    from engine.crawler import CrawlTask
    from engine.app_utils import cache_key_of

    d, chapters = _mk_book(key="stale_src_crawl1", completed_n=3, cache_n=2)
    # 第 3 章缓存丢失（completed 里仍在）→ 必须被剔除并重抓
    assert not os.path.exists(os.path.join(d, cache_key_of(chapters[2]["url"]) + ".cache"))
    invalid_path = os.path.join(d, cache_key_of(chapters[2]["url"]) + ".cache")
    if cache_kind == "empty":
        with open(invalid_path, "w", encoding="utf-8"):
            pass
    elif cache_kind == "directory":
        os.mkdir(invalid_path)
    elif cache_kind == "bad_utf8":
        with open(invalid_path, "wb") as f:
            f.write(b"\xff\xfe")
    elif cache_kind == "whitespace":
        with open(invalid_path, "w", encoding="utf-8") as f:
            f.write(" \n\t")

    src = {"uid": "stale_src", "bookSourceName": "假源",
           "bookSourceUrl": "http://stale.example.com"}
    task = CrawlTask(src, BOOK_URL, d, resume=True)
    assert task._has_cache(chapters[2]["url"]) is False
    if cache_kind == "directory":
        os.rmdir(invalid_path)  # Assert detection without obstructing atomic repair.
    assert task._load_state() is True
    assert len(task.completed) == 3
    if stale_failure:
        task.failed[chapters[0]["url"]] = "old failure after successful cache write"
        task._save_state()  # crawl(resume=True) reloads state; exercise persisted failure.

    fetched = []

    class _FakeCrawler:
        def __init__(self, *a, **k):
            pass

        def get_content(self, url, timeout=25):
            fetched.append(url)
            return f"{url} 的正文内容" * 20

    monkeypatch.setattr(task, "crawler", _FakeCrawler())
    monkeypatch.setattr(task, "progress_callback", lambda payload: None)
    task.crawl()

    assert chapters[2]["url"] in fetched, "缓存丢失的章节必须被重新抓取"
    assert chapters[0]["url"] not in fetched, "缓存还在的章节不得重复抓取"
    from engine.app_utils import cache_key_of as _ck
    assert os.path.exists(os.path.join(d, _ck(chapters[2]["url"]) + ".cache"))
    assert len(task.completed) == 3 and task._done_count() == 3
    assert task.failed == {}
    with open(os.path.join(d, "_state.json"), encoding="utf-8") as f:
        assert json.load(f)["failed"] == {}

    shutil.rmtree(d, ignore_errors=True)


def test_sync_completed_drops_urls_not_in_toc(monkeypatch):
    """目录里已经没有的旧 url 也算过期条目（换源/目录更新后不留僵尸进度）"""
    from engine.crawler import CrawlTask

    d, chapters = _mk_book(key="stale_src_zombie1", completed_n=1, cache_n=1)
    src = {"uid": "stale_src", "bookSourceName": "假源",
           "bookSourceUrl": "http://stale.example.com"}
    task = CrawlTask(src, BOOK_URL, d, resume=True)
    task._load_state()
    task.completed = list(task.completed) + ["http://stale.example.com/gone.html"]
    task._completed_set = set(task.completed)
    dropped = task._sync_completed_with_disk()
    assert dropped == 1
    assert "http://stale.example.com/gone.html" not in task.completed

    shutil.rmtree(d, ignore_errors=True)
