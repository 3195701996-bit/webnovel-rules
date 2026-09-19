# -*- coding: utf-8 -*-
"""拷贝漫画**网页通道**（纯 HTTP：无浏览器、无 Cookie、无登录）。

## 为什么存在

官方 APP 接口（`/api/v3/...`）被 210 风控点名（"破解版客户端…等待1小時"），
而网页通道此前被误判为"必须 Playwright 渲染"。误判根因是当初测的是**裸域**
`copy4000.com`——该域对匿名 HTTP 返回"格式正确、内容为空"的空壳。
实测 `https://www.copy4000.com` 上**普通 requests、无 Cookie、最简头**即可拿到
搜索/详情/章节/正文图片全部真实数据：

- 搜索 `GET /api/kb/web/searchci/comics?q=&limit=&offset=` → JSON
- 详情 `GET /comic/<path_word>` → 服务端渲染 HTML（正则/lxml 可解析）
- 章节 `GET /comicdetail/<path_word>/chapters` → JSON，`results` 是密文串
- 正文 `GET /comic/<path_word>/chapter/<uuid>` → HTML 内联 `var cct` / `var contentKey`

密文为 **AES-128-CBC + PKCS7**：`key = UTF8(ccz)`（章节）或 `UTF8(cct)`（图片），
密文串**前 16 字符是明文 IV**，其余是 hex 编码的密文。站点当前 `ccz == cct`
（`op0zzpvv.nmn.00p`），但**必须从页面内联脚本取，不得硬编码**。

## 传输实现（为什么不是 curl_cffi）

`copymanga._cp_request()` 的语义在这里**照抄**：出站代理的唯一事实来源是
`engine.netproxy`（不读环境/系统代理，否则"设置页明明直连却走了系统代理"对不上）、
每线程会话复用（keep-alive）、`trust_env=False`。区别只有一处：**只用 requests**。
curl_cffi 在 Android 没有 wheel，网页通道若要真机可用就不能依赖它；而匿名
requests 已实测可取全量数据（见上），没有伪指纹需求。

## 失败语义（重要）

所有函数**失败必抛 `MangaError` 子类并带原因**，绝不静默返回空：
源站改了结构就要让人一眼看到"页面结构可能已变更"，而不是"这部漫画没有章节"。
- `WebError`：网络/5xx/解析失败——可以换域重试
- `WebBlocked`：403 / Cloudflare 挑战——**立即停止**，换域只会把 IP 打得更狠
"""
import json
import re
import threading
import time
import urllib.parse

from .base import Chapter, Comic, ComicDetails, MangaError, decode_html, html_fromstring_safe

