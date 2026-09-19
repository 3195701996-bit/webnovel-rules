# -*- coding: utf-8 -*-
"""P1-2 审计补充：活动时间戳 60s 节流落盘 + 图片类路径请求日志豁免。

验收点：
1. 连续 10 次请求只触发 ≤1 次落盘（monkeypatch 计数 _write_activity_file）；
2. 活动时间戳在内存中每次请求即时更新（12h 空闲判定语义不变：
   判定用请求到来前的旧值，touch 后内存立即刷新）；
3. force=True（自动清理重置计时等关键节点）绕过节流立即落盘；
4. /img/、/proxy 路径豁免 [req] 请求日志，普通路径仍记录。
"""
import time

import pytest


@pytest.fixture()
def client():
    import app
    return app.app.test_client()


class TestActivityPersistThrottle:
    def test_ten_requests_at_most_one_persist(self, client, monkeypatch):
        import app as _app
        writes = []
        monkeypatch.setattr(_app, "_write_activity_file",
                            lambda: writes.append(time.time()))
        monkeypatch.setattr(_app, "_last_activity_persist_ts", 0.0)
        for _ in range(10):
            r = client.get("/")
            assert r.status_code == 200
        assert len(writes) <= 1, f"10 次请求落盘 {len(writes)} 次"

    def test_touch_updates_memory_immediately(self, client, monkeypatch):
        """节流只挡落盘：内存活动时间戳每请求即时刷新（12h 判定不受影响）"""
        import app as _app
        monkeypatch.setattr(_app, "_write_activity_file", lambda: None)
        monkeypatch.setattr(_app, "_last_activity_ts",
                            time.time() - 3600)   # 1h 前
        client.get("/")
        assert time.time() - _app._last_activity_ts < 5

    def test_idle_detection_uses_pre_touch_value(self, client, monkeypatch):
        """12h 空闲语义不变：idle_secs 取自本请求到来前的上次活动时间"""
        import app as _app
        seen = []
        monkeypatch.setattr(_app, "_maybe_auto_clean",
                            lambda idle_secs: seen.append(idle_secs))
        monkeypatch.setattr(_app, "_last_activity_ts",
                            time.time() - 13 * 3600)
        monkeypatch.setattr(_app, "_last_auto_check_ts", 0.0)
        monkeypatch.setattr(_app, "_write_activity_file", lambda: None)
        client.get("/")
        for _ in range(50):   # after_request 里线程异步触发，等其落地
            if seen:
                break
            time.sleep(0.02)
        assert seen and seen[0] >= 12 * 3600, \
            f"空闲判定值异常: {seen}"

    def test_force_persist_bypasses_throttle(self, monkeypatch):
        import app as _app
        writes = []
        monkeypatch.setattr(_app, "_write_activity_file",
                            lambda: writes.append(time.time()))
        monkeypatch.setattr(_app, "_last_activity_persist_ts", time.time())
        _app._persist_activity()              # 节流窗口内 → 不落盘
        assert not writes
        _app._persist_activity(force=True)    # 关键节点强制落盘
        assert len(writes) == 1

    def test_health_check_not_counted_as_activity(self, client, monkeypatch):
        import app as _app
        writes = []
        monkeypatch.setattr(_app, "_write_activity_file",
                            lambda: writes.append(time.time()))
        old = time.time() - 100
        monkeypatch.setattr(_app, "_last_activity_ts", old)
        monkeypatch.setattr(_app, "_last_activity_persist_ts", 0.0)
        client.get("/api/health")
        assert not writes
        assert _app._last_activity_ts == old   # 健康检查不刷新活动时间


class TestRequestLogExemption:
    def test_image_paths_skip_request_log(self, client, capsys):
        client.get("/api/manga/b03src/b03c/chapter/ch1/img/0")
        out = capsys.readouterr().out
        assert "[req]" not in out

    def test_proxy_paths_skip_request_log(self, client, capsys):
        client.get("/api/manga/b03src/b03c/chapter/ch1/proxy")
        out = capsys.readouterr().out
        assert "[req]" not in out

    def test_normal_path_still_logged(self, client, capsys):
        client.get("/api/health")
        out = capsys.readouterr().out
        assert "[req]" in out
