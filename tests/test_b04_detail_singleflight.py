# -*- coding: utf-8 -*-
"""B04 回归：漫画详情回源统一单飞注册表（离线，慢/失败假构建器 + 并发线程）

缺陷背景：详情仅"过期缓存"分支有 SWR 并发锁；本地快速路径每次缺
_info_full.json 就起新线程、冷缓存直接同步回源——同书多标签首访会重复
详情抓取并产生短命线程与并发缓存写入。

验收：
- 同书并发冷启动仅执行一次详情构建（慢构建器计数）
- 快速路径只提交一个后台刷新
- 失败保留冷却：同轮等待者共享失败，冷却期内不再回源，冷却后可重试
- 注册表有界（上限 + 挂死淘汰），完成后不泄漏
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server.state as S  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_detail_state():
    with S._detail_flight_guard:
        S._detail_flights.clear()
        S._detail_swr_fail_ts.clear()
    yield
    with S._detail_flight_guard:
        S._detail_flights.clear()
        S._detail_swr_fail_ts.clear()


def _run_concurrent(fn, n=8):
    out = [None] * n

    def _w(i):
        out[i] = fn()

    ts = [threading.Thread(target=_w, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(15)
    assert all(not t.is_alive() for t in ts), "有线程未在限定时间内结束"
    return out


def test_cold_start_concurrent_builds_once():
    """同 (source, comic_id) 并发冷启动：仅 leader 执行一次构建，
    全部请求共享同一轮结果，注册表事后清空"""
    calls = []

    def builder():
        calls.append(1)
        time.sleep(0.3)  # 慢源站
        return {"chapters": [{"id": "1", "name": "第1话"}]}

    out = _run_concurrent(
        lambda: S._detail_fetch_singleflight("s1", "c1", builder))
    assert len(calls) == 1, f"详情构建被执行 {len(calls)} 次（应为 1）"
    assert all(r == {"chapters": [{"id": "1", "name": "第1话"}]}
               for r, _e in out)
    assert all(e is None for _r, e in out)
    with S._detail_flight_guard:
        assert ("s1", "c1") not in S._detail_flights  # 不泄漏


def test_failure_shared_cooldown_then_retry():
    """构建失败（软失败 None / 异常）：
    - 同轮并发等待者共享同一失败，不各自回源
    - 失败入冷却：冷却期内新请求直接"暂不可用"，不再回源
    - 冷却过后新一轮可重试（不长期固化"失败=无结果"）"""
    calls = []

    def builder():
        calls.append(1)
        time.sleep(0.2)
        return None  # 软失败：拉取与降级均无数据

    out = _run_concurrent(
        lambda: S._detail_fetch_singleflight("s1", "c2", builder))
    assert len(calls) == 1
    assert all(r is None for r, _e in out)

    # 冷却期内：直接 DetailUnavailable，不回源
    r, e = S._detail_fetch_singleflight("s1", "c2", builder)
    assert r is None and isinstance(e, S.DetailUnavailable)
    assert len(calls) == 1

    # 异常同样入冷却
    def boom():
        calls.append(1)
        raise RuntimeError("源站风控")

    r, e = S._detail_fetch_singleflight("s1", "c3", boom)
    assert r is None and isinstance(e, RuntimeError)
    r2, e2 = S._detail_fetch_singleflight("s1", "c3", boom)
    assert isinstance(e2, S.DetailUnavailable)  # 冷却中，未再执行 boom

    # 冷却过后：新一轮允许重试（手动回拨失败时间戳模拟冷却结束）
    with S._detail_flight_guard:
        S._detail_swr_fail_ts[("s1", "c2")] -= S.DETAIL_SWR_FAIL_COOLDOWN + 1
    r3, e3 = S._detail_fetch_singleflight("s1", "c2", lambda: {"ok": 1})
    assert r3 == {"ok": 1} and e3 is None


def test_async_refresh_submits_only_once():
    """快速路径 / SWR 后台刷新：并发提交只产生一个刷新线程，
    完成前重复提交被合并；完成（成功）后可再次提交"""
    calls = []
    done = threading.Event()

    def builder():
        calls.append(1)
        time.sleep(0.3)
        done.set()
        return {"ok": 1}

    submitted = _run_concurrent(
        lambda: S._detail_refresh_async("s1", "c4", builder), n=6)
    assert submitted.count(True) == 1, f"提交了 {submitted.count(True)} 个刷新"
    assert done.wait(5)
    deadline = time.time() + 2
    while time.time() < deadline:
        with S._detail_flight_guard:
            if ("s1", "c4") not in S._detail_flights:
                break
        time.sleep(0.02)
    with S._detail_flight_guard:
        assert ("s1", "c4") not in S._detail_flights
    assert len(calls) == 1
    # 成功无冷却：可再次提交新刷新
    assert S._detail_refresh_async("s1", "c4", builder) is True
    assert done.wait(5) or True
    time.sleep(0.6)
    assert len(calls) == 2


def test_async_refresh_failure_enters_cooldown():
    """后台刷新失败：冷却期内不再重复提交短命线程（防每页访问各开线程）"""
    calls = []

    def builder():
        calls.append(1)
        raise RuntimeError("风控")

    assert S._detail_refresh_async("s1", "c5", builder) is True
    deadline = time.time() + 3
    while time.time() < deadline and len(calls) < 1:
        time.sleep(0.02)
    # 等 flight finish（失败入冷却 + 注册表移除）
    deadline = time.time() + 3
    while time.time() < deadline:
        with S._detail_flight_guard:
            done_ = ("s1", "c5") not in S._detail_flights
        if done_:
            break
        time.sleep(0.02)
    # 冷却期内：提交被合并/拒绝，不再起新线程回源
    for _ in range(5):
        assert S._detail_refresh_async("s1", "c5", builder) is False
    time.sleep(0.3)
    assert len(calls) == 1


def test_follower_timeout_returns_unavailable(monkeypatch):
    """跟随者等待超时：明确"暂不可用"，不绕过 leader 自行回源"""
    entered = threading.Event()
    release = threading.Event()

    def slow():
        entered.set()
        release.wait(5)
        return {"ok": 1}

    out = []

    def leader():
        out.append(S._detail_fetch_singleflight("s1", "c6", slow, timeout=3))

    t = threading.Thread(target=leader)
    t.start()
    assert entered.wait(5)
    r, e = S._detail_fetch_singleflight("s1", "c6", slow, timeout=0.2)
    assert r is None and isinstance(e, S.DetailUnavailable)
    release.set()
    t.join(10)
    assert out[0][0] == {"ok": 1}


def test_registry_bounded_and_stale_evicted():
    """注册表有界：挂死条目（leader 意外死亡未 finish）被淘汰并释放等待者，
    新任务总能注册成功"""
    with S._detail_flight_guard:
        for i in range(S._DETAIL_FLIGHT_MAX):
            f = S._DetailFlight()
            f.ts = time.time() - S._DETAIL_FLIGHT_STALE - 1  # 挂死
            S._detail_flights[("s9", f"c{i}")] = f
    f, leader, cooling = S._detail_flight_begin("s9", "new")
    assert leader and not cooling
    with S._detail_flight_guard:
        assert len(S._detail_flights) <= S._DETAIL_FLIGHT_MAX
    S._detail_flight_finish("s9", "new", f, {"ok": 1}, None)


# ── 路由级：并发冷启动 GET /api/manga/<source>/<id> 仅一次详情构建 ──


class _SlowAdapter:
    """慢适配器桩：计数 comic_info 调用（模拟源站详情抓取耗时）"""
    key = "t1"
    name = "测试源"

    def __init__(self):
        self.calls = 0
        self._lock = threading.Lock()

    def comic_info(self, comic_id):
        with self._lock:
            self.calls += 1
        time.sleep(0.4)
        from engine.manga.base import ComicDetails, Chapter
        return ComicDetails(id=comic_id, title="测试漫画",
                            chapters=[Chapter(id="1", name="第1话"),
                                      Chapter(id="2", name="第2话"),
                                      Chapter(id="3", name="第3话")])


def test_route_cold_start_single_build(monkeypatch):
    """同书 6 标签并发首访（无本地数据、无详情缓存）：
    适配器 comic_info 仅被调用一次，全部请求拿到 200 与相同章节"""
    import app as app_mod
    import server.manga_api as A

    ad = _SlowAdapter()
    monkeypatch.setattr(A, "_load_manga_adapters", lambda: None)
    monkeypatch.setattr(A, "_manga_adapter", lambda k: ad)

    resps = [None] * 6

    def _w(i):
        c = app_mod.app.test_client()
        resps[i] = c.get("/api/manga/t1/comic1")

    ts = [threading.Thread(target=_w, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)
    assert all(r is not None and r.status_code == 200 for r in resps), \
        [getattr(r, "status_code", None) for r in resps]
    assert ad.calls == 1, f"详情回源 {ad.calls} 次（应为 1）"
    bodies = [r.get_json() for r in resps]
    assert all(len(b.get("chapters") or []) == 3 for b in bodies)


def test_route_quick_path_single_refresh(monkeypatch):
    """本地快速路径（downloads/_info.json 存在但缺完整详情缓存）：
    立即返回本地数据，且并发访问只提交一个后台刷新"""
    import app as app_mod
    import server.manga_api as A
    from engine.config import MANGA_DOWNLOADS_DIR

    ad = _SlowAdapter()
    monkeypatch.setattr(A, "_load_manga_adapters", lambda: None)
    monkeypatch.setattr(A, "_manga_adapter", lambda k: ad)

    d = os.path.join(MANGA_DOWNLOADS_DIR, "t1", "comic2")
    os.makedirs(d, exist_ok=True)
    import json as _json
    with open(os.path.join(d, "_info.json"), "w", encoding="utf-8") as f:
        _json.dump({"title": "本地漫画", "cover": "",
                    "chapters": [{"id": "1", "name": "第1话", "group": ""},
                                 {"id": "2", "name": "第2话", "group": ""},
                                 {"id": "3", "name": "第3话", "group": ""}]}, f)

    resps = [None] * 6

    def _w(i):
        c = app_mod.app.test_client()
        resps[i] = c.get("/api/manga/t1/comic2")

    ts = [threading.Thread(target=_w, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)
    assert all(r is not None and r.status_code == 200 for r in resps)
    bodies = [r.get_json() for r in resps]
    assert all(b.get("quick") for b in bodies)  # 秒回本地数据
    # 后台刷新（含 0.2s 延迟 + 0.4s 慢构建）合并为一次
    deadline = time.time() + 5
    while time.time() < deadline and ad.calls < 1:
        time.sleep(0.05)
    time.sleep(0.8)  # 等刷新完成
    assert ad.calls == 1, f"后台刷新回源 {ad.calls} 次（应为 1）"
