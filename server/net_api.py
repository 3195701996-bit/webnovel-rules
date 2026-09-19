# -*- coding: utf-8 -*-
"""出站代理设置接口（0.73.0）。

  GET  /api/net/proxy        当前生效代理 + 它是从哪来的（没有代理就明说直连）
  POST /api/net/proxy        设置/清除代理（写 App 私有目录配置文件 + 立即生效）
  POST /api/net/proxy/test   拿候选代理去探"用户实际要用的那几个域"

为什么单独一个蓝图：代理是**网络路径**设置，与书源/漫画取数无关；放进
novel_api/manga_api 只会让那两个模块继续膨胀。

三条不能被违反的语义（都有测试）：
  1. 非法代理值**必须显式报错**，绝不静默回落直连——用户以为配上了其实没配，
     是最难排查的一类故障；
  2. 返回体里 `effective` 是**真实生效值**（环境变量优先于配置文件），不是
     "用户填了什么"；
  3. 测速如实分列 直连/经代理 两列：代理没让目标域可达时必须看得出来。
"""
import os
import time

from flask import Blueprint, jsonify, request

from engine import netproxy
from engine.netproxy import redact_proxy       # 报告/日志统一走同一份脱敏实现

bp = Blueprint("net", __name__)

# 探针目标：不选通用公网地址（如 gstatic），因为"代理通但目标站不通"对用户零价值。
# 这三条对应真机诊断里"直连被重置"的那几个域。
PROBE_TARGETS = (
    ("禁漫图片CDN", "https://cdn-msp.jmapinodeudzn.net/"),
    ("拷贝漫画API", "https://api.copy4000.com/api/v3/system/network2?platform=3"),
    ("包子漫画", "https://www.baozimh.com/"),
    ("nhentai", "https://nhentai.net/api/v2/"),
)


def _is_loopback():
    """请求是否来自本机（手机端引擎固定绑 127.0.0.1，天然满足）。"""
    ra = (request.remote_addr or "").strip()
    try:
        import ipaddress
        return ipaddress.ip_address(ra).is_loopback
    except ValueError:
        return False


def _write_allowed():
    """写操作只认本机来源。

    代理会改变**整个引擎**的出站路径（等于把抓取流量交给某个中间人），
    因此不允许局域网/公网客户端改它：手机端引擎固定绑 127.0.0.1，天然满足；
    桌面端本机浏览器/curl 也满足。确实需要远程配置时显式开 `WR_ALLOW_PROXY_API=1`
    （不默认开放——这与"不影响现有生产服务/最小权限"的既有纪律一致）。
    """
    if (os.environ.get("WR_ALLOW_PROXY_API") or "").strip() == "1":
        return True, ""
    if _is_loopback():
        return True, ""
    ra = (request.remote_addr or "").strip()
    return False, ("代理设置只允许从本机发起（当前来源：%s）；"
                   "确需远程配置请显式设置 WR_ALLOW_PROXY_API=1" % (ra or "?"))


def _novel_proxy_state():
    """**小说侧**代理状态（指南 P1-2：与漫画侧 `netproxy` 是**两套实现**，必须分开显示）。

    优先级（`engine/fetcher.py`）：**书源自带的 `proxy` 字段 > 环境变量 `WR_PROXY` >
    代理池**（`data/proxies.txt`，且**只对"直连失败过"的域名**轮换，并带健康/延迟记忆、
    进程中还有 `_proxy_good` 快代理优先表）。

    所以小说侧与漫画侧不是同一个开关：漫画侧只有一个显式代理（`/api/net/proxy` 设的那个），
    小说侧可能来自环境变量或池子，用户在 App 里**看不到就无从自查** ✗ —— 这里如实给出。
    """
    from engine.config import DATA_DIR
    from engine.fetcher import Fetcher
    env_p = (os.environ.get("WR_PROXY") or "").strip()
    pool = []
    try:
        with open(os.path.join(DATA_DIR, "proxies.txt"), encoding="utf-8") as f:
            pool = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    except OSError:
        pool = []
    try:
        good = list(getattr(Fetcher, "_proxy_good", []) or [])
    except Exception:                                            # noqa: BLE001
        good = []
    show = _is_loopback()
    return {
        "env_proxy": env_p if show else redact_proxy(env_p),
        "pool_size": len(pool),
        "good_count": len(good),
        "good": [g if show else redact_proxy(g) for g in good[:12]],
        "mode": ("env" if env_p else ("pool" if pool else "direct")),
        "note": ("书源自带 proxy 字段时以它为准；池只对**直连失败过**的域名启用，"
                 "不是全局开关。已下过的小说正文不需要代理即可离线阅读。"),
    }


