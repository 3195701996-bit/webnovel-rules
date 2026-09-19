# -*- coding: utf-8 -*-
"""小说章节缓存自愈（0.74.9）：**读取路径发现的事实，state 必须承认**。

## 缺陷

读取端点早就能发现"缓存文件丢失"（给出可行动原因），但 `_state.json` 的
`completed` 里仍留着那一章。而「检查更新 → 下载缺失」的缺失检测是拿
**目录 vs completed** 对比的（纯本地、不读磁盘正文），于是它认为"一个都不缺"——
用户只能逐章手点「重试」，整本"继续下载"永远补不回丢失的章节。

## 本文件锁死

1. 缓存丢失时，读取返回"缓存文件已丢失…可点重试 **或对整本检查更新自愈**"；
2. 同一次读取把该章从 `completed` **剔除并落盘**（其它章节不受影响）；
3. 剔除之后，缺失检测（目录 vs completed）能看见它 —— 这正是自愈的关键；
4. 自愈失败（文件不可写等）**不影响读取语义**：用户仍能看到原因并手动重试。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.app_utils import book_key_of, cache_key_of  # noqa: E402

UID = "demo_src__demo.example"
BURL = "https://demo.example/read/1/"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离数据目录 + 一本 3 章、全部"已下载"的书"""
    data = tmp_path / "data"
    import server.state as st
    import server.novel_api as na
    books = data / "books"
    books.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(st, "DATA_DIR", str(data), raising=False)
    monkeypatch.setattr(st, "BOOKS_DIR", str(books), raising=False)
    monkeypatch.setattr(na, "BOOKS_DIR", str(books), raising=False)
    monkeypatch.setattr(na, "DATA_DIR", str(data), raising=False)

    key = book_key_of(UID, BURL)
    d = books / key
    d.mkdir(parents=True, exist_ok=True)
    chs = [{"name": f"第{i}章", "url": f"https://demo.example/read/1/{i}.html"}
           for i in range(1, 4)]
    (d / "_state.json").write_text(json.dumps({
        "book": {"name": "演示书", "author": "作者", "source_uid": UID},
        "chapters": chs,
        "completed": [c["url"] for c in chs],
        "failed": {},
    }, ensure_ascii=False), encoding="utf-8")
    for c in chs:
        (d / (cache_key_of(c["url"]) + ".cache")).write_text(
            f"{c['name']}的正文内容" * 20, encoding="utf-8")

    import app
    return app.app.test_client(), d, chs, key


def _completed(d):
    return json.loads((d / "_state.json").read_text(encoding="utf-8")).get("completed") or []


def test_missing_cache_is_removed_from_completed(client):
    c, d, chs, key = client
    target = chs[1]                       # 第 2 章
    os.remove(d / (cache_key_of(target["url"]) + ".cache"))

    r = c.get(f"/api/books/{key}/chapter/2").get_json()
    assert r["downloaded"] is False
    assert "缓存文件已丢失" in (r.get("reason") or "")
    assert "检查更新" in (r.get("reason") or ""), "要告诉用户怎么整本自愈"

    done = _completed(d)
    assert target["url"] not in done, "丢失的章节必须从 completed 剔除"
    assert chs[0]["url"] in done and chs[2]["url"] in done, (
        "其它章节不许被误剔除：%s" % done)


def test_missing_detection_now_sees_the_lost_chapter(client):
    """剔除之后，缺失检测（目录 vs completed）才看得见它——自愈的关键一步"""
    c, d, chs, key = client
    os.remove(d / (cache_key_of(chs[0]["url"]) + ".cache"))
    c.get(f"/api/books/{key}/chapter/1")
    done = set(_completed(d))
    missing = [ch for ch in chs if ch["url"] not in done]
    assert [m["name"] for m in missing] == ["第1章"], missing


def test_healthy_chapter_read_does_not_touch_state(client):
    """正常读取不得改 state（避免把"读一次"变成"写一次"）"""
    c, d, chs, key = client
    before = _completed(d)
    r = c.get(f"/api/books/{key}/chapter/1").get_json()
    assert r["downloaded"] is True and r["content"]
    assert _completed(d) == before


def test_unread_chapter_never_in_completed_is_reported_plainly(client):
    """从没下载过的章节：说"尚未下载"，且不做任何 state 改动"""
    c, d, chs, key = client
    st = json.loads((d / "_state.json").read_text(encoding="utf-8"))
    st["completed"] = [chs[0]["url"]]           # 只下过第 1 章
    (d / "_state.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    os.remove(d / (cache_key_of(chs[2]["url"]) + ".cache"))
    before = _completed(d)
    r = c.get(f"/api/books/{key}/chapter/3").get_json()
    assert r["downloaded"] is False and "尚未下载" in (r.get("reason") or "")
    assert _completed(d) == before


def test_selfheal_failure_does_not_break_reading(client, monkeypatch):
    """state 写不进去（磁盘满/权限）时，读取语义不受影响：照样给出原因"""
    c, d, chs, key = client
    import server.novel_api as na

    def _boom(*a, **k):
        raise OSError("模拟磁盘满")
    monkeypatch.setattr(na, "atomic_write", _boom, raising=False)
    os.remove(d / (cache_key_of(chs[0]["url"]) + ".cache"))
    r = c.get(f"/api/books/{key}/chapter/1").get_json()
    assert r["downloaded"] is False and "缓存文件已丢失" in (r.get("reason") or "")
