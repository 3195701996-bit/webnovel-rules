# -*- coding: utf-8 -*-
"""URLPolicy：统一 SSRF 防护（单一实现源）

原本 _url_is_public / _safe_target_url 只存在于 app.py，engine 层（书源导入
与可用性校验）完全没有校验，导致 /api/sources/import 可让服务器代拉内网地址。
本模块下沉为共享实现，app.py 与 engine 均引用此处，避免逻辑分叉。

只做字面 host 判断（不做 DNS 解析，避免解析竞态与请求延迟）；
域名型 host 放行（源站均为公网域名），直接 IP 型 host 严格过滤。
"""
import ipaddress
import os
import re
import socket
import threading
import time
from urllib.parse import urlparse

__all__ = ["url_is_public", "safe_target_url", "ip_is_public",
           "url_is_public_resolved", "proxy_is_safe", "MAX_REDIRECTS",
           "SSRFBlocked", "DNSResolveUnavailable", "PinUnavailable",
           "connect_pin_entries", "pin_curl_session", "resolve_public_ips",
           "effective_proxy_for_requests", "NON_CURL_PIN_LIMITATION"]


class SSRFBlocked(ValueError):
    """请求目标被 SSRF 策略拒绝（含重定向逐跳校验失败）。"""


class DNSResolveUnavailable(ConnectionError):
    """DNS 无法解析出可用地址，导致连接层无法完成 IP 绑定。

    这是**可重试的网络错误**，不是安全拒绝，也不是永久封源依据：
    调用方的重试链应正常重试；不得据此把域名/书源标记为永久失败或
    全局禁用（解析失败不缓存为私网永久封锁，见 resolve_public_ips）。
    """


class PinUnavailable(ConnectionError):
    """连接层**受控 IP 绑定能力不可用**（非 curl 传输无法建立 pin）。

    同样是**可重试的网络错误**：调用方必须据此重试/降级，**绝不允许**
    静默退回未绑定的连接（那会把"校验后 DNS 重写为内网"的窗口放回）。
    仅在 urllib3/SSL 依赖缺失或适配器挂载失败时抛出。
    """


# 重定向逐跳校验的最大跳数（超过视为异常，拒绝）
MAX_REDIRECTS = 5

# ── 非 curl 回退传输的受控绑定与**残余限制** ────────────────────────
# requests / cloudscraper 回退传输不再"只靠校验阶段解析"：本模块在**同一
# 会话上局部挂载** HTTPAdapter（urllib3 连接类把建连地址钉到已校验 IP，
# self.host 仍为真实域名 → SNI/证书校验/Host 头不变），不改进程全局 DNS、
# 不改 urllib3 默认类。绑定不可用时抛 PinUnavailable（可重试），不静默直连。
#
# 仍未覆盖的边界（明确声明，非"用它当前提"）：
#   1) 经代理出站（requests 语义下的实际代理，含 NO_PROXY/trust_env 判定）
#      时连接终点是代理，目标 IP 绑定不适用——依赖代理自身的 proxy_is_safe
#      校验，而不是"静默直连"；
#   2) 为保持 TLS 指纹并复用连接，挂载适配器以会话现有适配器类为基类；若
#      站点校验客户端 TLS 指纹，替换适配器可能改变指纹（cloudscraper 详见）。
NON_CURL_PIN_LIMITATION = (
    "非 curl 传输经代理出站时不做目标 IP 绑定（连接终点为代理）；"
    "直连时以 urllib3 受控连接绑定已校验 IP，绑定不可用则抛 PinUnavailable")

# 关闭 DNS 解析校验的逃生阀（默认开启解析校验）。
# 仅在解析开销不可接受或环境有特殊 DNS 拓扑时使用。
_RESOLVE_DISABLED = os.environ.get("WR_SSRF_SKIP_DNS", "").strip() == "1"


def ip_is_public(ip_str):
    """判断单个 IP 字面量是否为公网地址。"""
    try:
        ip = ipaddress.ip_address(str(ip_str).strip())
    except ValueError:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_private \
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified \
            or ip.is_global is False:
        return False
    # CGNAT 100.64.0.0/10（is_private 在部分版本不含）与 0.0.0.0/8 显式拦截
    if ip.version == 4:
        n = int(ip)
        if (0x64400000 <= n <= 0x647FFFFF) or (n < 0x01000000):
            return False
    # IPv4-mapped / 6to4 / Teredo：取出内嵌的 v4 地址再判一次。
    # 注：当前 CPython 对 ::ffff:127.0.0.1 已能正确判定 is_private/is_global，
    # 故本段是冗余的纵深防御（变异测试删除后无用例失败）。保留原因：
    # 6to4/Teredo 的内嵌地址判定在各版本间不一致，且此处成本极低。
    if ip.version == 6:
        for embedded in (getattr(ip, "ipv4_mapped", None),
                         getattr(ip, "sixtofour", None),
                         getattr(ip, "teredo", None)):
            if embedded is None:
                continue
            cand = embedded[1] if isinstance(embedded, tuple) else embedded
            if not ip_is_public(cand):
                return False
    return True


