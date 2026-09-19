# -*- coding: utf-8 -*-
"""真断网（飞行模式/无信号）时的归因契约（离线，不触网）

起因（2026-09-15，P1-C"无网"一条）：断网时**每个源**都会抛异常，而旧代码把
所有异常都写成"搜索失败（源站异常或限流），请稍后重试"。用户手机没信号时会
得到完全错误的结论——以为是源站坏了，反复重试，永远等不到结果；反过来，
"没有结果"也会被当成"真的没有这部作品"。

新口径（方向基线 §7.2 P1-C、§5.3 不许给出超过证据的解释）：
  1. 本机没有路由（ENETUNREACH/EHOSTUNREACH、'Network is unreachable'）这类
     错误必须**如实说成本机网络问题**，不许甩给源站；
  2. 连接被拒绝/超时（域名被封锁、源站真挂了）**不算**断网——那是源站侧；
  3. 只有**整轮没有任何结果**且证据（逐源归因或本机实测）成立时，才在
     payload 里给 network_down=True，供客户端说"本机当前没有网络"。
"""
import errno
import os
import socket
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import neterr as ne  # noqa: E402
import server.manga_api as ma  # noqa: E402
from server import state as st  # noqa: E402


# ── 1) 异常分类 ────────────────────────────────────────────────────────────

def test_classify_enetunreach_is_local_network():
    e = OSError(errno.ENETUNREACH, "Network is unreachable")
    assert ne.classify(e) == "unreachable"


def test_classify_wrapped_errors_are_still_recognized():
    """requests/urllib3 会把 OSError 包好几层（reason/args），必须穿透"""
    inner = OSError(errno.ENETUNREACH, "Network is unreachable")

    class _Wrapped(Exception):
        def __init__(self, reason):
            super().__init__("Max retries exceeded")
            self.reason = reason

    assert ne.classify(_Wrapped(inner)) == "unreachable"

    class _Chained(Exception):
        pass

    outer = _Chained("HTTPSConnectionPool: failed")
    outer.__cause__ = OSError(101, "Network is unreachable")
    assert ne.classify(outer) == "unreachable"


def test_classify_dns_failure_is_separate_kind():
    e = socket.gaierror(-3, "Temporary failure in name resolution")
    assert ne.classify(e) == "dns"


def test_classify_refused_and_timeout_are_not_offline():
    """域名被封锁（连接被拒）/源站挂掉（超时）不得说成本机没网"""
    assert ne.classify(ConnectionRefusedError(111, "Connection refused")) == ""
    assert ne.classify(socket.timeout("timed out")) == ""


def test_failure_text_does_not_blame_the_source_when_offline():
    txt, kind = ne.failure_text("禁漫", OSError(errno.ENETUNREACH,
                                              "Network is unreachable"),
                                "禁漫 搜索失败（源站异常或限流），请稍后重试")
    assert kind == "unreachable"
    assert "本机没有可用路由" in txt
    assert "源站异常" not in txt, "断网时不许把原因甩给源站"


def test_failure_text_keeps_source_wording_when_online():
    txt, kind = ne.failure_text("禁漫", ConnectionRefusedError(111, "Connection refused"),
                                "禁漫 搜索失败（源站异常或限流），请稍后重试")
    assert kind == "" and "源站异常" in txt


# ── 2) 本机出网探测 ───────────────────────────────────────────────────────

def test_probe_says_down_only_on_no_route(monkeypatch):
    monkeypatch.setenv("WR_NET_PROBE_FORCE", "1")   # WR_TEST 环境默认关闭探测

    def _unreachable(addr, timeout=None):
        raise OSError(errno.ENETUNREACH, "Network is unreachable")

    monkeypatch.setattr(socket, "create_connection", _unreachable)
    assert ne.local_network_down() is True


def test_probe_is_conservative_on_ambiguous_failures(monkeypatch):
    """连接被拒/超时都**不算**断网（宁可不解释，也不给错解释）"""
    monkeypatch.setenv("WR_NET_PROBE_FORCE", "1")
    for exc in (ConnectionRefusedError(111, "Connection refused"),
                socket.timeout("timed out")):
        def _fail(addr, timeout=None, _e=exc):
            raise _e

        monkeypatch.setattr(socket, "create_connection", _fail)
        assert ne.local_network_down() is False


