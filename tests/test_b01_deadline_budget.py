# -*- coding: utf-8 -*-
"""B01 回归：请求预算（deadline）贯穿并发链路。

验收场景（全部离线、确定性）：
1) 节流等待纳入预算：窗口占满 + 短 deadline → 立即熔断，不睡满窗口。
2) Session 租约排队纳入预算：连接池占满 + 短预算请求 → 到期抛
   DeadlineExceeded，不产生迟到网络调用（假传输计数 == 0）；
   users 簿记回滚（最终全部可再借）。
3) 每域名并发信号量排队纳入预算：信号量占满 + 短预算 → 到期熔断，
   不触碰租约簿记、不发网络请求。
4) 流式搜索客户端断连：关闭生成器 → 未启动的 future 被取消、
   不执行搜索；运行中的任务保持协作语义（放行后自然结束）。
"""
import os
import sys
import threading
import time

import pytest

HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HUB)

import engine.fetcher as fm  # noqa: E402

Fetcher = fm.Fetcher
DeadlineExceeded = fm.DeadlineExceeded


class _CountingSession:
    """假传输：任何网络调用都计数并视为失败（测试中不应被调到）。"""
    def __init__(self):
        self.calls = 0
        self.closed = False

    def get(self, *a, **k):
        self.calls += 1
        raise AssertionError("预算耗尽后不应发出网络调用")

    def post(self, *a, **k):
        self.calls += 1
        raise AssertionError("预算耗尽后不应发出网络调用")

    def close(self):
        self.closed = True


def _make_state(host):
    """独立域名的 _HostState：单成员池 + 假传输 session。"""
    st = Fetcher._state_for(host)
    sess = _CountingSession()
    with st.session_lock:
        st.session = sess          # setter：重置为单成员池
        st.backend = 'cloud'
        st.ua = 'test'
        st.proxy = None
        st.session_users.clear()
        st.retired_sessions.clear()
    return st, sess


def _reset_state(st):
    with st.session_lock:
        st.session = None
        st.session_users.clear()
        st.retired_sessions.clear()


def _assert_resources_releasable(st, permits):
    """租约与信号量不泄漏：全部许可/成员锁最终可再借。"""
    got = 0
    try:
        for _ in range(permits):
            assert st.sem.acquire(timeout=0.2), "信号量许可泄漏"
            got += 1
    finally:
        for _ in range(got):
            st.sem.release()
    with st.session_lock:
        assert not st.session_users, f"users 簿记未回滚: {st.session_users}"
        entries = list(st.pool)
    for e in entries:
        assert e.lock.acquire(timeout=0.2), "池成员锁泄漏"
        e.lock.release()


# ── 1) 节流等待纳入预算 ─────────────────────────────
def test_throttle_wait_respects_deadline():
    st, _ = _make_state('b01-throttle.example')
    try:
        # 窗口 1 次/60s：第一次只记账不等待，第二次需等约 60s
        Fetcher._throttle(st, 'b01-throttle.example', n=1, ms=60000)
        t0 = time.monotonic()
        with pytest.raises(DeadlineExceeded):
            Fetcher._throttle(st, 'b01-throttle.example', n=1, ms=60000,
                              deadline=time.monotonic() + 0.2)
        el = time.monotonic() - t0
        assert el < 2.0, f"应在预算到期即熔断，实际等待 {el:.2f}s"
        # deadline=None：行为不变（整段 sleep，此处 0 等待直接返回）
        Fetcher._throttle(st, 'b01-throttle.example', n=99, ms=10)
    finally:
        _reset_state(st)


# ── 2) 连接池占满 + 短预算 → 无迟到网络调用，簿记回滚 ──
def test_lease_wait_deadline_no_late_network_call():
    st, sess = _make_state('b01-lease.example')
    entry = st.pool[0]
    entry.lock.acquire()           # 占满连接池（唯一成员被借用）
    try:
        t0 = time.monotonic()
        with pytest.raises(DeadlineExceeded):
            Fetcher().get('http://b01-lease.example/',
                          source={'bookSourceUrl': 'http://b01-lease.example'},
                          timeout=5, retries=1,
                          deadline=time.monotonic() + 0.3)
        el = time.monotonic() - t0
        assert el < 2.0, f"应在预算到期即放弃排队，实际 {el:.2f}s"
        assert sess.calls == 0, "到期后不应产生迟到网络调用"
        with st.session_lock:
            assert not st.session_users, "users 簿记须回滚"
    finally:
        entry.lock.release()
    _assert_resources_releasable(st, Fetcher._host_concurrency())
    _reset_state(st)


