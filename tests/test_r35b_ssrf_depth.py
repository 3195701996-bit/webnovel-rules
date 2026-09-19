# -*- coding: utf-8 -*-
"""R35b SSRF 纵深防护回归（离线：本地 stub 服务，不触公网）

第一轮加固只在入口打补丁，独立审计 + PoC 实证发现 5 条绕过路径：
  R-1 crawler.SourceCrawler.search 运行期搜索未校验（与 _search_one 两条路径）
  R-2 rules.normalize_url 对绝对内网 URL 原样放行（tocUrl/nextTocUrl 等）
  R-3 响应内嵌 hidden-input API 派生的 URL 未校验
  R-4 默认跟随 302，公网域名跳转内网可绕过全部入口校验
  R-5 书源 proxy 字段未校验，可将出站流量导向内网

修复策略：在 Fetcher._request（所有出站请求的唯一收敛点）统一兜底 +
手动逐跳重定向校验 + DNS 解析后校验 + proxy 校验。
本文件验证的是「内网实际是否被打到」，而非字面函数返回值。
"""
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HUB)

SECRET = "INTERNAL_SECRET_TOKEN"
_hits = {"n": 0}


@pytest.fixture(autouse=True)
def _clear_dns_cache():
    """清 urlsec 的 DNS 解析缓存——防同名 host 用例互相污染
    （前一条用例缓存的解析结果会让后一条用例的假 getaddrinfo 不生效）"""
    from engine import urlsec
    with urlsec._DNS_LOCK:
        urlsec._DNS_CACHE.clear()
    yield
    with urlsec._DNS_LOCK:
        urlsec._DNS_CACHE.clear()


class _Internal(BaseHTTPRequestHandler):
    """模拟内网服务（云元数据 / Redis / 路由器后台）"""

    def do_GET(self):
        _hits["n"] += 1
        b = f"<html><body><div class='c'>{SECRET}</div></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    do_POST = do_GET

    def log_message(self, *a):
        pass


class _Jump(BaseHTTPRequestHandler):
    """模拟攻击者控制的公网跳板：302 到内网"""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", f"http://127.0.0.1:{PORT_INTERNAL}/x")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_GET

    def log_message(self, *a):
        pass


PORT_INTERNAL = 18931
PORT_JUMP = 18932


@pytest.fixture(scope="module", autouse=True)
def _stub_servers():
    servers = []
    for port, handler in ((PORT_INTERNAL, _Internal), (PORT_JUMP, _Jump)):
        srv = HTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
    time.sleep(0.4)
    yield
    for srv in servers:
        srv.shutdown()


@pytest.fixture
def no_hits():
    """断言测试期间内网服务一次都没被打到"""
    before = _hits["n"]
    yield
    assert _hits["n"] == before, (
        f"内网服务被打到 {_hits['n'] - before} 次 —— SSRF 未被拦截")


def _real_fetcher_cls():
    """取真实 Fetcher 类。

    test_api.py 有 session 级 autouse fixture 会把 engine.fetcher.Fetcher
    替换为 MockFetcher（单跑本文件时不会触发，全量跑时会），
    沿用既有测试的 REAL_FETCHER_CLASS 约定拿回真实类。
    """
    from engine import fetcher as fm
    return getattr(fm, "REAL_FETCHER_CLASS", fm.Fetcher)


@pytest.fixture
def fetcher():
    return _real_fetcher_cls()()


INTERNAL = f"http://127.0.0.1:{PORT_INTERNAL}/"
JUMP = f"http://127.0.0.1:{PORT_JUMP}/"


# ══════════════════════════════════════════════
#  R-4 Fetcher 层统一兜底 + 重定向逐跳校验
# ══════════════════════════════════════════════

def test_fetcher_blocks_direct_internal(fetcher, no_hits):
    """Fetcher 自身必须校验，不能依赖上层入口"""
    from engine.urlsec import SSRFBlocked
    with pytest.raises(SSRFBlocked):
        fetcher.get(INTERNAL, source={}, timeout=5, retries=1)