def url_is_public(url):
    """字面判断 URL 是否指向公网 http/https 目标（不解析 DNS）。

    注意：域名一律放行，因此本函数单独使用无法防住把域名解析到内网的
    攻击（nip.io / DNS rebinding）。网络层请改用 url_is_public_resolved。
    """
    if not url:
        return False
    try:
        p = urlparse(str(url))
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True  # 域名：字面阶段放行
    return ip_is_public(host)


# R74: DNS 解析结果短缓存——漫画阅读/下载逐图调用 url_is_public_resolved,
# 每图一次 socket.getaddrinfo(冷解析实测 0.5s, 系统缓存失效时每图都吃),
# 热缓存把重复域名校验压到微秒级。SSRF 语义: 域名→公网判定带 120s TTL,
# 到期强制重解析(防 DNS rebinding 长期豁免); 解析失败不缓存(fail-open
# 语义与真实连接一致, 下次仍会尝试)。
# 2026-09-10: 缓存同时保存**已校验的公网 IP 列表**——连接层据此绑定实际
# 建连地址(见 connect_pin_entries), 使"校验"与"建连"使用同一份解析结果,
# 消除"校验后 DNS 被改写为内网"的重绑定窗口。
_DNS_CACHE = {}          # host -> (ts, ok, reason, ips)
_DNS_TTL = 120
_DNS_LOCK = threading.Lock()


def _dns_lookup_cached(host):
    """返回 (ok, reason, ips)；命中缓存直接返回。ips 为已校验的公网地址。"""
    now = time.time()
    with _DNS_LOCK:
        hit = _DNS_CACHE.get(host)
        if hit and now - hit[0] < _DNS_TTL:
            return hit[1], hit[2], hit[3]
    return None


def _dns_store(host, ok, reason, ips=()):
    with _DNS_LOCK:
        _DNS_CACHE[host] = (time.time(), ok, reason, tuple(ips))
        if len(_DNS_CACHE) > 512:
            _now = time.time()
            # 超限先淘汰已过期项
            for _h in [k for k, v in _DNS_CACHE.items()
                       if _now - v[0] > _DNS_TTL]:
                _DNS_CACHE.pop(_h, None)
            # 仍超限（TTL 内活跃域名超 512）→ 按写入时间戳最旧淘汰，
            # 保证缓存有硬上限，防内存随活跃域名数无界增长
            if len(_DNS_CACHE) > 512:
                _stale = sorted(_DNS_CACHE.items(), key=lambda kv: kv[1][0])
                for _h, _ in _stale[:len(_DNS_CACHE) - 512]:
                    _DNS_CACHE.pop(_h, None)


def normalize_host(host, keep_trailing_dot=False):
    """域名规范化：小写、非 ASCII 域名转 IDNA(Punycode)，默认去尾点。

    - DNS 解析/缓存/校验用（默认）：去尾点（`example.com.` 与 `example.com`
      是同一域名，需归一到同一缓存键）；
    - `keep_trailing_dot=True`：**保留尾点**——libcurl 以 URL 中字面 host（含
      尾点）匹配 CURLOPT_RESOLVE，去尾点会绑到另一个 key → 该 URL 绑定落空、
      退回真实 DNS（SSRF 窗口）。故 curl RESOLVE 条目必须按此形态。
    IP 字面量（含 IPv6）原样返回。转换失败时回退原值（不放大影响）。
    """
    host = (host or "").strip().lower()
    if not keep_trailing_dot:
        host = host.rstrip(".")
    if not host:
        return ""
    try:
        if all(ord(c) < 128 for c in host):
            return host
        if keep_trailing_dot and host.endswith("."):
            return host[:-1].encode("idna").decode("ascii") + "."
        return host.encode("idna").decode("ascii")
    except Exception:
        return host


# 判断"实际是否经代理出站"时检查的环境变量（libcurl 默认读取这些小写名，
# 为稳妥同时兼容大写）。仅用于**注明语义**，不再用"任意一个就跳过绑定"。
_PROXY_ENV_VARS = ("http_proxy", "https_proxy", "all_proxy")


