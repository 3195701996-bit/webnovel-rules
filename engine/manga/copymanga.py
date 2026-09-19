# -*- coding: utf-8 -*-
"""拷贝漫画适配器：完整移植 venera copy_manga.js 的反爬方案
- HMAC-SHA256 签名（x-auth-signature = HMAC(secret, ts)）
- 设备指纹（deviceinfo/device/pseudoid 持久化）
- 动态 API 域（refreshAppApi）
- request_id（营销 API）
- status 210 频率限制退避
- 图片乱序重排（words）+ 画质替换（c1500x.webp）

本轮起**网页通道优先**（`engine/manga/copy_web.py`，纯 HTTP、无浏览器/无 Cookie）：
搜索/详情/章节/正文图片四个入口都先走网页通道，失败再回落本文件的 APP 通道
（回落时 APP 侧行为与 210 状态机**完全不变**）。理由：APP `/api/v3/...` 被 210
点名"破解版客户端"，而网页通道匿名 HTTP 即可取全量数据——它是"少一条被风控的
请求"，不是"换一条通道"。通道选择见 `WR_COPY_TRANSPORT=web|app|auto`。
"""
import hashlib
import hmac
import json
import urllib.parse
import os
import random
import re
import threading
import string
import time

from . import copy_web
from .base import Comic, ComicDetails, Chapter, MangaAdapter, MangaError
from ..app_utils import atomic_write as _atomic_write

SECRET_B64 = "M2FmMDg1OTAzMTEwMzJlZmUwNjYwNTUwYTA1NjNhNTM="
DEFAULT_API = "api.copy4000.com"
# 多域名池（210 自动切换；network2 动态发现补充）
# 含 Mihon 扩展(copymanga-copy20)使用的活跃域名：api.mangacopy.com / api.copy3000.com
# 官方站已迁到 copy4000.com 系（源站 210 原文里给出的就是 copy4000.com/download）。
# 2026-09-17 实测：`api.copy4000.com` 的搜索接口 200 正常；旧域（api.mangacopy.com）
# 搜索仍通但详情返回 error —— 表现为"时好时坏"。因此把官方现域放**最前**。
DOMAIN_POOL = ["api.copy4000.com", "api.copy202601.com", "api.2026copy.com",
               "api.copy2000.online", "api.copy-manga.com",
               "api.mangacopy.com", "api.copy3000.com"]
# ── 传输通道选择 ─────────────────────────────────────────────────────────
# web  = 只用网页通道（copy_web，纯 HTTP）；失败直接抛错，不碰 APP 接口
# app  = 只用 APP 通道（原行为，210 状态机全量生效）
# auto = 网页通道优先，失败回落 APP 通道（默认）
# 环境变量 WR_COPY_TRANSPORT 优先于这个模块级常量（排查时不用改代码）。
TRANSPORT = "auto"
_VALID_TRANSPORT = ("web", "app", "auto")


def _transport_mode():
    """本次请求走哪条通道（每次调用读取，便于运行期切换/测试）"""
    v = (os.environ.get("WR_COPY_TRANSPORT") or "").strip().lower()
    if v not in _VALID_TRANSPORT:
        v = str(TRANSPORT or "auto").strip().lower()
    return v if v in _VALID_TRANSPORT else "auto"
# 版本：从官网 copy4000.com 动态发现；常量兜底 3.0.9
COPY_VERSION = "3.0.9"
COPY_VERSION_URL = "https://copy4000.com/download"
HEADERS_BASE = {
    "User-Agent": f"COPY/{COPY_VERSION}",
    "source": "copyApp",
    "platform": "3",
    "referer": f"com.copymanga.app-{COPY_VERSION}",
    "version": COPY_VERSION,
    "Accept": "application/json",
    "region": "0",
    "umstring": "b4c89ca4104ea9a97750314d791520ac",
}


_version_cache = {"v": None, "ts": 0.0}

# P2：APP 通道 Session 单例（连接复用，消除每请求 TLS 握手）。
# curl_cffi Session 非线程安全 → 创建双检锁 + 请求持锁串行（调用频率
# 已被 4s/6s 令牌桶限制，串行无吞吐损失；阅读通道低频请求同理）。
_session_lock = threading.Lock()
_session = None



# ── 传输垫片（0.70.0）：无 curl_cffi 时降级到 requests ─────────────────────
# 为什么可以降级：2026-09-17 实测（走本地代理，纯 requests、无任何指纹伪装）——
#   /api/v3/search/comic → HTTP 200 正常 JSON。
# 也就是说"必须 curl_cffi 的 Chrome TLS 指纹"这个前提**不成立**：之前的失败是
# 域名过期（api.mangacopy.com → 现为 copy4000.com 系）+ APP 通道 210 风控，
# 与 TLS 指纹无关。于是 Android（没有 curl_cffi wheel）也能走同一条 API 通道。
#
# requests 路径同样做**每线程会话复用**（keep-alive），避免每请求重付握手。
_cp_local = threading.local()
_CP_HAS_CURL = None


