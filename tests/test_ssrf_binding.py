# -*- coding: utf-8 -*-
"""SSRF 连接层绑定（DNS 重绑定防护）回归——全部离线、确定性

设计要点（2026-09-10 结构性优化 + 收敛）：
- 校验（url_is_public_resolved）与实际建连必须使用**同一份解析结果**：
  connect_pin_entries 由已校验的公网 IP 生成 curl CURLOPT_RESOLVE 条目，
  pin_curl_session 写入 curl 会话 → 连接发往已校验 IP，Host/SNI 仍为域名。
- 多 IP 必须合并进**一条** `host:port:ip1,ip2` 条目（libcurl 对同一
  host:port 的多条条目只保留最后一条 → 覆盖丢失）；IPv6 地址用方括号。
- 失败语义与校验对齐（修复 reason 被丢弃）：pin 时重新解析到私网 →
  SSRFBlocked，禁止继续发请求；解析失败 → DNSResolveUnavailable（可重试
  网络错误，不硬封源、不缓存为永久私网封锁）。
 - 绑定是**请求级**作用域：不 pin/换主机的分支真正清除上一次遗留的 RESOLVE
   （下发 `-host:port` 移除条目；仅 `opts.pop` 不清 libcurl DNS 缓存，会
   stale 复用旧绑定——两者均有真实传输用例）。
 - **代理不再导致跳过**（修复"绑定被跳过"的确证缺口）：curl 会话无论是否配置
   代理都写目标 RESOLVE（经代理建连时该条目不被使用、写入无害；不改
   CurlOpt.PROXY，专用代理协议仍走代理）；requests/cloudscraper 会话按 requests
   真实语义（URL scheme / NO_PROXY / trust_env / 显式代理）判定**实际出站**——
   直连则挂同会话受控适配器把建连钉到已校验 IP（SNI/证书按域名），经代理则不钉
   （连接终点是代理）；无法建立绑定抛 PinUnavailable（可重试），绝不静默退回
   未绑定连接。

测试用真实本地 HTTP 服务 + 假会话/假解析器做确定性断言；危险交错用内存
状态直接构造，不依赖真实 DNS。
"""
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import urlsec as US  # noqa: E402
from engine import fetcher as _FETCH_MOD  # noqa: E402

# 收集期捕获真实 Fetcher 类：其它测试（test_api 等）会把
# engine.fetcher.Fetcher 换成 MockFetcher，运行期再 import 会拿到替身
_REAL_FETCHER = getattr(_FETCH_MOD, "REAL_FETCHER_CLASS", _FETCH_MOD.Fetcher)


@pytest.fixture(autouse=True)
def _clear_dns_cache_and_proxy_env(monkeypatch):
    with US._DNS_LOCK:
        US._DNS_CACHE.clear()
    # 代理环境会让 pin 判定变化——测试须确定性，先清空代理与 NO_PROXY 变量
    for k in ("http_proxy", "https_proxy", "all_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)
    yield
    with US._DNS_LOCK:
        US._DNS_CACHE.clear()


def _mk_addrinfo(ips):
    """构造 socket.getaddrinfo 风格返回值"""
    return [(2, 1, 6, "", (ip, 0)) for ip in ips]


@pytest.fixture()
def local_http():
    """本地 HTTP 服务（127.0.0.1:随机端口），返回 (host, port, hits)"""
    hits = []

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield "127.0.0.1", srv.server_address[1], hits
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def local_http6():
    """本地 IPv6 HTTP 服务（[::1]:随机端口）；无 IPv6 环境跳过"""
    if not socket.has_ipv6:
        pytest.skip("环境无 IPv6")
    hits = []

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = b"ok6"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    class _S6(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    try:
        srv = _S6(("::1", 0), _H)
    except OSError:
        pytest.skip("IPv6 回环不可用")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield "::1", srv.server_address[1], hits
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def local_http_rec():
    """本地 HTTP 服务，记录每次请求的 (path, Host 头)；返回 (host, port, recs)"""
    recs = []

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            recs.append((self.path, self.headers.get("Host")))
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield "127.0.0.1", srv.server_address[1], recs
    finally:
        srv.shutdown()
        srv.server_close()


def _start_http(tag=b"ok"):
    """启动返回 tag 的本地 HTTP 服务（127.0.0.1:随机端口）→ (srv, port)"""
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(tag)))
            self.end_headers()
            self.wfile.write(tag)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ══════════════════════════════════════════════════════════════
# 1. 真实传输验证：绑定覆盖系统解析（域名不存在也能连通）
# ══════════════════════════════════════════════════════════════

def test_curl_resolve_pin_overrides_dns(local_http, monkeypatch):
    """把不存在域名的端口绑定到本地服务 IP → 请求成功"""
    from curl_cffi import requests as creq
    from curl_cffi import CurlOpt

    host, port, hits = local_http
    fake_host = "pin-probe.invalid"
    sess = creq.Session(impersonate="chrome")
    sess.curl_options[CurlOpt.RESOLVE] = [f"{fake_host}:{port}:{host}"]
    r = sess.get(f"http://{fake_host}:{port}/pin-ok", timeout=10)
    assert r.status_code == 200
    assert r.text == "ok"
    assert hits == ["/pin-ok"]


