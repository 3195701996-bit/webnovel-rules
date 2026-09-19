# -*- coding: utf-8 -*-
"""诊断记录与脱敏报告（方向基线 §7.1：给用户一次可导出的脱敏诊断）

要回答的问题是："漫画在用户手机上完全不可用——卡在哪一层？"
分层口径（与 §7.1 一致）：入口/路由 → 本机连接契约 → 源注册与配置 →
搜索或分类 → 详情/目录 → 章节图片地址 → 图片请求/解码 → 本地文件 → 进度。

因此报告只收集**这几层各自的状态与失败证据**，并且必须脱敏：
  - 不带 token / Cookie / 鉴权头 / 会话值；
  - URL **去掉查询串**（`?q=...` 一类的参数可能含私人搜索词）；
  - 实例 id 只留前 8 位；不写任何源站凭据。
"""
import os
import threading
import time
from collections import deque
from urllib.parse import urlsplit

# 失败请求与异常的有界环形缓冲：只要足够定位，不无限增长
_MAX_EVENTS = 80
_lock = threading.Lock()
_failed = deque(maxlen=_MAX_EVENTS)      # (ts, method, path(无查询串), status, ms)
_errors = deque(maxlen=_MAX_EVENTS)      # (ts, where, kind, msg(截断))

# 这些前缀是高频图片/静态请求：失败也记，但单独截断，避免把报告淹掉
_NOISY_SUFFIX = ("/proxy", "/cover")


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def scrub_path(path):
    """去掉查询串与片段：搜索词、参数可能含私人信息"""
    try:
        p = urlsplit(str(path or ""))
        return (p.path or "/")[:200]
    except Exception:
        return "/"


def scrub_text(text, limit=200):
    """抹掉可能的凭据字样，只留可读错误"""
    s = str(text or "")
    for key in ("token", "password", "passwd", "cookie", "authorization",
                "secret", "session"):
        low = s.lower()
        idx = low.find(key)
        while idx >= 0:
            end = s.find(" ", idx)
            end = len(s) if end < 0 else end
            s = s[:idx] + key + "=<removed>" + s[end:]
            low = s.lower()
            idx = low.find(key, idx + len(key) + 10)
    return s[:limit]


def record_request(method, path, status, ms):
    """记录**失败**请求（4xx/5xx）。静态资源与图片路径不进报告列表。"""
    try:
        st = int(status)
    except Exception:
        return
    if st < 400:
        return
    # 路径也要抹凭据字样：/api/sources/token%3Dabc/toggle 这种会被 Flask 解码成
    # 带 token=... 的路径，直接写进报告就等于把凭据抄给了接收方
    p = scrub_text(scrub_path(path), 200)
    if p.startswith("/static/"):
        return
    with _lock:
        _failed.append((_now(), str(method)[:6], p, st, int(ms or 0)))


def record_error(where, exc, msg=None):
    with _lock:
        _errors.append((_now(), str(where)[:60], type(exc).__name__,
                        scrub_text(msg if msg is not None else exc, 160)))


def events():
    with _lock:
        return {"failed_requests": list(_failed)[-30:], "errors": list(_errors)[-20:]}


def reset():
    with _lock:
        _failed.clear()
        _errors.clear()


# ── 报告正文 ────────────────────────────────────────────────

def _caps_lines():
    try:
        from server import capabilities as cap
        data = cap.probe()
    except Exception as e:
        record_error("capabilities.probe", e)
        return ["（能力探测失败：%s）" % scrub_text(e, 120)]
    out = []
    for name, info in sorted((data or {}).items()):
        ok = info.get("available") if isinstance(info, dict) else bool(info)
        detail = (info or {}).get("detail", "") if isinstance(info, dict) else ""
        out.append("  %-18s %s%s" % (name, "可用" if ok else "不可用",
                                     ("　" + scrub_text(detail, 100)) if detail else ""))
    return out or ["（无）"]