def _cp_has_curl():
    global _CP_HAS_CURL
    if _CP_HAS_CURL is None:
        try:
            import curl_cffi  # noqa: F401
            _CP_HAS_CURL = True
        except Exception:
            _CP_HAS_CURL = False
    return _CP_HAS_CURL


def _cp_request(url, headers=None, timeout=15, proxy=None):
    """统一的 GET：优先 curl_cffi（Chrome 指纹），否则 requests（带连接池）。

    返回 requests/curl_cffi 的 Response（两者 .status_code/.json()/.text 同构）。
    """
    if proxy is None:
        try:
            from .. import netproxy as _np
            proxy = _np.current_proxy() or None
        except Exception:
            proxy = None
    if _cp_has_curl():
        from curl_cffi import requests as creq
        return creq.get(url, headers=headers, impersonate="chrome",
                        timeout=timeout, proxies=({"http": proxy, "https": proxy}
                                                 if proxy else None))
    import requests
    sess = getattr(_cp_local, "sess", None)
    if sess is None:
        from requests.adapters import HTTPAdapter
        sess = requests.Session()
        # 出站路径的唯一事实来源是 engine/netproxy：不读环境/系统代理，
        # 否则"设置页里明明是直连、实际却走了系统代理"，排查时对不上。
        sess.trust_env = False
        sess.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0))
        sess.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0))
        _cp_local.sess = sess
    # 会话级代理同步到"当前值"：切换/清除代理后不能残留旧路径
    # （set_proxy 会重置会话，这里是第二道保险）
    want = {"http": proxy, "https": proxy} if proxy else {}
    if dict(sess.proxies) != want:
        sess.proxies.clear()
        sess.proxies.update(want)
    return sess.get(url, headers=headers, timeout=timeout, proxies=want or None)


def _get_session():
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                if _cp_has_curl():
                    from curl_cffi import requests as creq
                    _session = creq.Session(impersonate="chrome")
                else:
                    # 无 curl_cffi（Android）：requests 会话 + 连接池，语义一致
                    import requests
                    from requests.adapters import HTTPAdapter
                    _session = requests.Session()
                    _session.mount("https://", HTTPAdapter(pool_connections=4,
                                                          pool_maxsize=8, max_retries=0))
    return _session


def _reset_session():
    """连接异常后丢弃旧 Session（下次请求重建连接），逼近旧实现
    "每请求新建连接" 的故障恢复语义。"""
    global _session
    with _session_lock:
        old, _session = _session, None
    if old is not None:
        try:
            old.close()
        except Exception:
            pass


def _fetch_latest_version():
    """从官网解析最新 APP 版本号（类级缓存 1h，避免每次实例初始化都请求官网）"""
    now = time.time()
    if _version_cache["v"] and now - _version_cache["ts"] < 3600:
        return _version_cache["v"]
    try:
        r = _cp_request(COPY_VERSION_URL, timeout=10)
        m = re.search(r"copymanga\.release_(\d+)_([\d.]+)\.apk", r.text)
        if m:
            _version_cache["v"] = m.group(2)
            _version_cache["ts"] = now
            return m.group(2)
    except Exception:
        pass
    return COPY_VERSION


def _randint(a, b):
    return random.randint(a, b)


def _rand_char_a():
    return chr(65 + _randint(0, 25))


def _rand_digit():
    return chr(48 + _randint(0, 9))


def _generate_device_info():
    return f"{_randint(1000000, 9999999)}V-{_randint(1000, 9999)}"


def _generate_device():
    return (_rand_char_a() + _rand_char_a() + _rand_digit() + _rand_char_a() + "."
            + "".join(_rand_digit() for _ in range(6)) + "."
            + "".join(_rand_digit() for _ in range(3)))


def _generate_pseudoid():
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(16))


def _hmac_sha256_hex(key: bytes, msg: bytes) -> str:
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


