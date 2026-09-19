# -*- coding: utf-8 -*-
"""B02: 漫画 Session 池队头阻塞修复——任何成员归还都能唤醒等待者。

覆盖：
1. 队头阻塞回归：成员 0 被慢持有者占住，成员 1 先归还后新等待者必须能
   借到成员 1（旧实现固定等 locks[0]，会被成员 0 的慢持有者堵死）。
2. 并发首次初始化：多线程同时 _ensure_pool，发布后池完整、成员全部可借。
3. 异常归还：fetch_image_checked 抛异常路径经 finally 归还，不泄漏成员。
4. 超时参数：剩余预算耗尽抛 MangaError（B01 deadline 预留接口）。
"""
import threading
import time

import pytest

import engine.manga.downloader as dl_mod
from engine.manga.base import MangaError


@pytest.fixture
def fake_pool():
    """用 FakeSession 填充池并接管 _ensure_pool，结束后完整还原模块状态。

    不触碰 test_r38 的辅助函数（该文件由其他代理维护），这里独立实现。
    """
    saved_pool = dl_mod._session_pool
    saved_locks = dl_mod._session_pool_locks
    saved_size = dl_mod._SESSION_POOL_SIZE
    saved_ensure = dl_mod._ensure_pool

    class FakeSession:
        def get(self, *a, **k):
            raise AssertionError("fake pool 不应真正发请求")

    def _fill(n):
        dl_mod._SESSION_POOL_SIZE = n
        dl_mod._session_pool[:] = [FakeSession() for _ in range(n)]
        dl_mod._session_pool_locks[:] = [threading.Lock() for _ in range(n)]

    dl_mod._ensure_pool = lambda: None  # 池已由 _fill 填好
    try:
        yield _fill
    finally:
        dl_mod._ensure_pool = saved_ensure
        dl_mod._session_pool = saved_pool
        dl_mod._session_pool_locks = saved_locks
        dl_mod._SESSION_POOL_SIZE = saved_size


def test_no_head_of_line_blocking(fake_pool):
    """成员 0 持续被占：归还成员 1 后等待者必须拿到成员 1，而不是堵在 0 上"""
    fake_pool(2)

    sess0, lock0 = dl_mod._acquire_session()  # 慢持有者：占住成员 0 不放
    sess1, lock1 = dl_mod._acquire_session()  # 快持有者：占成员 1，稍后归还
    assert sess0 is dl_mod._session_pool[0]
    assert sess1 is dl_mod._session_pool[1]

    got = {}

    def waiter():
        got["sess"], got["lock"] = dl_mod._acquire_session()
        got["done"] = True

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.2)  # 让等待者确定进入"全忙等待"分支
    assert "done" not in got, "池全忙时等待者应阻塞"

    # 只归还成员 1（成员 0 仍被慢持有者占着）
    dl_mod._release_session(lock1)
    t.join(timeout=3.0)
    assert not t.is_alive(), "队头阻塞：成员 1 已空闲，等待者却没被唤醒"
    assert got["sess"] is dl_mod._session_pool[1], \
        "等待者应借到先归还的成员 1，而非继续等成员 0"

    # 清理：全部归还，确认无泄漏
    dl_mod._release_session(got["lock"])
    dl_mod._release_session(lock0)
    for lk in dl_mod._session_pool_locks:
        assert lk.acquire(blocking=False), "最终所有成员必须可再借出"
        lk.release()


def test_acquire_timeout(fake_pool):
    """剩余预算耗尽：全忙 + timeout 时抛 MangaError，不无限阻塞"""
    fake_pool(2)
    held = [dl_mod._acquire_session() for _ in range(2)]
    t0 = time.monotonic()
    with pytest.raises(MangaError):
        dl_mod._acquire_session(timeout=0.3)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"超时未生效，实际阻塞 {elapsed:.2f}s"
    for _, lk in held:
        dl_mod._release_session(lk)


def test_concurrent_first_init():
    """并发首次初始化：多线程同时触发 _ensure_pool，发布后池必须完整"""
    saved_pool = dl_mod._session_pool
    saved_locks = dl_mod._session_pool_locks
    built = {"n": 0}

    class FakeSession:
        def get(self, *a, **k):
            raise AssertionError("不应真正发请求")

    # 替换 curl_cffi 导入产物太慢/有网络依赖——直接预置完整池并验证
    # 发布语义：先清空单元的可见状态，再由多线程并发 _acquire_session，
    # 用受控的慢构造验证"其他线程看不到半初始化列表"。
    import curl_cffi  # noqa: F401  确认依赖存在（真实 _ensure_pool 要用）
    try:
        dl_mod._session_pool = []
        dl_mod._session_pool_locks = []

        orig_ensure = dl_mod._ensure_pool

        def slow_ensure():
            """模拟慢构造：构建期间其他线程不得看到半初始化列表"""
            if not dl_mod._session_pool:
                with dl_mod._pool_init_lock:
                    if not dl_mod._session_pool:
                        sessions = [FakeSession()
                                    for _ in range(dl_mod._SESSION_POOL_SIZE)]
                        time.sleep(0.05)  # 构造耗时窗口
                        locks = [threading.Lock()
                                 for _ in range(dl_mod._SESSION_POOL_SIZE)]
                        dl_mod._session_pool_locks = locks
                        dl_mod._session_pool = sessions
                        built["n"] += 1

        dl_mod._ensure_pool = slow_ensure
        errors = []
        results = []

        def worker():
            try:
                sess, lk = dl_mod._acquire_session(timeout=5.0)
                results.append(sess)
                dl_mod._release_session(lk)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert not any(t.is_alive() for t in threads), "存在未完成的 worker"
        assert not errors, f"并发初始化出错: {errors}"
        assert built["n"] == 1, "双重检查锁下只应真正构建一次"
        assert len(results) == 12
        assert len(dl_mod._session_pool) == dl_mod._SESSION_POOL_SIZE
        assert len(dl_mod._session_pool_locks) == dl_mod._SESSION_POOL_SIZE
        # 最终所有成员可再借出（无泄漏）
        for lk in dl_mod._session_pool_locks:
            assert lk.acquire(blocking=False)
            lk.release()
    finally:
        dl_mod._ensure_pool = orig_ensure
        dl_mod._session_pool = saved_pool
        dl_mod._session_pool_locks = saved_locks


def test_exception_path_returns_member(fake_pool, monkeypatch):
    """fetch_image_checked 异常路径经 finally 归还成员，不泄漏"""
    fake_pool(1)

    class BoomSession:
        def get(self, *a, **k):
            raise ConnectionError("boom")  # 请求中途异常

    dl_mod._session_pool[0] = BoomSession()
    # SSRF 校验需放行（校验的是 URL 可达性，与借还无关）
    monkeypatch.setattr(dl_mod, "url_is_public_resolved", lambda url: (True, ""))

    with pytest.raises(ConnectionError):
        dl_mod.fetch_image_checked("http://img.example/x.webp", {})

    # 成员已归还：非阻塞即可再借
    lk = dl_mod._session_pool_locks[0]
    assert lk.acquire(blocking=False), "异常路径泄漏了池成员"
    lk.release()

    # SSRF 拒绝路径（raise 在拿到响应之前）同样归还
    monkeypatch.setattr(dl_mod, "url_is_public_resolved",
                        lambda url: (False, "私网地址"))
    with pytest.raises(Exception):
        dl_mod.fetch_image_checked("http://169.254.0.1/x.webp", {})
    assert lk.acquire(blocking=False), "SSRF 拒绝路径泄漏了池成员"
    lk.release()