def effective_proxy_for_requests(sess, url, proxy=None):
    """按 **requests 真实语义**判断该 URL 实际出站是否经代理（返回代理串或 None）。

    与 requests 的 `merge_environment_settings` / `Session.send` 对齐：
    - 显式代理（请求级 `proxy` / 会话 `sess.proxies`）总是生效；
    - 仅当 `sess.trust_env` 为真时才合并**环境变量**代理，且
      `requests.utils.get_environ_proxies` 已按 `NO_PROXY` 过滤本 URL；
    - 最后按 URL scheme 选择（`select_proxy`），`all` 兜底。

    关键：NO_PROXY 命中该 URL、或 `trust_env=False` 而环境仅有代理变量时，
    实际是**直连**——此时必须绑定（不能像 `env_proxy_present` 那样只看
    "环境里有没有代理"就跳过，那正是被跳过的 SSRF 缺口）。
    """
    proxies = {}
    sp = getattr(sess, "proxies", None)
    if isinstance(sp, dict):
        proxies.update(sp)
    try:
        if getattr(sess, "trust_env", True):
            import requests.utils as _ru
            for k, v in _ru.get_environ_proxies(url).items():
                proxies.setdefault(k, v)
    except Exception:
        pass
    if proxy:
        # 请求级显式代理优先
        try:
            from urllib.parse import urlparse as _up
            proxies.setdefault(_up(url).scheme, proxy)
        except Exception:
            proxies.setdefault("all", proxy)
    try:
        import requests.utils as _ru
        return _ru.select_proxy(url, proxies)
    except Exception:
        try:
            from urllib.parse import urlparse as _up
            sch = (_up(url).scheme or "").lower()
            return proxies.get(sch) or proxies.get("all")
        except Exception:
            return None


def _url_host_port(url, keep_trailing_dot=False):
    """从 URL 取规范化 host 与端口；非法 URL/端口返回 None。

    端口缺省按 scheme 补 80/443（RESOLVE 条目必须带端口）；p.port 对越界
    端口会抛 ValueError，此类 URL 直接判为不可绑定。
    连接层绑定（curl RESOLVE）传 `keep_trailing_dot=True`：libcurl 按 URL 字面
    host 匹配 RESOLVE，尾点必须保留，否则绑到另一 key → 绑定落空。
    """
    try:
        p = urlparse(str(url))
    except Exception:
        return None
    if p.scheme not in ("http", "https"):
        return None
    host = normalize_host(p.hostname or "", keep_trailing_dot=keep_trailing_dot)
    if not host:
        return None
    try:
        port = p.port
    except ValueError:
        return None
    if not port:
        port = 443 if p.scheme == "https" else 80
    return host, port