def test_multi_ip_single_entry_fallback_real_transport(local_http, monkeypatch):
    """多 IP 必须合并成一条条目 → 首个不可达时回退到可达 IP。

    这是"多条同 host:port 条目互相覆盖"的直接反证：若拆成两条，
    libcurl 只保留最后一条（可能覆盖掉可达 IP）。
    """
    from curl_cffi import requests as creq
    from curl_cffi import CurlOpt

    host, port, hits = local_http
    fake_host = "multi-probe.invalid"
    entry = US._resolve_entry(fake_host, port, ["127.0.0.2", host])
    assert entry.count(",") == 1, entry        # 两地址合并进一条
    sess = creq.Session(impersonate="chrome")
    sess.curl_options[CurlOpt.RESOLVE] = [entry]
    r = sess.get(f"http://{fake_host}:{port}/multi-ok", timeout=10)
    assert r.status_code == 200
    assert hits == ["/multi-ok"]


def test_ipv6_bracket_entry_real_transport(local_http6, monkeypatch):
    """IPv6 目标地址在 RESOLVE 条目中必须方括号化并能连通"""
    from curl_cffi import requests as creq
    from curl_cffi import CurlOpt

    host6, port6, hits = local_http6
    fake_host = "v6-probe.invalid"
    entry = US._resolve_entry(fake_host, port6, [host6])
    assert f"[{host6}]" in entry, entry
    sess = creq.Session(impersonate="chrome")
    sess.curl_options[CurlOpt.RESOLVE] = [entry]
    r = sess.get(f"http://{fake_host}:{port6}/v6-ok", timeout=10)
    assert r.status_code == 200
    assert hits == ["/v6-ok"]


def test_connect_pin_entries_uses_validated_ips(monkeypatch):
    """多 IP 合并为单条逗号条目（不再拆条覆盖）"""
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US.socket, "getaddrinfo",
                        lambda *a, **k: _mk_addrinfo(["93.184.216.34",
                                                      "93.184.216.35"]))
    entries = US.connect_pin_entries("https://multi.example.com/x?y=1")
    assert entries == ["multi.example.com:443:93.184.216.34,93.184.216.35"]


def test_connect_pin_entries_ipv6_bracketed(monkeypatch):
    """IPv6 解析结果的条目地址方括号化"""
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US.socket, "getaddrinfo",
                        lambda *a, **k: _mk_addrinfo(["2606:4700:4700::1111"]))
    entries = US.connect_pin_entries("https://v6.example.com/")
    assert entries == ["v6.example.com:443:[2606:4700:4700::1111]"]


def test_connect_pin_entries_idna_and_port(monkeypatch):
    """IDNA 域名用 Punycode；端口缺省按 scheme 补 80/443；**尾点保留**

    尾点必须保留：libcurl 按 URL 中**字面 host**（含尾点）匹配 CURLOPT_RESOLVE，
    把 `example.com.` 归一去尾点后绑到 `example.com` 是**另一个 key** → 该 URL
    绑定落空、退回真实 DNS（SSRF 窗口）。故 RESOLVE 条目为 `example.com.`。
    """
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US.socket, "getaddrinfo",
                        lambda *a, **k: _mk_addrinfo(["93.184.216.34"]))
    https = US.connect_pin_entries("https://例え.jp/x")
    http = US.connect_pin_entries("http://EXAMple.com./x")
    assert https == ["xn--r8jz45g.jp:443:93.184.216.34"]
    assert http == ["example.com.:80:93.184.216.34"]      # 尾点保留（勿归一去点）


def test_connect_pin_entries_invalid_port(monkeypatch):
    """越界端口 → 不可绑定（返回 []，不抛）"""
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US.socket, "getaddrinfo",
                        lambda *a, **k: _mk_addrinfo(["93.184.216.34"]))
    assert US.connect_pin_entries("https://x.example.com:99999/") == []


def test_connect_pin_entries_rejects_private(monkeypatch):
    """解析到私网 → 无绑定条目且校验拒绝"""
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US.socket, "getaddrinfo",
                        lambda *a, **k: _mk_addrinfo(["127.0.0.1"]))
    assert US.connect_pin_entries("http://rebind.example.com/") == []
    ok, why = US.url_is_public_resolved("http://rebind.example.com/")
    assert ok is False and "非公网" in why
    ips, reason = US.resolve_public_ips("rebind.example.com")
    assert ips == [] and reason


def test_resolve_failure_fail_open_no_pin(monkeypatch):
    """解析失败：校验 fail-open（既有语义不变），但不产生绑定条目"""
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)

    def _boom(*a, **k):
        raise OSError("dns down")
    monkeypatch.setattr(US.socket, "getaddrinfo", _boom)
    ok, why = US.url_is_public_resolved("http://flaky.example.com/")
    assert ok is True and why == ""
    assert US.connect_pin_entries("http://flaky.example.com/") == []


# ══════════════════════════════════════════════════════════════
# 2. 重绑定窗口：同一请求内"先校验、后改解析"仍连已校验 IP
# ══════════════════════════════════════════════════════════════

