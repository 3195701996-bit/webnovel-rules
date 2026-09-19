# -*- coding: utf-8 -*-
"""小说搜索"不再为一个慢源干等单源时限"的契约回归（离线，不触网）

起因（2026-09-15，与漫画端 6s 耐心上限对齐）：小说流式搜索此前只在
"精确命中够多"或"总时限到"时收尾。一个卡死的源会让流一直挂到它的单源时限
（手机 9s / 桌面 6s，卡在 deadline 覆盖不到的路径还要再等 +2s）——
用户明明已经看到结果，汇总行却迟迟不出现，`搜索中` 状态也一直不消失。

新口径（与 server/manga_api._MANGA_PATIENCE 同一口径）：
  1. 只要已有源返回结果，最多再等 NOVEL_PATIENCE 秒就收尾；
  2. 未返回的源如实写进 errors（"仍在查询…"），**不**假装超时、也不假装没结果；
  3. 这一轮结果只**短暂**缓存（state.NOVEL_PARTIAL_TTL），避免"部分结果"被当成
     一小时的完整结果；
  4. 一个结果都没有时不适用耐心上限（那属于"真没搜到/源都失败"，由单源时限收尾）。
"""
import json
import os
import socket
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server.novel_api as api  # noqa: E402
import engine.search_service as ss  # noqa: E402
from server import state as st  # noqa: E402


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


def _src(uid):
    return {"uid": uid, "bookSourceName": "源" + uid,
            "bookSourceUrl": f"http://{uid}.example.com",
            "searchUrl": "/search?q={{key}}", "enabled": True}


def _book(name, uid):
    return {"name": name, "author": "作者", "book_url": f"http://{uid}/b/1",
            "source_uid": uid, "source_name": "源" + uid, "chapter_count": 10,
            "intro": "", "cover": "", "last_chapter": "", "update_time": "",
            "word_count": ""}


@pytest.fixture(autouse=True)
def _clean_cache():
    with st._toc_lock:
        st._search_cache.clear()
    st._slow_tracker._slow.clear()
    yield
    with st._toc_lock:
        st._search_cache.clear()
    st._slow_tracker._slow.clear()


def test_stream_ends_early_when_a_source_hangs(monkeypatch, client):
    """一个卡死源不得把流挂到单源时限：已有结果 + 耐心到 → 立刻收尾"""
    class _Crawler:
        adapter = None

        def __init__(self, source):
            self.source = source

        def search(self, keyword):
            if self.source.get("uid") == "hang":
                time.sleep(30)          # 卡到远远超过耐心上限
                return []
            return [_book("耐心上限测试书", "fast")]

    monkeypatch.setattr(ss, "SourceCrawler", _Crawler)
    monkeypatch.setattr(api, "load_enabled",
                        lambda: [_src("fast"), _src("hang")])
    monkeypatch.setattr(api, "NOVEL_PATIENCE", 0.6)
    monkeypatch.setattr(api, "STREAM_STOP_EXACT", 999)   # 关掉精确命中提前停止

    t = time.time()
    r = client.get("/api/search/stream?q=耐心上限测试书")
    dt = time.time() - t
    evts = _events(r)
    assert evts and evts[-1].get("finished") is True
    assert dt < 5, f"已有结果时应在耐心上限内收尾，实际 {dt:.1f}s"
    assert evts[-1]["groups"], "结果必须照常下发"
    errs = evts[-1].get("errors") or {}
    assert any("仍在查询" in v for v in errs.values()), errs
    assert "源hang" in errs
    print(f"[证据] 收尾耗时 {dt:.2f}s；errors={errs}")