def test_probe_returns_false_when_any_target_reachable(monkeypatch):
    """第一个目标不通、第二个通 → 有网（不能用"个别目标被封锁"当成断网）"""
    monkeypatch.setenv("WR_NET_PROBE_FORCE", "1")
    class _Sock:
        def close(self):
            pass

    seen = []

    def _conn(addr, timeout=None):
        seen.append(addr[0])
        if addr[0] == "223.5.5.5":
            raise socket.timeout("timed out")     # 该目标被封（拿不准）
        return _Sock()

    monkeypatch.setattr(socket, "create_connection", _conn)
    assert ne.local_network_down() is False


def test_probe_checks_all_targets_before_concluding(monkeypatch):
    """不能因为第一个目标失败就下结论：必须把所有目标都试过"""
    monkeypatch.setenv("WR_NET_PROBE_FORCE", "1")
    calls = []

    def _unreachable(addr, timeout=None):
        calls.append(addr)
        raise OSError(errno.ENETUNREACH, "Network is unreachable")

    monkeypatch.setattr(socket, "create_connection", _unreachable)
    assert ne.local_network_down() is True
    assert len(calls) == len(ne._PROBE_TARGETS), "每个探测目标都要试过"


# ── 3) 整轮判定 ───────────────────────────────────────────────────────────

def test_round_is_offline_needs_device_level_evidence(monkeypatch):
    """整机断网结论只认**设备级**证据：逐源失败文字不算（2026-09-15 评审 P1）"""
    assert ne.round_is_offline(True, ["任何失败"]) is False
    assert ne.round_is_offline(False, []) is False

    # 只有逐源文字时 → 仍然要问设备级证据（不因为"某个源说没路由"就下结论）
    calls = []

    def _probe(*a, **k):
        calls.append(1)
        return False

    monkeypatch.setattr(ne, "local_network_down", _probe)
    assert ne.round_is_offline(
        False, ["A 搜索失败（本机没有可用路由（请检查 Wi-Fi / 移动数据））"]) is False
    assert calls, "必须调用设备级判据"

    # 客户端明确说"本机没有网络" → 直接成立，无需再探测
    def _boom(*a, **k):
        raise AssertionError("有设备级信号时不该再探测")

    monkeypatch.setattr(ne, "local_network_down", _boom)
    assert ne.round_is_offline(False, ["A 失败"], device_offline=True) is True


def test_single_target_unreachable_is_not_phone_offline(monkeypatch):
    """单个源"目标不可达"（No route to host）不许升级成"手机没网"（评审 P1）"""
    monkeypatch.setattr(ne, "local_network_down", lambda *a, **k: False)
    txt, kind = ne.failure_text(
        "某源", OSError(113, "No route to host"),
        "某源 搜索失败（源站异常或限流），请稍后重试")
    assert kind == "host-unreachable"
    assert "目标地址不可达" in txt
    assert "本机" not in txt, f"目标级不可达不得说成本机没网：{txt}"


def test_probe_can_be_disabled_by_env(monkeypatch):
    """探测可关闭（评审要求：不让独立探测成为业务可用性的前置条件）"""
    def _boom(addr, timeout=None):
        raise AssertionError("关闭探测后不得再发起连接")

    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setenv("WR_DISABLE_NET_PROBE", "1")
    monkeypatch.delenv("WR_NET_PROBE_FORCE", raising=False)
    assert ne.local_network_down() is False


def test_empty_adapter_list_returns_business_result(monkeypatch):
    """空适配器列表（指定源不可用/缺依赖）必须是明确业务结果，不是异常（评审 P0）"""
    _patch(monkeypatch, [])
    out = ma._do_manga_search("空源", "nosuch", 1)
    assert out["results"] == []
    assert out["sources"] == 0
    assert out["network_down"] is False
    assert "_sources" in out["errors"] and "没有可用的漫画源" in out["errors"]["_sources"]