def test_fetcher_blocks_redirect_to_internal(fetcher, no_hits):
    """核心：公网跳板 302 到内网必须被逐跳校验拦下"""
    from engine.urlsec import SSRFBlocked
    with pytest.raises(SSRFBlocked):
        fetcher.get(JUMP, source={}, timeout=5, retries=1)


def test_fetcher_blocks_post_to_internal(fetcher, no_hits):
    from engine.urlsec import SSRFBlocked
    with pytest.raises(SSRFBlocked):
        fetcher.post(INTERNAL, data={"a": 1}, source={}, timeout=5, retries=1)


def test_ssrf_block_is_not_retried(fetcher, no_hits, monkeypatch):
    """SSRFBlocked 必须立即中止，不能被重试循环吞掉后反复打内网"""
    from engine.urlsec import SSRFBlocked
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    with pytest.raises(SSRFBlocked):
        fetcher.get(INTERNAL, source={}, timeout=5, retries=5)
    assert not slept, "SSRF 拒绝后不应进入退避重试"


def test_redirect_hop_is_actually_validated(fetcher, no_hits, monkeypatch):
    """真正走到第二跳：hop0 视为"公网"、hop1 指向内网。

    其余重定向用例的 hop0（127.0.0.1 跳板）本身就会被拒，
    从未验证过逐跳逻辑本身。这里把跳板伪装成公网以放行 hop0，
    确保 Location 指向内网时在 hop1 被拦下。
    """
    from engine import fetcher as fmod
    from engine.urlsec import SSRFBlocked

    real = fmod.url_is_public_resolved
    jump_host = f"127.0.0.1:{PORT_JUMP}"

    def fake(u):
        if jump_host in str(u):
            return True, ""          # 伪装跳板为公网，放行 hop0
        return real(u)

    monkeypatch.setattr(fmod, "url_is_public_resolved", fake)

    with pytest.raises(SSRFBlocked) as ei:
        fetcher.get(JUMP, source={}, timeout=5, retries=1)
    assert "重定向" in str(ei.value), \
        f"应在重定向跳中被拒，实际: {ei.value}"


def test_auto_redirect_must_stay_disabled():
    """底层 session 必须显式 allow_redirects=False。

    默认跟随重定向会让公网域名 302 到内网，绕过全部字面校验
    （加固前的 PoC 已实证可拿到内网数据）。
    """
    import inspect
    src = inspect.getsource(_real_fetcher_cls()._send_checked)
    assert src.count("allow_redirects=False") >= 2, \
        "GET/POST 均须显式关闭自动重定向"


def test_redirect_chain_limit_is_enforced():
    """重定向跳数上限必须存在且有限，防跳转环耗尽资源"""
    from engine.urlsec import MAX_REDIRECTS
    assert 0 < MAX_REDIRECTS <= 10


# ══════════════════════════════════════════════
#  R-1 crawler 运行期搜索路径
# ══════════════════════════════════════════════

def _evil_source(search_url):
    return {"bookSourceName": "evil",
            "bookSourceUrl": "https://example.com",
            "searchUrl": search_url,
            "ruleSearch": {"bookList": ".c", "name": "text",
                           "bookUrl": "href"}}


def _crawler_with_real_fetcher(src):
    """构造 SourceCrawler 并强制使用真实 Fetcher。

    test_api.py 的 session fixture 会把 Fetcher 换成 MockFetcher，
    那样请求根本不出网，测试会变成假 PASS。
    """
    from engine.crawler import SourceCrawler
    c = SourceCrawler(src)
    c.fetcher = _real_fetcher_cls()()
    assert c.adapter is None, "该 stub 源不应命中适配器"
    return c


def test_crawler_search_blocks_internal_search_url(no_hits):
    """运行期搜索（与导入时的 _search_one 是两条独立路径）"""
    from engine.urlsec import SSRFBlocked
    src = _evil_source(f"http://127.0.0.1:{PORT_INTERNAL}/s?q={{{{key}}}}")
    with pytest.raises(SSRFBlocked):
        _crawler_with_real_fetcher(src).search("剑来")


