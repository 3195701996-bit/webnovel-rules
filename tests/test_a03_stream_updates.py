# -*- coding: utf-8 -*-
"""A03 回归：流式搜索"增量新增 + 更新 + 最终快照"协议（服务端侧）。

场景：同书的不同来源先后返回（离线、确定性）——
1) 增量阶段：首个携带该分组的事件必须是 groups（新增语义）且只有 1 个来源；
   后续事件以 updates 语义补齐同 key 分组的第 2 个来源（不再整组重发 groups）。
2) finished 事件：groups 为全量最终快照，同书分组包含全部来源，
   排序为最终排序序（与到达顺序无关：晚到的精确命中排前）。
3) 缓存命中：清空缓存后实时搜索收尾 → 再次同参数请求秒回，
   cached 事件的分组数据/排序与实时收尾快照完全一致。
"""
import json
import os
import sys

import pytest

HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HUB)


@pytest.fixture(scope="module")
def client():
    import app
    app.app.config['TESTING'] = True
    return app.app.test_client()


def _events(resp):
    out = []
    for chunk in resp.data.decode('utf-8').split('\n\n'):
        chunk = chunk.strip()
        if chunk.startswith('data:'):
            out.append(json.loads(chunk[5:].strip()))
    return out


def _key(g):
    return (g.get('name') or '') + '|' + (g.get('author') or '')


@pytest.fixture()
def two_sources(monkeypatch):
    """两个假书源：A 先完成（同书 1 源），B 后完成（同书第 2 源 + 一本精确命中书）。

    确定性手段：
    - SEARCH_MAX_WORKERS=1 → 线程池串行，future 按提交顺序完成（A 先 B 后）
    - STREAM_STOP_EXACT=999 → 关闭精确命中提前停止
    - 清空 _search_cache → 保证走实时搜索而非缓存秒回
    """
    import server.novel_api as api
    from server import state as _state

    src_a = {"uid": "a03_src_a", "bookSourceName": "A03源A",
             "bookSourceUrl": "http://a03-a.example.com",
             "searchUrl": "/search?q={{key}}", "enabled": True}
    src_b = {"uid": "a03_src_b", "bookSourceName": "A03源B",
             "bookSourceUrl": "http://a03-b.example.com",
             "searchUrl": "/search?q={{key}}", "enabled": True}

    def _book(name, author, url, uid, sname, chapters):
        return {"name": name, "author": author, "book_url": url,
                "source_uid": uid, "source_name": sname,
                "chapter_count": chapters, "intro": "", "cover": "",
                "last_chapter": "", "update_time": "", "word_count": ""}

    books_by_uid = {
        "a03_src_a": [
            _book("A03协议测试书", "作者甲", "http://a03-a.example.com/book/1",
                  "a03_src_a", "A03源A", 100),
        ],
        "a03_src_b": [
            _book("A03协议测试书", "作者甲", "http://a03-b.example.com/book/9",
                  "a03_src_b", "A03源B", 250),
            # 书名精确 == 关键词 → 最终排序必须排第一（晚到但排前）
            _book("A03协议测试", "作者乙", "http://a03-b.example.com/book/2",
                  "a03_src_b", "A03源B", 30),
        ],
    }

    monkeypatch.setattr(api, "load_enabled", lambda: [src_a, src_b])
    monkeypatch.setattr(api, "SEARCH_MAX_WORKERS", 1)
    monkeypatch.setattr(api, "STREAM_STOP_EXACT", 999)
    monkeypatch.setattr(
        api._search_worker, "search_one",
        lambda s, q, tag=None: [dict(b) for b in books_by_uid.get(s.get("uid"), [])])
    with _state._toc_lock:
        _state._search_cache.clear()
    return src_a, src_b


def test_stream_incremental_update_and_final_snapshot(client, two_sources):
    r = client.get("/api/search/stream?q=A03协议测试")
    assert r.status_code == 200
    evts = _events(r)
    assert len(evts) >= 2, "两源先后完成 + 最终快照，至少应有进度事件与 finished 事件"
    final = evts[-1]
    assert final.get("finished") is True

    key = "A03协议测试书|作者甲"

    # 1) 增量新增：首个携带该分组的事件必须是 groups（新增），且只有源 A
    first_with = next(
        (e for e in evts if any(_key(g) == key for g in e.get("groups", []))),
        None)
    assert first_with is not None, "同书首个来源应以 groups（新增语义）推送"
    assert not first_with.get("finished"), "新增应发生在增量阶段"
    g0 = next(g for g in first_with["groups"] if _key(g) == key)
    assert [s["source_uid"] for s in g0["sources"]] == ["a03_src_a"]

    # 2) 增量更新：同 key 分组的晚到来源必须经 updates 事件补齐
    upd_events = [e for e in evts[:-1]
                  if any(_key(g) == key for g in e.get("updates", []))]
    assert upd_events, "同书晚到来源应通过 updates（更新语义）事件补齐"
    g1 = next(g for g in upd_events[-1]["updates"] if _key(g) == key)
    assert {s["source_uid"] for s in g1["sources"]} == {"a03_src_a", "a03_src_b"}, \
        "updates 应携带补齐全部来源的完整分组"

    # 3) 最终快照：同书一个分组包含全部来源；排序为最终序而非到达序
    gfin = next(g for g in final["groups"] if _key(g) == key)
    assert {s["source_uid"] for s in gfin["sources"]} == {"a03_src_a", "a03_src_b"}
    names = [_key(g) for g in final["groups"]]
    assert names[0] == "A03协议测试|作者乙", \
        "晚到的精确命中书应经最终快照校准排序排第一（到达顺序中它最后）"
    assert key in names


def test_stream_cache_hit_matches_realtime_snapshot(client, two_sources):
    """实时收尾快照与缓存命中秒回的分组数据/排序完全一致。"""
    q = "A03协议测试"
    r1 = client.get(f"/api/search/stream?q={q}")
    evts1 = _events(r1)
    realtime_final = evts1[-1]
    assert realtime_final.get("finished") is True
    assert not realtime_final.get("cached"), "缓存已清空，首搜必须走实时搜索"

    r2 = client.get(f"/api/search/stream?q={q}")
    evts2 = _events(r2)
    assert len(evts2) == 1, "缓存命中应为单事件秒回"
    cached = evts2[0]
    assert cached.get("cached") is True and cached.get("finished") is True
    assert cached["groups"] == realtime_final["groups"], \
        "缓存命中的分组数据/排序必须与实时收尾快照一致"