def _is_ip_literal(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _addr_fmt(ip):
    """IPv6 地址在 RESOLVE 条目中必须用方括号包裹（内含冒号会破坏分隔）。"""
    return f"[{ip}]" if ":" in ip else ip


def _resolve_entry(host, port, ips):
    """生成**单条** CURLOPT_RESOLVE 条目：host:port:ip1,ip2,...

    关键：同一 host:port 的多条独立条目在 libcurl 中会互相覆盖（后者生效），
    多地址必须合并进一条、以逗号分隔；IPv6 地址用方括号，IPv6 字面量 host
    也用方括号。这样才真正实现"多 IP 候选 + 连接失败回退"。
    """
    h = f"[{host}]" if ":" in host else host
    addrs = ",".join(_addr_fmt(ip) for ip in ips)
    return f"{h}:{port}:{addrs}"


# 绑定决策状态
_PIN_OK = "ok"            # 解析出公网地址，可绑定
_PIN_SKIP = "skip"        # 显式关闭 DNS 校验（WR_SSRF_SKIP_DNS）
_PIN_PRIVATE = "private"  # 解析到非公网地址 → 必须拒绝建连
_PIN_UNRESOLVED = "unresolved"  # 解析失败 → 可重试网络错误，不绑定


def _resolve_for_pin(host):
    """连接层绑定用的解析决策：返回 (ips, reason, status)。

    - _PIN_OK：ips 为已校验公网地址，且与校验同源（同一缓存）
    - _PIN_PRIVATE：reason 说明，调用方必须拒绝
    - _PIN_UNRESOLVED：解析失败，调用方应抛可重试网络错误（不硬封源）
    - _PIN_SKIP：显式关闭解析，保持既有跳过语义
    """
    if _RESOLVE_DISABLED:
        return [], "", _PIN_SKIP
    if _is_ip_literal(host):
        # 字面 IP 无解析环节（不存在 DNS 重绑定窗口），无需 bind
        return ([host], "", _PIN_OK) if ip_is_public(host) else \
            ([], f"目标是非公网地址 {host}", _PIN_PRIVATE)
    ips, reason = resolve_public_ips(host)
    if reason:
        return [], reason, _PIN_PRIVATE
    if not ips:
        return [], "", _PIN_UNRESOLVED
    return ips, "", _PIN_OK


def resolve_public_ips(host):
    """解析 host 并校验全部地址为公网，返回 (公网IP列表, reason)。

    - 返回的 IP 列表与"校验通过"同源(同一份 getaddrinfo 结果)，供连接层
      绑定实际建连地址使用（connect_pin_entries）
    - 解析失败 → ([], "")：fail-open，交由真实连接阶段报错（与
      url_is_public_resolved 的既有语义一致）。**不缓存**解析失败，避免把
      一次 DNS 抖动固化为此后的永久封锁。
    - 字面公网 IP → ([ip], "")
    - 命中私网/回环等 → ([], reason)，该 host 不得建连
    """
    host = normalize_host(host)
    if not host:
        return [], "目标主机为空"
    if _RESOLVE_DISABLED:
        # 测试/显式关闭解析：不解析也不绑定（保持既有"跳过 DNS 校验"语义）
        return [], ""
    try:
        ip = ipaddress.ip_address(host)
        return ([str(ip)], "") if ip_is_public(host) else \
            ([], f"目标是非公网地址 {host}")
    except ValueError:
        pass
    cached = _dns_lookup_cached(host)
    if cached is not None:
        ok, reason, ips = cached
        return (list(ips) if ok else []), reason
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except Exception:
        return [], ""            # fail-open（不缓存，下次重试解析）
    ips = []
    for info in infos:
        addr = info[4][0]
        if not ip_is_public(addr):
            # 0.0.0.0 / :: 是**占位解析**（DNS 封锁、域名已废弃、运营商劫持都会返回它），
            # 与"真的指向内网"不是一回事。安全决策不变（一样拒绝建连），
            # 但给用户的原因必须能分辨：前者是"这个站在这条网络上访问不了"，
            # 后者才是"目标是非公网地址"。实测 6 个源都命中 0.0.0.0。
            reason = rejection_reason(host, addr)
            _dns_store(host, False, reason, ())
            return [], reason
        if addr not in ips:
            ips.append(addr)
    _dns_store(host, True, "", ips)
    return ips, ""


def rejection_reason(host, addr):
    """非公网解析结果的**原因文案**（纯函数，便于单测）。

    0.0.0.0 / :: 是占位解析——DNS 封锁、域名废弃、运营商劫持都返回它，
    与"真的指向内网"不是一回事（实测 6 个内置书源都命中 0.0.0.0）。
    安全决策不变（一样拒绝建连），只是原因要能让人分辨，
    否则用户会以为是自己填了内网地址。
    """
    if addr in ("0.0.0.0", "::", "0:0:0:0:0:0:0:0"):
        return (f"域名 {host} 解析到 {addr}（占位解析：域名可能已被 DNS 封锁、"
                f"已废弃或遭劫持），本机网络无法访问该站")
    return f"域名 {host} 解析到非公网地址 {addr}"


def connect_pin_entries(url):
    """构造 curl CURLOPT_RESOLVE 条目，返回**单元素列表**或 []。

    返回如 ["host:443:ip1,ip2"]（多 IP 逗号合并、IPv6 方括号）。同一
    host:port 拆成多条会互相覆盖（libcurl 只保留最后一条），因此必须合并。
    无可用解析结果（解析失败/私网/关闭解析/IP 字面量）返回 []——字面量无需
    bind（无解析环节）。校验与建连同源：条目 IP 即校验通过的那份解析结果。
    """
    hp = _url_host_port(url, keep_trailing_dot=True)
    if hp is None:
        return []
    host, port = hp
    if _is_ip_literal(host):
        return []                       # 字面 IP 无解析环节，无需绑定
    ips, _reason, status = _resolve_for_pin(host)
    if status != _PIN_OK or not ips:
        return []
    return [_resolve_entry(host, port, ips)]


def _pin_key(host, port):
    return f"{host}:{port}"


def _clear_resolve(sess, opts):
    """解除本会话上一次写入的 RESOLVE 绑定——**真正清**

    实测（本仓库 venv, curl_cffi 0.16.1 + libcurl）：仅把 `curl_options` 里的
    `CurlOpt.RESOLVE` pop 掉**不会**清除 libcurl 的 DNS 缓存——handle 上已解析的
    绑定仍在，后续"无绑定"请求会**继续复用旧 IP**（stale）。要真正解除，必须
    再对该 host:port 下发**移除条目** `-host:port`（libcurl 会据此从 DNS 缓存
    删除），见 test_ssrf_binding 的 stale/purge 用例。

    安全说明：本模块从不把私网 IP 写进 RESOLVE（私网在写入前就抛
    SSRFBlocked），故"无绑定路径复用旧私网缓存"结构上不可能发生；此处清的是
    **公网**旧绑定，避免"域名换 IP 后仍打到旧 IP / 跨主机串味"。
    """
    old = getattr(sess, "_wr_pin_key", None)
    try:
        from curl_cffi import CurlOpt
    except Exception:
        return
    if old:
        # 关键：下发移除条目，真正从 libcurl DNS 缓存删除该绑定
        try:
            opts[CurlOpt.RESOLVE] = [f"-{old}"]
        except Exception:
            try:
                opts.pop(CurlOpt.RESOLVE, None)
            except Exception:
                pass
    else:
        try:
            opts.pop(CurlOpt.RESOLVE, None)
        except Exception:
            pass
    try:
        sess._wr_pin_key = None
    except Exception:
        pass


def _bind_resolve(sess, opts, host, port, ips):
    """写入绑定；若上一次绑的是**另一**主机则同时下发其移除条目（防跨主机串味）。

    同一 host:port 的**换 IP** 用单条新条目覆盖即可（实测 libcurl 后者生效）。
    """
    from curl_cffi import CurlOpt
    key = _pin_key(host, port)
    old = getattr(sess, "_wr_pin_key", None)
    entries = []
    if old and old != key:
        entries.append(f"-{old}")
    entries.append(_resolve_entry(host, port, ips))
    opts[CurlOpt.RESOLVE] = entries
    sess._wr_pin_key = key


# ══════════════════════════════════════════════════════════════════════
# 非 curl 传输（requests / cloudscraper）：同会话局部 HTTPAdapter 受控绑定
# ══════════════════════════════════════════════════════════════════════
# urllib3(2.x) 的 HTTP(S)Connection 用 `self._dns_host` 建连、用 `self.host`
# 作 TLS SNI 与证书校验名。子类只把建连地址钉到已校验 IP（覆盖 `_new_conn`），
# `self.host` 保持真实域名 → Host 头 / SNI / 证书校验 / assert_hostname 不变。
# 通过会话上 mount 一个局部 HTTPAdapter 生效，不改进程全局 DNS、不改默认类。
_PIN_CLASSES = None       # None=未构建; False=不可用; 否则 classes 元组
_MISSING = object()       # mount 原值哨兵：区分"原本无 mount"与"原值为 None"


def _get_pin_classes():
    """惰性构建 urllib3 受控连接类（缓存）。缺依赖/SSL 不支持时返回 None。"""
    global _PIN_CLASSES
    if _PIN_CLASSES is False:
        return None
    if _PIN_CLASSES:
        return _PIN_CLASSES
    try:
        from urllib3.connection import HTTPConnection, HTTPSConnection
        from urllib3.connectionpool import (HTTPConnectionPool,
                                            HTTPSConnectionPool)
        from urllib3.exceptions import NewConnectionError
        from urllib3.util.connection import create_connection

        def _pinned_new_conn(self):
            ips = getattr(self, "_wr_pin_ips", None) or [self._dns_host]
            last = None
            for ip in ips:
                try:
                    return create_connection(
                        (ip, self.port), self.timeout,
                        source_address=self.source_address,
                        socket_options=getattr(self, "socket_options", None))
                except OSError as e:          # 多候选 IP 依序回退
                    last = e
            raise NewConnectionError(
                self, f"无法连接到已校验 IP {ips}: {last}")

        class _PinnedHTTPConnection(HTTPConnection):
            _wr_pin_ips = None

            def __init__(self, *a, **kw):
                self._wr_pin_ips = kw.pop("pinned_ips", None)
                super().__init__(*a, **kw)

            _new_conn = _pinned_new_conn

        class _PinnedHTTPSConnection(HTTPSConnection):
            _wr_pin_ips = None

            def __init__(self, *a, **kw):
                self._wr_pin_ips = kw.pop("pinned_ips", None)
                super().__init__(*a, **kw)

            _new_conn = _pinned_new_conn

        class _PinnedHTTPConnectionPool(HTTPConnectionPool):
            ConnectionCls = _PinnedHTTPConnection

        class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
            ConnectionCls = _PinnedHTTPSConnection

        pools = {"http": _PinnedHTTPConnectionPool,
                 "https": _PinnedHTTPSConnectionPool}
        _PIN_CLASSES = (pools,)
        return _PIN_CLASSES
    except Exception:
        _PIN_CLASSES = False
        return None


def _clone_pinned_poolmanager(src_pm, ips, pools, key_fn):
    """以源 PoolManager 的**池配置**克隆一个受控 poolmanager。

    复制 `connection_pool_kw`（含 ssl_context / cert_reqs / ca_certs / 池大小
    等）与 headers，仅追加 `pinned_ips` 并替换 pool 类 / key_fn —— 保住会话既有
    TLS 配置，绝不以 default 新建后声称保全。**不 close 源 pm**（仍归用户）。
    """
    import urllib3.poolmanager as _pm
    kw = dict(getattr(src_pm, "connection_pool_kw", {}) or {})
    kw.pop("pinned_ips", None)
    kw["pinned_ips"] = tuple(ips)
    # PoolManager 不保留 num_pools 属性，池容量在 pools._maxsize；优先按原容量克隆
    num_pools = getattr(src_pm, "num_pools", None) \
        or getattr(getattr(src_pm, "pools", None), "_maxsize", None) or 10
    pm = _pm.PoolManager(
        num_pools=int(num_pools),
        headers=getattr(src_pm, "headers", None),
        **kw)
    pm.pool_classes_by_scheme = dict(pools)
    pm.key_fn_by_scheme = {"http": key_fn, "https": key_fn}
    return pm


def _make_pinned_adapter(base_adapter, ips, pools):
    """以会话现有适配器**实例**为模板，复制其全部配置后仅换受控 poolmanager。

    复制内容含 TLS/SSLContext/cipher、重试、池参数、代理管理器等（`__dict__`
    整体复制）——cloudscraper 现有 TLS 配置因此得以保留，**不是 default 新建**。

    注入点：`connection_pool_kw['pinned_ips']` → 经 urllib3 `_new_pool` 的
    request_context 进入 `HTTPConnectionPool.__init__(**conn_kw)` → 传入
    `ConnectionCls`（受控连接 `__init__` 弹出该键）。该键**不参与连接池 key**，
    故同时替换 `key_fn_by_scheme`：在构造 PoolKey 前剥掉它——否则 urllib3 的
    `_default_key_normalizer` 会把它前缀成 `key_pinned_ips`，触发
    `PoolKey.__new__() unexpected keyword argument`。
    """
    import urllib3.poolmanager as _pm
    base_cls = type(base_adapter)

    def _pin_key_fn(context):
        ctx = {k: v for k, v in context.items() if k != "pinned_ips"}
        return _pm._default_key_normalizer(_pm.PoolKey, ctx)

    pinned_cls = type("_WrPinnedAdapter", (base_cls,), {})

    adapter = pinned_cls.__new__(pinned_cls)
    adapter.__dict__.update(getattr(base_adapter, "__dict__", {}) or {})
    adapter._wr_pinned_ips = tuple(ips)
    adapter._wr_is_pinned = True
    src_pm = getattr(base_adapter, "poolmanager", None) or _pm.PoolManager()
    adapter.poolmanager = _clone_pinned_poolmanager(src_pm, ips, pools, _pin_key_fn)
    return adapter


def _prepared_url(url):
    """requests 实际发送用的 URL（`PreparedRequest.url`）。

    `get_adapter` 按该串做字面前缀匹配；尾点/IDNA/userinfo 必须依此精确选
    prefix，否则会落到 scheme 级默认 adapter（=未绑定直连）。
    """
    try:
        from requests.models import PreparedRequest
        pr = PreparedRequest()
        pr.prepare(method="GET", url=str(url))
        return pr.url or str(url)
    except Exception:
        return str(url)


def _authority_prefixes(sess, url, scheme, host, port):
    """mount 前缀集合：**精确 authority 前缀**（取自 PreparedRequest.url，保留
    userinfo / 尾点 / 显式端口与 IDNA）＋规范化 host 形式（双保险）。"""
    prefixes = []
    u = _prepared_url(url)
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://([^/?#]*)", u)
    if m and m.group(1):
        prefixes.append(f"{scheme}://{m.group(1)}/")
    if host:
        prefixes.append(f"{scheme}://{host}/")
    default = 443 if scheme == "https" else 80
    if host and port != default:
        prefixes.append(f"{scheme}://{host}:{port}/")
    out = []
    for p in prefixes:
        if p not in out:
            out.append(p)
    return out


def _unmount_pinned(sess):
    """解除受控挂载：**close 旧受控适配器**并**恢复各 prefix 原有 mount**。

    - close：受控适配器持有独立 poolmanager，`close()` 清空连接池、释放
      keep-alive socket——重定向换 host/IP 长跑若只从 `adapters` pop 不 close，
      会泄漏 socket。同一适配器挂多个 prefix 时按 id 去重，只 close 一次。
    - 恢复：挂载时记录了每个 prefix 的原值（用户可能已 mount 自定义 adapter），
      逐一还原，绝不破坏用户原配置；原值缺失/为空则删除该 prefix。
    """
    mounts = list(getattr(sess, "_wr_pin_mounts", None) or [])
    prev = getattr(sess, "_wr_pin_prev", None) or {}
    closed = set()
    for p in mounts:
        cur = None
        try:
            cur = sess.adapters.get(p)
        except Exception:
            pass
        if cur is not None and getattr(cur, "_wr_is_pinned", False) \
                and id(cur) not in closed:
            closed.add(id(cur))
            try:
                cur.close()
            except Exception:
                pass
        orig = prev.get(p, _MISSING)
        try:
            if orig is _MISSING or orig is None:
                sess.adapters.pop(p, None)
            else:
                sess.mount(p, orig)      # 还原用户原 mount（维持前缀排序）
        except Exception:
            pass
    try:
        sess._wr_pin_mounts = []
        sess._wr_pin_prev = {}
        sess._wr_pin_sig = None
    except Exception:
        pass


def _mount_pinned(sess, url, host, port, ips):
    from urllib.parse import urlparse as _up
    pools, = _get_pin_classes()
    scheme = (_up(url).scheme or "http").lower()
    prefixes = _authority_prefixes(sess, url, scheme, host, port)
    sig = (tuple(prefixes), tuple(ips))
    if getattr(sess, "_wr_pin_sig", None) == sig and \
            all(p in sess.adapters for p in prefixes):
        return                      # 同主机同 IP：复用，保住 keep-alive
    _unmount_pinned(sess)           # 复位旧受控挂载（close + 还原用户原配置）
    base = None
    ad = getattr(sess, "adapters", None)
    if ad:
        base = ad.get(scheme + "://")
    if base is None:
        from requests.adapters import HTTPAdapter as _HA
        base = _HA()
    adapter = _make_pinned_adapter(base, ips, pools)
    prev = {}
    for p in prefixes:
        prev[p] = sess.adapters.get(p, _MISSING)
    for p in prefixes:
        sess.mount(p, adapter)
    # 先记录，再校验：fail-closed 失败路径的 _unmount_pinned 才能 close 本适配器
    # 并还原用户原 mount（否则残留受控适配器 → socket 泄漏且默认配置被占位）。
    sess._wr_pin_prev = prev
    sess._wr_pin_mounts = prefixes
    # fail-closed 确证：实际请求 URL 必命中受控适配器，绝不静默退回未绑定连接
    probe = _prepared_url(url)
    try:
        got = sess.get_adapter(probe)
    except Exception:
        got = None
    if got is not adapter:
        _unmount_pinned(sess)
        raise PinUnavailable(
            f"受控适配器未命中请求 URL（authority 规范化不一致）: {probe}")
    sess._wr_pin_sig = sig


def _pin_requests_session(sess, url, proxy=None):
    """requests/cloudscraper 连接的受控 IP 绑定（不改进程全局 DNS）。"""
    if not hasattr(sess, "mount"):
        raise PinUnavailable("未知传输：无 curl 绑定能力也无 requests 适配器")
    hp = _url_host_port(url)
    if hp is None:
        _unmount_pinned(sess)
        return False
    host, port = hp
    if _is_ip_literal(host):
        _unmount_pinned(sess)          # 字面 IP 无解析环节
        return False
    # 实际是否经代理出站（requests 语义，含 NO_PROXY/trust_env/显式代理）
    if proxy or effective_proxy_for_requests(sess, url):
        # 非直连：连接终点是代理，目标 IP 绑定不适用——这是"经代理"，
        # 不是"静默直连"（代理本身另经 proxy_is_safe 校验）。
        _unmount_pinned(sess)
        return False
    ips, reason, status = _resolve_for_pin(host)
    if status == _PIN_PRIVATE:
        _unmount_pinned(sess)
        raise SSRFBlocked(f"连接层校验拒绝: {reason} ({host})")
    if status == _PIN_UNRESOLVED:
        _unmount_pinned(sess)
        raise DNSResolveUnavailable(
            f"DNS 解析失败，无法建立 IP 绑定连接: {host}")
    if status != _PIN_OK or not ips:
        _unmount_pinned(sess)
        return False
    if _get_pin_classes() is None:
        _unmount_pinned(sess)
        raise PinUnavailable("urllib3 受控绑定不可用（缺少 urllib3/SSL 支持）")
    try:
        _mount_pinned(sess, url, host, port, ips)
    except PinUnavailable:
        raise
    except Exception as e:
        _unmount_pinned(sess)
        raise PinUnavailable(f"受控绑定挂载失败: {e}") from e
    return True


def pin_curl_session(sess, url, proxy=None):
    """把 url 主机的**已校验公网 IP**绑定到实际建连（curl 或 requests）。

    连接层绑定：校验与建连使用同一份解析结果——校验通过后 DNS 被改写为内网时，
    本次请求仍发往已校验的公网 IP，消除重绑定窗口。Host 头 / TLS SNI / 证书
    校验仍按域名（两大类传输均不受影响）。

    **代理不再导致跳过**（修复原 `env_proxy_present` 只看"环境里有代理"就跳过，
    而 NO_PROXY 命中 / trust_env=False 实际直连 → 绑定被跳过 = SSRF 缺口）：

    - curl 路径：**无论是否配置代理都写入目标 RESOLVE**。libcurl 经代理建连时
      该条目不被使用（代理自行解析，写入无害），直连时正是所需绑定；且不改动
      `CurlOpt.PROXY`，专用代理协议仍走代理、绝不因此转直连（语义保留）。
    - requests/cloudscraper 路径：按 requests 真实语义判定**实际出站**——
      `effective_proxy_for_requests` 综合 URL scheme / NO_PROXY / trust_env /
      显式代理；**直连**则用同会话局部挂载的 urllib3 受控连接把建连地址钉到
      已校验 IP（SNI/证书校验仍按域名）；**经代理**则不做目标 IP 绑定（连接
      终点是代理，非直连）。

    失败语义与校验对齐：
    - 解析到私网 → 抛 SSRFBlocked（禁止建连，不静默放行）
    - 解析失败（DNS 不可用）→ 抛 DNSResolveUnavailable（可重试，不硬封源）
    - 非 curl 且无法建立受控绑定 → 抛 PinUnavailable（可重试，**绝不静默直连**）
    - 显式 WR_SSRF_SKIP_DNS → 返回 False（保持跳过语义）
    - 字面 IP → 返回 False（无解析环节）

    返回 True 表示绑定在**请求级**已生效；解除时真正清除 libcurl DNS 缓存
    （见 _clear_resolve），共享会话不串味（会话由调用方独占）。
    """
    opts = getattr(sess, "curl_options", None)
    if not isinstance(opts, dict):
        # 非 curl 回退传输：实现受控绑定或显式抛可重试错误
        return _pin_requests_session(sess, url, proxy=proxy)
    # 保留尾点：libcurl 按 URL 字面 host 匹配 RESOLVE，去尾点会绑到另一 key
    hp = _url_host_port(url, keep_trailing_dot=True)
    if hp is None:
        _clear_resolve(sess, opts)
        return False
    host, port = hp
    if _is_ip_literal(host):
        _clear_resolve(sess, opts)
        return False
    ips, reason, status = _resolve_for_pin(host)
    if status == _PIN_PRIVATE:
        _clear_resolve(sess, opts)
        raise SSRFBlocked(f"连接层校验拒绝: {reason} ({host})")
    if status == _PIN_UNRESOLVED:
        _clear_resolve(sess, opts)
        raise DNSResolveUnavailable(
            f"DNS 解析失败，无法建立 IP 绑定连接: {host}")
    if status == _PIN_OK and ips:
        try:
            _bind_resolve(sess, opts, host, port, ips)
            return True
        except Exception:
            _clear_resolve(sess, opts)
            return False
    # _PIN_SKIP：显式关闭解析，保持既有跳过语义
    _clear_resolve(sess, opts)
    return False


def url_is_public_resolved(url):
    """在字面校验基础上解析 DNS，确认所有解析结果均为公网地址。

    封堵 http://127.0.0.1.nip.io/ 这类"公网域名解析到内网"的绕过。
    返回 (ok: bool, reason: str)。
    R74: 解析结果短缓存(120s)——漫画阅读逐图调用, 系统 DNS 缓存
    失效时每图冷解析 0.5s 会拖慢整章加载; 缓存到期强制重解析保持
    SSRF 防御时效。
    2026-09-10: 与 resolve_public_ips 共用同一份缓存/解析结果，连接层
    可用同一结果绑定建连地址（校验与建连一致，防 DNS 重绑定）。
    """
    if not url_is_public(url):
        return False, "目标不是公网 http/https 地址"
    if _RESOLVE_DISABLED:
        return True, ""
    host = normalize_host(urlparse(str(url)).hostname)
    try:
        ipaddress.ip_address(host)
        return True, ""            # 字面 IP：上面已校验过
    except ValueError:
        pass
    _ips, reason = resolve_public_ips(host)
    if reason:
        return False, reason
    return True, ""


def safe_target_url(url, what="URL"):
    """校验用户提供的代拉目标 URL；不合法 → 抛 ValueError（调用方转 400/404）"""
    if not url or not str(url).startswith(("http://", "https://")):
        raise ValueError(f"{what}不合法")
    if not url_is_public(str(url)):
        raise ValueError(f"{what}目标被拒绝（仅允许公网地址）")
    return str(url).strip()


def proxy_is_safe(proxy):
    """校验代理地址：书源 proxy 字段用户可控，指向内网即可做端口探测。

    代理模式下真实 TCP 连接建立到代理地址，target URL 的校验形同虚设，
    故代理本身必须是公网 http/https/socks 地址。
    """
    if not proxy:
        return True
    vals = list(proxy.values()) if isinstance(proxy, dict) else [proxy]
    for v in vals:
        v = str(v or "").strip()
        if not v:
            continue
        # 补 scheme 便于 urlparse 取 hostname（socks5://、http:// 等均可）
        cand = v if "://" in v else "http://" + v
        try:
            host = (urlparse(cand).hostname or "").lower().rstrip(".")
        except Exception:
            return False
        if not host or host == "localhost" or host.endswith(".localhost"):
            return False
        try:
            ipaddress.ip_address(host)
        except ValueError:
            continue                      # 代理域名：放行
        if not ip_is_public(host):
            return False
    return True