def test_all_fast_sources_are_unaffected(monkeypatch, client):
    """全部源都快：行为不变（没有 errors，不需要等耐心上限）"""
    class _Crawler:
        adapter = None

        def __init__(self, source):
            self.source = source

        def search(self, keyword):
            return [_book("全快测试书", self.source.get("uid"))]

    monkeypatch.setattr(ss, "SourceCrawler", _Crawler)
    monkeypatch.setattr(api, "load_enabled", lambda: [_src("a"), _src("b")])
    monkeypatch.setattr(api, "NOVEL_PATIENCE", 5.0)
    monkeypatch.setattr(api, "STREAM_STOP_EXACT", 999)

    t = time.time()
    r = client.get("/api/search/stream?q=全快测试书")
    dt = time.time() - t
    evts = _events(r)
    assert evts[-1].get("finished") is True
    assert dt < 3, f"全部源都快时不该等耐心上限，实际 {dt:.1f}s"
    assert not (evts[-1].get("errors") or {})


def test_no_results_is_not_cut_short_by_patience(monkeypatch, client):
    """一个结果都没有时不适用耐心上限：必须等单源时限，并如实写"超时\""""
    class _Crawler:
        adapter = None

        def __init__(self, source):
            self.source = source

        def search(self, keyword):
            if self.source.get("uid") == "hang":
                time.sleep(30)
            return []                    # 另一个源也没结果

    monkeypatch.setattr(ss, "SourceCrawler", _Crawler)
    monkeypatch.setattr(api, "load_enabled", lambda: [_src("empty"), _src("hang")])
    monkeypatch.setattr(api, "NOVEL_PATIENCE", 0.2)
    monkeypatch.setattr(api, "SEARCH_SOURCE_TIMEOUT", 1)
    monkeypatch.setattr(api, "STREAM_STOP_EXACT", 999)

    t = time.time()
    r = client.get("/api/search/stream?q=没有结果测试")
    dt = time.time() - t
    evts = _events(r)
    assert evts[-1].get("finished") is True
    errs = evts[-1].get("errors") or {}
    # 等到了单源时限（~1s+2s 兜底），而不是 0.2s 的耐心上限
    assert dt >= 1.0, f"没有结果时不该按耐心上限提前收尾，实际 {dt:.1f}s"
    assert any(("超时" in v) for v in errs.values()) or errs == {}, errs


def test_partial_results_are_cached_only_briefly(monkeypatch, client):
    """部分结果只短暂缓存：过后重跑，慢源才有机会补上"""
    class _Crawler:
        adapter = None

        def __init__(self, source):
            self.source = source

        def search(self, keyword):
            if self.source.get("uid") == "hang":
                time.sleep(30)
                return []
            return [_book("部分缓存测试书", "fast")]

    monkeypatch.setattr(ss, "SourceCrawler", _Crawler)
    monkeypatch.setattr(api, "load_enabled", lambda: [_src("fast"), _src("hang")])
    monkeypatch.setattr(api, "NOVEL_PATIENCE", 0.4)
    monkeypatch.setattr(api, "STREAM_STOP_EXACT", 999)

    # 必须**消费响应体**，否则 Flask 测试客户端的生成器根本不执行（也就不会写缓存）
    evts = _events(client.get("/api/search/stream?q=部分缓存测试书"))
    assert evts and evts[-1].get("finished") is True
    with st._toc_lock:
        entry = st._search_cache.get(("部分缓存测试书", "", "default", "normal"))
    if entry is None:      # 缓存键形态可能随参数变化：按唯一键兜底取
        with st._toc_lock:
            assert len(st._search_cache) == 1, list(st._search_cache)
            entry = list(st._search_cache.values())[0]
            entry_key = list(st._search_cache.keys())[0]
    else:
        entry_key = ("部分缓存测试书", "", "default", "normal")
    assert (entry["payload"] or {}).get("partial") is True, "部分结果必须标记 partial"
    assert st._cache_ok(entry) is True, "刚写入的部分结果应当可用（短缓存内）"
    # 人为把时间拨老到超过短 TTL：必须失效（否则用户一小时内都拿不到慢源结果）
    entry["ts"] = time.time() - (st.NOVEL_PARTIAL_TTL + 1)
    with st._toc_lock:
        st._search_cache[entry_key] = entry
    assert st._cache_ok(entry) is False, "部分结果超过短 TTL 必须失效"
