# -*- coding: utf-8 -*-
"""出站代理（0.73.0）：优先级 / 持久化 / 会话重置 / 两条图片通道的语义差异。

用户背景（2026-09-17 真机诊断）：API 三段很快但首图 4995ms，且 copy4000 /
baozimh / nhentai 等域在开发网络直连被重置、走代理即恢复——"各源降级"的
主因是**网络可达性**，因此用户批准加"自定义代理"。

本文件锁定四件事（改坏了必须红）：
  1. 优先级：WR_PROXY > WR_PROXY_FILE 配置文件 > 直连，非法值一律忽略；
  2. set_proxy 会写配置、设环境变量、清缓存、**重置各通道会话**；
  3. 图片 requests 通道：**配了代理不再因"未能绑定 IP"判不可用**（否则等于
     "一配代理图片全挂"），且代理字典确实传给了请求；
  4. 图片 curl 通道：pin 收到 proxy 参数、请求带 proxies——两条路径语义一致。
"""
import os

import pytest

import engine.netproxy as NP


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例前后都回到"无代理"状态（netproxy 有模块级缓存 + 直写 os.environ）。"""
    monkeypatch.delenv("WR_PROXY", raising=False)
    monkeypatch.delenv("WR_PROXY_FILE", raising=False)
    NP._cached = None
    NP._env_pushed = None
    NP._HEALTH.update({"value": None, "ts": 0.0, "ok": True, "err": "",
                       "fell_back": False})
    monkeypatch.delenv("WR_PROFILE", raising=False)
    yield
    os.environ.pop("WR_PROXY", None)
    NP._cached = None
    NP._env_pushed = None


# ---------------------------------------------------------------- 取值与校验

def test_valid_normalizes_and_rejects():
    assert NP._valid("127.0.0.1:7897") == "http://127.0.0.1:7897"
    assert NP._valid("http://127.0.0.1:7897") == "http://127.0.0.1:7897"
    assert NP._valid("  socks5h://127.0.0.1:1080  ") == "socks5h://127.0.0.1:1080"
    assert NP._valid("socks5://u:p@127.0.0.1:1080") == "socks5://u:p@127.0.0.1:1080"
    # 显式"不用代理"的各种写法都归一到空串（=直连）
    for v in (None, "", "   ", "none", "NONE", "off", "direct", "0"):
        assert NP._valid(v) == "", v
    # 非法 scheme 必须拒绝，绝不"看着配了其实直连"
    for v in ("ftp://127.0.0.1:21", "file:///etc/passwd", "socks://h:1"):
        assert NP._valid(v) == "", v


def test_env_wins_over_config_file(tmp_path, monkeypatch):
    cfg = tmp_path / "proxy.txt"
    cfg.write_text("socks5://127.0.0.1:1080\n", encoding="utf-8")
    monkeypatch.setenv("WR_PROXY_FILE", str(cfg))
    monkeypatch.setenv("WR_PROXY", "127.0.0.1:7897")
    assert NP.current_proxy() == "http://127.0.0.1:7897"
    assert NP.is_proxied() is True


def test_config_file_used_when_no_env(tmp_path, monkeypatch):
    cfg = tmp_path / "proxy.txt"
    # 只认第一行：文件被追加过内容也不影响生效值
    cfg.write_text("127.0.0.1:7897\nsocks5://127.0.0.1:1080\n", encoding="utf-8")
    monkeypatch.setenv("WR_PROXY_FILE", str(cfg))
    assert NP.current_proxy() == "http://127.0.0.1:7897"


def test_config_file_invalid_or_missing_means_direct(tmp_path, monkeypatch):
    monkeypatch.setenv("WR_PROXY_FILE", str(tmp_path / "nope.txt"))
    assert NP.current_proxy() == ""
    assert NP.proxy_dict() is None

    cfg = tmp_path / "bad.txt"
    cfg.write_text("ftp://127.0.0.1:21\n", encoding="utf-8")
    monkeypatch.setenv("WR_PROXY_FILE", str(cfg))
    assert NP.current_proxy() == ""
    assert NP.is_proxied() is False


def test_config_cache_invalidates_on_mtime(tmp_path, monkeypatch):
    cfg = tmp_path / "proxy.txt"
    cfg.write_text("127.0.0.1:7897\n", encoding="utf-8")
    monkeypatch.setenv("WR_PROXY_FILE", str(cfg))
    assert NP.current_proxy() == "http://127.0.0.1:7897"

    cfg.write_text("socks5://127.0.0.1:1080\n", encoding="utf-8")
    st = os.stat(cfg)
    os.utime(cfg, (st.st_atime + 60, st.st_mtime + 60))    # 保证 mtime 变化
    assert NP.current_proxy() == "socks5://127.0.0.1:1080"


def test_proxy_dict_shape():
    assert NP.proxy_dict() is None
    os.environ["WR_PROXY"] = "127.0.0.1:7897"
    assert NP.proxy_dict() == {"http": "http://127.0.0.1:7897",
                               "https": "http://127.0.0.1:7897"}


# ------------------------------------------------------------ set_proxy 语义

def test_set_proxy_persists_env_and_resets_sessions(tmp_path, monkeypatch):
    cfg = tmp_path / "sub" / "proxy.txt"        # 目录不存在：必须自动建
    monkeypatch.setenv("WR_PROXY_FILE", str(cfg))
    calls = []
    monkeypatch.setattr(NP, "reset_channel_sessions", lambda: calls.append(1) or 3)
    NP._cached = ("stale", 1.0)                 # 故意留脏缓存

    assert NP.set_proxy("127.0.0.1:7897") == "http://127.0.0.1:7897"
    assert cfg.read_text(encoding="utf-8") == "http://127.0.0.1:7897\n"
    assert os.environ["WR_PROXY"] == "http://127.0.0.1:7897"
    assert NP.current_proxy() == "http://127.0.0.1:7897"
    assert calls == [1], "切换代理必须重置各通道会话，否则改了代理不生效"


def test_set_proxy_empty_clears_and_stays_direct(tmp_path, monkeypatch):
    cfg = tmp_path / "proxy.txt"
    monkeypatch.setenv("WR_PROXY_FILE", str(cfg))
    monkeypatch.setattr(NP, "reset_channel_sessions", lambda: 0)

    NP.set_proxy("127.0.0.1:7897")
    assert NP.current_proxy() == "http://127.0.0.1:7897"

    assert NP.set_proxy("") == ""
    assert cfg.read_text(encoding="utf-8") == "\n"
    assert "WR_PROXY" not in os.environ
    assert NP.current_proxy() == ""


def test_set_proxy_ignores_invalid_value(tmp_path, monkeypatch):
    cfg = tmp_path / "proxy.txt"
    monkeypatch.setenv("WR_PROXY_FILE", str(cfg))
    monkeypatch.setattr(NP, "reset_channel_sessions", lambda: 0)
    os.environ["WR_PROXY"] = "127.0.0.1:7897"

    assert NP.set_proxy("ftp://127.0.0.1:21") == ""
    assert "WR_PROXY" not in os.environ
    assert NP.is_proxied() is False


def test_set_proxy_without_config_file_still_sets_env(monkeypatch):
    monkeypatch.setattr(NP, "reset_channel_sessions", lambda: 0)
    assert NP.set_proxy("127.0.0.1:7897") == "http://127.0.0.1:7897"
    assert os.environ["WR_PROXY"] == "http://127.0.0.1:7897"


def test_reset_channel_sessions_drops_jm_api_session():
    import engine.manga.jm as jm
    jm._jm_api_reset()
    assert jm._jm_api_session() is not None
    assert NP.reset_channel_sessions() >= 1
    assert getattr(jm._jm_api_local, "sess", None) is None, \
        "重置后必须丢掉旧会话，否则继续走旧网络路径"


def test_jm_api_session_picks_up_proxy():
    import engine.manga.jm as jm
    os.environ["WR_PROXY"] = "127.0.0.1:7897"
    try:
        jm._jm_api_reset()
        s = jm._jm_api_session()
        assert s.proxies.get("http") == "http://127.0.0.1:7897"
        assert s.proxies.get("https") == "http://127.0.0.1:7897"
    finally:
        jm._jm_api_reset()


# ------------------------------------------------- 代理可达性（0.74.2 真机事故）

def test_health_disabled_by_default_on_desktop(tmp_path, monkeypatch):
    """桌面默认不做探测：行为与改动前完全一致（不留额外 2 秒/次）。"""
    cfg = tmp_path / "proxy.txt"
    cfg.write_text("127.0.0.1:1\n", encoding="utf-8")      # 1 端口不会有代理
    monkeypatch.setenv("WR_PROXY_FILE", str(cfg))
    NP._cached = None
    assert NP.health()["enabled"] is False
    assert NP.current_proxy() == "http://127.0.0.1:1", "桌面不做可达性判定"
    assert NP.source() == "file"


def test_unreachable_proxy_falls_back_to_direct(tmp_path, monkeypatch):
    """**配了但连不上 → 临时直连并如实记录**。

    真机事故：用户按上一版说明把代理指向电脑局域网地址，之后电脑关机/换网络，
    于是所有出站请求都发往死地址 → 每个源都"超时"，界面上却只显示"经代理"。
    产品要求是"App 随时独立可用"，所以过期设置不能把整个应用变砖——
    但也不能静默：health() 必须说明原因，界面/诊断据此告知用户。
    """
    cfg = tmp_path / "proxy.txt"
    cfg.write_text("127.0.0.1:1\n", encoding="utf-8")
    monkeypatch.setenv("WR_PROXY_FILE", str(cfg))
    monkeypatch.setenv("WR_PROXY_HEALTHCHECK", "1")
    NP._cached = None
    NP._HEALTH.update({"value": None, "ts": 0.0, "ok": True, "err": "",
                       "fell_back": False})
    assert NP.current_proxy() == "", "不可达时代理不生效（走直连）"
    assert NP.proxy_dict() is None
    assert NP.is_proxied() is False
    assert NP.source() == "direct"
    h = NP.health()
    assert h["ok"] is False and h["fell_back"] is True
    assert h["configured"].endswith("127.0.0.1:1"), h
    assert h["err"], "必须给出失败原因（界面要显示）"


def test_reachable_proxy_is_used(tmp_path, monkeypatch):
    """能连上的代理照常生效（不能因为加了一层判定就一律直连）。"""
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        cfg = tmp_path / "proxy.txt"
        cfg.write_text("127.0.0.1:%d\n" % port, encoding="utf-8")
        monkeypatch.setenv("WR_PROXY_FILE", str(cfg))
        monkeypatch.setenv("WR_PROXY_HEALTHCHECK", "1")
        NP._cached = None
        NP._HEALTH.update({"value": None, "ts": 0.0, "ok": True, "err": "",
                           "fell_back": False})
        assert NP.current_proxy() == "http://127.0.0.1:%d" % port
        h = NP.health()
        assert h["ok"] is True and h["fell_back"] is False
    finally:
        srv.close()


def test_health_probe_is_cached(tmp_path, monkeypatch):
    """可达性判定有缓存：不能每次请求都去连一下代理（那会白白加延迟）。"""
    cfg = tmp_path / "proxy.txt"
    cfg.write_text("127.0.0.1:1\n", encoding="utf-8")
    monkeypatch.setenv("WR_PROXY_FILE", str(cfg))
    monkeypatch.setenv("WR_PROXY_HEALTHCHECK", "1")
    NP._cached = None
    NP._HEALTH.update({"value": None, "ts": 0.0, "ok": True, "err": "",
                       "fell_back": False})
    calls = []
    monkeypatch.setattr(NP, "_probe", lambda v, timeout=2.0: (calls.append(v), (False, "拒连"))[1])
    NP.current_proxy(); NP.current_proxy(); NP.current_proxy()
    assert len(calls) == 1, f"缓存内的重复调用不该再探测：{calls}"


def test_mobile_profile_turns_health_on(tmp_path, monkeypatch):
    """手机档默认开启（桌面默认关闭）：产品是手机端，过期设置不能让它变砖。"""
    monkeypatch.setenv("WR_PROFILE", "mobile")
    monkeypatch.delenv("WR_PROXY_HEALTHCHECK", raising=False)
    assert NP._health_enabled() is True
    monkeypatch.setenv("WR_PROXY_HEALTHCHECK", "0")
    assert NP._health_enabled() is False


def test_proxy_host_port_parsing():
    assert NP._proxy_host_port("http://127.0.0.1:7897") == ("127.0.0.1", 7897)
    assert NP._proxy_host_port("socks5://u:p@10.0.0.2:1080") == ("10.0.0.2", 1080)
    assert NP._proxy_host_port("127.0.0.1:7897") == ("127.0.0.1", 7897)
    assert NP._proxy_host_port("[::1]:1080") == ("::1", 1080)
    assert NP._proxy_host_port("") == ("", 0)


# ------------------------------------------------- 图片通道：requests 降级路径

class _Resp:
    status_code = 200
    headers = {}
    content = b"x"


class _FakeSess:
    proxies = {}
    trust_env = False

    def __init__(self):
        self.sent = []

    def get(self, url, **kw):
        self.sent.append((url, kw))
        return _Resp()


@pytest.fixture
def requests_path(monkeypatch):
    """把下载器按到 requests 降级路径上，并抓住实际发出的 kwargs。"""
    import engine.manga.downloader as DL
    monkeypatch.setattr(DL, "_curl_engine_available", lambda: False)
    monkeypatch.setattr(DL, "url_is_public_resolved", lambda u: (True, ""))
    monkeypatch.setattr(DL, "pin_requests_session",
                        lambda sess, url, proxy=None: False)   # 一律"绑定不了"
    sess = _FakeSess()
    monkeypatch.setattr(DL, "_requests_session", lambda: sess)
    return DL, sess


def test_requests_path_proxied_skips_pin_requirement(requests_path):
    DL, sess = requests_path
    os.environ["WR_PROXY"] = "127.0.0.1:7897"
    r = DL.fetch_image_checked("https://img.example.com/a.webp", {}, timeout=5)
    assert r.status_code == 200
    assert len(sess.sent) == 1
    url, kw = sess.sent[0]
    assert kw.get("proxies") == {"http": "http://127.0.0.1:7897",
                                 "https": "http://127.0.0.1:7897"}
    assert kw.get("allow_redirects") is False


def test_requests_path_direct_still_fails_closed(requests_path):
    """没代理时"绑不上"必须判不可用，绝不静默发未绑定请求。"""
    from engine.urlsec import PinUnavailable
    DL, sess = requests_path
    with pytest.raises(PinUnavailable):
        DL.fetch_image_checked("https://img.example.com/a.webp", {}, timeout=5)
    assert sess.sent == []


def test_requests_path_proxied_still_checks_each_hop(requests_path, monkeypatch):
    """代理只改变"要不要绑 IP"，SSRF 逐跳校验照旧（跳内网必须拒绝）。"""
    from engine.urlsec import SSRFBlocked
    DL, sess = requests_path
    os.environ["WR_PROXY"] = "127.0.0.1:7897"
    monkeypatch.setattr(DL, "url_is_public_resolved",
                        lambda u: (False, "私网地址"))
    with pytest.raises(SSRFBlocked):
        DL.fetch_image_checked("https://img.example.com/a.webp", {}, timeout=5)
    assert sess.sent == []


# ------------------------------------------------------ 图片通道：curl 主路径

def test_curl_path_passes_proxy_to_pin_and_request(monkeypatch):
    import threading
    import engine.manga.downloader as DL

    os.environ["WR_PROXY"] = "socks5://127.0.0.1:1080"
    monkeypatch.setattr(DL, "_curl_engine_available", lambda: True)
    monkeypatch.setattr(DL, "url_is_public_resolved", lambda u: (True, ""))
    pins = []
    monkeypatch.setattr(DL, "pin_curl_session",
                        lambda sess, url, proxy=None: pins.append((url, proxy)))

    class _Sess(_FakeSess):
        pass

    sess = _Sess()
    lock = threading.Lock()

    def _acquire(timeout=None):
        lock.acquire()
        return sess, lock
    monkeypatch.setattr(DL, "_acquire_session", _acquire)

    r = DL.fetch_image_checked("https://img.example.com/a.webp", {}, timeout=5)
    assert r.status_code == 200
    assert pins == [("https://img.example.com/a.webp", "socks5://127.0.0.1:1080")]
    assert sess.sent[0][1].get("proxies") == {"http": "socks5://127.0.0.1:1080",
                                              "https": "socks5://127.0.0.1:1080"}
    assert not lock.locked(), "成功路径必须归还会话锁"


def test_curl_path_direct_passes_none_proxy(monkeypatch):
    import threading
    import engine.manga.downloader as DL

    monkeypatch.setattr(DL, "_curl_engine_available", lambda: True)
    monkeypatch.setattr(DL, "url_is_public_resolved", lambda u: (True, ""))
    pins = []
    monkeypatch.setattr(DL, "pin_curl_session",
                        lambda sess, url, proxy=None: pins.append(proxy))
    sess = _FakeSess()
    lock = threading.Lock()

    def _acquire(timeout=None):
        lock.acquire()
        return sess, lock
    monkeypatch.setattr(DL, "_acquire_session", _acquire)

    DL.fetch_image_checked("https://img.example.com/a.webp", {}, timeout=5)
    assert pins == [None]
    assert sess.sent[0][1].get("proxies") is None
    assert not lock.locked()
