# -*- coding: utf-8 -*-
"""R78 回归：流式搜索的 `done/total` 在**缓存命中路径**必须与实时路径同语义。

实测缺陷（2026-09-18，手机口径 15 个启用源）：
  · App 用流式事件里的 `done/total` 显示进度行：**"已返回 N/M 个源"**；
  · 实时路径 `done` = 已完成的源数（1…N）✓；
  · 但**缓存命中路径**把 `done` 填成了 `timed_out_sources`（通常是 0）——
    于是同一关键词第二次搜索（1h 内命中缓存）会显示
    **"已返回 0/18 个源"**，而结果其实**全在**（95 个分组、520 条来源）。
    用户看到的是"什么都没返回"，还会以为搜索坏了。

判据：
  1. 缓存命中事件的 `done == total`（等价于"全部源都已返回"：结果本就是
     那一次全量搜索的快照）；
  2. `timed_out_sources` / `partial` 作为**独立字段**带出，不再顶替 `done`；
  3. 缓存与实时的分组数一致（不因缓存而少给结果）。
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _seed_cache(q, groups, timed_out=0, partial=False):
    from server import novel_api as na
    key = (q, "", "default", "all")
    with na._toc_lock:
        na._search_cache[key] = {
            "ts": time.time(),
            "ver": na.SEARCH_CACHE_VERSION,
            "payload": {"groups": groups, "total": len(groups),
                        "partial": partial, "timed_out_sources": timed_out},
        }
    return key


def _read_events(client, q):
    r = client.get(f"/api/search/stream?q={q}", buffered=False)
    assert r.status_code == 200
    out = []
    for raw in r.response:
        line = (raw.decode() if isinstance(raw, bytes) else raw).strip()
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


def test_cached_stream_reports_all_sources_returned(client, monkeypatch):
    q = "缓存语义测试关键词"
    groups = [{"name": "测试书", "author": "作者", "sources": [
        {"source_uid": "u1", "source_name": "源1", "chapter_count": 10}]}]
    _seed_cache(q, groups, timed_out=2, partial=False)

    # 源列表是本地的（命中缓存前仍要它算 total），注入固定列表让断言确定；
    # 关键是**不得**发生实时搜索——真去联网的话事件数会 >1。
    from server import novel_api as na
    fake = [{"uid": f"u{i}", "bookSourceName": f"源{i}",
             "bookSourceUrl": f"https://s{i}.test", "searchUrl": "https://s.test/s?q={key}"}
            for i in range(1, 6)]
    monkeypatch.setattr(na, "load_enabled", lambda: list(fake))

    evts = _read_events(client, q)
    assert len(evts) == 1, f"缓存命中应只推一条事件，实际 {len(evts)}"
    ev = evts[0]
    assert ev.get("finished") is True and ev.get("cached") is True
    assert ev["total"] == len(fake), ev["total"]
    assert ev["done"] == ev["total"], (
        f"缓存命中的 done 必须等于 total（App 显示'已返回 N/M 个源'），"
        f"实际 {ev['done']}/{ev['total']}")
    assert ev.get("timed_out_sources") == 2, "超时源数要独立成字段带出来"
    assert ev.get("partial") is False
    assert len(ev.get("groups") or []) == len(groups)


def test_cached_stream_keeps_partial_flag(client):
    """部分结果（有源超时）也要如实带出，便于界面说明"""
    q = "缓存语义测试关键词2"
    _seed_cache(q, [{"name": "书", "author": "", "sources": [
        {"source_uid": "u1", "chapter_count": 1}]}], timed_out=3, partial=True)
    ev = _read_events(client, q)[-1]
    assert ev["done"] == ev["total"]
    assert ev.get("partial") is True and ev.get("timed_out_sources") == 3