def _manga_lines():
    try:
        from engine.manga.verify import results_payload
        from engine.manga.manager import list_adapters
        from server import capabilities as cap
    except Exception as e:
        record_error("manga.diag.import", e)
        return ["（漫画诊断不可用：%s）" % scrub_text(e, 120)]
    out = []
    try:
        adapters = list_adapters()
    except Exception as e:
        record_error("manga.list_adapters", e)
        adapters = []
    try:
        payload = results_payload() or {}
        known = {it.get("key"): it for it in (payload.get("items") or [])}
    except Exception as e:
        record_error("manga.results_payload", e)
        known = {}
    for a in adapters:
        key = a.get("key") or "?"
        st = "未知"
        try:
            st = (cap.source_status(key) or {}).get("status") or "未知"
        except Exception:
            pass
        rec = known.get(key) or {}
        out.append("  · %s（%s）依赖判定=%s 实测=%s" % (
            a.get("name") or key, key, st, rec.get("status") or "未实测"))
        if rec.get("reason"):
            out.append("      原因：%s" % scrub_text(rec.get("reason"), 140))
        for stage, info in (rec.get("stages") or {}).items():
            if not isinstance(info, dict):
                continue
            out.append("      %-8s ok=%s ms=%s %s" % (
                stage, info.get("ok"), info.get("ms"),
                scrub_text(info.get("detail") or "", 120)))
    return out or ["（未注册任何漫画适配器）"]


def _source_lines():
    try:
        from engine.source_mgr import load_all
        from engine import source_verify as sv
    except Exception as e:
        record_error("sources.diag.import", e)
        return ["（书源诊断不可用：%s）" % scrub_text(e, 120)]
    try:
        allsrc = load_all() or []
    except Exception as e:
        record_error("sources.load_all", e)
        return ["（读取书源失败：%s）" % scrub_text(e, 120)]
    uids = {}
    for s in allsrc:
        u = s.get("uid") or "?"
        uids.setdefault(u, []).append(bool(s.get("enabled", True)))
    enabled = sum(1 for v in uids.values() if any(v))
    dup = {u: len(v) for u, v in uids.items() if len(v) > 1}
    out = ["  文件 %d 个 / 唯一 uid %d 个 / 启用 %d 个" % (len(allsrc), len(uids), enabled)]
    if dup:
        out.append("  ⚠ uid 重复（同 uid 多文件）：%s" %
                   "、".join("%s×%d" % (k, v) for k, v in list(dup.items())[:8]))
    try:
        items = {it.get("uid"): it for it in (sv.results_payload().get("items") or [])}
    except Exception:
        items = {}
    tally = {}
    for u in uids:
        st = (items.get(u) or {}).get("status") or "未实测"
        tally[st] = tally.get(st, 0) + 1
    out.append("  功能验证：%s" % "、".join("%s %d" % (k, v) for k, v in sorted(tally.items())))
    return out


def _storage_lines():
    out = []
    try:
        from engine.config import (DATA_DIR, SOURCES_DIR, MANGA_DIR,
                                   MANGA_DOWNLOADS_DIR, BOOKS_DIR)
    except Exception as e:
        record_error("config.import", e)
        return ["（读取目录配置失败：%s）" % scrub_text(e, 120)]

    def _size(p, cap_files=20000):
        total = 0
        n = 0
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                    n += 1
                except OSError:
                    pass
                if n >= cap_files:
                    return total, n, True
        return total, n, False

    for label, p in (("数据目录", DATA_DIR), ("书源目录", SOURCES_DIR),
                     ("小说书库", BOOKS_DIR), ("漫画目录", MANGA_DIR),
                     ("漫画下载", MANGA_DOWNLOADS_DIR)):
        try:
            if not os.path.isdir(p):
                out.append("  %-8s 不存在" % label)
                continue
            size, n, capped = _size(p)
            out.append("  %-8s %s / %d 个文件%s" % (
                label, _human(size), n, "（已达统计上限）" if capped else ""))
        except Exception as e:
            out.append("  %-8s 统计失败：%s" % (label, scrub_text(e, 80)))
    try:
        st = os.statvfs(DATA_DIR)
        out.append("  可用空间 %.1f GB / 总计 %.1f GB" % (
            st.f_bavail * st.f_frsize / 1e9, st.f_blocks * st.f_frsize / 1e9))
    except Exception:
        pass
    return out