def test_rebinding_after_validation_keeps_validated_pin(monkeypatch):
    """校验时解析为公网；随后 DNS 被改写为内网 → 绑定条目仍是公网 IP"""
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    calls = {"n": 0}

    def _resolver(host, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _mk_addrinfo(["93.184.216.34"])
        return _mk_addrinfo(["127.0.0.1"])

    monkeypatch.setattr(US.socket, "getaddrinfo", _resolver)
    url = "https://victim.example.com/a"
    ok, why = US.url_is_public_resolved(url)
    assert ok is True
    entries = US.connect_pin_entries(url)
    assert entries == ["victim.example.com:443:93.184.216.34"]
    assert all("127.0.0.1" not in e for e in entries)

    with US._DNS_LOCK:
        US._DNS_CACHE.clear()
    ok2, why2 = US.url_is_public_resolved(url)
    assert ok2 is False and "非公网" in why2


# ══════════════════════════════════════════════════════════════
# 3. 失败语义对齐（危险交错）：不得丢弃 reason 继续发请求
# ══════════════════════════════════════════════════════════════

class _FakeCurlSession:
    def __init__(self, proxies=None, options=None):
        self.proxies = proxies or {}
        self.curl_options = {} if options is None else options


def test_pin_raises_when_revalidation_private(monkeypatch):
    """校验（缓存）返回 public，pin 重新解析到 private → 抛 SSRFBlocked。

    模拟危险交错：校验通过后 TTL 到期/DNS 被改写，pin 二次解析得到私网。
    修复前 pin 返回 False、reason 被丢弃 → 请求按系统解析发往内网。
    """
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    calls = {"n": 0}

    def _resolver(host, *a, **k):
        calls["n"] += 1
        # 校验时公网（被缓存），随后解析被改写为内网
        return _mk_addrinfo(["93.184.216.34"] if calls["n"] == 1
                            else ["10.0.0.9"])

    monkeypatch.setattr(US.socket, "getaddrinfo", _resolver)
    url = "https://interleave.example.com/a"
    ok, _ = US.url_is_public_resolved(url)
    assert ok is True
    with US._DNS_LOCK:                     # 模拟 TTL 到期
        US._DNS_CACHE.clear()
    # 此时 connect_pin_entries 不再产出条目（旧代码会静默放过）
    assert US.connect_pin_entries(url) == []
    sess = _FakeCurlSession()
    with pytest.raises(US.SSRFBlocked):
        US.pin_curl_session(sess, url)
    from curl_cffi import CurlOpt
    assert CurlOpt.RESOLVE not in sess.curl_options      # 未写入任何残留


def test_pin_resolve_failure_is_retryable_not_permanent_ban(monkeypatch):
    """解析失败：pin 抛可重试 DNSResolveUnavailable（非 SSRFBlocked），
    且不缓存为私网永久封锁（后续校验仍 fail-open）。"""
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)

    def _boom(*a, **k):
        raise socket.gaierror("temp dns down")
    monkeypatch.setattr(US.socket, "getaddrinfo", _boom)
    sess = _FakeCurlSession()
    with pytest.raises(US.DNSResolveUnavailable) as ei:
        US.pin_curl_session(sess, "https://flaky2.example.com/x")
    assert not isinstance(ei.value, US.SSRFBlocked)
    # 未被记成私网（缓存中无该 host），下次仍会尝试解析
    with US._DNS_LOCK:
        assert "flaky2.example.com" not in US._DNS_CACHE
    ok, _ = US.url_is_public_resolved("https://flaky2.example.com/x")
    assert ok is True


# ══════════════════════════════════════════════════════════════
# 4. 实际出站判定与请求级绑定：curl 恒绑定（代理错跳修复）、
#    requests 按 NO_PROXY/trust_env/显式代理判定；stale 清理（移除条目真清缓存）
# ══════════════════════════════════════════════════════════════

def test_pin_session_writes_curl_options(monkeypatch):
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US.socket, "getaddrinfo",
                        lambda *a, **k: _mk_addrinfo(["1.2.3.4"]))
    sess = _FakeCurlSession()
    assert US.pin_curl_session(sess, "https://a.example.com/x") is True
    from curl_cffi import CurlOpt
    assert sess.curl_options[CurlOpt.RESOLVE] == ["a.example.com:443:1.2.3.4"]


def test_pin_curl_binds_even_with_session_proxy(monkeypatch):
    """curl 会话配置代理**仍写目标 RESOLVE**（修复错跳）。

    经代理建连时 libcurl 不读该条目（写入无害）；直连时正是所需绑定。
    不改 CurlOpt.PROXY → 专用代理协议仍走代理，绝不因此转直连。
    """
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US.socket, "getaddrinfo",
                        lambda *a, **k: _mk_addrinfo(["1.2.3.4"]))
    from curl_cffi import CurlOpt
    sess = _FakeCurlSession(proxies={"https": "http://proxy.example:8080"})
    assert US.pin_curl_session(sess, "https://a.example.com/x") is True
    assert sess.curl_options[CurlOpt.RESOLVE] == ["a.example.com:443:1.2.3.4"]
    assert CurlOpt.PROXY not in sess.curl_options      # 代理语义不被改动


def test_pin_curl_binds_under_env_proxy_and_replaces_stale(monkeypatch):
    """环境变量代理不改变 curl 绑定；上次遗留的旧 RESOLVE 被本次绑定覆盖。"""
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US.socket, "getaddrinfo",
                        lambda *a, **k: _mk_addrinfo(["1.2.3.4"]))
    from curl_cffi import CurlOpt
    sess = _FakeCurlSession(options={CurlOpt.RESOLVE: ["stale:443:9.9.9.9"]})
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    assert US.pin_curl_session(sess, "https://a.example.com/x") is True
    assert sess.curl_options[CurlOpt.RESOLVE] == ["a.example.com:443:1.2.3.4"]


def test_pin_curl_ignores_request_level_proxy(monkeypatch):
    """请求级 proxy 形参：curl 路径仍绑定（RESOLVE 与 PROXY 互不干扰）"""
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US.socket, "getaddrinfo",
                        lambda *a, **k: _mk_addrinfo(["1.2.3.4"]))
    from curl_cffi import CurlOpt
    sess = _FakeCurlSession()
    assert US.pin_curl_session(
        sess, "https://a.example.com/x",
        proxy="http://proxy.example:8080") is True
    assert sess.curl_options[CurlOpt.RESOLVE] == ["a.example.com:443:1.2.3.4"]