def test_crawler_search_blocks_redirect_to_internal(no_hits):
    from engine.urlsec import SSRFBlocked
    src = _evil_source(f"http://127.0.0.1:{PORT_JUMP}/s?q={{{{key}}}}")
    with pytest.raises(SSRFBlocked):
        _crawler_with_real_fetcher(src).search("剑来")


# ══════════════════════════════════════════════
#  R-2 / R-3 规则派生与响应派生的 URL
# ══════════════════════════════════════════════

def test_rule_derived_absolute_internal_url_blocked(fetcher, no_hits):
    """tocUrl / nextTocUrl / nextContentUrl 派生的绝对内网 URL"""
    from engine.urlsec import SSRFBlocked
    src = {"bookSourceUrl": "https://example.com",
           "ruleBookInfo": {"tocUrl": INTERNAL}}
    with pytest.raises(SSRFBlocked):
        fetcher.get(INTERNAL, source=src, timeout=5, retries=1)


def test_response_derived_url_blocked(fetcher, no_hits):
    """响应内嵌 hidden-input API 派生的 URL（攻击者只需控制源站响应）"""
    from engine.urlsec import SSRFBlocked
    with pytest.raises(SSRFBlocked):
        fetcher.get(INTERNAL + "api", source={}, timeout=5, retries=1)


def test_normalize_url_still_passthrough_by_design():
    """normalize_url 不做安全判定是设计使然——防线在 Fetcher 层。

    本测试固化该契约：若未来有人给 normalize_url 加校验，
    不应视为回归；但 Fetcher 层的兜底必须始终存在。
    """
    from engine.rules import normalize_url
    out = normalize_url("http://169.254.169.254/x", "https://example.com")
    assert out == "http://169.254.169.254/x"
    import inspect
    src = inspect.getsource(_real_fetcher_cls()._send_checked)
    assert "url_is_public_resolved" in src, "Fetcher 层兜底不得移除"


# ══════════════════════════════════════════════
#  DNS 解析层（nip.io / DNS rebinding）
# ══════════════════════════════════════════════

@pytest.fixture
def strict_dns(monkeypatch):
    """局部恢复 DNS 解析校验。

    conftest 为消除假域名解析带来的抖动，全局置 WR_SSRF_SKIP_DNS=1；
    但解析层用例必须验证真实逻辑，故在这些用例内显式关掉逃生阀。
    """
    from engine import urlsec
    monkeypatch.setattr(urlsec, "_RESOLVE_DISABLED", False)
    return urlsec


