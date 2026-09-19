# -*- coding: utf-8 -*-
"""B06 回归残留修复（离线）：暂停/停止后导出入口可生成部分全文

B06 把 merge_txt 改为仅 done 路径合并（state.py），但导出端点
/api/books/<key>/txt 在 book.txt 缺失时直接 404——任何暂停/停止过
的书从此无法导出（旧版本此处可导出已下载部分）。

修复契约：
1. book.txt 缺失时按需构造 CrawlTask(resume=True) → _load_state() →
   merge_txt() 生成"已下载部分"全文，导出 200 且章节有序；
2. book.txt 存在但 _state.json updated_at 晚于其 mtime（暂停后续爬
   过/单章重爬过）时按需重建，导出内容含新章；
3. book.txt 与当前章节内容版本一致（_export.json 的 rev）时不重建，原样下发；
   导出版本机制（2026-09-10）取代 updated_at/mtime 时间戳推断；
   旧数据（无 _export.json）视为陈旧 → 首次导出重建并补齐版本元数据；
4. 无章节/无缓存可合并时仍 404。
"""
import json
import os
import shutil
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOOK_URL = "http://b06x.example.com/book/1"


@pytest.fixture(scope="module")
def client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


@pytest.fixture()
def fake_source(monkeypatch):
    """导出重建路径需要 get_by_uid 命中一个源（CrawlTask 构造用，不走网络）"""
    import server.novel_api as napi
    monkeypatch.setattr(napi, "get_by_uid", lambda uid: {
        "uid": uid, "bookSourceName": "B06X假源",
        "bookSourceUrl": "http://b06x.example.com"})


def _mk_book(key, chapters, completed_n, updated_at=None):
    """造一本书：_state.json + 前 completed_n 章的 .cache；不生成 book.txt"""
    from server.state import BOOKS_DIR, invalidate_book_state
    from engine.app_utils import cache_key_of

    d = os.path.join(BOOKS_DIR, key)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    state = {
        "book": {"source_uid": "b06x_fake", "book_url": BOOK_URL,
                 "name": "部分下载书"},
        "chapters": chapters,
        "completed": [c["url"] for c in chapters[:completed_n]],
        "failed": {},
        "updated_at": updated_at
                      or time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    sp = os.path.join(d, "_state.json")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    invalidate_book_state(sp)
    for c in chapters[:completed_n]:
        with open(os.path.join(d, cache_key_of(c["url"]) + ".cache"),
                  "w", encoding="utf-8") as f:
            f.write(f"{c['name']}\n\n{c['name']}的正文内容，足够长以区分指纹。" * 20)
    return d


def _chapters(n):
    return [{"name": f"第{i}章", "url": f"{BOOK_URL}/c{i}.html"}
            for i in range(1, n + 1)]


def test_export_missing_txt_generates_partial(client, fake_source):
    """暂停态（book.txt 缺失）→ 导出端点按需合并已下载部分，200 且有序"""
    key = "b06x_missing1"
    d = _mk_book(key, _chapters(3), completed_n=2)
    try:
        assert not os.path.exists(os.path.join(d, "book.txt"))
        r = client.get(f"/api/books/{key}/txt")
        assert r.status_code == 200
        txt = r.get_data(as_text=True)
        assert "第1章" in txt and "第2章" in txt
        assert "第3章" not in txt, "未下载章节不得出现在部分全文中"
        assert txt.index("第1章") < txt.index("第2章"), "导出章节必须有序"
        # 副作用：book.txt 已按需生成，后续导出直接命中
        assert os.path.exists(os.path.join(d, "book.txt"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_export_stale_txt_rebuilt(client, fake_source):
    """book.txt 滞后于断点（updated_at 更晚）→ 重建后含新下载的章"""
    key = "b06x_stale1"
    chapters = _chapters(3)
    d = _mk_book(key, chapters, completed_n=3)
    try:
        # 旧的 book.txt 只含第一章（模拟暂停前的旧合并产物），
        # mtime 拨到 1 小时前 → updated_at（现在）更晚 → 必须重建
        p = os.path.join(d, "book.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("第1章\n旧合并产物只有第一章。")
        old = time.time() - 3600
        os.utime(p, (old, old))
        r = client.get(f"/api/books/{key}/txt")
        assert r.status_code == 200
        txt = r.get_data(as_text=True)
        assert "旧合并产物" not in txt, "滞后 book.txt 未被重建"
        assert "第3章" in txt, "重建后应包含全部已下载章"
        assert txt.index("第1章") < txt.index("第2章") < txt.index("第3章")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_export_fresh_txt_not_rebuilt(client, fake_source):
    """版本一致的 book.txt → 原样下发，不重建

    导出版本机制：新鲜度 = _export.json 的 rev 与当前章节缓存指纹一致
    （不再依赖 updated_at/mtime 时间戳）。首次导出生成全文与元数据，
    随后再次导出必须命中新鲜路径：内容一致且文件未被重写（mtime_ns 不变）。
    """
    key = "b06x_fresh1"
    chapters = _chapters(2)
    d = _mk_book(key, chapters, completed_n=2,
                 updated_at=time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(time.time() - 3600)))
    try:
        r1 = client.get(f"/api/books/{key}/txt")
        assert r1.status_code == 200
        p = os.path.join(d, "book.txt")
        first = r1.get_data(as_text=True)
        assert "第1章" in first and "第2章" in first
        mt = os.stat(p).st_mtime_ns
        r2 = client.get(f"/api/books/{key}/txt")
        assert r2.status_code == 200
        assert r2.get_data(as_text=True) == first
        assert os.stat(p).st_mtime_ns == mt, "版本一致时不应重建 book.txt"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_export_legacy_txt_without_meta_rebuilt(client, fake_source):
    """旧数据迁移：无 _export.json 的既有 book.txt 视为陈旧 → 重建并补元数据"""
    from engine.crawler import read_export_meta

    key = "b06x_legacy1"
    chapters = _chapters(2)
    d = _mk_book(key, chapters, completed_n=2,
                 updated_at=time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(time.time() - 3600)))
    try:
        p = os.path.join(d, "book.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("旧全文（无版本元数据）")
        r = client.get(f"/api/books/{key}/txt")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "旧全文" not in body, "无版本元数据 → 应重建"
        assert "第1章" in body and "第2章" in body
        assert read_export_meta(d).get("rev"), "重建后应写入版本元数据"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_export_no_chapters_still_404(client, fake_source):
    """没有任何可合并内容（无章节）时仍 404"""
    key = "b06x_empty1"
    d = _mk_book(key, [], completed_n=0)
    try:
        r = client.get(f"/api/books/{key}/txt")
        assert r.status_code == 404
        assert not os.path.exists(os.path.join(d, "book.txt"))
    finally:
        shutil.rmtree(d, ignore_errors=True)