def _human(n):
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return "%.1f %s" % (n / div, unit)
    return "%d B" % n



# ── 阅读链路分段测速（0.71.0）────────────────────────────────────────────
# 为什么放进诊断：提速与"源站风控"这类问题**必须在目标机上量**才有结论。
# 用户反馈"禁漫很慢"时，我们要的是"哪一段慢、走的是哪条传输"，
# 而不是靠猜。这里做一次**有界、只读**的测量（每步超时 10s，失败如实记录）。
def measure_latency(source="jm", comic_id="", timeout=10):
    """分段测量某个漫画源的取数耗时（搜索/详情/章节/首图）。

    返回 dict：transport（curl_cffi|requests）、各段 {ok, ms, detail}、
    以及缓存/预热参数。**不写盘、不改状态**，失败一律如实记录不吞。
    """
    import time as _t
    out = {"source": source, "transport": "unknown", "steps": {}}

    def _step(name, fn):
        t0 = _t.time()
        try:
            detail = fn()
            out["steps"][name] = {"ok": True, "ms": int((_t.time() - t0) * 1000),
                                  "detail": scrub_text(detail or "", 120)}
        except Exception as e:
            out["steps"][name] = {"ok": False, "ms": int((_t.time() - t0) * 1000),
                                  "detail": "%s: %s" % (type(e).__name__,
                                                        scrub_text(e, 100))}
            record_error("latency." + name, e)

    try:
        from server.state import _manga_read_adapter, _manga_adapter
    except Exception as e:
        record_error("latency.import", e)
        return out

    ad = None
    try:
        ad = _manga_read_adapter(source) or _manga_adapter(source)
    except Exception as e:
        record_error("latency.adapter", e)
    if ad is None:
        out["transport"] = "no-adapter"
        return out
    try:
        from engine.manga.downloader import _curl_engine_available
        out["transport"] = "curl_cffi" if _curl_engine_available() else "requests"
    except Exception:
        try:
            import curl_cffi  # noqa: F401
            out["transport"] = "curl_cffi"
        except Exception:
            out["transport"] = "requests"
    if hasattr(ad, "_cp_has_curl"):
        out["cp_has_curl"] = bool(ad._cp_has_curl())

    # 一次性代价单独计：图片域探测/初始化只发生一次，混进"搜索耗时"会让数字无法解释
    if hasattr(ad, "_ensure_img_domain"):
        _step("domain_probe", lambda: (ad._ensure_img_domain(), "图片域=%s"
                                       % getattr(ad, "_img_domain", "?"))[1])
    _kw = "巨人" if source in ("copymanga", "copymanga_web") else "汉化"
    _hits = {}

    def _search():
        res = ad.search(_kw)
        _hits["cid"] = str((res[0].id if res else "") or "")
        return "命中 %d 条，首条 id=%s" % (len(res or []), _hits["cid"])

    _step("search", _search)
    _cid = comic_id or _hits.get("cid") or ""
    if _cid:
        def _detail():
            d = ad.comic_info(_cid)
            _hits["chapter"] = str((d.chapters[0].id if d.chapters else "") or "")
            return "章节 %d 个" % len(d.chapters or [])
        _step("detail", _detail)
    if _cid and _hits.get("chapter"):
        def _chapter():
            imgs = ad.images(_cid, _hits["chapter"])
            _hits["img0"] = (imgs or [""])[0]
            return "图片 %d 张" % len(imgs or [])
        _step("chapter", _chapter)
    if _hits.get("img0"):
        def _first_image():
            from engine.manga.downloader import fetch_image_checked
            h = {}
            try:
                h = dict(ad.image_headers(_hits["img0"]) or {})
            except Exception:
                h = {}
            r = fetch_image_checked(_hits["img0"], h, timeout=timeout)
            return "%d 字节" % len(getattr(r, "content", b"") or b"")
        _step("first_image", _first_image)
    # 本机缓存/预热参数（都是只读常量，便于对照"为什么慢"）
    try:
        from server import manga_api as _m
        out["warm_concurrency"] = int(getattr(_m, "_WARM_CONCURRENCY", 0) or 0)
        out["warm_lead"] = [int(getattr(_m, "_WARM_LEAD_PAGES", 0) or 0),
                            int(getattr(_m, "_WARM_LEAD_CONCURRENCY", 0) or 0)]
        out["cover_gate"] = int(getattr(_m, "_COVER_PROXY_MAX_FILES", 0) or 0)
    except Exception:
        pass
    try:
        from engine.manga.jm import PROCESS_VERSION as _pv
        if source == "jm":
            out["process_version"] = int(_pv)
    except Exception:
        pass
    return out