def test_pin_non_curl_without_mount_raises_pin_unavailable(monkeypatch):
    """既非 curl 又无 mount/adapters → 抛可重试 PinUnavailable（不静默直连）"""
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)

    class _Plain:               # 无 mount/adapters：无法建立受控绑定
        proxies = {}

    with pytest.raises(US.PinUnavailable):
        US.pin_curl_session(_Plain(), "https://a.example.com/x")


def test_pin_skips_literal_ip(monkeypatch):
    """字面公网 IP 无解析环节 → 无需绑定"""
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    sess = _FakeCurlSession()
    assert US.pin_curl_session(sess, "https://93.184.216.34/x") is False
    assert sess.curl_options == {}


# ── 4b. requests 真实语义下的"实际出站"判定 ─────────────────────────

def _direct_session():
    """**直连**会话：显式关闭 trust_env。

    背景（实测）：macOS 上 `requests.utils.get_environ_proxies` 会连**系统代理**
    一起读（urllib.getproxies → _scproxy）。开发机开着 Clash 之类的系统代理时
    （本机实测 127.0.0.1:7897），"验证直连 IP 绑定"的用例会走进"经代理不绑定"
    分支而失败——那不是代码错，是环境差异。这些用例验证的是直连语义，故显式声明。
    """
    import requests
    s = requests.Session()
    s.trust_env = False
    return s


def test_effective_proxy_env_applies_when_trust_env(monkeypatch):
    import requests
    s = requests.Session()
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    assert US.effective_proxy_for_requests(
        s, "https://a.example.com/x") == "http://proxy.example:8080"


def test_effective_proxy_trust_env_false_is_direct(monkeypatch):
    """trust_env=False：忽略环境代理 → 实际直连（返回 None）"""
    import requests
    s = requests.Session()
    s.trust_env = False
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    assert US.effective_proxy_for_requests(s, "https://a.example.com/x") is None


def test_effective_proxy_no_proxy_match_is_direct(monkeypatch):
    """NO_PROXY 命中该 URL → 实际直连（返回 None），不得据此跳过绑定"""
    import requests
    s = requests.Session()
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "a.example.com")
    assert US.effective_proxy_for_requests(s, "https://a.example.com/x") is None


def test_effective_proxy_session_proxy_always_applies(monkeypatch):
    """显式会话代理即便 trust_env=False 也生效（返回代理）"""
    import requests
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"https": "http://sessproxy:9"}
    assert US.effective_proxy_for_requests(
        s, "https://a.example.com/x") == "http://sessproxy:9"


# ── 4c. requests/cloudscraper 会话的请求级受控绑定 ──────────────────

def test_pin_requests_direct_when_no_proxy_match_pins(monkeypatch):
    """NO_PROXY 命中（实际直连）→ 必须绑定（原实现此处被错跳）"""
    import requests
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US, "_resolve_for_pin",
                        lambda host: (["93.184.216.34"], "", US._PIN_OK))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "a.example.com")
    s = requests.Session()
    assert US._pin_requests_session(s, "https://a.example.com/x") is True
    assert getattr(s, "_wr_pin_mounts")


def test_pin_requests_direct_when_trust_env_false_pins(monkeypatch):
    """trust_env=False（实际直连）→ 必须绑定（原实现此处被错跳）"""
    import requests
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US, "_resolve_for_pin",
                        lambda host: (["93.184.216.34"], "", US._PIN_OK))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    s = requests.Session()
    s.trust_env = False
    assert US._pin_requests_session(s, "https://b.example.com/x") is True
    assert getattr(s, "_wr_pin_mounts")


def test_pin_requests_skips_only_when_proxy_effective(monkeypatch):
    """环境代理确实生效（trust_env=True 且未 NO_PROXY）→ 不绑定（终点是代理）"""
    import requests
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US, "_resolve_for_pin",
                        lambda host: (["93.184.216.34"], "", US._PIN_OK))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    s = requests.Session()
    assert US._pin_requests_session(s, "https://c.example.com/x") is False
    assert not getattr(s, "_wr_pin_mounts", None)


def test_pin_requests_actual_binding_real_transport(local_http_rec, monkeypatch):
    """真实 requests 传输：受控适配器把建连钉到已校验 IP（域名不可解析也能连通），
    且 Host 头仍为域名（SNI/证书校验按域名）。"""
    import requests
    host, port, recs = local_http_rec
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US, "_resolve_for_pin",
                        lambda h: ([host], "", US._PIN_OK))
    s = _direct_session()
    fake = "noncurl-pin-probe.invalid"
    assert US._pin_requests_session(s, f"http://{fake}:{port}/x") is True
    r = s.get(f"http://{fake}:{port}/pin", timeout=5)
    assert r.status_code == 200 and r.text == "ok"
    assert recs == [("/pin", f"{fake}:{port}")]      # Host 仍是域名，非 IP


def test_pinned_connection_keeps_domain_for_sni():
    """受控连接把建连地址钉到 IP，但 host/_dns_host 仍是域名（SNI/证书按域名）"""
    classes = US._get_pin_classes()
    assert classes is not None, "urllib3 受控绑定类应可构建"
    (pools,) = classes
    conn = pools["https"].ConnectionCls(
        "secure-pin.invalid", 443, pinned_ips=("10.0.0.1",), timeout=1)
    assert conn.host == "secure-pin.invalid"
    assert conn._dns_host == "secure-pin.invalid"
    assert conn._wr_pin_ips == ("10.0.0.1",)