def test_unknown_source_endpoint_returns_business_result(monkeypatch):
    """HTTP 层同样：未知源不得 500（评审要求给空源/未知源明确业务结果）"""
    import app as _app
    _patch(monkeypatch, [])
    client = _app.app.test_client()
    r = client.get("/api/manga/search?q=任意&source=nosuch")
    assert r.status_code == 200, f"未知源不应 5xx：HTTP {r.status_code} {r.get_data(as_text=True)[:200]}"
    body = r.get_json()
    assert body.get("network_down") is False
    assert "没有可用的漫画源" in (body.get("errors") or {}).get("_sources", "")


def test_round_is_offline_uses_probe_for_ambiguous_round(monkeypatch):
    monkeypatch.setattr(ne, "local_network_down", lambda *a, **k: True)
    assert ne.round_is_offline(False, ["A 搜索超时（源站无响应或服务器繁忙），请稍后重试"]) is True
    monkeypatch.setattr(ne, "local_network_down", lambda *a, **k: False)
    assert ne.round_is_offline(False, ["A 搜索超时（源站无响应或服务器繁忙），请稍后重试"]) is False


# ── 4) 漫画搜索：逐源归因 + payload 标志 ─────────────────────────────────

class _OfflineAdapter:
    key, name = "dead", "离线源"

    def search(self, keyword, page=1, order=None):
        raise OSError(errno.ENETUNREACH, "Network is unreachable")


class _RefusedAdapter:
    key, name = "refused", "被拒源"

    def search(self, keyword, page=1, order=None):
        raise ConnectionRefusedError(111, "Connection refused")


class _OkAdapter:
    key, name = "ok", "正常源"

    def search(self, keyword, page=1, order=None):
        from engine.manga.base import Comic
        return [Comic(id="c1", title="漫画", author="", cover="", tags=[],
                      source_key=self.key)]


@pytest.fixture(autouse=True)
def _clean_caches():
    st._manga_search_cache.clear()
    st._manga_search_partial.clear()
    st._manga_search_fail.clear()
    ma._manga_search_latency.clear()
    yield
    st._manga_search_cache.clear()
    st._manga_search_partial.clear()
    st._manga_search_fail.clear()


def _patch(monkeypatch, adapters):
    monkeypatch.setattr(ma, "_manga_search_adapters", lambda source: adapters)


def test_manga_search_reports_network_down(monkeypatch):
    monkeypatch.setattr(ne, "local_network_down", lambda *a, **k: True)
    _patch(monkeypatch, [_OfflineAdapter(), _OfflineAdapter()])
    out = ma._do_manga_search("测试", "", 1)
    assert out["network_down"] is True
    assert out["results"] == []
    for txt in out["errors"].values():
        assert "本机没有可用路由" in txt, txt


def test_manga_search_does_not_claim_offline_when_source_fails(monkeypatch):
    """源站自己失败（连接被拒）时不得说成本机没网"""
    monkeypatch.setattr(ne, "local_network_down", lambda *a, **k: False)
    _patch(monkeypatch, [_RefusedAdapter()])
    out = ma._do_manga_search("测试", "", 1)
    assert out["network_down"] is False
    assert "源站异常或限流" in out["errors"]["refused"]


def test_manga_search_with_results_is_never_network_down(monkeypatch):
    """有结果就说明网是通的：不得同时给"没网"标志"""
    monkeypatch.setattr(ne, "local_network_down", lambda *a, **k: False)
    _patch(monkeypatch, [_OkAdapter(), _OfflineAdapter()])
    out = ma._do_manga_search("测试", "", 1)
    assert len(out["results"]) == 1
    assert out["network_down"] is False


def test_manga_stream_finished_event_carries_network_down(monkeypatch):
    import json as _json
    import app as _app
    monkeypatch.setattr(ne, "local_network_down", lambda *a, **k: True)
    _patch(monkeypatch, [_OfflineAdapter()])
    client = _app.app.test_client()
    r = client.get("/api/manga/search/stream?q=断网流式&page=1")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    parsed = [_json.loads(ln[len("data:"):].strip()) for ln in body.splitlines()
              if ln.startswith("data:")]
    assert parsed and parsed[-1].get("finished") is True
    assert parsed[-1].get("network_down") is True
    assert "本机没有可用路由" in (parsed[-1].get("errors") or {}).get("dead", "")


