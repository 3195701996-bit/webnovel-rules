# -*- coding: utf-8 -*-
"""能力台账（阶段 C）：把"这台设备上哪些能力真的可用"讲清楚。

设计依据：
- §11 阶段 C 退出条件："列出每项能力/每个适配器的 supported/degraded/unsupported
  及原因，不能用首页能打开代替业务通过"
- §4 禁止走的捷径："不创建名为 curl_cffi 的 requests 转发 shim 来冒充原依赖"、
  "Playwright 第一版应显式禁用，不静默假兼容"

因此这里做三件事：
  1. 探测可选依赖/通道是否真的可用（可导入 + 关键子能力）；
  2. 按"适配器 → 所需能力"给出 supported / degraded / unsupported + 原因；
  3. 输出可供 /api/health、/api/manga/sources 与移动端能力清单共用的摘要。

注意：这里的判定只回答"代码能不能在这个运行时上跑通"，不代替真实联网验证；
联网可达性仍由各适配器自己的错误路径报告。
"""
import importlib
import threading
import time

# 探测结果短 TTL 记忆（导入探测有成本；运行中依赖不会变化）
_TTL = 300.0
_lock = threading.Lock()
_cache = {"ts": 0.0, "data": None}


def _try_import(modname):
    try:
        importlib.import_module(modname)
        return True, ""
    except Exception as e:                     # ImportError / 二进制加载失败都算不可用
        return False, f"{type(e).__name__}: {e}"


def _probe_uncached():
    caps = {}
    # 规则引擎按规则类型需要的解析依赖也在这里登记（单一事实来源）：
    #   cssselect   → legado 默认的 CSS 链式写法（".list@li@a"）
    #   lxml        → XPath 规则（"//div/a"）
    #   jsonpath_ng → JSONPath 规则（"$.data[*]"）
    #   opencc      → 简繁转换（部分源用）
    # 逐源功能验证需要据此判断"该源用到的规则类型能不能跑"，而不是凭整体猜。
    for name, mod in (("curl_cffi", "curl_cffi"),
                      ("requests", "requests"),
                      ("lxml", "lxml"),
                      ("pillow", "PIL"),
                      ("cryptography", "cryptography"),
                      ("cssselect", "cssselect"),
                      ("jsonpath_ng", "jsonpath_ng"),
                      ("opencc", "opencc")):
        ok, err = _try_import(mod)
        caps[name] = {"available": ok, "detail": err}
    # playwright：需要可导入 **且** 有可用浏览器（桌面 Chrome 路径由 copymanga_web 决定）
    ok, err = _try_import("playwright.sync_api")
    caps["playwright"] = {"available": ok, "detail": err}
    # 浏览器可执行文件：copymanga_web 目前写死 macOS Chrome 路径
    # （设计 B1 已记为不能原样封装进 APK 的根因），这里如实探测它是否存在。
    _CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    import os as _os
    _has_chrome = _os.path.exists(_CHROME)
    caps["playwright_browser"] = {
        "available": bool(ok and _has_chrome),
        "detail": (_CHROME if _has_chrome else
                   ("playwright 不可用" if not ok else f"未找到浏览器: {_CHROME}")),
    }
    return caps


def probe(force=False):
    """可选依赖/通道探测（带 TTL 记忆）"""
    now = time.time()
    with _lock:
        if not force and _cache["data"] is not None and now - _cache["ts"] < _TTL:
            return _cache["data"]
    data = _probe_uncached()
    with _lock:
        _cache["ts"] = time.time()
        _cache["data"] = data
    return data


def reset_cache():
    with _lock:
        _cache["ts"] = 0.0
        _cache["data"] = None