def latency_lines(source="jm", comic_id="", data=None):
    """把测速结果转成人类可读的几行（进诊断报告/界面）。

    data 传入时**复用**已有测量结果——否则调用方"先取 JSON 再取文本"会打两遍源站
    （实测踩到：端点里两次测量，耗时翻倍且数字不一致）。
    """
    d = data if isinstance(data, dict) and data else measure_latency(source, comic_id)
    lines = ["  源=%s 传输=%s" % (d.get("source"), d.get("transport"))]
    if d.get("cp_has_curl") is not None:
        lines.append("  拷贝漫画 curl_cffi 可用=%s" % d.get("cp_has_curl"))
    for name, info in (d.get("steps") or {}).items():
        lines.append("  %-12s ok=%-5s %6s ms  %s" % (
            name, info.get("ok"), info.get("ms"), info.get("detail") or ""))
    if d.get("warm_concurrency"):
        lines.append("  预热并发=%s（前 %s 页用 %s 并发）封面缓存上限=%s" % (
            d.get("warm_concurrency"),
            (d.get("warm_lead") or [0, 0])[0], (d.get("warm_lead") or [0, 0])[1],
            d.get("cover_gate")))
    if d.get("process_version"):
        lines.append("  图片处理版本=%s" % d.get("process_version"))
    return lines


def _net_lines():
    """出站网络路径（代理）：报告必须说清"测速是在哪条路径上测的"。

    Windows/手机上都可能配了代理：不写这一段，读报告的人会把"经代理才通"
    误判成"源站慢/源站封了"。凭据一律脱敏（报告会外发）。
    """
    try:
        from engine import netproxy
    except Exception as e:                                   # noqa: BLE001
        return ["  （代理状态不可用：%s）" % scrub_text(e, 120)]
    out = []
    val = netproxy.current_proxy()
    cfg = netproxy.config_path()
    if val:
        src = netproxy.source()
        where = {"env": "用户设的环境变量 WR_PROXY",
                 "set": "本机设置页写入（已落盘，重启引擎仍生效）",
                 "file": "应用配置文件"}.get(src, src)
        out.append("  出站代理：已启用 %s（来源：%s）"
                   % (netproxy.redact_proxy(val), where))
    else:
        out.append("  出站代理：未配置（全部直连）")
    _h = netproxy.health()
    if _h.get("configured") and not _h.get("ok"):
        out.append("  ⚠ 配置的代理**当前不可达**（%s）→ 已在临时直连；"
                   "地址仍保留在配置里" % scrub_text(_h.get("err") or "连接失败", 80))
    out.append("  配置文件：%s" % (scrub_path(cfg) if cfg else "（未设置 WR_PROXY_FILE）"))
    out.append("  说明：代理只改变网络路径，不绕过任何站点的风控；"
               "经代理时目标 IP 绑定（防 DNS 重绑定）不再适用。")
    return out