# ── 5) 小说搜索：慢源台账也要如实归因 ─────────────────────────────────────

def test_novel_search_worker_marks_offline_reason(monkeypatch):
    from engine.search_service import SearchWorker, SlowSourceTracker

    class _Crawler:
        adapter = None

        def __init__(self, source):
            pass

        def search(self, keyword):
            raise OSError(errno.ENETUNREACH, "Network is unreachable")

    monkeypatch.setattr("engine.search_service.SourceCrawler", _Crawler)
    tracker = SlowSourceTracker()
    worker = SearchWorker(tracker)
    books = worker.search_one({"uid": "u1", "bookSourceName": "某书源"}, "关键词")
    assert books == []
    slow, _ = tracker.snapshot()
    assert slow, "失败必须记进慢源台账"
    reason = slow["u1"]["reason"]
    assert "本机没有可用路由" in reason, reason

# ── 6) 断网时不要干等源站超时（设备实测：禁漫/拷贝要拖满 25s）──────────────

class _DnsFailAdapter:
    """解析失败（Android 断网实测就是这种：No address associated with hostname）。

    key 必须各不相同——errors 按源 key 记账，同 key 会被合并成一条，
    那样"多个源都报本机网络问题"的证据就永远凑不齐（写用例时踩到过）。
    """

    def __init__(self, key="dnsfail", name="解析失败源"):
        self.key, self.name = key, name

    def search(self, keyword, page=1, order=None):
        raise socket.gaierror(-2, "Name or service not known")


class _HangAdapter:
    key, name = "hang", "卡死源"

    def search(self, keyword, page=1, order=None):
        time.sleep(60)          # 模拟要拖满 20/25s 期限的源
        return []


def test_offline_round_ends_early_with_honest_reason(monkeypatch):
    """多个源报本机网络问题 + 本机确实没路由 → 立刻收尾，并如实标注未返回的源"""
    monkeypatch.setattr(ne, "local_network_down", lambda *a, **k: True)
    monkeypatch.setattr(ma, "_MANGA_OFFLINE_GRACE", 0.5)
    _patch(monkeypatch, [_DnsFailAdapter("dns1", "解析失败源一"),
                         _DnsFailAdapter("dns2", "解析失败源二"), _HangAdapter()])
    t = time.time()
    out = ma._do_manga_search("测试", "", 1)
    dt = time.time() - t
    assert dt < 3, f"断网时应提前收尾，实际等了 {dt:.1f}s"
    assert out["network_down"] is True
    assert "本机没有可用路由" in out["errors"]["hang"], out["errors"]["hang"]
    assert "本机" in out["errors"]["dns1"]


def test_online_round_is_not_cut_short(monkeypatch):
    """探测说"有网"时不得提前收尾（宁可不提前结束，也不误伤正常网络）"""
    monkeypatch.setattr(ne, "local_network_down", lambda *a, **k: False)
    monkeypatch.setattr(ma, "_MANGA_OFFLINE_GRACE", 0.2)
    monkeypatch.setattr(ma, "_MANGA_FAST_DEADLINE", 2)
    _patch(monkeypatch, [_DnsFailAdapter(), _HangAdapter()])
    t = time.time()
    out = ma._do_manga_search("测试", "", 1)
    dt = time.time() - t
    assert dt >= 1.5, f"没网证据不成立时必须等源站期限，实际只等了 {dt:.2f}s"
    assert "本机网络不可达" not in out["errors"]["hang"]


def test_stream_round_also_ends_early_when_offline(monkeypatch):
    import json as _json
    import app as _app
    monkeypatch.setattr(ne, "local_network_down", lambda *a, **k: True)
    monkeypatch.setattr(ma, "_MANGA_OFFLINE_GRACE", 0.5)
    _patch(monkeypatch, [_DnsFailAdapter("dns1", "解析失败源一"),
                         _DnsFailAdapter("dns2", "解析失败源二"), _HangAdapter()])
    client = _app.app.test_client()
    t = time.time()
    r = client.get("/api/manga/search/stream?q=断网提前收尾&page=1")
    body = r.get_data(as_text=True)
    dt = time.time() - t
    parsed = [_json.loads(ln[len("data:"):].strip()) for ln in body.splitlines()
              if ln.startswith("data:")]
    assert parsed[-1].get("finished") is True
    assert parsed[-1].get("network_down") is True
    assert dt < 3, f"流式端点同样应提前收尾，实际 {dt:.1f}s"
    assert "本机没有可用路由" in (parsed[-1].get("errors") or {}).get("hang", "")