def test_noncurl_binding_guard_noproxy_direct_and_ssl_domain_verify(monkeypatch):
    """内存变异确认（非 curl / requests 路径，全离线、确定性）：

    (a) NO_PROXY 命中 → 实际直连 → 绑定守卫**必须**挂载受控适配器；
    (b) 反向变异（强制 effective_proxy 声称经代理）→ 守卫改为不绑定，证明其
        依据**实际出站**判定而非硬编码，不放过任何直连；
    (c) 绑定时 SSL 域名校验保持：受控连接 host/_dns_host 仍是域名（证书/SNI
        按域名），且未关闭 verify。
    """
    import requests
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US, "_resolve_for_pin",
                        lambda host: (["93.184.216.34"], "", US._PIN_OK))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "d.example.com")
    url = "https://d.example.com/x"

    # (a) 实际直连 → 必须绑定
    s = requests.Session()
    assert US._pin_requests_session(s, url) is True
    assert getattr(s, "_wr_pin_mounts")
    assert s.verify is True                     # 证书校验未被关闭

    # (c) 受控连接仍以域名做 SNI / 证书校验
    (pools,) = US._get_pin_classes()
    conn = pools["https"].ConnectionCls(
        "d.example.com", 443, pinned_ips=("93.184.216.34",), timeout=1)
    assert conn.host == "d.example.com"
    assert conn._dns_host == "d.example.com"
    assert conn._wr_pin_ips == ("93.184.216.34",)

    # (b) 反向变异：假装经代理 → 守卫改为不绑定（证明"实际出站"守卫有效）
    s2 = requests.Session()
    monkeypatch.setattr(
        US, "effective_proxy_for_requests",
        lambda sess, u, proxy=None: "http://proxy.example:8080")
    assert US._pin_requests_session(s2, url) is False
    assert not getattr(s2, "_wr_pin_mounts", None)
    assert s2.verify is True


# ── 4d. stale 清理：仅 pop 不清 libcurl DNS 缓存，移除条目才真正清 ──

def test_bind_resolve_entries_unit(monkeypatch):
    """同 host:port 换 IP 直接覆盖；换主机附带旧主机移除条目"""
    from curl_cffi import CurlOpt

    class _S:
        def __init__(self):
            self.curl_options = {}

    s = _S()
    US._bind_resolve(s, s.curl_options, "a.example.com", 443, ["1.1.1.1"])
    assert s.curl_options[CurlOpt.RESOLVE] == ["a.example.com:443:1.1.1.1"]
    US._bind_resolve(s, s.curl_options, "a.example.com", 443, ["2.2.2.2"])
    assert s.curl_options[CurlOpt.RESOLVE] == ["a.example.com:443:2.2.2.2"]
    US._bind_resolve(s, s.curl_options, "b.example.com", 443, ["3.3.3.3"])
    assert s.curl_options[CurlOpt.RESOLVE] == [
        "-a.example.com:443", "b.example.com:443:3.3.3.3"]


def test_clear_resolve_emits_removal_when_key_tracked(monkeypatch):
    """_clear_resolve 对已跟踪 key 下发移除条目（而非仅 pop）"""
    from curl_cffi import CurlOpt

    class _S:
        pass

    s = _S()
    s.curl_options = {CurlOpt.RESOLVE: ["a.example.com:443:1.1.1.1"]}
    s._wr_pin_key = "a.example.com:443"
    US._clear_resolve(s, s.curl_options)
    assert s.curl_options[CurlOpt.RESOLVE] == ["-a.example.com:443"]
    assert s._wr_pin_key is None


def test_pop_only_leaves_stale_binding_real_transport(monkeypatch):
    """反证（真实传输）：仅 opts.pop 不清 libcurl DNS 缓存——同域名请求
    仍复用旧绑定（stale）。这正是 _clear_resolve 必须下发移除条目的原因。"""
    from curl_cffi import requests as creq, CurlOpt
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    srv, port = _start_http(b"A")
    try:
        fake = "stale-probe.invalid"
        s = creq.Session()
        US._bind_resolve(s, s.curl_options, fake, port, ["127.0.0.1"])
        assert s.get(f"http://{fake}:{port}/", timeout=5).text == "A"
        s.curl_options.pop(CurlOpt.RESOLVE, None)      # 旧实现（仅 pop）
        assert s.get(f"http://{fake}:{port}/", timeout=5).text == "A"
    finally:
        srv.shutdown()
        srv.server_close()


def test_clear_resolve_purges_curl_dns_cache_real_transport(monkeypatch):
    """真实传输：_clear_resolve 下发移除条目 → 真正清除绑定；无 bind 路径
    不再复用旧缓存（域名不存在 → DNSError，而非打到旧 IP）。"""
    from curl_cffi import requests as creq, CurlOpt
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    srv, port = _start_http(b"A")
    try:
        fake = "purge-probe.invalid"
        s = creq.Session()
        US._bind_resolve(s, s.curl_options, fake, port, ["127.0.0.1"])
        assert s.get(f"http://{fake}:{port}/", timeout=5).text == "A"
        US._clear_resolve(s, s.curl_options)
        assert s.curl_options[CurlOpt.RESOLVE] == [f"-{fake}:{port}"]
        assert getattr(s, "_wr_pin_key", None) is None
        with pytest.raises(Exception):
            s.get(f"http://{fake}:{port}/", timeout=5)
    finally:
        srv.shutdown()
        srv.server_close()


