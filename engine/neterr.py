"""连接失败的归因：把"本机没有路由""目标不可达""解析失败"分开，不互相冒充。

背景（2026-09-15 评审意见 P1-网络归因）：
  · 断网时**每个源**都会抛异常，旧代码把所有异常统一写成
    "搜索失败（源站异常或限流），请稍后重试"——用户手机没信号时会得到完全
    错误的结论（以为是源站坏了，反复重试）；
  · 但反向的过度归因同样错误：**单个源**报"目标不可达"不能升级成
    "你手机没网"。目标地址、某条线路、某个域名、某个地址族的局部不可达，
    与设备整体离线是两件事。

因此本模块把失败分成三类，各有各的说法：
  · unreachable      本机没有可用路由（ENETUNREACH / "Network is unreachable"）
                     —— 操作系统说"连不出去"，可以如实说成本机网络问题；
  · host-unreachable 目标地址不可达（EHOSTUNREACH / "No route to host"）
                     —— 只说这个目标/线路有问题，**不说**本机没网；
  · dns              域名解析失败 —— 可能是本机 DNS，也可能是该域名本身。

"整轮都没有结果"时是否可以说"本机当前没有网络"，只认**设备级证据**：
  1. 客户端给的设备联网状态（Android ConnectivityManager，见 NetState.kt）；或
  2. 本模块的直连探测 local_network_down()：两个公网 IP 目标**都**报
     "没有路由"才算数。
逐源错误文字**不**作为设备级证据（这正是评审要求纠正的地方）。

探测（local_network_down）的用途与边界：
  · 用途：只用于**措辞**（说清是本机没网还是源站问题）与**是否继续干等**
    （提前结束等待）；它**不**决定要不要搜索，也不是业务可用性的前置条件——
    探测不可用/被禁用时，搜索照常按原来的期限跑完。
  · 方式：对两个公网 DNS 服务 IP（223.5.5.5、1.1.1.1）各做一次 TCP 连接，
    连上立即关闭；不发送任何业务数据，不是第三方业务后端，也不上报任何信息。
  · 关闭：设 WR_DISABLE_NET_PROBE=1（测试环境 WR_TEST=1 亦视为关闭）；
    目标可用 WR_NET_PROBE_TARGETS="host:port,host:port" 覆盖。
"""
from __future__ import annotations

import errno as _errno
import os
import socket

# 本机没有可用路由（设备级，**不是**源站问题）
UNREACHABLE_MARKERS = (
    "network is unreachable",
    "network down",
)
# 目标地址不可达（目标/线路级：只说这个目标有问题）
HOST_UNREACHABLE_MARKERS = (
    "no route to host",
    "destination host unreachable",
)
# 域名解析失败：可能是本机网络/DNS 不可用，也可能是该域名本身不可用
DNS_MARKERS = (
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname provided",
    "no address associated with hostname",
    "getaddrinfo failed",
)

REASON_UNREACHABLE = "本机没有可用路由（请检查 Wi-Fi / 移动数据）"
REASON_HOST_UNREACHABLE = "目标地址不可达（可能是源站或线路问题）"
REASON_DNS = "域名解析失败（可能是本机 DNS，也可能是该域名不可用）"

_LOCAL_KINDS = ("unreachable",)
_ALL_KINDS = ("unreachable", "host-unreachable", "dns")

_PROBE_TRUTHY = ("1", "true", "yes", "on")
_PROBE_TARGETS = (("223.5.5.5", 443), ("1.1.1.1", 443))


def _texts(exc, depth=0):
    """异常链上所有可读文本（含 args 片段），用于标记匹配"""
    if exc is None or depth > 6:
        return
    try:
        yield type(exc).__name__ + ": " + str(exc)
    except Exception:
        return
    for a in (getattr(exc, "args", ()) or ()):
        if isinstance(a, BaseException):
            yield from _texts(a, depth + 1)
        elif isinstance(a, (tuple, list)):
            for x in a:
                if isinstance(x, BaseException):
                    yield from _texts(x, depth + 1)
                else:
                    yield str(x)
        else:
            yield str(a)
    for attr in ("reason", "error", "__cause__", "__context__"):
        v = getattr(exc, attr, None)
        if isinstance(v, BaseException):
            yield from _texts(v, depth + 1)
        elif isinstance(v, str) and v:
            yield v


def _errnos(exc, depth=0):
    if exc is None or depth > 6:
        return
    e = getattr(exc, "errno", None)
    if isinstance(e, int):
        yield e
    for attr in ("reason", "error", "__cause__", "__context__"):
        v = getattr(exc, attr, None)
        if isinstance(v, BaseException):
            yield from _errnos(v, depth + 1)