# ── 7) 小说流式端点同样提前收尾 ───────────────────────────────────────────

def _novel_src(uid):
    return {"uid": uid, "bookSourceName": "源" + uid,
            "bookSourceUrl": f"http://{uid}.example.com",
            "searchUrl": "/search?q={{key}}", "enabled": True}


def test_novel_stream_ends_early_when_offline(monkeypatch):
    """小说端与漫画端同一口径：本机没网时不要对着"搜索中"干等源站期限"""
    import json as _json
    import app as _app
    import server.novel_api as api
    import engine.search_service as ss
    from server import state as _state

    class _Crawler:
        adapter = None      # 走规则引擎兜底路径（crawler.search 自带快速失败）

        def __init__(self, source):
            self.source = source

        def search(self, keyword):
            uid = self.source.get("uid")
            if uid == "off1":
                raise socket.gaierror(-2, "Name or service not known")
            if uid == "off2":
                raise OSError(errno.ENETUNREACH, "Network is unreachable")
            time.sleep(3)          # 卡死源：正常要等满单源时限
            return []

    monkeypatch.setattr(ss, "SourceCrawler", _Crawler)
    monkeypatch.setattr(api, "load_enabled",
                        lambda: [_novel_src("off1"), _novel_src("off2"),
                                 _novel_src("hang")])
    monkeypatch.setattr(api, "NOVEL_OFFLINE_GRACE", 0.2)
    monkeypatch.setattr(ne, "local_network_down", lambda *a, **k: True)
    with _state._toc_lock:
        _state._search_cache.clear()
    _state._slow_tracker._slow.clear()

    client = _app.app.test_client()
    t = time.time()
    r = client.get("/api/search/stream?q=断网小说")
    dt = time.time() - t
    parsed = [_json.loads(ln[len("data:"):].strip()) for ln in
              r.get_data(as_text=True).splitlines() if ln.startswith("data:")]
    assert parsed and parsed[-1].get("finished") is True
    assert parsed[-1].get("network_down") is True
    assert dt < 2, f"断网时小说流也应提前收尾，实际 {dt:.1f}s"
    assert any("本机没有可用路由" in v for v in parsed[-1]["errors"].values()), \
        parsed[-1]["errors"]


def test_novel_stream_not_cut_short_when_online(monkeypatch):
    """探测说"有网"时不许提前收尾（宁可慢，也不误报断网）"""
    import json as _json
    import app as _app
    import server.novel_api as api
    import engine.search_service as ss
    from server import state as _state

    class _Crawler:
        adapter = None      # 走规则引擎兜底路径（crawler.search 自带快速失败）

        def __init__(self, source):
            self.source = source

        def search(self, keyword):
            if self.source.get("uid") == "off1":
                raise socket.gaierror(-2, "Name or service not known")
            time.sleep(1.5)
            return []

    monkeypatch.setattr(ss, "SourceCrawler", _Crawler)
    monkeypatch.setattr(api, "load_enabled",
                        lambda: [_novel_src("off1"), _novel_src("slow2")])
    monkeypatch.setattr(api, "NOVEL_OFFLINE_GRACE", 0.2)
    monkeypatch.setattr(ne, "local_network_down", lambda *a, **k: False)
    with _state._toc_lock:
        _state._search_cache.clear()
    _state._slow_tracker._slow.clear()

    client = _app.app.test_client()
    t = time.time()
    r = client.get("/api/search/stream?q=在线小说")
    dt = time.time() - t
    parsed = [_json.loads(ln[len("data:"):].strip()) for ln in
              r.get_data(as_text=True).splitlines() if ln.startswith("data:")]
    assert parsed[-1].get("finished") is True
    assert dt >= 1.2, f"没网证据不成立时必须等源站返回，实际只等了 {dt:.2f}s"
    assert not parsed[-1].get("network_down")