def _state():
    """代理状态。

    代理串里的用户名密码只回显给**本机**调用方（手机 App 走 127.0.0.1，用户要能
    看到并改回自己填的值）；其它来源只拿到脱敏后的样子（诊断报告同理）。
    """
    val = netproxy.current_proxy()
    src = netproxy.source()
    _h = netproxy.health()
    return {
        "proxy": val if _is_loopback() else redact_proxy(val),
        "proxied": bool(val),
        "source": src,
        # 配了但连不上 → 本次直连；界面必须说清，否则用户以为"配好了却没效果"
        "health": (_h if _is_loopback()
                   else dict(_h, configured=redact_proxy(_h.get("configured") or ""))),
        "config_path": netproxy.config_path(),
        "supported": ["http://", "https://", "socks5://", "socks5h://"],
        "probe_targets": [name for name, _ in PROBE_TARGETS],
        # 小说侧（另一套实现）：与上面 4 个字段**不是同一个开关**，必须分开看
        "novel": _novel_proxy_state(),
    }


def _probe_one(url, proxy, timeout):
    """单目标探测：只发一次 GET，不跟随重定向。返回 (ok, http, ms, note)。"""
    import requests
    t0 = time.time()
    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    try:
        r = requests.get(url, timeout=timeout, proxies=proxies,
                         allow_redirects=False,
                         headers={"User-Agent": "Mozilla/5.0 (webnovel-rules probe)"})
        return (True, r.status_code, int((time.time() - t0) * 1000), "")
    except Exception as e:                                  # noqa: BLE001
        return (False, 0, int((time.time() - t0) * 1000),
                "%s: %s" % (type(e).__name__, str(e)[:80]))


def probe(proxy=None, timeout=6, targets=None, workers=4):
    """并发探测各目标域（直连 + 经代理两列，代理为空时只测直连）。

    "可达"的判定：拿到任何 HTTP 响应即算可达（403/404 也说明网络通了）——
    我们要回答的是"路通不通"，不是"站点业务是否正常"，后者是逐源实测的事。
    """
    import concurrent.futures as _cf
    targets = tuple(targets or PROBE_TARGETS)
    val = netproxy._valid(proxy) if proxy else netproxy.current_proxy()
    modes = [("direct", None)]
    if val:
        modes.append(("proxied", val))
    rows = []
    with _cf.ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futs = {}
        for mode, p in modes:
            for name, url in targets:
                futs[ex.submit(_probe_one, url, p, timeout)] = (mode, name, url)
        got = {}
        for f in _cf.as_completed(futs):
            mode, name, url = futs[f]
            try:
                got[(mode, name)] = f.result()
            except Exception as e:                          # noqa: BLE001
                got[(mode, name)] = (False, 0, 0, "%s" % type(e).__name__)
    for mode, _p in modes:
        for name, url in targets:
            ok, code, ms, note = got.get((mode, name), (False, 0, 0, "未执行"))
            rows.append({"mode": mode, "target": name, "url": url, "ok": ok,
                         "http": code, "ms": ms, "note": note})
    return {"proxy": val, "modes": [m for m, _ in modes], "rows": rows,
            "timeout": timeout}