def classify(exc):
    """异常 → '' | 'unreachable' | 'host-unreachable' | 'dns'"""
    if exc is None:
        return ""
    _net_unreach = _errno.ENETUNREACH
    _host_unreach = getattr(_errno, "EHOSTUNREACH", 113)
    for e in _errnos(exc):
        if e == _net_unreach:
            return "unreachable"
        if e == _host_unreach:
            return "host-unreachable"
    try:
        blob = " | ".join(_texts(exc)).lower()
    except Exception:
        return ""
    for m in UNREACHABLE_MARKERS:
        if m in blob:
            return "unreachable"
    for m in HOST_UNREACHABLE_MARKERS:
        if m in blob:
            return "host-unreachable"
    for m in DNS_MARKERS:
        if m in blob:
            return "dns"
    return ""


def is_local_kind(kind):
    """只有"本机没有可用路由"才算本机网络问题（目标不可达不算）"""
    return kind in _LOCAL_KINDS


def reason_for(kind):
    if kind == "unreachable":
        return REASON_UNREACHABLE
    if kind == "host-unreachable":
        return REASON_HOST_UNREACHABLE
    if kind == "dns":
        return REASON_DNS
    return ""


def _proxy_hint():
    """当前是否经代理出站 → 给失败文案补一个**极短**的标记（无代理返回空串）。

    两件事必须同时成立，缺一个就会出事故：

    1. **要说明**：0.74.1 那次事故里用户配的代理关掉后成了死地址，每个源都失败，
       界面上完全看不出"请求正在经代理"，只能靠猜；
    2. **要极短**：这是**逐源**文案，几十个源失败时会拼成一整屏。
       0.74.2 我写成了一整句（"当前经代理 … 可在设置里清除或更换"），
       用户实测反馈"整个屏幕都是报错信息，看不到搜索结果"——回归是我引入的。
       现在只留一个括号标记，完整指引放在「设置 → 网络 → 代理」页与诊断报告里。
    """
    try:
        from engine import netproxy as _np
        val = _np.current_proxy()
        if not val:
            return ""
        return "（经代理 %s）" % _np.redact_proxy(val)
    except Exception:                                            # noqa: BLE001
        return ""


def failure_text(name, exc, fallback):
    """逐源失败说明 → (文案, 类别)。只影响**这个源**的说法，不做整机结论"""
    kind = classify(exc)
    _hint = _proxy_hint()
    if kind:
        return f"{name} 搜索失败（{reason_for(kind)}）{_hint}", kind
    return (fallback + _hint) if _hint else fallback, ""


def looks_local(reason_text):
    """这段失败说明是不是"本机网络"口径（仅用于措辞判断，不作为整机断网证据）"""
    return bool(reason_text) and REASON_UNREACHABLE in reason_text


# ── 设备级证据 ────────────────────────────────────────────────────────────

def probe_enabled():
    if os.environ.get("WR_DISABLE_NET_PROBE", "").lower() in _PROBE_TRUTHY:
        return False
    # 测试环境默认不联网（桌面回归必须离线、确定性）；确需探测的用例显式打开
    if os.environ.get("WR_TEST", "").lower() in _PROBE_TRUTHY \
            and os.environ.get("WR_NET_PROBE_FORCE", "").lower() not in _PROBE_TRUTHY:
        return False
    return True


def _targets():
    raw = (os.environ.get("WR_NET_PROBE_TARGETS") or "").strip()
    if not raw:
        return _PROBE_TARGETS
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        host, _, port = part.partition(":")
        try:
            out.append((host, int(port or 443)))
        except ValueError:
            continue
    return tuple(out) or _PROBE_TARGETS


def local_network_down(timeout=1.0, targets=None):
    """本机是否**确定**连不出去——设备级判据（只认"没有路由"）。

    只在失败路径上调用（一轮搜索/校验已经没有任何结果时）：
      1. 有任一目标能连上 → 有网；
      2. 全部目标都失败且**每一个**都是 ENETUNREACH（"没有路由"）→ 没网；
      3. 其余（超时、连接被拒、部分可达、"目标不可达"）→ 拿不准，False。
    宁可不解释，也不给错解释。禁用探测（WR_DISABLE_NET_PROBE/WR_TEST）时恒为 False，
    搜索仍按原期限正常跑完——探测不是业务可用性的前置条件。
    """
    if not probe_enabled():
        return False
    targets = targets or _targets()
    unreachable = 0
    for host, port in targets:
        try:
            s = socket.create_connection((host, port), timeout)
            s.close()
            return False            # 能连出去 → 有网
        except OSError as e:
            if classify(e) == "unreachable":
                unreachable += 1
            else:
                return False        # 拿不准 → 不下结论
        except Exception:
            return False
    return unreachable > 0 and unreachable == len(targets)


def round_is_offline(has_results, error_texts=None, device_offline=None):
    """整轮搜索没有任何结果时，本机是否**确定**连不出去。

    证据只认设备级：客户端给的联网状态（device_offline=True）或直连探测。
    error_texts（逐源失败文字）只用于判断"这一轮确实什么都没有"，
    **不**作为整机断网的证据——局部不可达不能冒充全局结论。
    """
    if has_results:
        return False
    if error_texts is not None and not error_texts:
        return False
    if device_offline:
        return True
    return local_network_down()