def build_report():
    """生成脱敏诊断报告（纯文本）。任何一节失败都只影响该节，不整篇报错。"""
    lines = []
    lines.append("本机引擎脱敏诊断报告")
    lines.append("生成时间：%s" % _now())
    lines.append("")
    lines.append("【1. 引擎】")
    st = {}
    try:
        from server.mobile_entry import get_runtime   # 手机端运行时（桌面端不存在）
        rt = get_runtime()
        st = rt.status() if rt is not None else {}
    except Exception as e:
        record_error("diag.mobile_runtime", e)
    try:
        import sys as _sys
        lines.append("  Python：%s" % _sys.version.split()[0])
    except Exception:
        pass
    for k in ("state", "port", "pid", "wsgi", "uptime_s", "instance_id"):
        if isinstance(st, dict) and st.get(k) not in (None, ""):
            v = st.get(k)
            if k == "instance_id":
                v = str(v)[:8] + "…"        # 实例 id 只留前 8 位
            lines.append("  %s：%s" % (k, v))
    if isinstance(st, dict) and st.get("business"):
        b = st["business"] or {}
        lines.append("  业务运行时：state=%s initialized=%s workers=%s" % (
            b.get("state"), b.get("initialized"),
            ",".join(str(w) for w in (b.get("workers") or []))[:120]))
    if isinstance(st, dict) and st.get("error"):
        lines.append("  最近错误：%s" % scrub_text(st.get("error"), 160))
    if not st:
        lines.append("  （拿不到手机运行时状态——桌面运行或被裁剪；按不可用处理）")
    elif str(st.get("state") or "") in ("", "stopped") and not st.get("port"):
        # 桌面/测试环境跑 pytest 时手机运行时并不启动：如实说明，别让人以为引擎挂了
        lines.append("  （当前不是手机 App 运行时：手机运行时未启动，以上为本地探针结果）")

    lines.append("")
    lines.append("【2. 能力台账（依赖级）】")
    lines.extend(_caps_lines())

    lines.append("")
    lines.append("【3. 书源（小说）】")
    lines.extend(_source_lines())

    lines.append("")
    lines.append("【4. 漫画源（依赖判定与实测结论分开）】")
    lines.extend(_manga_lines())

    lines.append("")
    lines.append("【5. 存储】")
    lines.extend(_storage_lines())

    lines.append("")
    lines.append("【6. 最近的失败请求与异常】")
    ev = events()
    if not ev["failed_requests"] and not ev["errors"]:
        lines.append("  （本次运行未记录到失败请求）")
    for ts, method, path, status, ms in ev["failed_requests"]:
        lines.append("  [%s] %s %s → HTTP %s（%sms）" % (ts, method, path, status, ms))
    for ts, where, kind, msg in ev["errors"]:
        lines.append("  [%s] %s: %s: %s" % (ts, where, kind, msg))

    lines.append("")
    lines.append("【7. 出站网络路径（代理）】")
    lines.extend(_net_lines())

    lines.append("")
    lines.append("【8. 阅读链路分段测速（只读；每步超时 10s，失败如实记录）】")
    try:
        for _ln in latency_lines("jm"):
            lines.append(_ln)
    except Exception as e:
        record_error("diag.latency", e)
        lines.append("  （测速不可用：%s）" % scrub_text(e, 120))
    lines.append("")
    lines.append("【9. 本报告的范围与脱敏声明】")
    lines.append("  · 不含 token / Cookie / 鉴权头 / 会话值；URL 已去掉查询串；实例 id 只留前 8 位。")
    lines.append("  · 只记录失败请求（4xx/5xx）与异常，最多各 30/20 条。")
    lines.append("  · 「依赖判定」= 本机有没有该源需要的运行环境；「实测」= 逐源功能验证")
    lines.append("    （搜索 → 详情/目录 → 章节图片 → 真实取一张图）的结果；未实测显示未实测。")
    lines.append("  · 报告不含书库正文与图片内容本身。")
    return "\n".join(lines) + "\n"
