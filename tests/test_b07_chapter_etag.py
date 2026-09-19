# -*- coding: utf-8 -*-
"""B07 回归（离线）：单章端点条件缓存前置 + 高精度 ETag。

原实现（server/novel_api.py api_book_chapter）：
先把 .cache 全文读进内存，再比 If-None-Match——304 也付出了完整读盘；
且 ETag 用 int(getmtime)，同秒重爬/修复后标识不变，客户端拿陈旧正文。

修复契约：
1. 先 stat 取 (mtime_ns, size) 指纹比对条件请求头，命中直接 304，不读正文；
2. ETag 含 mtime_ns 与 size：同秒内更新章节也产生新标识；
3. 行为契约不变：200 带 Cache-Control: private, max-age=3600 与 ETag，
   304 带同样两个头；未下载章节仍返回 downloaded=False 且无缓存头。
"""
import builtins
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOOK_KEY = "b07src_a1b2c3d4"
CH_URL = "http://b07.example.com/chapter/1"
CH_NAME = "第一章 测试"


@pytest.fixture(scope="module")
def client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


@pytest.fixture(params=[BOOK_KEY, "中文书源_a1b2c3d4"])
def book(request):
    """造一本单章"已下载"的书，返回 (cache_path, chapter_api_url)"""
    from server.state import BOOKS_DIR, invalidate_book_state
    from engine.app_utils import cache_key_of

    key = request.param
    d = os.path.join(BOOKS_DIR, key)
    os.makedirs(d, exist_ok=True)
    state_path = os.path.join(d, "_state.json")
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({
            "book": {"source_uid": "b07src", "book_url": "http://b07.example.com/"},
            "chapters": [{"url": CH_URL, "name": CH_NAME}],
            "completed": [CH_URL],
            "failed": {},
        }, f, ensure_ascii=False)
    invalidate_book_state(state_path)
    cp = os.path.join(d, cache_key_of(CH_URL) + ".cache")
    with open(cp, "w", encoding="utf-8") as f:
        f.write("旧正文内容，版本一。")
    yield cp, f"/api/books/{key}/chapter/1"


def _count_open(monkeypatch, target):
    """统计对 target 路径的 open() 次数（其余路径正常放行）"""
    real_open = builtins.open
    calls = []

    def spy(file, *a, **k):
        if os.fspath(file) == target:
            calls.append(file)
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    return calls


def test_etag_hit_returns_304_without_reading_body(client, book, monkeypatch):
    cp, url = book
    r1 = client.get(url)
    assert r1.status_code == 200
    etag = r1.headers["ETag"]
    assert r1.headers["Cache-Control"] == "private, max-age=3600"

    calls = _count_open(monkeypatch, cp)
    r2 = client.get(url, headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.headers["ETag"] == etag
    assert r2.headers["Cache-Control"] == "private, max-age=3600"
    assert calls == [], "命中 ETag 时不得读取正文文件"


def test_same_second_update_yields_new_etag_and_body(client, book):
    cp, url = book
    # 固定到整秒：mtime 小数清零
    sec = int(os.stat(cp).st_mtime)
    os.utime(cp, ns=(sec * 10**9, sec * 10**9))
    r1 = client.get(url)
    etag1 = r1.headers["ETag"]

    # 同秒、同长度更新正文（仅 mtime_ns 不同）→ 必须产生新 ETag
    old_size = os.stat(cp).st_size
    with open(cp, "w", encoding="utf-8") as f:
        f.write("新正文内容，版本二。")  # 与旧内容同字节数
    assert os.stat(cp).st_size == old_size, "前提：尺寸不变，仅 mtime_ns 区分"
    os.utime(cp, ns=(sec * 10**9 + 999, sec * 10**9 + 999))
    assert int(os.stat(cp).st_mtime) == sec, "前提：mtime 仍在同一秒"

    r2 = client.get(url, headers={"If-None-Match": etag1})
    assert r2.status_code == 200, "同秒更新后旧 ETag 不得命中"
    assert r2.headers["ETag"] != etag1
    assert r2.get_json()["content"] == "新正文内容，版本二。"


def test_etag_format_and_unchanged_contract(client, book):
    cp, url = book
    st = os.stat(cp)
    r = client.get(url)
    assert r.status_code == 200
    assert r.headers["ETag"] == f'"1:{st.st_mtime_ns}:{st.st_size}"'
    assert r.headers["ETag"].isascii()
    r.headers["ETag"].encode("latin-1")
    assert r.headers["Cache-Control"] == "private, max-age=3600"
    body = r.get_json()
    assert body["downloaded"] is True
    assert body["name"] == CH_NAME
    assert body["content"] == "旧正文内容，版本一。"
    assert body["total"] == 1 and body["prev"] is None and body["next"] is None


def test_not_downloaded_has_no_cache_headers(client, book):
    cp, url = book
    os.remove(cp)
    r = client.get(url)
    assert r.status_code == 200
    assert r.get_json()["downloaded"] is False
    assert "ETag" not in r.headers
    assert "Cache-Control" not in r.headers