def test_rebind_same_domain_changes_binding_real_transport(monkeypatch):
    """真实传输：同域名换目标端口重新绑定 → 旧 key 移除条目 + 新条目；新绑定
    生效、旧绑定被真正清除（不复用）。"""
    from curl_cffi import requests as creq, CurlOpt
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    srvA, pA = _start_http(b"A")
    srvB, pB = _start_http(b"B")
    try:
        fake = "rebind-probe.invalid"
        s = creq.Session()
        US._bind_resolve(s, s.curl_options, fake, pA, ["127.0.0.1"])
        assert s.get(f"http://{fake}:{pA}/", timeout=5).text == "A"
        US._bind_resolve(s, s.curl_options, fake, pB, ["127.0.0.1"])
        entries = s.curl_options[CurlOpt.RESOLVE]
        assert f"-{fake}:{pA}" in entries
        assert f"{fake}:{pB}:127.0.0.1" in entries
        assert s.get(f"http://{fake}:{pB}/", timeout=5).text == "B"
        with pytest.raises(Exception):
            s.get(f"http://{fake}:{pA}/", timeout=5)
    finally:
        srvA.shutdown()
        srvA.server_close()
        srvB.shutdown()
        srvB.server_close()


# ══════════════════════════════════════════════════════════════
# 5. 传输收敛点：先绑定后请求；pin 失败时不得发出请求（fail-closed）
# ══════════════════════════════════════════════════════════════

def test_fetcher_send_checked_pins_before_request(monkeypatch):
    """Fetcher._send_checked 每跳校验后调用绑定，且用绑定后的会话发请求"""
    from curl_cffi import CurlOpt

    pinned = []

    def _pin(sess, url, proxy=None):      # 0.73.0：产品签名多了 proxy 参数
        pinned.append(url)
        sess.curl_options[CurlOpt.RESOLVE] = ["pin"]
        return True

    monkeypatch.setattr("engine.fetcher.pin_curl_session", _pin)
    monkeypatch.setattr("engine.fetcher.url_is_public_resolved",
                        lambda u: (True, ""))

    class _Resp:
        status_code = 200
        headers = {}

    class _Sess:
        proxies = {}
        curl_options = {}
        def get(self, url, **kw):
            assert self.curl_options.get(CurlOpt.RESOLVE) == ["pin"], \
                "必须在绑定后再发请求"
            return _Resp()

    f = _REAL_FETCHER.__new__(_REAL_FETCHER)
    r = f._send_checked(_Sess(), "GET", "http://a.example.com/x", None, {}, 5)
    assert r.status_code == 200
    assert pinned == ["http://a.example.com/x"]


def test_fetcher_fail_closed_no_request_when_pin_raises(monkeypatch):
    """危险交错：pin 判定私网/解析失败必须阻止请求发出（不得静默继续）"""
    def _boom(sess, url, proxy=None):
        raise US.SSRFBlocked("连接层校验拒绝: 私网")

    monkeypatch.setattr("engine.fetcher.pin_curl_session", _boom)
    monkeypatch.setattr("engine.fetcher.url_is_public_resolved",
                        lambda u: (True, ""))

    sent = []

    class _Sess:
        proxies = {}
        curl_options = {}
        def get(self, url, **kw):
            sent.append(url)
            raise AssertionError("pin 失败却仍发出请求")

    f = _REAL_FETCHER.__new__(_REAL_FETCHER)
    with pytest.raises(US.SSRFBlocked):
        f._send_checked(_Sess(), "GET", "http://a.example.com/x", None, {}, 5)
    assert sent == []


def test_manga_fetch_image_checked_pins_before_request(monkeypatch):
    """漫画图片通道同样先绑定后请求"""
    import engine.manga.downloader as DL
    from curl_cffi import CurlOpt

    pinned = []

    def _pin(sess, url, proxy=None):
        pinned.append(url)
        sess.curl_options[CurlOpt.RESOLVE] = ["imgpin"]
        return True

    monkeypatch.setattr(DL, "pin_curl_session", _pin)
    monkeypatch.setattr(DL, "url_is_public_resolved", lambda u: (True, ""))

    class _Resp:
        status_code = 200
        headers = {"Content-Type": "image/webp"}
        content = b"x" * 600

    class _Sess:
        proxies = {}
        curl_options = {}
        def get(self, url, **kw):
            assert self.curl_options.get(CurlOpt.RESOLVE) == ["imgpin"]
            return _Resp()

    import threading as _th
    lock = _th.Lock()

    def _acquire(timeout=None):
        lock.acquire()
        return _Sess(), lock
    monkeypatch.setattr(DL, "_acquire_session", _acquire)

    r = DL.fetch_image_checked("https://img.example.com/a.webp", {}, timeout=5)
    assert r.status_code == 200
    assert pinned == ["https://img.example.com/a.webp"]


def test_manga_fail_closed_no_request_when_pin_raises(monkeypatch):
    """漫画通道：pin 失败必须阻止请求发出并归还会话锁"""
    import engine.manga.downloader as DL

    def _boom(sess, url, proxy=None):
        raise US.SSRFBlocked("连接层校验拒绝: 私网")

    monkeypatch.setattr(DL, "pin_curl_session", _boom)
    monkeypatch.setattr(DL, "url_is_public_resolved", lambda u: (True, ""))

    sent = []

    class _Sess:
        proxies = {}
        curl_options = {}
        def get(self, url, **kw):
            sent.append(url)
            raise AssertionError("pin 失败却仍发出请求")

    import threading as _th
    lock = _th.Lock()

    def _acquire(timeout=None):
        lock.acquire()
        return _Sess(), lock
    monkeypatch.setattr(DL, "_acquire_session", _acquire)

    with pytest.raises(US.SSRFBlocked):
        DL.fetch_image_checked("https://img.example.com/a.webp", {}, timeout=5)
    assert sent == []
    assert not lock.locked(), "失败路径必须归还会话锁"