# ── 域池 ──────────────────────────────────────────────────────────────────
# 第 1 个是已验证可用的主域；**裸域 copy4000.com 绝不能进池**——它对匿名
# HTTP 返回"格式正确、内容为空"的空壳，会伪装成"搜索无结果/章节为空"。
WEB_DOMAINS = [
    "https://www.copy4000.com",
    "https://www.mangacopy.com",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
# 超时拆成 (连接, 读取)：**连接**才是黑洞/丢包时的杀手——手机上 15 秒的连接等待
# 会让一次搜索白等半分钟（2026-09-17 实测：域不可达时单次调用 30 秒，再叠加回落
# 通道就是十几分钟，App 的 30 秒客户端超时先炸 → 用户看到"所有源都超时"）。
TIMEOUT_CONNECT = 5
TIMEOUT_READ = 12
TIMEOUT = (TIMEOUT_CONNECT, TIMEOUT_READ)
# 单域内的重试：**0 次**（失败即换域）。重试留给上层"换域"来承担，
# 同一台不可达的主机重试只会把等待翻倍。
MAX_RETRIES = 0
# 整次调用的总预算（秒）：域池轮换、探测、重试全都必须在预算内收尾。
# 之所以必须有它：调用方（搜索编排）给 copymanga 的期限是 25 秒，
# 而"域池 × 重试 × 超时"是乘法——没有总预算就必然超期。
TOTAL_BUDGET = 9.0
BASE_TTL = 600           # 域探测结果缓存 10 分钟
KEY_TTL = 3600           # ccz/dnt 缓存 1 小时（站点级常量，但不得硬编码）
DETAIL_TTL = 300         # 详情页 HTML 缓存 5 分钟（page_key 与 web_detail 共用）

# 章节类型：源站在 `build.type` 里给出（1=話 2=卷 3=番外篇），这里是兜底表。
# 同一个映射站点 JS 里也硬编码（switch 1/2/3 → 話/卷/番外），实测两边一致。
TYPE_NAMES = {1: "話", 2: "卷", 3: "番外篇"}

_LOCK = threading.Lock()
_BASE = {"base": "", "ts": 0.0}
# 失败负缓存：域池整体不可达后，短时间内不再逐个域重探。
# 没有它，用户连点两次搜索就要白等两个 9 秒预算（实测 8 秒/次）。
_NEG = {"ts": 0.0, "msg": ""}
NEG_TTL = 30.0


class _Budget(object):
    """整次调用的时间预算（秒）。left() 用尽即视为不可达，绝不无限轮换。"""

    __slots__ = ("end",)

    def __init__(self, seconds=None):
        import os as _os
        v = seconds
        if v is None:
            raw = (_os.environ.get("WR_COPY_WEB_BUDGET") or "").strip()
            try:
                v = float(raw) if raw else TOTAL_BUDGET
            except ValueError:
                v = TOTAL_BUDGET
        self.end = time.time() + max(1.0, float(v))

    def left(self):
        return self.end - time.time()

    def exhausted(self):
        return self.left() <= 0.5

    def slice_timeout(self, base=None):
        """在一次请求上最多花的时间：预算剩多少就用多少（但不低于 1 秒）。"""
        t = base or TIMEOUT
        conn, read = (t if isinstance(t, tuple) else (t, t))
        if self.left() < conn + read:
            conn = max(1.0, min(conn, self.left()))
            read = max(1.0, min(read, max(1.0, self.left())))
        return (conn, read)
_KEYS = {}               # path_word -> {"ccz":.., "dnt":.., "ts":..}
_DETAILS = {}            # path_word -> {"html":.., "ts":..}
_local = threading.local()


class WebError(MangaError):
    """网页通道的网络/HTTP/解析失败（可换域重试）"""


class WebUnreachable(WebError):
    """**本机到域池全都不通/超时**（与"页面结构变更"分开）。

    为什么要单独一个类型：回落 APP 通道的决策靠它——
    网页主机不可达时，APP 主机（同站点家族）大概率也不可达，而 APP 通道是
    "多域 × 重试 × 15 秒"的长链，回落等于把一次搜索拖成十几分钟。
    """


class WebBlocked(MangaError):
    """网页通道被拒绝（403 / Cloudflare 挑战）——立即停止，不换域硬撞"""


# ── HTTP 传输 ────────────────────────────────────────────────────────────
def reset_session():
    """丢弃本线程会话（netproxy 切换代理/连接异常后调用，语义同
    `copymanga._reset_session`：不能残留旧出站路径）"""
    sess = getattr(_local, "sess", None)
    _local.sess = None
    if sess is not None:
        try:
            sess.close()
        except Exception:
            pass


def _session():
    """每线程 requests 会话（连接复用，消除每请求 TLS 握手）"""
    sess = getattr(_local, "sess", None)
    if sess is not None:
        return sess
    import requests
    from requests.adapters import HTTPAdapter
    sess = requests.Session()
    # 出站路径唯一事实来源是 engine.netproxy：trust_env=False 防止
    # requests 自己捡走环境里的 HTTP(S)_PROXY，与设置页显示不一致。
    sess.trust_env = False
    sess.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0))
    sess.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0))
    _local.sess = sess
    return sess


def _proxy():
    """当前出站代理（engine.netproxy 为唯一事实来源；无则 None）"""
    try:
        from .. import netproxy as _np
        return _np.current_proxy() or None
    except Exception:
        return None