def test_offline_round_ends_early_even_when_every_source_hangs(monkeypatch):
    """所有源都卡死（一个失败都没有）时，只要本机确定没路由也要提前收尾"""
    monkeypatch.setattr(ne, "local_network_down", lambda *a, **k: True)
    monkeypatch.setattr(ma, "_MANGA_OFFLINE_GRACE", 0.5)
    _patch(monkeypatch, [_HangAdapter()])
    t = time.time()
    out = ma._do_manga_search("测试", "", 1)
    dt = time.time() - t
    assert dt < 3, f"本机没路由时应提前收尾，实际等了 {dt:.1f}s"
    assert out["network_down"] is True
    assert "本机没有可用路由" in out["errors"]["hang"], out["errors"]["hang"]

def test_device_signal_offline_is_enough_without_probe(monkeypatch):
    """客户端说"本机没有网络"时：可以直接给结论，且**不必**再探测（评审建议）"""
    import json as _json
    import app as _app
    _patch(monkeypatch, [_RefusedAdapter()])

    def _boom(*a, **k):
        raise AssertionError("有设备级信号时不该再探测")

    monkeypatch.setattr(ne, "local_network_down", _boom)
    client = _app.app.test_client()
    r = client.get("/api/manga/search/stream?q=设备没网&page=1",
                   headers={"X-Device-Net": "offline"})
    parsed = [_json.loads(ln[len("data:"):].strip()) for ln in
              r.get_data(as_text=True).splitlines() if ln.startswith("data:")]
    assert parsed[-1].get("network_down") is True


def test_device_signal_online_does_not_hide_failures(monkeypatch):
    """设备说"在线"不能反过来否定失败：该报的源站失败照报"""
    _patch(monkeypatch, [_RefusedAdapter()])
    monkeypatch.setattr(ne, "local_network_down", lambda *a, **k: False)
    import app as _app
    client = _app.app.test_client()
    r = client.get("/api/manga/search?q=设备在线&source=refused",
                   headers={"X-Device-Net": "online"})
    body = r.get_json()
    assert body.get("network_down") is False
    assert "源站异常或限流" in (body.get("errors") or {}).get("refused", "")

def test_failure_text_mentions_proxy_when_in_use(monkeypatch):
    """经代理时失败文案必须点明"当前经代理"（0.74.1 事故的界面缺口）。

    事故里用户配的代理关掉后成了死地址，每个源都失败，而界面上完全看不出
    "请求正在经代理"——只能靠猜。这里锁死"文案里带代理"这条。
    """
    import engine.neterr as ne
    import engine.netproxy as np
    monkeypatch.setenv("WR_PROXY", "http://127.0.0.1:7897")
    np._cached = None
    txt, kind = ne.failure_text("禁漫天堂", ConnectionResetError("boom"), "兜底")
    assert "经代理" in txt and "127.0.0.1:7897" in txt, txt
    # 必须**极短**：这是逐源文案，几十个源失败时会拼成一整屏
    # （0.74.2 我第一次写成了一整句，用户实测"整个屏幕都是报错"）
    assert len(txt) < 60, "逐源报错文案过长会淹没搜索结果：%r" % txt
    assert "设置 → 网络 → 代理" not in txt, "完整指引放在代理页/诊断报告，不占逐源文案"

    monkeypatch.delenv("WR_PROXY", raising=False)
    monkeypatch.delenv("WR_PROXY_FILE", raising=False)
    np._cached = None
    txt2, _ = ne.failure_text("禁漫天堂", ConnectionResetError("boom"), "兜底")
    assert "经代理" not in txt2, txt2


def test_failure_text_proxy_hint_is_redacted(monkeypatch):
    """提示里的代理串必须脱敏（errors 会进界面、也会进诊断报告）"""
    import engine.neterr as ne
    import engine.netproxy as np
    monkeypatch.setenv("WR_PROXY", "socks5://user:secret@10.0.0.9:1080")
    np._cached = None
    txt, _ = ne.failure_text("包子", TimeoutError("slow"), "兜底")
    assert "secret" not in txt and "user:" not in txt, txt
    assert "10.0.0.9:1080" in txt, txt