# ══════════════════════════════════════════════════════════════
# 6. 收敛确证（仅本次确证点）：unmount close+还原、authority 精确 prefix
#    命中（尾点/IDNA/userinfo）、curl 尾点 RESOLVE 命中、TLS 配置保全、
#    真实 HTTPS（SNI/证书按域名）
# ══════════════════════════════════════════════════════════════

def test_unmount_pinned_closes_adapter_once_and_restores_original(monkeypatch):
    """点A：_unmount_pinned 必须 close 受控适配器（同一实例去重仅一次），
    并**还原用户原 mount**——只 pop 不 close 会在重定向换 host/IP 长跑时泄漏
    socket；不还原则破坏用户已 mount 的自定义适配器配置。
    """
    import requests
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US, "_resolve_for_pin",
                        lambda host: (["93.184.216.34"], "", US._PIN_OK))

    s = _direct_session()
    orig = requests.adapters.HTTPAdapter()
    s.mount("https://a.example.com/", orig)          # 用户原有的自定义 mount

    # 非默认端口 → authority 前缀与规范化 host 前缀是两个不同 prefix
    assert US._pin_requests_session(s, "https://a.example.com:8443/x") is True
    prefixes = list(s._wr_pin_mounts)
    assert len(prefixes) == 2, prefixes             # 同一受控适配器挂 2 个 prefix
    pinned = s.get_adapter("https://a.example.com:8443/x")
    assert pinned is not orig and getattr(pinned, "_wr_is_pinned", False)

    closed = []
    monkeypatch.setattr(pinned, "close", lambda: closed.append(id(pinned)))

    US._unmount_pinned(s)
    assert closed == [id(pinned)], "受控适配器必须被 close（去重后仅一次）"
    # 用户原 mount 被逐 prefix 还原（:8443 无原值 → 删除；a.example.com/ → 还原）
    assert s.get_adapter("https://a.example.com/x") is orig
    assert "https://a.example.com:8443/" not in s.adapters
    assert not getattr(s, "_wr_pin_mounts", None)
    assert getattr(s, "_wr_pin_prev", None) == {}


@pytest.mark.parametrize("url", [
    "https://bind-probe.example.com/x",               # 基线
    "https://bind-probe.example.com./x",              # 尾点
    "https://bücher.bind-probe.example/x",            # IDNA → Punycode
])
def test_mount_pinned_get_adapter_is_pinned(monkeypatch, url):
    """点B：挂载后对**实际请求 URL**（PreparedRequest.url 规范化 authority）
    调用 get_adapter 必命中受控适配器（is pinned），绝不静默落到 scheme 级
    默认 adapter（未绑定直连）。尾点/IDNA 路径须能绑定。
    """
    import requests
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US, "_resolve_for_pin",
                        lambda host: (["93.184.216.34"], "", US._PIN_OK))
    s = _direct_session()
    assert US._pin_requests_session(s, url) is True
    probe = US._prepared_url(url)
    got = s.get_adapter(probe)
    assert getattr(got, "_wr_is_pinned", False), f"未命中受控适配器: {url} -> {probe}"
    assert got is not s.adapters.get("https://")     # 不是 scheme 级默认 adapter
    US._unmount_pinned(s)


def test_mount_pinned_userinfo_binds_where_plain_prefix_misses(monkeypatch):
    """点B 反证：URL 带 userinfo 时，requests 的 PreparedRequest.url 是
    `https://user:pass@host/...`，**纯字符串 host 前缀会 miss → 落默认 adapter**；
    按 authority 精确取 prefix 才能命中受控适配器。
    """
    import requests
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US, "_resolve_for_pin",
                        lambda host: (["93.184.216.34"], "", US._PIN_OK))
    url = "https://user:pass@bind-probe.example.com/x"
    probe = US._prepared_url(url)
    assert probe.startswith("https://user:pass@")    # userinfo 保留

    # 只挂规范化 host 前缀（naive 做法）→ 对带 userinfo 的实际 URL 不命中
    naive = _direct_session()
    naive.mount("https://bind-probe.example.com/", requests.adapters.HTTPAdapter())
    assert naive.get_adapter(probe) is naive.adapters["https://"]

    # 本模块按实际 authority 精确挂载 → 命中受控适配器
    s = _direct_session()
    assert US._pin_requests_session(s, url) is True
    assert getattr(s.get_adapter(probe), "_wr_is_pinned", False)
    US._unmount_pinned(s)


def test_mount_pinned_fail_closed_when_probe_misses(monkeypatch):
    """点B fail-closed：若挂载后实际 URL 未命中受控适配器 → 抛 PinUnavailable
    （可重试），且失败路径复位（移除受控挂载，不留残留）。"""
    import requests
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US, "_resolve_for_pin",
                        lambda host: (["93.184.216.34"], "", US._PIN_OK))
    s = _direct_session()
    # 破坏 get_adapter：永远返回 scheme 级默认 → 触发 fail-closed
    monkeypatch.setattr(s, "get_adapter", lambda u: s.adapters["https://"])
    with pytest.raises(US.PinUnavailable):
        US._pin_requests_session(s, "https://fail-probe.example/x")
    assert not getattr(s, "_wr_pin_mounts", None)    # 失败路径无残留受控挂载


