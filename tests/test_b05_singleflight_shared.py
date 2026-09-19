# -*- coding: utf-8 -*-
"""B05 回归：单飞失败与超时路径共享同轮结果，不再转为重复请求（离线）

缺陷背景：
1. server/state.py 漫画搜索单飞：跟随者 wait 后递归重入（"深度≤2"仅靠注释，
   无实际计数），leader 失败（空结果/带 errors 不缓存）时跟随者各自重跑
   一遍全源搜索；
2. server/manga_api.py 图片单飞：跟随者等候后若无本地缓存直接重新取远程图
   ——leader 失败/超时后出现第二个同图生产者。

验收：
- leader 失败时所有跟随者收到同轮错误/负载，回源次数=1，不串行重跑
- 失败结果秒级冷却：瞬时重试共享同轮失败；冷却后新一轮用户操作可重试
- leader 超时不出现第二个同图生产者（明确"暂不可用"）
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server.state as S  # noqa: E402

KEY = ("kw-b05", "", 1, "")


@pytest.fixture(autouse=True)
def _clean_search_state():
    with S._MANGA_SEARCH_LOCK:
        S._MANGA_SEARCH_FETCHING.clear()
        S._manga_search_cache.clear()
        S._manga_search_fail.clear()
    yield
    with S._MANGA_SEARCH_LOCK:
        S._MANGA_SEARCH_FETCHING.clear()
        S._manga_search_cache.clear()
        S._manga_search_fail.clear()


def _run_concurrent(fn, n=6):
    out = [None] * n

    def _w(i):
        try:
            out[i] = ("ok", fn())
        except Exception as e:
            out[i] = ("err", e)

    ts = [threading.Thread(target=_w, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(15)
    assert all(not t.is_alive() for t in ts), "有线程未在限定时间内结束"
    return out


def test_search_failure_payload_shared_no_rerun():
    """leader 返回失败负载（空结果 + errors，不入主缓存）：
    全部跟随者共享同一轮负载，回源次数 = 1（旧实现跟随者会递归重跑）"""
    calls = []

    def builder():
        calls.append(1)
        time.sleep(0.3)
        return {"results": [], "errors": {"src": "源站超时"}}

    out = _run_concurrent(lambda: S._manga_search_singleflight(KEY, builder))
    assert len(calls) == 1, f"回源 {len(calls)} 次（应为 1）"
    oks = [v for tag, v in out if tag == "ok"]
    assert len(oks) == 6, out
    assert all(p[0].get("errors") == {"src": "源站超时"} for p in oks)

    # 秒级冷却：瞬时重试共享同轮失败负载，不立刻再回源
    payload, cached = S._manga_search_singleflight(KEY, builder)
    assert payload.get("errors") == {"src": "源站超时"} and cached is False
    assert len(calls) == 1

    # 冷却过后：新一轮用户操作可重试（不长期固化"失败=无结果"）
    with S._MANGA_SEARCH_LOCK:
        ts, p, e = S._manga_search_fail[KEY]
        S._manga_search_fail[KEY] = (
            ts - S._MANGA_SEARCH_FAIL_COOLDOWN - 1, p, e)
    payload2, _ = S._manga_search_singleflight(
        KEY, lambda: {"results": [{"id": "x"}]})
    assert payload2.get("results") == [{"id": "x"}]


def test_search_leader_exception_shared_to_all():
    """leader 抛异常：所有跟随者收到同轮错误（同一异常类型），
    不串行重跑；冷却期内瞬时重试直接失败不回源"""

    class Boom(Exception):
        pass

    calls = []

    def builder():
        calls.append(1)
        time.sleep(0.3)
        raise Boom("源站风控")

    out = _run_concurrent(lambda: S._manga_search_singleflight(KEY, builder))
    assert len(calls) == 1, f"回源 {len(calls)} 次（应为 1）"
    errs = [v for tag, v in out if tag == "err"]
    assert len(errs) == 6 and all(isinstance(e, Boom) for e in errs)

    with pytest.raises(Boom):
        S._manga_search_singleflight(KEY, builder)
    assert len(calls) == 1  # 冷却期内未再回源


def test_search_follower_timeout_unavailable(monkeypatch):
    """跟随者等待超时：明确 RuntimeError"暂不可用"，不绕过 leader 自行构建"""
    monkeypatch.setattr(S, "_MANGA_SEARCH_WAIT_TIMEOUT", 0.2)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow():
        calls.append(1)
        entered.set()
        release.wait(5)
        return {"results": [{"id": "x"}]}

    out = []
    t = threading.Thread(
        target=lambda: out.append(S._manga_search_singleflight(KEY, slow)))
    t.start()
    assert entered.wait(5)
    with pytest.raises(RuntimeError, match="暂不可用"):
        S._manga_search_singleflight(KEY, slow)
    assert len(calls) == 1  # 跟随者未自行回源
    release.set()
    t.join(10)
    assert out and out[0][0].get("results") == [{"id": "x"}]


def test_search_success_still_cached_and_shared():
    """成功路径不回归：leader 落主缓存，跟随者共享；后续请求命中缓存"""
    calls = []

    def builder():
        calls.append(1)
        time.sleep(0.2)
        return {"results": [{"id": "a"}, {"id": "b"}]}

    out = _run_concurrent(lambda: S._manga_search_singleflight(KEY, builder))
    assert len(calls) == 1
    assert all(tag == "ok" and v[0]["results"] for tag, v in out)
    payload, cached = S._manga_search_singleflight(KEY, builder)
    assert cached is True and len(calls) == 1


# ── 图片端点：leader 失败/超时不出现第二个同图生产者 ──


@pytest.fixture
def _img_patches(monkeypatch):
    import server.manga_api as A
    with A._img_fetching_lock:
        A._img_fetching.clear()
        A._img_fail_ts.clear()
    monkeypatch.setattr(A, "_manga_read_adapter", lambda k: object())
    yield A
    with A._img_fetching_lock:
        A._img_fetching.clear()
        A._img_fail_ts.clear()


_IMG_URL = "/api/manga/t1/comic9/chapter/c9/img/0"


def _hit_image_concurrent(app, n=6):
    resps = [None] * n

    def _w(i):
        c = app.test_client()
        resps[i] = c.get(_IMG_URL)

    ts = [threading.Thread(target=_w, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(15)
    assert all(not t.is_alive() for t in ts)
    return resps


def test_image_leader_failure_no_second_producer(_img_patches, monkeypatch):
    """leader 取图失败：跟随者共享同轮失败（503 暂不可用），
    不自行回源当第二个生产者；秒级冷却后刷新可重试"""
    import app as app_mod
    A = _img_patches
    calls = []

    def fake_remote(ad, source, comic_id, chapter_id, idx):
        calls.append(1)
        time.sleep(0.3)
        raise RuntimeError("源站 503")

    monkeypatch.setattr(A, "_serve_remote_image", fake_remote)

    resps = _hit_image_concurrent(app_mod.app)
    assert len(calls) == 1, f"远程取图 {len(calls)} 次（应为 1）"
    codes = sorted(r.status_code for r in resps)
    assert codes.count(500) == 1          # leader：同轮失败
    assert codes.count(503) == 5          # 跟随者：共享失败，明确暂不可用

    # 秒级冷却：立即刷新不再回源
    c = app_mod.app.test_client()
    r = c.get(_IMG_URL)
    assert r.status_code == 503 and len(calls) == 1

    # 冷却过后：新一轮用户操作允许重试
    with A._img_fetching_lock:
        A._img_fail_ts[("t1", "comic9", "c9", 0)] -= A._IMG_FAIL_COOLDOWN + 1
    r = c.get(_IMG_URL)
    assert r.status_code == 500 and len(calls) == 2


def test_image_follower_timeout_no_second_producer(_img_patches, monkeypatch):
    """leader 挂慢：跟随者等待超时返回 503，不当第二个生产者"""
    import app as app_mod
    A = _img_patches
    monkeypatch.setattr(A, "_IMG_FETCH_WAIT_TIMEOUT", 0.2)
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def slow_remote(ad, source, comic_id, chapter_id, idx):
        calls.append(1)
        entered.set()
        release.wait(5)
        raise RuntimeError("超时兜底失败")

    monkeypatch.setattr(A, "_serve_remote_image", slow_remote)

    resps = [None]

    def _leader():
        c = app_mod.app.test_client()
        resps[0] = c.get(_IMG_URL)

    t = threading.Thread(target=_leader)
    t.start()
    assert entered.wait(5)
    c = app_mod.app.test_client()
    r = c.get(_IMG_URL)
    assert r.status_code == 503           # 等待超时：明确暂不可用
    assert len(calls) == 1                # 未出现第二个生产者
    release.set()
    t.join(10)
    assert resps[0].status_code == 500


def test_image_leader_success_followers_served_zero_refetch(
        _img_patches, monkeypatch):
    """leader 成功：图片已落磁盘缓存，跟随者本地重查直出（零回源），
    全部请求 200，远程取图仅一次"""
    import app as app_mod
    A = _img_patches
    calls = []

    def fake_remote(ad, source, comic_id, chapter_id, idx):
        calls.append(1)
        d = S._manga_media_root(source, comic_id, chapter_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{idx:04d}.webp"), "wb") as f:
            f.write(b"\x00" * 2000)
        time.sleep(0.3)
        return A._serve_local_image(d, idx)

    monkeypatch.setattr(A, "_serve_remote_image", fake_remote)

    resps = _hit_image_concurrent(app_mod.app)
    assert len(calls) == 1, f"远程取图 {len(calls)} 次（应为 1）"
    assert all(r.status_code == 200 for r in resps), \
        [r.status_code for r in resps]
    # 无失败 → 无冷却：后续请求（新单飞轮）本地直出，不再回源
    c = app_mod.app.test_client()
    r = c.get(_IMG_URL)
    assert r.status_code == 200 and len(calls) == 1