class CopyManga(MangaAdapter):
    key = "copymanga"
    name = "拷贝漫画"
    version = "1.4.1"
    concurrent = 2

    def __init__(self, state_dir=None, throttle=True):
        """throttle=False = 阅读通道实例：绕过令牌桶限流与 210 状态机升级，
        逐张读图不被 4s/请求 串行化（下载任务保持 throttle=True 防风控）"""
        self._throttle = throttle
        self._state_dir = state_dir  # 持久化设备指纹/API 域
        self._api = DEFAULT_API
        self._deviceinfo = None
        self._device = None
        self._pseudoid = None
        self._token = ""
        self._reqid = ""
        self._reqid_ts = 0.0
        # 动态版本（防版本风控）：**懒取**——网页通道不需要它，构造时就打官网
        # 等于每次冷启动白花一个请求（手机首次用拷贝漫画实测多等 1~2 秒）
        self._version = None
        # 实例级风控状态机（每实例独立：下载/阅读互不影响——
        # 下载触发 210 冷却时阅读仍可正常请求）
        self._req_lock = threading.Lock()
        self._last_req = [0.0]
        self._rg_lock = threading.Lock()
        # P3-11: 设备指纹轮换/状态落盘锁——单例多线程并发时保证
        # deviceinfo/device/pseudoid 三元组一致，且 _save_state 不交叉写
        self._fp_lock = threading.Lock()
        self._rg_state = "normal"      # normal / degraded / cooldown
        self._rg_210_count = 0
        self._rg_cooldown_until = 0.0
        self._rg_last_probe = 0.0
        self._rg_last_210_ts = 0.0     # 最近一次 210 的时间戳（敏感期判定）
        self._had_state = self._load_state()
        # 域名池轮换索引
        self._dom_pool = list(DOMAIN_POOL)
        if self._api not in self._dom_pool:
            self._dom_pool.insert(0, self._api)
        self._api_refreshed = False
        # 注意：**不在这里刷 API 域**（同上：网页通道用不到 APP 域名，构造期不打网络）。
        # 真正要发 APP 请求时由 api_url 触发一次，见 _ensure_api()。

    # ── 状态持久化（设备指纹复用，避免频繁更换触发风控）──
    def _state_path(self):
        if not self._state_dir:
            return None
        return os.path.join(self._state_dir, "copymanga_state.json")

    def _load_state(self):
        p = self._state_path()
        if p and os.path.exists(p):
            try:
                st = json.load(open(p, encoding="utf-8"))
                self._api = st.get("api", DEFAULT_API)
                self._deviceinfo = st.get("deviceinfo")
                self._device = st.get("device")
                self._pseudoid = st.get("pseudoid")
                self._token = st.get("token", "")
                return True
            except Exception:
                pass
        return False

    def _save_state(self):
        """P3-11: 加锁 + 原子写（tmp+os.replace），多线程并发不再交叉写坏"""
        with self._fp_lock:
            self._save_state_nolock()

    def _save_state_nolock(self):
        """调用方须已持有 _fp_lock"""
        p = self._state_path()
        if not p:
            return
        _atomic_write(p, {"api": self._api, "deviceinfo": self._deviceinfo,
                          "device": self._device, "pseudoid": self._pseudoid,
                          "token": self._token})

    # 每请求轮换指纹的开关（默认关）。2026-09-17 实测：接口返回的 210 原文是
    #   "您或您身邊的人曾經下載過破解版本的拷貝漫畫，請下載安裝正版之後等待1小時，
    #    限制會自動解除。"
    # 而"每次请求都换一套设备指纹"恰恰是破解版客户端的典型特征，因此默认改为
    # **进程内稳定指纹**（落盘复用），只在显式设置 WR_COPY_ROTATE_FP=1 时才轮换
    # （保留给需要试验的场合）。
    def _should_rotate_fp(self):
        return (os.environ.get("WR_COPY_ROTATE_FP") or "").strip() in ("1", "true", "yes")

    def _rotate_fingerprint(self, persist=False):
        """P3-11: 加锁轮换设备指纹（三元组原子一致）；persist=True 时原子落盘"""
        with self._fp_lock:
            self._deviceinfo = _generate_device_info()
            self._device = _generate_device()
            self._pseudoid = _generate_pseudoid()
            if persist:
                self._save_state_nolock()

    # ── 签名与请求头 ──
    @property
    def headers(self):
        # P3-11: 锁内懒初始化并快照三元组——避免与 _get 的每请求轮换
        # 交叉出"deviceinfo 属第 N 代、device 属第 N+1 代"的混合指纹
        with self._fp_lock:
            if not self._deviceinfo:
                self._deviceinfo = _generate_device_info()
                self._device = _generate_device()
                self._pseudoid = _generate_pseudoid()
                self._save_state_nolock()
            _di, _dev, _pid = self._deviceinfo, self._device, self._pseudoid
        now = int(time.time())
        ts = str(now)
        dt = time.strftime("%Y.%m.%d", time.localtime())
        secret = __import__("base64").b64decode(SECRET_B64)
        sig = _hmac_sha256_hex(secret, ts.encode())
        token = (" " + self._token) if self._token else ""
        hb = dict(HEADERS_BASE)
        # 动态版本（官网发现的最新版，防版本风控）
        hb["User-Agent"] = f"COPY/{self._ver()}"
        hb["referer"] = f"com.copymanga.app-{self._ver()}"
        hb["version"] = self._ver()
        return {
            **hb,
            "deviceinfo": _di,
            "dt": dt,
            "device": _dev,
            "pseudoid": _pid,
            "authorization": f"Token{token}",
            "x-auth-timestamp": ts,
            "x-auth-signature": sig,
        }

    # ── 请求封装（含 210 退避）──
    # 风控状态机已移至实例级（__init__）：下载/阅读各自独立，
    # 避免下载触发冷却拖垮阅读（边下边读）

    def _rate_wait(self):
        """令牌桶限流：normal 4s，degraded 6s；cooldown 期间直接拒绝。
        阅读通道实例（_throttle=False）完全绕过——在线阅读逐张读图不能等"""
        if not self._throttle:
            return
        with self._rg_lock:
            state = self._rg_state
            if state == "cooldown":
                now = time.time()
                if now < self._rg_cooldown_until:
                    raise MangaError("copymanga 冷却中，请稍后重试")
                # 冷却结束：低频探测（P3-2 修复——原逻辑 _rg_last_probe 从不
                # 更新，探测限流名存实亡，冷却一结束即全量恢复）。
                # 现在：每 60s 只放行一个探测请求（放行时真正记录时间戳），
                # 探测期间保持 cooldown 状态，待 _mark_ok 确认探测成功才恢复
                # normal；探测再遇 210 则由时间戳继续节流，60s 后再探。
                if now - self._rg_last_probe < 60:
                    raise MangaError("copymanga 冷却恢复探测中，请稍后重试")
                self._rg_last_probe = now
            interval = 6.0 if state == "degraded" else 4.0
        with self._req_lock:
            gap = time.time() - self._last_req[0]
            if gap < interval:
                time.sleep(interval - gap)
            self._last_req[0] = time.time()

    def _mark_210(self):
        """记录 210：升级状态机（阅读通道实例不升级——单张失败即重试/用户刷新，
        不应让阅读请求把整个源拖进 cooldown 拒绝后续请求）"""
        if not self._throttle:
            return
        with self._rg_lock:
            self._rg_210_count += 1
            self._rg_last_210_ts = time.time()
            if self._rg_state == "normal" and self._rg_210_count >= 2:
                self._rg_state = "degraded"
                print("[copymanga] 风控降级 degraded（5s/请求）", flush=True)
            elif self._rg_state == "degraded" and self._rg_210_count >= 4:
                self._rg_state = "cooldown"
                self._rg_cooldown_until = time.time() + 300
                print("[copymanga] 风控冷却 cooldown 5 分钟", flush=True)

    def _mark_ok(self):
        with self._rg_lock:
            if self._rg_state == "degraded":
                self._rg_state = "normal"
                self._rg_210_count = 0
            elif self._rg_state == "cooldown" and \
                    time.time() >= self._rg_cooldown_until:
                # P3-2: 冷却期后的探测请求成功 → 恢复正常（冷却期内的成功
                # 不存在——_rate_wait 在冷却期内不放行请求）
                self._rg_state = "normal"
                self._rg_210_count = 0

    def in_cooldown(self):
        """是否处于风控冷却/敏感期（供上层提前走降级通道，避免徒劳重试）
        - cooldown 状态：直接敏感
        - 最近 10 分钟内出现过 210：敏感（IP 标记 TTL≈1h，保守起见）
        """
        with self._rg_lock:
            if self._rg_state == "cooldown":
                return True
            if time.time() - self._rg_last_210_ts < 600:
                return True
            return False

    def _get(self, url, timeout=15, retries=3):
        """请求封装（curl_cffi Chrome TLS 指纹 + 令牌桶 + 风控状态机）
        - curl_cffi 单例 Session（impersonate=chrome）：TLS 指纹与真实 Chrome
          一致（210 根因修复），连接复用消除每请求 TLS 握手（P2）
        - 令牌桶限流（normal 2s / degraded 5s / cooldown 暂停）
        - 210 → 状态机降级；连接异常 → 丢弃 Session 并刷新 API 域
        """
        refreshed = False
        # 指纹策略（0.70.0）：**默认稳定**。旧实现每请求轮换，理由是"新指纹不累计
        # 请求数"；但实测 210 的原文点名"破解版客户端"，而频繁更换设备指纹正是这类
        # 客户端的特征。要试验轮换时设置 WR_COPY_ROTATE_FP=1。
        if self._should_rotate_fp():
            self._rotate_fingerprint()
        for attempt in range(retries + 2):
            self._rate_wait()
            try:
                _h = self.headers
                print(f"[copymanga] GET {url[:70]} 指纹:{self._deviceinfo} 版本:{self._ver()}", flush=True)
                # Session 非线程安全：请求全程持锁串行（频率已被令牌桶限制）
                with _session_lock:
                    resp = _get_session().get(url, headers=_h, timeout=timeout)
                print(f"[copymanga] → {resp.status_code}", flush=True)
            except Exception as e:
                _reset_session()
                if not refreshed:
                    print(f"[copymanga] 请求失败({type(e).__name__})，刷新 API 域", flush=True)
                    self._refresh_api()
                    refreshed = True
                    # 仅替换 API 域主机；营销/官网等外部 URL 保持原样
                    if url.startswith("https://" + self._api) or \
                       any(url.startswith(f"https://{d}") for d in self._dom_pool):
                        url = re.sub(r"https://[^/]+", self.api_url, url)
                    continue
                raise MangaError(f"请求失败: {e}") from e
            if resp.status_code == 210:
                msg = ""
                wait = 40
                try:
                    body = resp.json()
                    msg = body.get("message", "")
                    m = re.search(r"(\d+)\s*seconds", msg)
                    if m:
                        wait = int(m.group(1))
                except Exception:
                    msg = resp.text[:200]
                print(f"[copymanga] 210: {msg[:100]}", flush=True)
                # 阅读通道实例：快速失败，不做 35s×N 重试链——
                # 上层(api_manga_image)捕获后自动降级 copymanga_web 通道
                if not self._throttle:
                    raise MangaError(
                        f"copymanga 风控：{msg[:90] or 'HTTP 210'}"
                        "（源站限制约 1 小时后自动解除）")
                # 破解版/等待1小时类标记（IP 或设备被标记）
                # 策略：旋转指纹 + 切换域名池下一个域重试（Mihon RATE_LIMIT_DOMAIN 思路）
                # 所有域均被标记 → 进入冷却（5 分钟）等待 TTL
                if any(k in msg for k in ("破解", "等待1小時", "等待 1 小時", "1小时", "正版")):
                    # 被标记为"破解版客户端"：原文说明等约 1 小时自动解除。
                    # 这里仍尝试切换域池里的其它域；默认**不**改指纹（改指纹正是被标记的特征）。
                    if self._should_rotate_fp():
                        self._rotate_fingerprint(persist=True)
                    # 尝试下一个域名
                    _next = None
                    for _d in self._dom_pool:
                        if _d != self._api:
                            _next = _d
                            break
                    if _next:
                        self._api = _next
                        self._save_state()
                        print(f"[copymanga] 风控标记，切换域名 → {self._api}", flush=True)
                        url = re.sub(r"https://[^/]+", self.api_url, url)
                        continue
                    # 域池耗尽 → 冷却 5 分钟（TTL≈1h，等待恢复）
                    print("[copymanga] 所有域名被标记，冷却 5 分钟", flush=True)
                    with self._rg_lock:
                        self._rg_state = "cooldown"
                        self._rg_cooldown_until = time.time() + 300
                    raise MangaError(
                        "copymanga 风控：" + (msg[:90] or "全部域名被标记")
                        + "；源站限制约 1 小时后自动解除，期间请勿频繁请求")
                # 纯频率限制 → 状态机降级 + 等待
                self._mark_210()
                print(f"[copymanga] 频率限制，等待 {wait}s（{self._rg_state}）", flush=True)
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                raise MangaError(f"HTTP {resp.status_code}: {url[:80]}")
            self._mark_ok()
            return resp.json()
        raise MangaError("请求失败")

    def _get_request_id(self):
        """从营销 API 获取 request_id（5 分钟缓存复用；
        营销 API 与拷贝漫画风控独立，用轻量请求避免拖慢主链路）"""
        now = time.time()
        if self._reqid and now - self._reqid_ts < 300:
            return self._reqid
        try:
            url = "https://marketing.aiacgn.com/api/v2/adopr/query3/?format=json&ident=200100001"
            resp = _cp_request(url, headers=self.headers, timeout=6)
            if resp.status_code == 200:
                rid = resp.json().get("results", {}).get("request_id", "")
                if rid:
                    self._reqid = rid
                    self._reqid_ts = now
                    return rid
        except Exception:
            pass
        # 兜底：旧缓存仍可用则复用
        if self._reqid:
            return self._reqid
        return ""

    def _refresh_api(self):
        """动态发现 API 域（绕过封锁换域）"""
        try:
            from ..fetcher import Fetcher
            f = Fetcher()
            st = f._session_for(None)
            resp = st.session.get("https://api.copy-manga.com/api/v3/system/network2?platform=3",
                                  headers=self.headers, timeout=10)
            data = resp.json()
            api = data.get("results", {}).get("api", [[None]])[0][0]
            if api:
                self._api = api.replace("https://", "").rstrip("/")
                self._save_state()
                print(f"[copymanga] API 域更新: {self._api}", flush=True)
        except Exception as e:
            print(f"[copymanga] refreshAppApi 失败: {e}", flush=True)

    def _ensure_api(self):
        """首次真正要用 APP 通道时才刷一次 API 域（有持久化状态就不刷）"""
        if self._api_refreshed or self._had_state:
            self._api_refreshed = True
            return
        self._api_refreshed = True
        try:
            self._refresh_api()
        except Exception:
            pass

    def _ver(self):
        """APP 版本号（懒取 + 类级 1h 缓存；网页通道不触发）"""
        if self._version is None:
            self._version = _fetch_latest_version()
        return self._version

    @property
    def api_url(self):
        self._ensure_api()
        return f"https://{self._api}"

    # ── 通道调度：网页通道优先，失败回落 APP 通道 ──────────────────────
    def _via_web(self, op, webfn, appfn):
        """先试网页通道；失败按 transport 模式决定回落还是抛错。

        - 日志里必须一眼看出这次走的哪条通道（排查"到底谁在请求"的唯一依据）
        - `web` 模式不回落：调用方明确要求网页通道时，偷偷碰 APP 接口才是错的
          （APP 请求正是 210 风控的来源，静默换通道=把风控重新引回来）
        - **被明确拦截（403 / Cloudflare 挑战）也不回落**：网页通道被拦说明这个 IP
          已经被盯上，此时再去敲 APP 接口，等于把 R59"不再出现 APP 请求特征"的
          约定重新破坏掉，而且只会让风控更重。回落只用于"网页通道自己出了网络/
          解析问题"这一类——那时 APP 通道确实可能是另一条活路。
        """
        mode = _transport_mode()
        if mode == "app":
            print(f"[copymanga] {op}：transport=app 强制 APP 通道（网页通道不参与）",
                  flush=True)
            return appfn()
        try:
            r = webfn()
        except copy_web.WebUnreachable as e:
            # **网络不通时不回落**：APP 主机与网页主机同属一个站点家族，
            # 网页域全不可达时 APP 域大概率也不可达；而 APP 通道是
            # "7 个域 × 重试 3 次 × 15 秒" 的长链（实测能把一次搜索拖到十几分钟，
            # App 的 30 秒客户端超时先炸 → 用户看到"所有源都超时"）。
            print(f"[copymanga] {op}：网页通道不可达（{str(e)[:80]}），"
                  f"不回落 APP（同家族域，回落只会长时间占住搜索）", flush=True)
            raise MangaError(
                f"{op}失败：连不上拷贝漫画站点（{str(e)[:120]}）。"
                f"请检查网络能否访问 www.copy4000.com") from e
        except copy_web.WebBlocked as e:
            print(f"[copymanga] {op}：网页通道被拦截（{e}），不回落 APP"
                  f"（避免把风控引到 APP 通道）", flush=True)
            raise MangaError(f"{op} 网页通道被源站拦截: {e}") from e
        except Exception as e:
            if mode == "web":
                print(f"[copymanga] {op}：transport=web 网页通道失败({e})，不回落",
                      flush=True)
                raise MangaError(f"{op} 网页通道失败: {e}") from e
            print(f"[copymanga] {op}：transport=auto 网页通道失败"
                  f"({type(e).__name__}: {e})，回落 APP 通道", flush=True)
        else:
            print(f"[copymanga] {op}：transport={mode} 走网页通道(copy_web) 成功",
                  flush=True)
            return r
        return appfn()

    # ── 搜索 ──
    def search(self, keyword, page=1):
        from ..config import MANGA_PAGE_SIZE as _PS
        try:
            _pg = max(1, int(page))
        except (TypeError, ValueError):
            _pg = 1
        return self._via_web(
            "搜索",
            lambda: self._web_search(keyword, _pg, _PS),
            lambda: self._app_search(keyword, _pg))

    def _web_search(self, keyword, page=1, page_size=None):
        """网页通道搜索（limit/offset 分页；total 由 copy_web 写进每条 Comic）"""
        from ..config import MANGA_PAGE_SIZE as _PS
        _ps = int(page_size or _PS)
        comics, _total = copy_web.web_search(
            keyword, limit=_ps, offset=(max(1, int(page)) - 1) * _ps,
            source_key=self.key)
        print(f"[copymanga] 网页搜索 '{keyword}' p{page}: {len(comics)}/{_total}",
              flush=True)
        return comics

    def _app_search(self, keyword, page=1):
        """APP 通道搜索（原实现，行为与 210 逻辑一字未改）"""
        from ..config import MANGA_PAGE_SIZE as _PS
        kw = urllib.parse.quote(keyword)
        try:
            _pg = max(1, int(page))
        except (TypeError, ValueError):
            _pg = 1
        url = (f"{self.api_url}/api/v3/search/comic?limit={_PS}"
               f"&offset={(_pg - 1) * _PS}&q={kw}&q_type=")
        try:
            data = self._get(url)
        except Exception as e:
            raise MangaError(f"搜索失败: {e}") from e
        comics = []
        _total = int((data.get("results") or {}).get("total") or 0)
        for item in (data.get("results") or {}).get("list", []):
            c = item.get("comic") or item
            if not c or not c.get("path_word"):
                continue
            tags = [t.get("name", "") for t in (c.get("theme") or []) if t.get("name")]
            author = ""
            if isinstance(c.get("author"), list) and c["author"]:
                author = c["author"][0].get("name", "")
            comics.append(Comic(
                id=c["path_word"], title=c.get("name", ""), author=author,
                cover=c.get("cover", ""), tags=tags, url=c.get("path_word", ""),
                source_key=self.key, total=_total))
        return comics

    # ── 详情 ──
    def comic_info(self, comic_id):
        return self._via_web(
            "详情",
            lambda: self._web_comic_info(comic_id),
            lambda: self._app_comic_info(comic_id))

    def _web_comic_info(self, comic_id):
        """网页通道详情：详情页 HTML + 章节接口（页内 ccz 解密）"""
        d = copy_web.web_detail(comic_id)
        # sub_title 契约与 APP 通道一致（都用适配器显示名），不跟着网页标题走
        d.sub_title = self.name
        d.chapters = copy_web.web_chapters(comic_id)
        print(f"[copymanga] 网页详情 {comic_id}: {d.title} 章节 {len(d.chapters)}",
              flush=True)
        return d

    def _app_comic_info(self, comic_id):
        """APP 通道详情（原实现，行为与 210 逻辑一字未改）"""
        # 详情：comic2 + request_id + in_mainland
        rid = self._get_request_id()
        url = (f"{self.api_url}/api/v3/comic2/{comic_id}"
               f"?in_mainland=true&request_id={rid}&platform=3")
        data = self._get(url)
        results = data.get("results", {}) or {}
        comic = results.get("comic", {}) or {}
        groups = results.get("groups", {}) or {}  # {组名: {name, path_word}}
        authors = comic.get("author", [])
        author = authors[0].get("name", "") if authors else ""
        tags = [t.get("name", "") for t in (comic.get("theme") or [])]
        ch_list = []
        # 章节：按分组拉取（limit=100 分页），分组参数用 path_word
        for gname, ginfo in (groups or {}).items():
            path_word = (ginfo or {}).get("path_word", "")
            if not path_word:
                continue
            offset = 0
            while True:
                rid = self._get_request_id()
                # limit=100（实测 limit=500 返回 total=0/list=0 空数据，
                # 拷贝漫画 API 单页上限 100；分页拉全同样完整）
                cu = (f"{self.api_url}/api/v3/comic/{comic_id}/group/{path_word}/chapters"
                      f"?limit=100&offset={offset}&in_mainland=true&request_id={rid}")
                try:
                    cd = self._get(cu)
                except Exception:
                    break
                total = (cd.get("results") or {}).get("total", 0)
                for ch in (cd.get("results") or {}).get("list", []):
                    ch_list.append(Chapter(
                        id=ch.get("uuid", ch.get("id", "")),
                        name=ch.get("name", ""), group=ginfo.get("name", gname),
                        url=ch.get("id", "")))
                offset += 100
                if offset >= (total or 0):
                    break
        # 正序（拷贝漫画接口返回倒序）
        ch_list.reverse()
        # 相关推荐（comic2 响应可能含 recommend 字段；无则空列表）
        recs = []
        for item in (comic.get("recommend") or [])[:8]:
            rid_ = item.get("path_word") or item.get("id") or ""
            if rid_ and rid_ != comic_id:
                recs.append(Comic(
                    id=rid_, title=item.get("name", ""),
                    cover=item.get("cover", ""), source_key=self.key))
        return ComicDetails(
            id=comic_id, title=comic.get("name", ""),
            cover=comic.get("cover", ""),
            sub_title=self.name,
            description=comic.get("brief", ""),
            author=author, tags=tags,
            upload_time=comic.get("datetime_updated", ""),
            update_time=comic.get("datetime_updated", ""),
            views=str(comic.get("popular", "")),
            likes=str(comic.get("likes_count", "")),
            url=comic.get("path_word", ""),
            chapters=ch_list, recommend=recs)

    def fetch_chapters(self, comic_id):
        """章节列表：网页通道优先，失败回落 APP 通道"""
        return self._via_web(
            "章节列表",
            lambda: self._web_fetch_chapters(comic_id),
            lambda: self._app_fetch_chapters(comic_id))

    def chapters(self, comic_id):
        """基类入口别名（部分调用方用 chapters()）"""
        return self.fetch_chapters(comic_id)

    def _web_fetch_chapters(self, comic_id):
        """网页通道章节：id 用章节 uuid；组名优先 groups[g].name"""
        chs = copy_web.web_chapters(comic_id)
        print(f"[copymanga] 网页章节 {comic_id}: {len(chs)} 话"
              f"（分组: {sorted({c.group for c in chs if c.group})}）", flush=True)
        return chs

    def _app_fetch_chapters(self, comic_id):
        """仅拉章节列表（不依赖 comic2 详情；comic2 210 时章节接口仍可用）"""
        ch_list = []
        try:
            rid = self._get_request_id()
            url = (f"{self.api_url}/api/v3/comic2/{comic_id}"
                   f"?in_mainland=true&request_id={rid}&platform=3")
            data = self._get(url)
            groups = (data.get("results") or {}).get("groups", {}) or {}
        except Exception:
            groups = {"default": {"path_word": "default", "name": "默認"}}
        for gname, ginfo in (groups or {}).items():
            path_word = (ginfo or {}).get("path_word", "")
            if not path_word:
                continue
            offset = 0
            while True:
                rid = self._get_request_id()
                cu = (f"{self.api_url}/api/v3/comic/{comic_id}/group/{path_word}/chapters"
                      f"?limit=100&offset={offset}&in_mainland=true&request_id={rid}")
                try:
                    cd = self._get(cu)
                except Exception:
                    break
                total = (cd.get("results") or {}).get("total", 0)
                for ch in (cd.get("results") or {}).get("list", []):
                    ch_list.append(Chapter(
                        id=ch.get("uuid", ch.get("id", "")),
                        name=ch.get("name", ""), group=ginfo.get("name", gname),
                        url=ch.get("id", "")))
                offset += 100
                if offset >= (total or 0):
                    break
        ch_list.reverse()
        return ch_list

    # ── 章节图片 ──
    def images(self, comic_id, chapter_id):
        """章节图片：网页通道优先，失败回落 APP 通道"""
        return self._via_web(
            "章节图片",
            lambda: self._web_images(comic_id, chapter_id),
            lambda: self._app_images(comic_id, chapter_id))

    def _web_images(self, comic_id, chapter_id):
        """网页通道：章节页内联 contentKey（AES，key=页内 cct）解出图片直链"""
        imgs = copy_web.web_images(comic_id, chapter_id)
        print(f"[copymanga] 网页图片 {comic_id}/{chapter_id}: {len(imgs)} 张",
              flush=True)
        return imgs

    def _app_images(self, comic_id, chapter_id):
        """APP 通道图片（原实现，行为与 210 逻辑一字未改）"""
        rid = self._get_request_id()
        url = (f"{self.api_url}/api/v3/comic/{comic_id}/chapter2/{chapter_id}"
               f"?in_mainland=true&request_id={rid}")
        data = self._get(url)
        chapter = (data.get("results") or {}).get("chapter", {}) or {}
        contents = chapter.get("contents", [])
        words = chapter.get("words", [])
        urls = [c.get("url", "") for c in contents]
        # 画质替换：c\d+x → c1500x.webp
        urls = [re.sub(r"([./])c\d+x\.[a-zA-Z]+$", r"\1c1500x.webp", u) for u in urls]
        # 乱序重排（words 为原始顺序）
        ordered = [""] * len(urls)
        for i, pos in enumerate(words):
            if 0 <= pos < len(ordered):
                ordered[pos] = urls[i]
        return [u for u in ordered if u]

    def image_headers(self, image_url):
        # 图片防盗链：Referer 用 API 域（拷贝漫画图片校验 referer）
        return {
            "User-Agent": f"COPY/{self._ver()}",
            "Referer": f"https://{self._api}/",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }


ADAPTERS = {"copymanga": CopyManga}