def probe_lines(data=None):
    """把探测结果排成人类可读文本（App 直接显示，不用再解析 JSON）。"""
    d = data if isinstance(data, dict) else probe()
    lines = ["代理连通性探测（每目标超时 %ss；拿到任何 HTTP 响应即算路通）"
             % d.get("timeout")]
    p = redact_proxy(d.get("proxy"))
    lines.append("  经代理：%s" % (p or "（未配置，只测直连）"))
    for mode in d.get("modes") or []:
        title = "直连" if mode == "direct" else "经代理"
        lines.append("  [%s]" % title)
        for row in d.get("rows") or []:
            if row.get("mode") != mode:
                continue
            if row.get("ok"):
                lines.append("    %-12s 可达 HTTP %s（%sms）"
                             % (row.get("target"), row.get("http"), row.get("ms")))
            else:
                lines.append("    %-12s 不可达（%sms）%s"
                             % (row.get("target"), row.get("ms"), row.get("note")))
    if len(d.get("modes") or []) == 2:
        def _reach(mode):
            return sum(1 for r in d["rows"] if r.get("mode") == mode and r.get("ok"))
        a, b = _reach("direct"), _reach("proxied")
        if b > a:
            lines.append("  结论：经代理可达 %d 个 / 直连 %d 个 —— 代理确实扩展了可达域。" % (b, a))
        elif b == a:
            lines.append("  结论：两条路径可达数相同（各 %d 个）—— 代理没带来额外可达域，"
                         "配它只会在慢链路上多一跳。" % b)
        else:
            lines.append("  结论：经代理可达 %d 个 / 直连 %d 个 —— 代理反而更差，"
                         "建议清掉代理。" % (b, a))
    return lines


@bp.route("/api/net/proxy")
def api_net_proxy_state():
    return jsonify({"ok": True, "data": _state()})


@bp.route("/api/net/proxy", methods=["POST"])
def api_net_proxy_set():
    allowed, why = _write_allowed()
    if not allowed:
        return jsonify({"ok": False, "error": why, "data": _state()}), 403
    data = request.get_json(silent=True) or {}
    raw = data.get("proxy", None)
    if raw is None:
        raw = request.args.get("proxy", "")
    raw = str(raw or "").strip()
    # 显式区分"想清空"与"填错了"：空/ none / off 是清空；其它非法值报 400
    clear = raw.lower() in ("", "none", "off", "direct", "0")
    if not clear and not netproxy._valid(raw):
        return jsonify({
            "ok": False,
            "error": "代理地址无法识别：%s" % redact_proxy(raw),
            "hint": "支持的写法：http://主机:端口、socks5://主机:端口"
                    "（可带用户名密码）；清空请传空字符串。",
            "data": _state(),
        }), 400
    val = netproxy.set_proxy(raw, persist=True)
    # 这里要**主动探一次**（不能只读缓存）：用户刚填完地址就想知道"到底通不通"，
    # 而 health() 只读不探。真实生效值以这次判定为准。
    _effective, _h = netproxy.healthy_proxy(val)
    if val and not _h.get("ok"):
        _note = ("已保存，但这个地址**当前连不上**（%s）——已临时直连，"
                 "地址仍保留；请确认代理已开启、且手机能访问它。"
                 % (_h.get("err") or "连接失败"))
    else:
        _note = ("已改为直连" if not val else "已保存并立即生效") + \
                "；各通道会话已重置（下次请求就走新路径）。"
    return jsonify({
        "ok": True,
        "data": _state(),
        "note": _note,
    })


@bp.route("/api/net/proxy/test", methods=["POST"])
def api_net_proxy_test():
    # 探测会从本机向外发请求（等于拿本机当跳板），同样只允许本机发起
    allowed, why = _write_allowed()
    if not allowed:
        return jsonify({"ok": False, "error": why}), 403
    data = request.get_json(silent=True) or {}
    raw = data.get("proxy", None)
    timeout = data.get("timeout", 6)
    try:
        timeout = max(2, min(20, int(timeout)))
    except Exception:                                       # noqa: BLE001
        timeout = 6
    if raw is None:
        res = probe(timeout=timeout)                 # 用当前生效代理
    else:
        candidate = netproxy._valid(raw)
        if str(raw or "").strip() and not candidate:
            return jsonify({"ok": False,
                            "error": "候选代理地址无法识别：%s" % redact_proxy(raw)}), 400
        res = probe(proxy=candidate, timeout=timeout)
    return jsonify({"ok": True, "data": res, "text": "\n".join(probe_lines(res))})