def test_lease_wait_deadline_rollback_closes_retired_session():
    """排队期间池被重建退休：簿记回滚时兜底关闭退休 session（防句柄泄漏）。"""
    st, sess = _make_state('b01-retire.example')
    entry = st.pool[0]
    key = id(sess)
    entry.lock.acquire()
    try:
        # 模拟排队中：簿记 +1（与 _lease_session 等待中的状态一致）
        with st.session_lock:
            st.session_users[key] = st.session_users.get(key, 0) + 1
            st.retired_sessions.add(key)   # 重建把在册旧会话标记退休
        Fetcher._rollback_lease_booking(st, entry)
        with st.session_lock:
            assert not st.session_users
            assert key not in st.retired_sessions
        assert sess.closed, "退休且计数归零的 session 应由回滚路径兜底关闭"
    finally:
        entry.lock.release()
    _reset_state(st)


# ── 3) 信号量占满 + 短预算 → 熔断，不触碰租约 ──
def test_semaphore_wait_respects_deadline():
    st, sess = _make_state('b01-sem.example')
    permits = Fetcher._host_concurrency()
    held = 0
    try:
        for _ in range(permits):   # 占满每域名并发信号量
            assert st.sem.acquire(timeout=1)
            held += 1
        t0 = time.monotonic()
        with pytest.raises(DeadlineExceeded):
            Fetcher().get('http://b01-sem.example/',
                          source={'bookSourceUrl': 'http://b01-sem.example'},
                          timeout=5, retries=1,
                          deadline=time.monotonic() + 0.3)
        el = time.monotonic() - t0
        assert el < 2.0, f"应在预算到期即放弃排队，实际 {el:.2f}s"
        assert sess.calls == 0, "到期后不应产生迟到网络调用"
        with st.session_lock:
            assert not st.session_users, "未过信号量不应触碰租约簿记"
    finally:
        for _ in range(held):
            st.sem.release()
    _assert_resources_releasable(st, permits)
    _reset_state(st)


# ── 4) 流式搜索断连：未启动 future 不执行 ──
def test_stream_close_cancels_unstarted_futures(monkeypatch):
    import app as app_mod
    import server.novel_api as api
    from server import state as _state

    started = []
    release = threading.Event()

    def _book(uid, sname):
        return {"name": "B01测试书", "author": "作者甲",
                "book_url": f"http://b01-{uid}.example.com/b/1",
                "source_uid": uid, "source_name": sname,
                "chapter_count": 1, "intro": "", "cover": "",
                "last_chapter": "", "update_time": "", "word_count": ""}

    srcs = [{"uid": f"b01_src_{i}", "bookSourceName": f"B01源{i}",
             "bookSourceUrl": f"http://b01-{i}.example.com",
             "searchUrl": "/s?q={{key}}", "enabled": True}
            for i in range(4)]

    def fake_search_one(s, q, tag=None):
        uid = s.get("uid", "")
        started.append(uid)
        if uid != "b01_src_0":
            release.wait(timeout=10)   # 运行中的任务阻塞，模拟慢源
            return []
        return [_book(uid, s.get("bookSourceName", ""))]

    monkeypatch.setattr(api, "load_enabled", lambda: list(srcs))
    monkeypatch.setattr(api, "SEARCH_MAX_WORKERS", 1)   # 串行：只启动 1 个
    monkeypatch.setattr(api, "STREAM_STOP_EXACT", 999)  # 关闭精确命中提前停止
    monkeypatch.setattr(api._search_worker, "search_one", fake_search_one)
    with _state._toc_lock:
        _state._search_cache.clear()   # 保证走实时搜索而非缓存秒回

    with app_mod.app.test_request_context("/api/search/stream?q=B01测试"):
        resp = api.api_search_stream()
        it = resp.iter_encoded()
        first = next(it)               # 源0完成 → 首个增量事件
        assert first.startswith(b"data:")
        # 等待 worker 拾起第二个源（运行中，不可取消）
        t_end = time.time() + 5
        while len(started) < 2 and time.time() < t_end:
            time.sleep(0.01)
        assert started == ["b01_src_0", "b01_src_1"]
        resp.close()                   # 客户端断连 → GeneratorExit → finally
    release.set()                      # 放行运行中的 worker
    time.sleep(0.5)                    # 若未启动任务被误执行，此窗口内会启动
    assert started == ["b01_src_0", "b01_src_1"], \
        f"未启动的 future 在断连后被执行了: {started}"