def _headers(dnt="", json_api=False):
    h = {
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8",
    }
    base = _BASE.get("base")
    if base:
        h["Referer"] = base + "/"
    if json_api:
        h["Accept"] = "application/json, text/plain, */*"
    else:
        h["Accept"] = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,*/*;q=0.8")
        h["Upgrade-Insecure-Requests"] = "1"
    if dnt:
        # 反爬头：值取自详情页 `<span id="dnt" value="3">`
        h["dnts"] = str(dnt)
    return h


class _Resp(object):
    """内部统一响应（把 requests.Response 收敛到 status/text/content）"""

    __slots__ = ("status_code", "text", "content", "headers", "url")

    def __init__(self, status_code, text="", content=None, headers=None, url=""):
        self.status_code = status_code
        self.text = text
        self.content = content if content is not None else text.encode("utf-8", "replace")
        self.headers = headers or {}
        self.url = url

    def json(self):
        return json.loads(self.text)


def _web_get(url, headers=None, timeout=TIMEOUT, retries=MAX_RETRIES, budget=None):
    """统一 GET：requests + 线程会话 + netproxy 代理 + 有限重试。

    成功返回 `_Resp`；失败抛 WebUnreachable（连不上/超时，**不回落 APP**）、
    WebBlocked（403/挑战，立即停）或 WebError（HTTP/结构问题，可换域）。
    """
    proxy = _proxy()
    want = {"http": proxy, "https": proxy} if proxy else None
    last = None
    _conn_fail = 0
    for attempt in range(max(0, retries) + 1):
        if budget is not None:
            if budget.exhausted():
                # 注意：**不能**写成 f"...%.0fs..." % TOTAL_BUDGET——
                # f-string 先把 {url} 插进来，URL 里的百分号编码（%XX）会被
                # 后面的 % 当成转换符，实测报 "not enough arguments for format
                # string"，把真实原因（超时）盖成一句 Python 报错。
                raise WebUnreachable(
                    f"网页通道超时（预算 {TOTAL_BUDGET:.0f}s 用尽）: {url[:100]}")
            timeout = budget.slice_timeout(timeout)
        sess = _session()
        if dict(sess.proxies) != (want or {}):
            # 会话级代理同步到"当前值"：切换/清除代理后不能残留旧路径
            sess.proxies.clear()
            if want:
                sess.proxies.update(want)
        try:
            r = sess.get(url, headers=headers, timeout=timeout, proxies=want,
                         allow_redirects=True)
        except Exception as e:                      # 连接重置/超时/DNS
            last = f"{type(e).__name__}: {e}"
            _conn_fail += 1
            continue
        code = r.status_code
        text = getattr(r, "text", "") or ""
        if code == 403 or "Just a moment" in text[:2000] or "cf-chl" in text[:2000]:
            raise WebBlocked(
                f"网页通道被拒绝（HTTP {code}／Cloudflare 挑战）: {url[:120]}"
                "；可能是本机 IP 被风控或需要换代理，请勿继续重试")
        if code >= 500:
            last = f"HTTP {code}"
            continue
        if code != 200:
            raise WebError(f"HTTP {code}: {url[:120]}")
        return _Resp(code, text, getattr(r, "content", None),
                     getattr(r, "headers", None), url)
    # 连接类失败单独归类：调用方据此**不回落** APP 通道（见 WebUnreachable 注释）
    _cls = WebUnreachable if _conn_fail else WebError
    raise _cls(f"请求失败(尝试 {_conn_fail or max(0, retries) + 1} 次): {last} ← {url[:120]}")


def _domain_pool():
    """域池（WR_COPY_WEB_DOMAINS 逗号分隔可覆盖，便于换域不改代码）"""
    import os
    raw = (os.environ.get("WR_COPY_WEB_DOMAINS") or "").strip()
    if raw:
        out = []
        for d in raw.split(","):
            d = d.strip().rstrip("/")
            if not d:
                continue
            if not d.startswith("http"):
                d = "https://" + d
            out.append(d)
        if out:
            return out
    return list(WEB_DOMAINS)


def _looks_like_site(text):
    """首页判定：真的拷贝漫画（标题含 拷贝/拷貝），空壳页会在这里被否掉"""
    head = (text or "")[:4000]
    return "拷贝" in head or "拷貝" in head


def _web_base(force=False, skip=None, budget=None):
    """当前可用 web 域（探测 + 10 分钟缓存；失败换下一个域）。

    `skip`：本次请求已经失败过的域（域池轮换时不重复撞同一个域）。
    与 `copymanga_web._web_base` 的区别：**域池全挂时抛异常**，不返回第一个域。
    旧的"全失败回退首个域"会把"全站不可用"变成"这部漫画没有章节"。
    """
    skip = set(skip or ())
    with _LOCK:
        if not force and _NEG["msg"] and time.time() - _NEG["ts"] < NEG_TTL:
            # 刚刚整池失败过：直接如实报同一原因，不再花第二个预算去撞墙
            raise WebUnreachable(_NEG["msg"] + "（%.0f 秒内不再重试）" % NEG_TTL)
        cur = _BASE["base"]
        if not force and cur and cur not in skip and \
                time.time() - _BASE["ts"] < BASE_TTL:
            return cur
    errs = []
    _unreach = 0
    for base in _domain_pool():
        if base in skip:
            continue
        if budget is not None and budget.exhausted():
            errs.append("预算用尽，未再探测其余域")
            break
        try:
            # 探测只试一次、超时更短：它只是"这个域活不活"，不该吃掉整个预算
            r = _web_get(base + "/", headers=_headers(), retries=0, budget=budget,
                         timeout=(min(4, TIMEOUT_CONNECT), min(6, TIMEOUT_READ)))
        except WebBlocked:
            raise
        except Exception as e:
            errs.append(f"{base}: {e}")
            _unreach += 1
            continue
        if _looks_like_site(r.text):
            with _LOCK:
                _BASE["base"] = base
                _BASE["ts"] = time.time()
                _NEG["msg"] = ""
                _NEG["ts"] = 0.0            # 探通了就清掉负缓存
            return base
        errs.append(f"{base}: 页面为空壳或非拷贝漫画站点")
    with _LOCK:
        _BASE["base"] = ""
        _BASE["ts"] = 0.0
    _msg = ("拷贝漫画网页通道不可用（域池全部失败）: "
            + ("; ".join(errs) if errs else f"无可用域（已跳过 {sorted(skip)}）"))
    # 全部都是连接类失败 → 归类为"不可达"（调用方据此不回落 APP 通道）
    _cls = WebUnreachable if (_unreach and _unreach >= len(errs)) else WebError
    if _cls is WebUnreachable:
        with _LOCK:
            _NEG["msg"] = _msg
            _NEG["ts"] = time.time()
    raise _cls(_msg)


def _rotate_base():
    """当前域失效：清缓存（下次 _web_base 会重新探测并按序换域）"""
    with _LOCK:
        _BASE["base"] = ""
        _BASE["ts"] = 0.0


def _get_path(path, headers=None, dnt="", json_api=False, timeout=TIMEOUT,
              budget=None):
    """按当前域请求 path；网络/HTTP 失败→换域重试（域池轮换）。

    403/Cloudflare（WebBlocked）**不换域**：继续硬撞只会让 IP 被标记更深。
    """
    last = None
    skip = None
    _budget = budget if budget is not None else _Budget()
    for i in range(len(_domain_pool())):
        if i and _budget.exhausted():
            break                                # 预算用尽：不再换域，如实报超时
        try:
            base = _web_base(force=(i > 0), skip=skip, budget=_budget)
            return _web_get(base + path, headers=headers or _headers(dnt, json_api),
                            timeout=timeout, budget=_budget)
        except WebBlocked:
            raise
        except WebUnreachable as e:              # 域池整体不可达：立刻停（不换域硬撞）
            last = e
            break
        except Exception as e:
            last = e
            skip = (skip or set()) | {(_BASE.get("base") or "")}
            _rotate_base()
    if isinstance(last, WebUnreachable):
        raise last
    if _budget.exhausted():
        raise WebUnreachable("网页通道超时（预算 %.0fs 用尽）: %s"
                             % (TOTAL_BUDGET, str(last)[:120]))
    raise WebError(f"所有域均失败: {last}")


# 空壳重试次数：源站偶尔返回 HTTP 200 + 结构正确但**业务数据为空**的响应
# （实测两次：章节列表 count=0；读者页 contentKey=''）。这类响应换域/重发一次
# 通常就好——它既不是"这部漫画没章节"，也不是"该通道不可用"。
# 次数固定为 1：既覆盖间歇抖动，又不会在预算里无限重试。
EMPTY_RETRY = 1


def _is_empty_shell_chapters(chs):
    return not chs


def _retry_on_empty(fn, is_empty, what, retries=EMPTY_RETRY):
    """对"空壳响应"重试（换域）——**只对空数据**生效，不掩盖真实错误。

    为什么单独做这件事：源站的匿名降级行为是"格式正确的空壳"，早期调查正是因为
    把一次空壳当成"网页通道不可用"，得出了错误的结论（见拷贝漫画移植研究 §六）。
    这里把它当成**可重试的抖动**处理，并把"重试后仍为空"如实抛出。
    """
    out = None
    for i in range(max(0, retries) + 1):
        out = fn()
        if not is_empty(out):
            return out
        if i < retries:
            print("[copy_web] %s 返回空壳（HTTP 200 但内容为空），换域重试" % what,
                  flush=True)
            _rotate_base()
    return out


def clear_cache():
    """清进程内缓存（测试/换域/需要重取密钥时用）"""
    with _LOCK:
        _BASE["base"] = ""
        _BASE["ts"] = 0.0
        _NEG["msg"] = ""
        _NEG["ts"] = 0.0
        _KEYS.clear()
        _DETAILS.clear()


# ── AES 解密 ─────────────────────────────────────────────────────────────
def _aes_decrypt_str(enc, key):
    """AES-128-CBC/PKCS7 解密拷贝漫画的密文串。

    密文串结构：**前 16 个字符是明文 IV**（不是 hex），其余部分是 hex 密文。
    等价 JS：`CryptoJS.AES.decrypt(Base64(Hex.parse(ct)), Utf8.parse(key),
    {iv: Utf8.parse(iv16), mode: CBC, padding: Pkcs7}).toString(Utf8)`
    """
    if not enc or len(enc) < 17:
        raise WebError(f"密文串过短({len(enc or '')} 字符)，无法解密（页面结构可能已变更）")
    if len(key) not in (16, 24, 32):
        raise WebError(f"AES 密钥长度非法({len(key or '')} 字符)，页面结构可能已变更")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding as sympad
    except ImportError as e:                       # pragma: no cover - 环境缺失
        raise WebError("缺少 cryptography（AES 解密不可用）") from e
    iv = enc[:16].encode("utf-8")
    try:
        ct = bytes.fromhex(enc[16:])
    except ValueError as e:
        raise WebError(f"密文 hex 解析失败（页面结构可能已变更）: {e}") from e
    if not ct or len(ct) % 16:
        raise WebError(f"密文长度非法({len(ct)} 字节)，页面结构可能已变更")
    try:
        d = Cipher(algorithms.AES(key.encode("utf-8")), modes.CBC(iv)).decryptor()
        raw = d.update(ct) + d.finalize()
        u = sympad.PKCS7(128).unpadder()
        return (u.update(raw) + u.finalize()).decode("utf-8").strip()
    except Exception as e:
        raise WebError(f"AES 解密失败（密钥/密文不匹配，页面结构可能已变更）: {e}") from e


# ── 详情页 HTML / 站点密钥 ───────────────────────────────────────────────
def _decode(resp):
    """HTML 解码：**不传 requests 的 resp.encoding**。

    requests 对没有 charset 的 text/html 默认给 ISO-8859-1，而 ISO-8859-1
    解码永不失败——直接用它会把中文解成乱码还"看起来成功"。
    交给 `base.decode_html` 按 BOM → body 内 meta charset → utf-8 → gb18030 走。
    """
    return decode_html(resp.content)


def _detail_html(path_word, force=False):
    """详情页 HTML（缓存 DETAIL_TTL 秒；page_key 与 web_detail 共用一次请求）"""
    if not path_word or not str(path_word).strip():
        raise WebError("path_word 为空，无法取详情页")
    path_word = str(path_word).strip()
    with _LOCK:
        rec = _DETAILS.get(path_word)
        if rec and not force and time.time() - rec["ts"] < DETAIL_TTL:
            return rec["html"]
    r = _get_path("/comic/" + urllib.parse.quote(path_word))
    html = _decode(r)
    if not html or len(html) < 200:
        raise WebError(f"详情页内容异常（{len(html or '')} 字符）：{path_word}；"
                       "域池里可能混进了空壳域，或站点结构已变更")
    with _LOCK:
        _DETAILS[path_word] = {"html": html, "ts": time.time()}
    return html


def _re_first(pat, text, flags=0):
    m = re.search(pat, text or "", flags)
    return m.group(1).strip() if m else ""


def _parse_ccz(html):
    """站点级 AES 密钥（页内 `var ccz = '...'`）——必须取自页面，不得硬编码"""
    key = _re_first(r"var\s+ccz\s*=\s*['\"]([^'\"]+)['\"]", html)
    if not key:
        raise WebError("详情页未找到内联密钥 ccz（页面结构可能已变更）；"
                       "站点换算法时必须同步更新 engine/manga/copy_web.py")
    return key


def _parse_dnt(html):
    """反爬头值：`<span id="dnt" style="display:none;" value="3">`"""
    m = re.search(r"<span[^>]*id=[\"']dnt[\"'][^>]*>", html or "")
    if not m:
        return ""
    vm = re.search(r"value=[\"']([^\"']+)[\"']", m.group(0))
    return vm.group(1).strip() if vm else ""


def page_key(path_word, force=False):
    """(ccz, dnt)：站点级 AES 密钥 + 反爬头值（进程内缓存 KEY_TTL 秒）。

    两个值都从详情页内联脚本/隐藏 span 里取——站点换值/换算法时这里**抛异常**
    而不是回退到硬编码，避免"用旧密钥解出乱码当成功"。
    """
    path_word = str(path_word or "").strip()
    with _LOCK:
        rec = _KEYS.get(path_word)
        if rec and not force and time.time() - rec["ts"] < KEY_TTL:
            return rec["ccz"], rec["dnt"]
    html = _detail_html(path_word, force=force)
    ccz = _parse_ccz(html)
    dnt = _parse_dnt(html)
    with _LOCK:
        _KEYS[path_word] = {"ccz": ccz, "dnt": dnt, "ts": time.time()}
    return ccz, dnt


# ── 解析：详情页 HTML ────────────────────────────────────────────────────
def _li_value(tree, label):
    """详情页 `<li><span>標籤：</span><... class="comicParticulars-right-txt">值</...>`

    标签文字繁简都认（页面实测是繁体，站点也可能给简体）。
    """
    for span in tree.xpath("//li/span[1]"):
        txt = (span.text_content() or "").strip()
        if not txt.startswith(label):
            continue
        sib = span.getnext()
        if sib is None:
            continue
        return (sib.text_content() or "").strip()
    return ""


def _first_chapter_href(tree, path_word):
    """详情页"開始閱讀"按钮 → 首话 UUID（也用于"页面是否真是详情页"的判据）"""
    for a in tree.xpath("//a[contains(@href, '/chapter/')]"):
        href = a.get("href") or ""
        m = re.search(r"/comic/([^/]+)/chapter/([a-f0-9A-F-]{8,})", href)
        if m:
            return m.group(2)
        m = re.search(r"/chapter/([a-zA-Z0-9-]{8,})", href)
        if m and (path_word in href):
            return m.group(1)
    return ""


def web_detail(path_word):
    """详情页 → `ComicDetails`（chapters 由 `web_chapters()` 单独拉取后再挂上）"""
    path_word = str(path_word or "").strip()
    html = _detail_html(path_word)
    from lxml import html as _lhtml
    try:
        tree = html_fromstring_safe(_lhtml, html)
    except Exception as e:
        raise WebError(f"详情页 HTML 解析失败（页面结构可能已变更）: {e}") from e

    title = ""
    hs = tree.xpath("//h6[@title]")
    if hs:
        title = (hs[0].get("title") or hs[0].text_content() or "").strip()
    if not title:
        t = _re_first(r"<title>(.*?)</title>", html, re.S)
        title = t.split("-")[0].strip() if t else ""
    first_ch = _first_chapter_href(tree, path_word)
    if not title:
        if "404" in html[:3000] or "不存在" in html[:3000]:
            raise WebError(f"漫画不存在: {path_word}")
        raise WebError(f"详情页缺少标题 h6（页面结构可能已变更）: {path_word}")
    if not first_ch and "chapter/" not in html:
        raise WebError(f"详情页没有任何章节链接（页面结构可能已变更）: {path_word}")

    alias = _li_value(tree, "別名") or _li_value(tree, "别名")
    author = _li_value(tree, "作者")
    status = _li_value(tree, "狀態") or _li_value(tree, "状态")
    update_time = _li_value(tree, "最後更新") or _li_value(tree, "最后更新")
    views = _li_value(tree, "熱度") or _li_value(tree, "热度")

    tags = []
    for a in tree.xpath("//span[contains(@class,'comicParticulars-left-theme-all')]//a"):
        t = (a.text_content() or "").strip().lstrip("#").strip()
        if t and t not in tags:
            tags.append(t)

    desc = ""
    ps = tree.xpath("//p[contains(@class,'intro')]")
    if ps:
        desc = (ps[0].text_content() or "").strip()

    cover = ""
    for img in tree.xpath("//img"):
        src = (img.get("data-src") or img.get("src") or "").strip()
        if "/cover/" in src and "/static/" not in src and not src.startswith("data:"):
            cover = src
            break

    base = _BASE.get("base") or _domain_pool()[0]
    detail = ComicDetails(
        id=path_word, title=title, cover=cover,
        sub_title="拷贝漫画",
        description=desc, author=author, tags=tags,
        update_time=update_time, views=views,
        url=f"{base}/comic/{path_word}", chapters=[])
    # 别名/状态/首话在 ComicDetails 里没有对应字段（**返回契约不能变**），
    # 但它们正是排查"页面结构是否变了"的关键，因此只打日志、不改返回结构。
    print(f"[copy_web] 详情 {path_word}: 状态={status or '-'} 别名={alias or '-'}"
          f" 首话={(first_ch or '-')[:8]}", flush=True)
    return detail


# ── 搜索 ─────────────────────────────────────────────────────────────────
def _parse_search(data):
    """搜索 JSON → (List[Comic], total)。

    `results` 结构缺失一律抛异常：那意味着**结构变了**，而不是"没有结果"。
    （`code==200` + 空 list 是合法的"搜不到"，照实返回空列表。）
    """
    if not isinstance(data, dict):
        raise WebError("搜索接口返回非 JSON 对象（页面结构可能已变更）")
    if int(data.get("code") or 0) != 200:
        raise WebError(f"搜索接口 code={data.get('code')} message={data.get('message')}")
    results = data.get("results")
    if not isinstance(results, dict):
        raise WebError("搜索接口 results 为空/结构变更（页面结构可能已变更）")
    if "list" not in results:
        raise WebError("搜索接口 results.list 缺失（页面结构可能已变更）")
    total = int(results.get("total") or 0)
    out = []
    for item in results.get("list") or []:
        c = (item or {}).get("comic") or item or {}
        pw = c.get("path_word") or c.get("id") or ""
        if not pw:
            continue
        au = c.get("author") or []
        if isinstance(au, dict):
            au = [au]
        author = ""
        if isinstance(au, list) and au:
            a0 = au[0]
            author = a0.get("name", "") if isinstance(a0, dict) else str(a0)
        tags = [t.get("name", "") for t in (c.get("theme") or [])
                if isinstance(t, dict) and t.get("name")]
        out.append(Comic(id=pw, title=c.get("name", ""), author=author,
                         cover=c.get("cover", ""), tags=tags, url=pw,
                         source_key="copymanga", total=total))
    return out, total


def web_search(kw, limit=30, offset=0, source_key="copymanga"):
    """网页搜索 → (List[Comic], total)。

    `platform=2&q_type=` 是站点搜索页自己带的参数（仓库 R64 实测：缺这两个
    参数时部分关键词——如"姐姐的朋友"——返回空结果）。
    """
    q = urllib.parse.quote(str(kw or ""))
    try:
        _limit = max(1, int(limit))
    except (TypeError, ValueError):
        _limit = 30
    try:
        _offset = max(0, int(offset))
    except (TypeError, ValueError):
        _offset = 0
    path = (f"/api/kb/web/searchci/comics?q={q}&limit={_limit}&offset={_offset}"
            "&platform=2&q_type=")
    r = _get_path(path, json_api=True, budget=_Budget())
    try:
        data = json.loads(r.text)
    except Exception as e:
        raise WebError(f"搜索接口返回非 JSON（页面结构可能已变更）: {e}") from e
    comics, _total = _parse_search(data)
    if source_key != "copymanga":
        for c in comics:
            c.source_key = source_key
    return comics, _total


# ── 章节列表 ─────────────────────────────────────────────────────────────
def _group_name(gname, ginfo, type_used, ch_type):
    """分组名：优先 `groups[g].name`，缺失时退回 build.type 映射（1=話 2=卷 3=番外篇）"""
    nm = ((ginfo or {}).get("name") or "").strip()
    if nm:
        return nm
    return (type_used.get(ch_type) or TYPE_NAMES.get(ch_type) or (gname or ""))


def _parse_chapters(plain, path_word):
    """解密后的章节 JSON → List[Chapter]（正序，与站点 JS 渲染顺序一致）。

    站点 `comic_detail_pass*.js` 逐数组项顺序生成 `<li>`，全程没有 reverse
    （离线核对过混淆脚本），所以这里**不反转**；APP 通道 `comic2` 返回倒序
    才需要 reverse，两条通道语义不同，不能照搬。
    """
    try:
        data = json.loads(plain)
    except Exception as e:
        raise WebError(f"章节密文解密结果不是 JSON（页面结构可能已变更）: {e}") from e
    if not isinstance(data, dict):
        raise WebError("章节密文解密结果不是对象（页面结构可能已变更）")
    groups = data.get("groups")
    if not isinstance(groups, dict) or not groups:
        raise WebError("章节接口缺少 groups（页面结构可能已变更）")
    build = data.get("build") or {}
    type_used = {}
    for t in (build.get("type") or []):
        try:
            type_used[int(t.get("id"))] = t.get("name") or ""
        except (TypeError, ValueError):
            continue
    base = _BASE.get("base") or _domain_pool()[0]
    out = []
    for gname, ginfo in groups.items():
        for ch in ((ginfo or {}).get("chapters") or []):
            cid = (ch or {}).get("id") or ""
            if not cid:
                continue
            ctype = ch.get("type")
            try:
                ctype = int(ctype)
            except (TypeError, ValueError):
                ctype = None
            out.append(Chapter(
                id=cid, name=(ch.get("name") or "").strip(),
                group=_group_name(gname, ginfo, type_used, ctype),
                url=f"{base}/comic/{path_word}/chapter/{cid}"))
    if not out:
        # 真实"0 章"几乎不存在（站点校验过 path_word），而 0 章当成功会让
        # 上层显示成"这部漫画没有章节"。抛异常交上层回落 APP 通道并暴露原因。
        counts = {k: (v or {}).get("count") for k, v in groups.items()}
        raise WebError(f"章节接口返回 0 章（groups={list(groups)} counts={counts}）"
                       "；可能是该 build 类型无内容，或站点结构已变更")
    return out


def web_chapters(path_word):
    """章节列表 → List[Chapter]（id = 章节 uuid；组名优先 groups[g].name）"""
    path_word = str(path_word or "").strip()
    ccz, dnt = page_key(path_word)
    _diag = {"msg": ""}

    def _one():
        r = _get_path(f"/comicdetail/{path_word}/chapters", dnt=dnt,
                      json_api=True)
        try:
            data = json.loads(r.text)
        except Exception as e:
            raise WebError(f"章节接口返回非 JSON（页面结构可能已变更）: {e}") from e
        if int(data.get("code") or 0) != 200:
            raise WebError(f"章节接口 code={data.get('code')} "
                           f"message={data.get('message')}")
        enc = data.get("results")
        if not isinstance(enc, str) or not enc.strip():
            raise WebError("章节接口 results 为空（页面结构可能已变更）")
        try:
            return _parse_chapters(_aes_decrypt_str(enc.strip(), ccz), path_word)
        except WebError as e:
            # "0 章"是**空壳**而不是结构错误：记下诊断信息并返回空列表，
            # 交给 _retry_on_empty 换域重试（源站偶发，实测重跑即恢复）。
            if "0 章" in str(e):
                _diag["msg"] = str(e)
                return []
            raise

    # 空壳（解密成功但 0 章）换域重试一次：源站偶发，不代表"这部漫画没有章节"
    chs = _retry_on_empty(_one, _is_empty_shell_chapters, "章节接口")
    if not chs:
        raise WebError((_diag["msg"] or "章节接口返回 0 章")
                       + "；换域重试后仍为空 —— 源站可能对匿名请求降级，"
                         "稍后重试或换网络再看")
    return chs


# ── 正文图片 ─────────────────────────────────────────────────────────────
def _parse_content_key(html):
    cct = _re_first(r"var\s+cct\s*=\s*['\"]([^'\"]+)['\"]", html)
    ck = _re_first(r"var\s+contentKey\s*=\s*['\"]([^'\"]*)['\"]", html)
    return cct, ck


def _filter_images(urls):
    """排除站内静态图/广告（与 copymanga_web 渲染通道同口径）。

    但过滤**不能把有效列表清空**：CDN 换域名时宁可返回未过滤的原始列表，
    也不要静默变成"0 图"。
    """
    keep = [u for u in urls
            if u.startswith("http") and "/static/" not in u]
    if not keep:
        keep = [u for u in urls if u.startswith("http")]
    return keep


def web_images(path_word, chapter_uuid):
    """正文图片 URL 列表（按阅读顺序；AES 解 `contentKey`，key = 页内 `cct`）"""
    path_word = str(path_word or "").strip()
    chapter_uuid = str(chapter_uuid or "").strip()
    if not chapter_uuid:
        raise WebError("chapter_uuid 为空，无法取正文图片")
    _ccz, dnt = page_key(path_word)

    def _one():
        r = _get_path(f"/comic/{path_word}/chapter/{chapter_uuid}", dnt=dnt)
        html = _decode(r)
        cct, ck = _parse_content_key(html)
        if not cct:
            raise WebError("章节页缺少内联密钥 cct（页面结构可能已变更）")
        if not ck:
            # **实测到的空壳形态**：HTTP 200、页面正常、但 contentKey 是空串
            # （源站对匿名客户端的偶发降级）。返回空列表交给上层换域重试，
            # 而不是直接判定"通道不可用"——那正是早期误判的来源。
            return []
        plain = _aes_decrypt_str(ck, cct)
        try:
            data = json.loads(plain)
        except Exception as e:
            raise WebError(f"contentKey 解密结果不是 JSON"
                           f"（页面结构可能已变更）: {e}") from e
        if isinstance(data, dict):
            data = data.get("contents") or data.get("list") or []
        if not isinstance(data, list):
            raise WebError("contentKey 解密结果不是数组（页面结构可能已变更）")
        out = []
        for x in data:
            if isinstance(x, dict):
                u = (x.get("url") or "").strip()
            elif isinstance(x, str):
                u = x.strip()
            else:
                u = ""
            if u:
                out.append(u)
        return _filter_images(out)

    urls = _retry_on_empty(_one, lambda u: not u, "章节页 contentKey")
    if not urls:
        raise WebError("章节页 contentKey 为空（换域重试后仍为空）：源站可能对匿名"
                       "请求降级，或该章需登录/VIP；稍后重试或换网络再看")
    return urls