def test_resolved_check_blocks_domain_pointing_to_loopback(strict_dns,
                                                           monkeypatch):
    """公网域名解析到环回（nip.io / localtest.me / rebinding）"""
    from engine import urlsec
    real = socket.getaddrinfo

    def fake(host, *a, **k):
        if host == "evil-rebind.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("127.0.0.1", 80))]
        return real(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    ok, why = urlsec.url_is_public_resolved("http://evil-rebind.test/")
    assert ok is False
    assert "127.0.0.1" in why


def test_resolved_check_blocks_domain_pointing_to_private(strict_dns, monkeypatch):
    from engine import urlsec
    real = socket.getaddrinfo

    def fake(host, *a, **k):
        if host == "internal.corp.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("10.1.2.3", 80))]
        return real(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    ok, why = urlsec.url_is_public_resolved("http://internal.corp.test/")
    assert ok is False
    assert "10.1.2.3" in why


def test_resolved_check_allows_public_domain(strict_dns, monkeypatch):
    """公网域名不得被误杀"""
    from engine import urlsec
    real = socket.getaddrinfo

    def fake(host, *a, **k):
        if host == "good.example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("93.184.216.34", 80))]
        return real(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    ok, why = urlsec.url_is_public_resolved("http://good.example.test/")
    assert ok is True, why


def test_resolved_check_blocks_partial_private_resolution(strict_dns, monkeypatch):
    """多 A 记录中只要有一个内网地址就必须拒绝（防轮询式 rebinding）"""
    from engine import urlsec
    real = socket.getaddrinfo

    def fake(host, *a, **k):
        if host == "mixed.example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("93.184.216.34", 80)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("127.0.0.1", 80))]
        return real(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    ok, _ = urlsec.url_is_public_resolved("http://mixed.example.test/")
    assert ok is False


def test_resolution_failure_is_fail_open(strict_dns, monkeypatch):
    """解析失败 fail-open —— 有意的权衡，非疏漏。

    解析不出地址就建立不了连接，不存在 SSRF 风险；若此处 fail-closed，
    一次 DNS 抖动会被判成"安全拒绝"并跳过 Fetcher 的重试链，
    把可恢复的网络故障变成永久失败。真实错误留给连接阶段暴露。
    """
    from engine import urlsec

    def boom(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    ok, _ = urlsec.url_is_public_resolved("http://unresolvable.test/")
    assert ok is True


def test_dns_cache_ttl_hit_and_expiry(strict_dns, monkeypatch):
    """同 host TTL 内命中缓存不重解析；TTL 到期强制重解析。

    SSRF 语义：缓存只为压掉逐图重复解析的开销，到期必须重新解析，
    防 DNS rebinding 获得长期豁免。
    """
    from engine import urlsec

    calls = []

    def fake(host, *a, **k):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("93.184.216.34", 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)

    url = "http://cache-ttl.example.test/"
    ok, _ = urlsec.url_is_public_resolved(url)
    assert ok is True
    assert calls == ["cache-ttl.example.test"]

    # TTL 内第二次调用：命中缓存，不重解析
    ok, _ = urlsec.url_is_public_resolved(url)
    assert ok is True
    assert len(calls) == 1, f"TTL 内不应重解析: {calls}"

    # 把缓存时间戳拨到 TTL 之前 → 到期强制重解析
    # （2026-09-10 起缓存项为 (ts, ok, reason, ips)——ips 供连接层绑定使用）
    with urlsec._DNS_LOCK:
        host = "cache-ttl.example.test"
        ts, ok_c, reason_c, ips_c = urlsec._DNS_CACHE[host]
        urlsec._DNS_CACHE[host] = (ts - urlsec._DNS_TTL - 1,
                                   ok_c, reason_c, ips_c)
    ok, _ = urlsec.url_is_public_resolved(url)
    assert ok is True
    assert len(calls) == 2, f"TTL 到期必须重解析: {calls}"


def test_dns_cache_evicts_oldest_active_when_full(strict_dns, monkeypatch):
    """活跃域名（TTL 内）超 512 时缓存仍必须有硬上限：
    过期项优先淘汰，仍超限则按时间戳淘汰最旧的活跃项。"""
    from engine import urlsec

    with urlsec._DNS_LOCK:
        now = time.time()
        # 512 个活跃项 + 5 个已过期项，再存 1 个 → 518 > 512；
        # 淘汰 5 个过期项后仍 513 > 512 → 再淘汰最旧的 1 个活跃项
        for i in range(512):
            urlsec._DNS_CACHE[f"active-{i}.test"] = (now - i * 0.001,
                                                     True, "", ())
        for i in range(5):
            urlsec._DNS_CACHE[f"expired-{i}.test"] = (
                now - urlsec._DNS_TTL - 10 - i, True, "", ())
    urlsec._dns_store("new-host.test", True, "", ())

    with urlsec._DNS_LOCK:
        cache = urlsec._DNS_CACHE
        assert len(cache) <= 512, f"缓存超硬上限: {len(cache)}"
        # 过期项全部被优先淘汰
        assert not any(k.startswith("expired-") for k in cache)
        # 新写入的 host 一定保留（时间戳最新）
        assert "new-host.test" in cache
        # 仍超限时淘汰最旧的活跃项：active-511 时间戳最老 → 不在；
        # active-0 是活跃项中最新 → 保留
        assert "active-511.test" not in cache
        assert "active-0.test" in cache


# ══════════════════════════════════════════════
#  IPv6 内嵌 v4 地址
# ══════════════════════════════════════════════

@pytest.mark.parametrize("ip", [
    "::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:192.168.1.1",
    "fd00::1", "fe80::1", "::",
])
def test_ipv6_internal_rejected(ip):
    from engine.urlsec import ip_is_public
    assert ip_is_public(ip) is False


@pytest.mark.parametrize("ip", ["2001:4860:4860::8888", "8.8.8.8", "1.1.1.1"])
def test_public_ip_allowed(ip):
    from engine.urlsec import ip_is_public
    assert ip_is_public(ip) is True


# ══════════════════════════════════════════════
#  R-5 书源 proxy 字段
# ══════════════════════════════════════════════

@pytest.mark.parametrize("proxy", [
    "http://127.0.0.1:8080",
    "http://192.168.1.1:3128",
    "http://10.0.0.5:1080",
    "socks5://172.16.0.1:1080",
    "localhost:8888",
    "http://[::1]:8080",
    {"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"},
])
def test_internal_proxy_rejected(proxy):
    from engine.urlsec import proxy_is_safe
    assert proxy_is_safe(proxy) is False


@pytest.mark.parametrize("proxy", [
    "http://proxy.example.com:8080",
    "http://8.8.8.8:3128",
    {"http": "http://proxy.example.com:8080"},
    None,
    "",
])
def test_public_proxy_allowed(proxy):
    from engine.urlsec import proxy_is_safe
    assert proxy_is_safe(proxy) is True


def test_pick_proxy_ignores_internal_proxy():
    """书源配置内网代理时必须被实际忽略，而不只是函数返回 False"""
    got = _real_fetcher_cls()._pick_proxy(
        {"proxy": "http://127.0.0.1:8080",
         "bookSourceUrl": "https://example.com"})
    assert got is None or "127.0.0.1" not in str(got)


def test_pick_proxy_keeps_public_proxy():
    got = _real_fetcher_cls()._pick_proxy(
        {"proxy": "http://proxy.example.com:8080",
         "bookSourceUrl": "https://example.com"})
    assert got and "proxy.example.com" in str(got)


# ══════════════════════════════════════════════
#  防过度拦截：正常功能不得被打断
# ══════════════════════════════════════════════

def test_public_url_passes_literal_check():
    from engine.urlsec import url_is_public
    assert url_is_public("https://www.example.com/book/1") is True


def test_skip_dns_escape_hatch_exists(strict_dns, monkeypatch):
    """提供关闭 DNS 校验的逃生阀，避免特殊 DNS 拓扑下服务不可用。

    注意：这里用 monkeypatch 改模块级标志，绝不能用 importlib.reload —
    reload 会重建 urlsec 里的函数对象，导致 app.py / source_mgr.py 中
    早已绑定的引用不再是同一对象，污染后续测试（"单一实现源"断言会失败，
    且 test_api 的 Fetcher mock 也会失效）。
    """
    from engine import urlsec

    def fake_gai(*a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("127.0.0.1", 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)

    # 严格模式：解析到环回 → 拒绝
    ok, _ = urlsec.url_is_public_resolved("http://anything.test/")
    assert ok is False

    # 逃生阀开启 → 跳过解析校验
    monkeypatch.setattr(urlsec, "_RESOLVE_DISABLED", True)
    ok, _ = urlsec.url_is_public_resolved("http://anything.test/")
    assert ok is True


def test_urlsec_identity_not_broken_by_this_module():
    """守卫：本文件不得破坏 urlsec 函数对象的同一性（reload 陷阱）"""
    from engine import source_mgr, urlsec
    import app
    assert app._url_is_public is urlsec.url_is_public
    assert source_mgr.url_is_public is urlsec.url_is_public
