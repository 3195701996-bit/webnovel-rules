# -*- coding: utf-8 -*-
"""R78 回归：小说任务快照必须带 `title`（否则下载页显示内部任务 id）。

实测缺陷（2026-09-18）：`/api/tasks` 的小说任务快照只有 id/book_url/book_key…，
**没有 title**；而客户端下载页渲染 `t.title.ifBlank { t.id }` ——
于是小说任务显示成 `t34328ff4f1` 这种机器串，暂停/删除提示也变成
"已请求暂停：t34328ff4f1"。漫画任务一直带 title，两端口径不一致。

判据（三层回退，都要成立）：
  1. 书目录里有书名 → 用书名；
  2. 书目录还没有/名字还是 URL → 用 URL 末段（有辨识度，仍不是 id）；
  3. 连 URL 都没有 → 才回退到任务 id；
  4. 同时带上 `type`（漫画分支本来就发，小说侧补 "novel"）。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def books_dir(monkeypatch, tmp_path):
    import server.state as st
    d = tmp_path / "books"
    d.mkdir()
    monkeypatch.setattr(st, "BOOKS_DIR", str(d), raising=False)
    st.invalidate_book_state(None) if hasattr(st, "invalidate_book_state") else None
    return d


def _mk_book(books, key, name):
    d = books / key
    d.mkdir(parents=True, exist_ok=True)
    with open(str(d / "_state.json"), "w", encoding="utf-8") as f:
        json.dump({"book": {"name": name, "book_url": "https://s.test/read/1/"},
                   "chapters": [{"name": "第1章", "url": "https://s.test/read/1/1.html"}],
                   "completed": [], "failed": {}}, f, ensure_ascii=False)
    import server.state as st
    st.invalidate_book_state(str(d / "_state.json"))


def test_title_uses_book_name(books_dir):
    import server.state as st
    _mk_book(books_dir, "src_abc", "剑来")
    snap = st._task_snapshot_synthetic({
        "id": "t1234567890", "source_uid": "src", "book_url": "https://s.test/read/1/",
        "book_key": "src_abc", "status": "running"})
    assert snap["title"] == "剑来"
    assert snap["type"] == "novel"


def test_title_falls_back_to_url_segment(books_dir):
    import server.state as st
    snap = st._task_snapshot_synthetic({
        "id": "t1234567890", "source_uid": "src",
        "book_url": "https://www.yetianlian.la/yt891/",
        "book_key": "missing_book", "status": "queued"})
    assert snap["title"] == "yt891", "书目录还没有时应回退到 URL 末段，而不是 id"
    assert snap["title"] != "t1234567890"


def test_title_falls_back_to_id_only_when_nothing_else(books_dir):
    import server.state as st
    snap = st._task_snapshot_synthetic({
        "id": "tabcdefghij", "source_uid": "src", "book_url": "",
        "book_key": "missing_book", "status": "queued"})
    assert snap["title"] == "tabcdefghij"


def test_state_name_equal_to_url_still_uses_url_segment(books_dir):
    """抓取早期 `book.name` 常常就是 book_url —— 那种"名字"没有信息量，
    不该当成书名显示（否则下载页会出现一长串 URL）。"""
    import server.state as st
    _mk_book(books_dir, "src_url", "https://s.test/read/1/")
    snap = st._task_snapshot_synthetic({
        "id": "t1234567890", "source_uid": "src", "book_url": "https://s.test/read/1/",
        "book_key": "src_url", "status": "running"})
    assert snap["title"] == "1", f"应退化为 URL 末段，实际 {snap['title']!r}"