def test_curl_trailing_dot_resolve_matches_real_transport(monkeypatch):
    """点C（真实传输）：带尾点的 URL host 必须用**保留尾点**的 RESOLVE 条目
    才能命中；把 host 归一去尾点后绑到另一个 key → 该 URL 解析失败。
    证明"别归一去尾点后绑定另一 key"。
    """
    from curl_cffi import requests as creq, CurlOpt
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US.socket, "getaddrinfo",
                        lambda *a, **k: _mk_addrinfo(["93.184.216.34"]))
    srv, port = _start_http(b"D")
    try:
        dotted = "dot-probe.invalid."
        # connect_pin_entries 的真实产物形态：保留尾点
        entries = US.connect_pin_entries(f"http://{dotted}:{port}/")
        assert entries == [f"{dotted}:{port}:93.184.216.34"], entries

        s = creq.Session()
        s.curl_options[CurlOpt.RESOLVE] = \
            [US._resolve_entry(dotted, port, ["127.0.0.1"])]
        assert s.get(f"http://{dotted}:{port}/", timeout=5).text == "D"

        # 去尾点条目 → 带尾点 URL 不命中（解析失败），不是打到旧 IP
        s2 = creq.Session()
        s2.curl_options[CurlOpt.RESOLVE] = \
            [US._resolve_entry("dot-probe.invalid", port, ["127.0.0.1"])]
        with pytest.raises(Exception):
            s2.get(f"http://{dotted}:{port}/", timeout=5)
    finally:
        srv.shutdown()
        srv.server_close()


def test_make_pinned_adapter_preserves_base_tls_config():
    """点D：受控适配器以会话现有适配器**实例**为模板复制其全部配置
    （ssl_context/cipherSuite/池参数），clone 出的 poolmanager 继承其
    connection_pool_kw —— 不是 default 新建后宣称保全（cloudscraper TLS 配置）。
    """
    import urllib3.poolmanager as _pm
    from urllib3.util.ssl_ import create_urllib3_context

    ctx = create_urllib3_context()

    class _FakeCSAdapter:                      # 模拟 cloudscraper 适配器配置
        def __init__(self):
            self.ssl_context = ctx
            self.cipherSuite = "ECDHE-RSA-AES128-GCM-SHA256"
            self.verify = True
            self.poolmanager = _pm.PoolManager(
                num_pools=7, cert_reqs="CERT_REQUIRED", ssl_context=ctx)

    base = _FakeCSAdapter()
    (pools,) = US._get_pin_classes()
    ad = US._make_pinned_adapter(base, ("93.184.216.34",), pools)

    assert ad is not base
    assert ad.ssl_context is ctx                       # TLS 上下文被复制
    assert ad.cipherSuite == "ECDHE-RSA-AES128-GCM-SHA256"
    assert ad.verify is True                           # 证书校验未被关闭
    assert ad.poolmanager is not base.poolmanager      # 克隆而非复用/新建默认
    cpk = ad.poolmanager.connection_pool_kw
    assert cpk.get("ssl_context") is ctx
    assert cpk.get("cert_reqs") == "CERT_REQUIRED"
    assert ad.poolmanager.pools._maxsize == 7          # 池容量被克隆
    assert cpk.get("pinned_ips") == ("93.184.216.34",)


def _make_self_signed_cert(hostname):
    """生成 SAN=hostname 的自签证书 → (cert_pem, key_pem)（仅测试用）。"""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.utcnow()
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.TraditionalOpenSSL,
                              serialization.NoEncryption()))


@pytest.fixture()
def local_https(tmp_path):
    """本地 HTTPS 服务（自签证书 SAN=假域名）→ (host, port, ca, seen_sni)"""
    import ssl
    host = "pin-https-probe.invalid"
    cert_pem, key_pem = _make_self_signed_cert(host)
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)
    seen_sni = []

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"https-ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    ctx.sni_callback = lambda sock, server_name, c: seen_sni.append(server_name)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield host, srv.server_address[1], str(cert_path), seen_sni
    finally:
        srv.shutdown()
        srv.server_close()


def test_pinned_https_real_transport_domain_sni_and_cert(local_https, monkeypatch):
    """真实 HTTPS 传输：受控适配器把建连钉到已校验 IP，但 TLS 握手用**域名**
    作 SNI、证书按域名校验（verify=True + 自签 CA）→ 请求成功。
    这落成用例的是此前探针的真实 HTTPS 结果：绑定不影响 SNI/证书语义。
    """
    import requests
    host, port, ca, seen_sni = local_https
    monkeypatch.setattr(US, "_RESOLVE_DISABLED", False)
    monkeypatch.setattr(US, "_resolve_for_pin",
                        lambda h: (["127.0.0.1"], "", US._PIN_OK))
    s = _direct_session()
    s.trust_env = False
    assert US._pin_requests_session(s, f"https://{host}:{port}/x") is True
    r = s.get(f"https://{host}:{port}/tls", timeout=10, verify=ca)
    assert r.status_code == 200 and r.text == "https-ok"
    assert seen_sni == [host], f"SNI 必须为域名（非已校验 IP）: {seen_sni}"
    US._unmount_pinned(s)


def test_rejection_reason_distinguishes_sinkhole_from_private():
    """0.0.0.0 是占位解析（DNS 封锁/废弃域名），原因文案要与"真的指向内网"区分开。

    这里直接测纯函数，不走 socket/monkeypatch：本文件有共享的 autouse 夹具与
    模块级缓存，走网络模拟的写法在全量套件里不稳定（上一轮已踩过一次）。
    安全决策（拒绝建连）由既有用例覆盖，本用例只锁定文案语义。
    """
    from engine.urlsec import rejection_reason
    r1 = rejection_reason("blocked.example.com", "0.0.0.0")
    assert "占位解析" in r1 and "0.0.0.0" in r1 and "DNS" in r1
    r2 = rejection_reason("inner.example.com", "192.168.1.10")
    assert "非公网地址 192.168.1.10" in r2 and "占位解析" not in r2
    r3 = rejection_reason("h", "::")
    assert "占位解析" in r3