# ── 适配器 → 所需能力 ────────────────────────────────────────
# api       ：硬依赖，缺则**无法搜索/取详情** → unsupported
# image_soft：图片通道的**软依赖**，缺失时降级到 requests 直连（无 TLS 指纹，
#             部分源可能拒绝或限流）→ degraded，且在 reason 里说明实际通道
# 说明（阶段 C 诊断第六节）：能力状态必须与真实分支一致；只写"缺 curl_cffi"
# 不足以描述能力，须记录实际传输后端与尚未做的功能级验证。
SOURCE_REQUIREMENTS = {
    "jm": {
        "name": "禁漫天堂",
        "api": ["requests"],              # API 走 urllib/requests 语义
        "image_soft": ["curl_cffi"],
    },
    "copymanga": {
        # 0.74.0 更正：主通道改为**网页通道（纯 HTTP）**，APP 接口降为回落通道。
        # 依据（2026-09-17 晚实测，普通 requests、无 Cookie、无登录）：
        #   搜索 /api/kb/web/searchci/comics → 真实 JSON（含 total 可翻页）
        #   详情 /comic/<pw> → 服务端渲染 HTML（标题/作者/标签/简介/封面）
        #   章节 /comicdetail/<pw>/chapters → AES-128-CBC 解密（密钥取自页面内联脚本）
        #   正文 /comic/<pw>/chapter/<uuid> → contentKey 同法解密出图片直链
        # 注意 host：必须是 www.copy4000.com —— **裸域 copy4000.com 对匿名 HTTP 返回
        # "格式正确、内容为空"的空壳**，早期"网页通道必须浏览器/必须登录"的结论就是
        # 拿裸域测出来的（见 03-验证结果/拷贝漫画移植研究-2026-09-17.md §六）。
        # 依赖只有 requests + cryptography + lxml，Android 包内均已包含。
        "name": "拷贝漫画(网页通道)",
        "api": ["requests", "cryptography", "lxml"],
        # 图片是直链（CDN 实测不校验 Referer）：有 curl_cffi 就用，没有走 requests；
        # 网页通道**不需要** TLS 指纹伪装。
        "image_soft": ["curl_cffi"],
        # 关键：本源的 image_soft 只是"可选加速"，缺它**不构成能力降级**——
        # 2026-09-17 实测：纯 HTTP 取图片直链并真的下到 257KB JPEG（无任何指纹伪装）。
        # 若仍按通用规则标"degraded"，用户会在手机上看到"缺 curl_cffi 可能被拒"，
        # 而它其实可用——这正是指南 §2.5 要避免的"依赖判定冒充可用性结论"。
        "image_soft_optional": True,
        # 真机可用性仍取决于：①你的网络能否到达 www.copy4000.com；
        # ②源站是否对匿名客户端升级风控（全站前置 Cloudflare）。
        # 因此标"条件可用"而不是"Android 不支持"；也**不**标"已验证"——
        # 后者必须由目标手机的四段闭环记录产生。
        "conditional": "需要网络可达 www.copy4000.com；网页通道匿名即可用，"
                       "但源站可能对受限网络拦截",
    },
    "copymanga_web": {
        # 桌面专用：Playwright 渲染通道。手机端没有 playwright（也没有浏览器内核），
        # 会如实显示为"缺少依赖"。**手机请用上面的「拷贝漫画(网页通道)」**——
        # 那条是纯 HTTP，不依赖浏览器（0.74.0 起）。
        "name": "拷贝漫画(网页版·桌面专用)",
        "api": ["playwright", "lxml"],
        "image_soft": ["curl_cffi"],
    },
    "baozi": {
        "name": "包子漫画",
        "api": ["lxml"],
        "image_soft": ["curl_cffi"],
        # 2026-09-17/18 实测（桌面直连）：`www.baozimh.com` **已迁域**到
        # `cn.bzmgcn.com`，新域返回
        # `{"challenge_url":"/__gatekeeper_challenge/start","error":"challenge_required"}`
        # （nginx + JS 挑战门）。跟着挑战页走一次能拿到 `__Host-gk_browser_*` cookie，
        # 但**再访首页仍 403** → 需要真实浏览器执行脚本才放行。
        # 实测经代理（出口 IP 未被挑战）时两条传输都 200。
        # **我们不做挑战求解**：指南停止项明确"不绕过网站技术保护、规避访问限制"。
        "conditional": "源站已迁域（→ cn.bzmgcn.com）并启用 JS 挑战门（challenge_required）："
                       "直连被拦、需真实浏览器；实测经代理可通。本 App 不实现挑战求解",
    },
    "mangadex": {"name": "MangaDex", "api": ["requests"], "image_soft": ["curl_cffi"]},
    "nhentai": {
        "name": "nhentai",
        "api": ["requests"],
        "image_soft": ["curl_cffi"],
        # 2026-09-17 四段实测（桌面直连）：搜索**超时**（该域在本机不可达）。
        # 验证流程已加硬性时限（120s → 20s），避免"一按校验干等两分钟"。
        "conditional": "本机直连该域不通（实测搜索超时）；可达性与你的网络/代理有关",
    },
    "komiic": {"name": "Komiic", "api": ["requests"], "image_soft": ["curl_cffi"]},
}


def _missing(caps, needs):
    return [n for n in needs if not caps.get(n, {}).get("available")]


def source_status(key, caps=None):
    """单个漫画源的可用性：supported / degraded / unsupported / pending + 原因。

    与旧版的区别（阶段 C 诊断第六节）：
      - **未登记适配器不再默认 supported**，而是 pending（需逐源功能验证）；
      - 记录**实际图片传输通道**（curl_cffi 还是 requests 降级），不再只凭
        "缺 curl_cffi" 就断言图片不可用；
      - 明确 `verified=False`：本函数只做依赖级判定，不代表已通过功能验证。
    """
    caps = caps if caps is not None else probe()
    req = SOURCE_REQUIREMENTS.get(key)
    if req is None:
        return {"status": "pending", "verified": False, "transport": {},
                "conditional": "",
                "reason": "未登记能力需求：需逐源做搜索/详情/阅读/下载功能验证后再标注"}
    miss_api = _missing(caps, req.get("api") or [])
    miss_soft = _missing(caps, req.get("image_soft") or [])
    soft_optional = bool(req.get("image_soft_optional"))
    transport = {
        "api": "ok" if not miss_api else f"missing:{','.join(miss_api)}",
        # 三种写法都要能自解释：有指纹 / 降级但已实测可用 / 降级且有风险
        "image": ("curl_cffi" if not miss_soft else
                  ("requests（实测可用，无需指纹）" if soft_optional
                   else "requests_fallback")),
    }
    # 已知条件（实测得到的"能不能用取决于什么"，例如：源站已迁域并启用挑战、
    # 本机直连不通、需代理）。**必须带出去**：这些文字以前从没被任何界面读过
    # （写了等于没写），用户只看到一句笼统的"未验证"。
    cond = str(req.get("conditional") or "").strip()
    base = {"verified": False, "transport": transport, "conditional": cond}
    if miss_api:
        return dict(base, status="unsupported",
                    reason=f"缺少 {'、'.join(miss_api)}，无法搜索/取详情")
    if miss_soft and not soft_optional:
        return dict(base, status="degraded",
                    reason=f"图片通道缺少 {'、'.join(miss_soft)}，已降级为 requests "
                           f"直连（无 TLS 指纹，个别源可能拒绝或限流）")
    # soft_optional 的源：缺指纹已由**实测**证明不影响取图，因此不降级、也不写
    # "降级"字样；是否真的可用仍由「实测」一栏回答（本函数只做依赖级判定）。
    return dict(base, status="supported", reason="",
                note=("图片走直链（纯 HTTP，实测可取）；%s 仅为可选加速"
                      % "、".join(req.get("image_soft") or []) if miss_soft else ""))


def combined_reason(st):
    """源的说明文字：**已知条件在前、依赖原因在后**（两者都不丢）。

    为什么需要它：`conditional` 是我们实测出来的"能不能用取决于什么"
    （源站迁域/挑战门、本机不可达、需代理），而 `reason` 常常只是一句通用的
    传输降级（缺 curl_cffi）。0.74.10 之前 `conditional` **从没被任何界面读过**
    （写了等于没写），手机上只显示通用那句，用户看不出该去做什么。
    漫画/api 与漫画/目录两条路径都用这里，避免再出现"两处各拼一份、只改了一处"。
    """
    cond = str((st or {}).get("conditional") or "").strip()
    dep = str((st or {}).get("reason") or "").strip()
    if cond and dep:
        return "%s｜%s" % (cond, dep)
    return cond or dep


def manga_sources_status(keys, caps=None):
    return {k: source_status(k, caps) for k in keys}


def summary(caps=None):
    """给 /api/health 与移动端能力清单用的摘要（不含路径/凭据）"""
    caps = caps if caps is not None else probe()
    return {
        "deps": {k: ("ok" if v["available"] else "missing")
                 for k, v in caps.items()},
        "missing": sorted(k for k, v in caps.items() if not v["available"]),
        "notes": {
            "curl_cffi": "Android 无可用安装组合（需要 cffi>=2.0.0），"
                         "按设计走 requests/cloudscraper 降级并逐源标注",
            "playwright": "桌面专属；移动端第一版显式不支持，不做替代通道",
        },
    }
