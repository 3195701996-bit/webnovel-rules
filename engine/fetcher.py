#!/usr/bin/env python3
"""HTTP 请求层 v2：curl_cffi（Chrome TLS 指纹）+ cloudscraper 双引擎

反爬/稳定性增强（相对 v1）：
- 双引擎：默认 curl_cffi 模拟 Chrome TLS 指纹（cloudscraper 基于 requests，易被识别），
  失败/未安装时自动降级 cloudscraper（可过 JS challenge）。
- UA 池轮换：真实现代浏览器 UA 池，按会话随机选取；被封锁时换 UA 重建会话。
- 封锁检测：HTTP 403/412/418/429/503 + 挑战页特征（CF/安全验证/验证码页）识别，
  命中即触发退避 + 自适应限速，而不是傻重试同一指纹。
- 智能重试：尊重 Retry-After 头；指数退避 + 随机抖动；连续被封锁自动重建会话
  （全新 TLS 会话 + Cookie + UA）。
- 自适应限速：每域名独立健康状态——被封锁时限速间隔翻倍（封顶 15s），
  连续成功逐步恢复到基准（AIMD）。
- 代理支持：书源字段 proxy / 环境变量 WR_PROXY / data/proxies.txt 代理池轮换。
"""
import os
import re
import json
import time
import random
import atexit
import threading
from urllib.parse import urlsplit, urljoin, urlparse

from .urlsec import (MAX_REDIRECTS, SSRFBlocked, proxy_is_safe,
                     url_is_public_resolved, pin_curl_session)
from .config import RATE_LIMIT_N as _CFG_RATE_N, RATE_LIMIT_MS as _CFG_RATE_MS
from .app_utils import atomic_write_text as _atomic_write_text


class DeadlineExceeded(TimeoutError):
    """业务级总时限耗尽，避免重试链继续占用请求线程。"""

import cloudscraper

# curl_cffi：Chrome TLS 指纹模拟（优先引擎）
try:
    from curl_cffi import requests as _curl_requests
    _HAS_CURL = True
except Exception:
    _HAS_CURL = False

# ── 真实浏览器 UA 池（定期更新，均为现网真实存在的高占比 UA）──
_UA_DESKTOP = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) '
    'Gecko/20100101 Firefox/127.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
    '(KHTML, like Gecko) Version/17.5 Safari/605.1.15',
]
_UA_MOBILE = [
    'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 '
    '(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36',
]

# 挑战页/反爬页面特征（仅对短页面判定，避免误伤正文里出现"验证码"的小说页）
# 强特征：完整挑战页（CF Turnstile/DDOS-Guard 等），正文被拦截
_CHALLENGE_MARKERS_STRONG = (
    'cf-chl', 'cf-mitigated', 'just a moment', 'attention required',
    '安全验证', '访问验证', '请输入验证码', '滑动验证', '点击验证',
    'ddos-guard', 'checking your browser', 'verify you are human',
    'security check', 'please complete the security check',
)
# 弱特征：CF 被动注入脚本（页面正文通常仍完整可读，如 ibiquxs 正文页
# 仅注入 challenge-platform js，内容正常）→ 仅极短页面判挑战
_CHALLENGE_MARKERS_WEAK = (
    'challenge-platform', '_cf_chl_opt', 'jsl_clearance', 'jsluid',
)
_CHALLENGE_MAX_LEN = 40 * 1024   # 只对 <40KB 的页面做挑战检测
_WEAK_MAX_LEN = 4 * 1024         # 弱特征仅在页面 <4KB 时判定（被动注入页通常更大）

# 被封锁视为可重试的状态码
_BLOCK_STATUS = {403, 412, 418, 429, 500, 502, 503, 504}
# R48: HTTP 反爬封锁(直连换代理即可尝试绕过的完整挑战类)
_HTTP_ANTI_STATUS = {403, 412, 418, 429}


class _PoolEntry:
    """池成员：一个 session + 独立锁。

    curl_cffi Session 非线程安全，借出期间必须独占（对齐漫画域
    engine/manga/downloader.py:91-122 的图片 Session 池模式）。"""
    __slots__ = ('session', 'lock')

    def __init__(self, session):
        self.session = session
        self.lock = threading.Lock()


class _SessionLease:
    """持有一个池成员的请求租约：独占该成员锁，并阻止重建线程提前关闭它。"""
    def __init__(self, state, entry):
        self._state = state
        self._entry = entry
        self.session = entry.session
        self._released = False

    def release(self):
        if self._released:
            return
        self._released = True
        old = None
        with self._state.session_lock:
            key = id(self.session)
            count = self._state.session_users.get(key, 0) - 1
            if count <= 0:
                self._state.session_users.pop(key, None)
                if key in self._state.retired_sessions:
                    self._state.retired_sessions.remove(key)
                    old = self.session
            else:
                self._state.session_users[key] = count
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        # 独占锁最后释放：保证 close 退休 session 不与其在途请求并发
        self._entry.lock.release()


class _HostState:
    """每域名健康状态：Session 池 + 自适应限速 + 封锁计数。"""
    __slots__ = ('pool', 'pool_rr', 'backend', 'ua', 'sem', 'rate_win',
                 'rate_n', 'rate_ms', 'interval_mul', 'success_streak',
                 'block_streak', 'net_streak', 'proxy', 'session_lock',
                 'session_cond', 'session_users', 'retired_sessions',
                 'session_generation', 'rebuild_lock')

    def __init__(self, sem_n):
        self.pool = []            # [_PoolEntry]，按配置大小懒构建
        self.pool_rr = 0          # 借出轮询游标
        self.backend = None       # 'curl' | 'cloud'
        self.ua = None
        self.sem = threading.BoundedSemaphore(sem_n)
        self.rate_win = []        # 滑动窗口时间戳
        self.rate_n = 0           # 0 → 用全局默认
        self.rate_ms = 0
        self.interval_mul = 1.0   # 自适应限速倍率（1.0 = 基准）
        self.success_streak = 0
        self.block_streak = 0
        self.net_streak = 0       # 连续网络错误（连接重置/SSL 中断常是 IP 级封锁）
        self.proxy = None
        self.session_lock = threading.RLock()
        self.session_cond = threading.Condition(self.session_lock)
        self.session_users = {}
        self.retired_sessions = set()
        self.session_generation = 0
        self.rebuild_lock = threading.Lock()

    # 兼容旧接口（测试 / manga.copymanga 直读 st.session）：池首成员的
    # session；赋值等价于把池重置为单成员池。
    @property
    def session(self):
        return self.pool[0].session if self.pool else None

    @session.setter
    def session(self, value):
        self.pool = [] if value is None else [_PoolEntry(value)]



class Fetcher:
    """HTTP 客户端。按站点域名复用会话（keep-alive），每域名独立限流/健康状态。"""

    _states = {}            # host -> _HostState
    _state_lock = threading.Lock()
    _proxy_pool = None      # data/proxies.txt 缓存 [(proxy_str, ts)]
    _proxy_pool_ts = 0.0
    _proxy_pool_lock = threading.Lock()
    _proxy_fails = {}       # proxy → 连续失败次数（超阈值临时停用）
    _proxy_latency = {}     # proxy → 平滑延迟（秒），快代理优先
    _proxy_good = []        # proxy → 最近成功代理（进程内记忆，跨请求优先）
    _direct_fail_hosts = {} # host → 直连失败时间（仅此类域名走代理池）
    # P3-10: proxy_good.txt / proxies.txt 写盘串行化锁（配原子写，防并发截断）
    _proxy_file_lock = threading.Lock()
    # 代理健康字典/列表的读改写锁（_proxy_fails/_proxy_latency/_proxy_good）：
    # 多域名线程并发下 += / check-then-act 无锁会丢更新、重复追加
    _proxy_state_lock = threading.Lock()

    # 默认每域名限流：窗口 1000ms 内最多 6 次
    # R47: 值统一取自 engine.config（此前类属性与 config 双份定义，互不引用）
    RATE_LIMIT_N = _CFG_RATE_N
    RATE_LIMIT_MS = _CFG_RATE_MS
    # 自适应限速：被封锁倍率 ×2（封顶 15s 间隔），连续成功 3 次减半恢复
    INTERVAL_MUL_MAX = 30.0
    RECOVER_AFTER = 3
    # 连续被封锁 N 次 → 重建会话（全新 TLS/Cookie/UA）
    REBUILD_AFTER_BLOCKS = 2

    def __init__(self):
        self._default = None   # 兼容旧接口：fetcher.session（autogen 等使用）

    # ── 兼容旧接口 ──
    @property
    def session(self):
        """旧接口 fetcher.session：默认 cloudscraper 会话（autogen 低频使用）"""
        with self._state_lock:
            if self._default is None:
                self._default = cloudscraper.create_scraper(
                    browser={'browser': 'chrome', 'platform': 'windows',
                             'mobile': False})
            return self._default

    # ── 每域名状态 ──
    @classmethod
    def _host_concurrency(cls):
        try:
            from .config import FETCH_HOST_CONCURRENCY
            return max(1, int(FETCH_HOST_CONCURRENCY))
        except Exception:
            return 4

    @classmethod
    def _state_for(cls, host):
        with cls._state_lock:
            st = cls._states.get(host)
            if st is None:
                st = _HostState(cls._host_concurrency())
                cls._states[host] = st
            return st

    @staticmethod
    def _host_of(source):
        u = (source or {}).get('bookSourceUrl') or ''
        u = re.sub(r'##.*$', '', u).strip()
        try:
            return urlparse(u).netloc or 'default'
        except Exception:
            return 'default'

    # ── 会话构建 ──
    @classmethod
    def _pick_ua(cls, source):
        """按站点 URL 判断移动端/桌面端，随机取 UA；书源 header 自带 UA 时优先"""
        h = Fetcher().parse_header(source)
        if h.get('User-Agent') or h.get('user-agent'):
            return h.get('User-Agent') or h.get('user-agent')
        url = (source or {}).get('bookSourceUrl') or ''
        # 主机名判断必须走 urlparse：原正则 `(^|\.)m\.` 匹配不到
        # "https://m.jhsssd.com"（m 前面是 / 而不是 . 或串首），于是移动站被当成
        # 桌面站发桌面 UA —— 实测该站在桌面 UA 下只回 1.2KB 的壳页（无书单），
        # 移动 UA 才回完整 14.9KB（书单齐全），表现为"搜索无结果/榜单 0 条"。
        host = ''
        try:
            host = urlsplit(url).hostname or ''
        except Exception:
            host = ''
        if (host.startswith('m.') or host.startswith('wap.') or host.startswith('mobile.')
                or re.search(r'/m/|wap|mobile', url, re.I)):
            return random.choice(_UA_MOBILE)
        return random.choice(_UA_DESKTOP + _UA_MOBILE[:1])

    @classmethod
    def _pick_proxy(cls, source):
        """代理优先级：书源 proxy 字段 > WR_PROXY > 代理池（仅直连失败过的域名走池，
        避免正常源被慢代理拖累）"""
        p = (source or {}).get('proxy')
        # R35b(SSRF): 书源 proxy 字段用户可控。代理模式下真实 TCP 连接建立到
        # 代理地址，target URL 校验形同虚设——内网代理可用于端口探测。
        if p and not proxy_is_safe(p):
            print(f"[fetcher] ⛔ 书源 proxy 指向非公网地址，已忽略: "
                  f"{str(p)[:80]}", flush=True)
            p = None
        if isinstance(p, dict):
            return p
        if isinstance(p, str) and p.strip():
            return {'http': p.strip(), 'https': p.strip()}
        env_p = os.environ.get('WR_PROXY', '').strip()
        if env_p:
            return {'http': env_p, 'https': env_p}
        # 仅直连失败过的域名（IP级封锁）才用代理池
        host = cls._host_of(source)
        if host not in cls._direct_fail_hosts:
            return None
        pool = cls._load_proxy_pool()
        if pool:
            # 成功代理优先：进程内记忆 + 跨进程持久化文件（对挑战站有效的好代理）
            try:
                from .config import DATA_DIR
                gpath = os.path.join(DATA_DIR, 'proxy_good.txt')
                with open(gpath, encoding='utf-8') as _f:
                    gs = [l.strip() for l in _f if l.strip()]
            except Exception:
                gs = []
            with cls._proxy_state_lock:
                _good_mem = list(cls._proxy_good)
                _fails = dict(cls._proxy_fails)
                # P2-3: 延迟表在同一把锁内取快照——此前在排序 key 回调里
                # 直接读 cls._proxy_latency，与其它线程写入并发读到中间态
                _lat = dict(cls._proxy_latency)
            good = [pr for pr in (_good_mem + gs)
                    if pr in pool and _fails.get(pr, 0) < 3]
            # 排除近期连续失败 ≥3 次的代理（临时停用），避免并发撞死代理
            candidates = [pr for pr in pool if _fails.get(pr, 0) < 3]
            if not candidates:
                candidates = pool  # 全停用则重置重试
            # 快代理优先：按历史延迟排序，前 1/3 加权（快代理被选概率高）
            if _lat:
                candidates.sort(key=lambda pr: _lat.get(pr, 5.0))
                fast = candidates[:max(1, len(candidates) // 3)]
                # good 代理（若有）并入快代理候选池，加大选中概率
                for g in good:
                    if g not in fast:
                        fast.append(g)
                pick = random.choice(fast)
            elif good:
                pick = random.choice(good)
            else:
                pick = random.choice(candidates)
            # http/https 用同一代理（避免 requests 走两个不同代理）
            return {'http': pick, 'https': pick}
        return None

    @classmethod
    def _record_latency(cls, proxy, seconds):
        """记录代理延迟（滑动平均），用于快代理优先"""
        if not proxy:
            return
        # P2-3: http/https 为同一代理，按值去重后再记——否则一次事件记两次，
        # 滑动平均被重复加权（等价 0.3 → 0.51），偏离真实延迟
        seen = set()
        for key in ('http', 'https'):
            pr = proxy.get(key)
            if pr and pr not in seen:
                seen.add(pr)
                with cls._proxy_state_lock:
                    old = cls._proxy_latency.get(pr)
                    cls._proxy_latency[pr] = seconds if old is None \
                        else old * 0.7 + seconds * 0.3

    @classmethod
    def _mark_direct_failed(cls, host):
        """标记某域名直连失败（下次起走代理池）"""
        cls._direct_fail_hosts[host] = time.time()

    @classmethod
    def _proxy_failed(cls, proxy):
        """标记代理失败（连接级），连续失败自动停用"""
        if not proxy:
            return
        # P2-3: http/https 同一代理按值去重——否则一次连接失败计数 +2，
        # 停用阈值 3 实际 2 次即触发，误杀可用代理
        seen = set()
        for key in ('http', 'https'):
            pr = proxy.get(key)
            if pr and pr not in seen:
                seen.add(pr)
                with cls._proxy_state_lock:
                    cls._proxy_fails[pr] = cls._proxy_fails.get(pr, 0) + 1

    @classmethod
    def _proxy_ok(cls, proxy):
        """标记代理成功，清零失败计数；并记入"成功代理"优先列表（跨请求）"""
        if not proxy:
            return
        # P2-3: http/https 同一代理按值去重——否则 good 列表重复入队、
        # 失败计数重复清零，12 条上限被同址挤占
        seen = set()
        for key in ('http', 'https'):
            pr = proxy.get(key)
            if not pr or pr in seen:
                continue
            seen.add(pr)
            with cls._proxy_state_lock:
                if cls._proxy_fails.get(pr, 0):
                    cls._proxy_fails[pr] = 0
                # 记入 good 列表（进程内），不重复
                if pr not in cls._proxy_good:
                    cls._proxy_good.append(pr)
                    if len(cls._proxy_good) > 12:
                        cls._proxy_good.pop(0)
            # 持久化到 data/proxy_good.txt（跨进程记忆：好代理对挑战站有效）
            # P3-10: 读-改-写全程持锁 + 原子替换（tmp+os.replace），
            # 多域名线程并发不再写出截断/丢失更新的文件
            try:
                from .config import DATA_DIR
                gpath = os.path.join(DATA_DIR, 'proxy_good.txt')
                with cls._proxy_file_lock:
                    try:
                        with open(gpath, encoding='utf-8') as _f:
                            gs = [l.strip() for l in _f if l.strip()]
                    except Exception:
                        gs = []
                    if pr not in gs:
                        gs.append(pr)
                        _atomic_write_text(gpath, '\n'.join(gs[-20:]) + '\n')
            except Exception:
                pass

    @classmethod
    def _load_proxy_pool(cls):
        """data/proxies.txt：一行一个 http://ip:port，60s 缓存。
        为空时自动从公共代理 API 拉取（封锁源用）。"""
        with cls._proxy_pool_lock:
            if cls._proxy_pool is not None and \
                    time.time() - cls._proxy_pool_ts < 60:
                return cls._proxy_pool
            try:
                from .config import DATA_DIR
                path = os.path.join(DATA_DIR, 'proxies.txt')
                pool = []
                if os.path.exists(path):
                    with open(path, encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith('#'):
                                if '://' not in line:
                                    line = 'http://' + line
                                pool.append(line)
                # 代理池为空 → 自动从公共 API 拉取（写回文件供持久化）
                if not pool:
                    pool = cls._fetch_public_proxies(path)
                cls._proxy_pool = pool
                cls._proxy_pool_ts = time.time()
                return pool
            except Exception:
                cls._proxy_pool = []
                cls._proxy_pool_ts = time.time()
                return []

    # 多源公共代理 API（提升池子多样性与存活率）
    _PUBLIC_PROXY_APIS = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=yes",
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=5000&country=all",
        "https://api.proxyscrape.com/v3/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=yes",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    ]

    @classmethod
    def _fetch_public_proxies(cls, path=None, limit=150):
        """从公共代理 API 拉取代理列表；并验证存活（只保留能访问外网的），
        写回 proxies.txt 供复用。"""
        import requests as _rq
        from concurrent.futures import ThreadPoolExecutor, as_completed
        raw = []
        seen = set()
        for api in cls._PUBLIC_PROXY_APIS:
            try:
                r = _rq.get(api, timeout=8)
                text = r.text.replace('\r', '\n')
                for line in text.split('\n'):
                    p = line.strip()
                    if not p or ':' not in p:
                        continue
                    # 兼容带协议前缀的格式（http://ip:port）
                    if '://' in p:
                        p = p.split('://', 1)[1]
                    # 兼容 ip:port:user:pass / ip:port 多列格式（只取 ip:port）
                    p = p.split(':')
                    if len(p) >= 2 and p[0] and p[1].isdigit():
                        p = f"{p[0]}:{p[1]}"
                    else:
                        continue
                    if p in seen:
                        continue
                    seen.add(p)
                    if len(raw) < limit * 3:
                        raw.append('http://' + p)
            except Exception:
                continue
        # 验证存活：并发测一个轻量可达站点（仅保留响应正常且非空页）
        def _alive(p):
            try:
                r = _rq.get("http://www.gstatic.com/generate_204", timeout=5,
                            proxies={"http": p, "https": p})
                return p if r.status_code == 204 else None
            except Exception:
                return None
        found = []
        with ThreadPoolExecutor(max_workers=30) as ex:
            futs = {ex.submit(_alive, p): p for p in raw[:limit * 2]}
            for f in as_completed(futs, timeout=30):
                p = f.result()
                if p and len(found) < limit:
                    found.append(p)
        if found and path:
            try:
                # P3-10: 持锁 + 原子写回 proxies.txt（与 proxy_good.txt 同一把锁）
                with cls._proxy_file_lock:
                    _atomic_write_text(path, '\n'.join(found) + '\n')
            except Exception:
                pass
        return found

    @classmethod
    def _build_session(cls, source, prefer=None):
        """构建新会话：curl_cffi 优先（TLS 指纹），失败降级 cloudscraper。
        prefer='cloud' 可强制 cloudscraper（curl 引擎被连接级封锁时切换）"""
        ua = cls._pick_ua(source)
        proxy = cls._pick_proxy(source)
        if _HAS_CURL and prefer != 'cloud':
            try:
                sess = _curl_requests.Session()
                sess.impersonate = _pick_impersonate()
                sess.headers['User-Agent'] = ua
                if proxy:
                    sess.proxies.update(proxy)
                return sess, 'curl', ua, proxy
            except Exception:
                pass
        sess = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})
        sess.headers['User-Agent'] = ua
        if proxy:
            sess.proxies.update(proxy)
        return sess, 'cloud', ua, proxy

    @classmethod
    def _session_pool_size(cls):
        """每域名 Session 池大小（engine.config.FETCH_SESSION_POOL_SIZE）。"""
        try:
            from .config import FETCH_SESSION_POOL_SIZE
            return max(1, int(FETCH_SESSION_POOL_SIZE))
        except Exception:
            return 3

    @classmethod
    def _ensure_pool_locked(cls, st, source):
        """st.session_lock 持有期间调用：池为空则按配置大小构建。

        池内成员各自独立构建（独立 TLS 会话/Cookie）；on-wire UA 由请求头
        统一覆盖为 st.ua，成员间 UA 差异不影响指纹一致性。
        cloudscraper/requests Session 线程安全，本可不池化；统一走同一池
        路径只为保持单一代码路径、并在 curl→cloud 降级后保留 keep-alive
        并行度（每成员锁对线程安全 session 只是瞬时借还，开销可忽略）。"""
        if st.pool:
            return
        first = cls._build_session(source)
        st.backend, st.ua, st.proxy = first[1], first[2], first[3]
        st.pool = [_PoolEntry(first[0])]
        for _ in range(cls._session_pool_size() - 1):
            try:
                s = cls._build_session(source)
            except Exception:
                break
            st.pool.append(_PoolEntry(s[0]))
        st.session_generation += 1

    @classmethod
    def _session_for(cls, source):
        host = cls._host_of(source)
        st = cls._state_for(host)
        with st.session_lock:
            cls._ensure_pool_locked(st, source)
        return st

    @classmethod
    def _lease_session(cls, st, source, deadline=None):
        """从池中借出一个 session 租约：独占该成员锁，重建只关闭无在途
        请求的旧 session（租约延迟关闭语义不变）。

        选择策略：轮询游标起找空闲（users==0）成员；全忙则取占用最少的
        成员并在其锁上排队——池 < 并发信号量时锁等待即自然排队。
        users 簿记在 session_lock 内完成后再等成员锁，避免与重建的
        retire/close 产生竞态，也避免持 session_lock 阻塞造成死锁。

        deadline（monotonic 绝对时刻）非空时成员锁等待纳入预算：分段
        acquire 到点即放弃，回滚 users 簿记后抛 DeadlineExceeded——排队
        请求不再于预算耗尽后发出迟到网络调用。回滚不在公共锁内等待
        （仅短时持 session_lock 改计数，与借出对称）。"""
        with st.session_lock:
            cls._ensure_pool_locked(st, source)
            entries = st.pool
            n = len(entries)
            start = st.pool_rr % n
            st.pool_rr += 1
            chosen = None
            chosen_u = 0
            for i in range(n):
                e = entries[(start + i) % n]
                u = st.session_users.get(id(e.session), 0)
                if u == 0:
                    chosen = e
                    break
                if chosen is None or u < chosen_u:
                    chosen, chosen_u = e, u
            key = id(chosen.session)
            st.session_users[key] = st.session_users.get(key, 0) + 1
        # 成员锁等待在 session_lock 外：不阻塞其他借还/重建的簿记
        if deadline is None:
            chosen.lock.acquire()
        else:
            acquired = False
            while not acquired:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                # 分段获取（≤50ms 粒度）：到点醒在 deadline 上
                acquired = chosen.lock.acquire(timeout=min(0.05, left))
            if not acquired:
                cls._rollback_lease_booking(st, chosen)
                raise DeadlineExceeded(
                    f"request deadline exceeded (session lease): "
                    f"{cls._host_of(source)}")
        return _SessionLease(st, chosen)

    @classmethod
    def _rollback_lease_booking(cls, st, entry):
        """租约等待超时/取消的簿记回滚：撤销 users 计数（借出的逆操作）。
        若该 session 在排队期间被重建退休且计数归零——没有任何租约会再
        release 它——按退休语义在此兜底关闭，避免泄漏句柄。"""
        old = None
        with st.session_lock:
            key = id(entry.session)
            count = st.session_users.get(key, 0) - 1
            if count <= 0:
                st.session_users.pop(key, None)
                if key in st.retired_sessions:
                    st.retired_sessions.discard(key)
                    old = entry.session
            else:
                st.session_users[key] = count
        if old is not None:
            try:
                old.close()
            except Exception:
                pass

    @classmethod
    def _rebuild_session(cls, host, source, reason=''):
        """被封锁后整池重建会话（全员换 TLS 指纹/Cookie/UA，封锁是域名级
        的，与单成员无关）；有在途请求的旧 session 租约延迟关闭。"""
        st = cls._state_for(host)
        with st.rebuild_lock:
            try:
                with st.session_lock:
                    old_backend = st.backend
                    old_proxy = st.proxy
                prefer = 'cloud' if old_backend == 'curl' else None
                # 重建时临时提升失败代理的计数（强制换代理，避免复用刚失败的）
                if old_proxy:
                    cls._proxy_failed(old_proxy)
                built = [cls._build_session(source, prefer=prefer)]
                for _ in range(cls._session_pool_size() - 1):
                    try:
                        built.append(cls._build_session(source, prefer=prefer))
                    except Exception:
                        break
                new_backend, new_ua, new_proxy = built[0][1], built[0][2], built[0][3]
                old_to_close = []
                with st.session_lock:
                    old_entries = st.pool
                    st.pool = [_PoolEntry(b[0]) for b in built]
                    st.backend = new_backend
                    st.ua = new_ua
                    st.proxy = new_proxy
                    st.session_generation += 1
                    for e in old_entries:
                        old_key = id(e.session)
                        if st.session_users.get(old_key, 0):
                            st.retired_sessions.add(old_key)
                        else:
                            st.session_users.pop(old_key, None)
                            old_to_close.append(e.session)
                for old in old_to_close:
                    try:
                        old.close()
                    except Exception:
                        pass
                print(f"[fetcher] {host} 会话池重建（{reason}，引擎={new_backend}，"
                      f"成员×{len(built)}）", flush=True)
            except Exception as e:
                print(f"[fetcher] {host} 会话池重建失败: {e}", flush=True)

    @classmethod
    def close_all(cls):
        """进程退出/域名清理：关闭全部域名池内会话（尽力而为）。
        退休（retired）会话由持有租约的请求线程在 release 时关闭。"""
        with cls._state_lock:
            states = list(cls._states.values())
            cls._states.clear()
        for st in states:
            with st.session_lock:
                entries = st.pool
                st.pool = []
            for e in entries:
                try:
                    e.session.close()
                except Exception:
                    pass

    # ── 限流（滑动窗口 + 自适应倍率）──
    @staticmethod
    def _budget_sleep(seconds, deadline, what='wait'):
        """可被打断的分段等待：deadline（monotonic 绝对时刻）到期立即抛
        DeadlineExceeded，不做超出预算的迟到等待。deadline 为空退化为整段
        sleep（无预算调用方行为不变）。"""
        if seconds <= 0:
            return
        if deadline is None:
            time.sleep(seconds)
            return
        end = time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise DeadlineExceeded(f"budget exhausted during {what}")
            rem = end - time.monotonic()
            if rem <= 0:
                return
            # 分段（≤50ms 粒度）：到点醒在 deadline 上，而非睡过整个等待
            time.sleep(min(0.05, rem, left))

    @classmethod
    def _throttle(cls, st, host, n=None, ms=None, deadline=None):
        """窗口 ms 内最多 n 次；自适应倍率：被封锁后 interval 翻倍，成功后恢复。
        deadline 非空时节流等待纳入预算：到期抛 DeadlineExceeded 而不是
        睡满窗口后再发一个迟到请求。"""
        n = n or cls.RATE_LIMIT_N
        ms = ms or cls.RATE_LIMIT_MS
        window = (ms / 1000.0) * st.interval_mul
        now = time.time()
        with cls._state_lock:
            win = st.rate_win
            cutoff = now - window
            win[:] = [t for t in win if t > cutoff]
            if len(win) >= n:
                wait = win[0] + window - now
                if wait > 0:
                    win.pop(0)
                    win.append(now + wait)
                    _wait = wait
                else:
                    win.append(now)
                    _wait = 0.0
            else:
                win.append(now)
                _wait = 0.0
        if _wait > 0:
            cls._budget_sleep(_wait, deadline, what=f'throttle {host}')

    # ── 健康反馈 ──
    @classmethod
    def _on_success(cls, st):
        st.success_streak += 1
        st.block_streak = 0
        st.net_streak = 0
        if st.interval_mul > 1.0 and st.success_streak >= cls.RECOVER_AFTER:
            st.interval_mul = max(1.0, st.interval_mul / 2)
            st.success_streak = 0

    @classmethod
    def _on_block(cls, st, status=403):
        st.block_streak += 1
        st.success_streak = 0
        # 5xx（500/502/503/504）多为源站瞬时故障而非 IP 封锁：
        # 惩罚温和（倍率 ×1.2，封顶 4），避免目录/正文大并发时被拖到 60s+ 级等待；
        # 4xx 反爬（403/412/418/429）才是真封锁 → 严厉惩罚（×2，封顶 30）
        if status in (500, 502, 503, 504):
            st.interval_mul = min(4.0, max(1.0, st.interval_mul * 1.2))
        else:
            st.interval_mul = min(cls.INTERVAL_MUL_MAX, max(2.0, st.interval_mul * 2))

    # ── 挑战页检测 ──
    @staticmethod
    def _looks_blocked(text):
        """挑战页判定：强特征（完整挑战页）命中即判；弱特征（CF 被动注入，
        正文通常仍完整）仅在极短页面判定，避免误伤 ibiquxs 类正文页"""
        if not text or len(text) > _CHALLENGE_MAX_LEN:
            return False
        low = text[:8192].lower()
        if any(m in low for m in _CHALLENGE_MARKERS_STRONG):
            return True
        if len(text) <= _WEAK_MAX_LEN:
            return any(m in low for m in _CHALLENGE_MARKERS_WEAK)
        return False

    # ── 公开 API（与 v1 兼容）──
    def parse_header(self, source):
        h = (source or {}).get('header') or ''
        if isinstance(h, dict):
            return h
        if isinstance(h, str) and h.strip():
            try:
                return json.loads(h)
            except Exception:
                return {}
        return {}

    def get(self, url, source=None, timeout=30, retries=3, extra_headers=None,
            deadline=None):
        return self._request('GET', url, source=source, timeout=timeout,
                             retries=retries, extra_headers=extra_headers,
                             deadline=deadline)

    def post(self, url, data=None, source=None, timeout=30, retries=3,
             extra_headers=None, deadline=None):
        return self._request('POST', url, data=data, source=source,
                             timeout=timeout, retries=retries,
                             extra_headers=extra_headers, deadline=deadline)

    # ── R35b SSRF 统一兜底 ──
    # 上层入口逐个打补丁不可靠：书源规则派生的 URL（tocUrl / nextTocUrl /
    # nextContentUrl / 响应内嵌 API）与运行期搜索路径都会绕过入口校验，
    # 且底层默认跟随 302 可让公网域名跳转到内网。Fetcher._request 是所有
    # 出站请求的唯一收敛点，故在此做统一校验 + 手动逐跳重定向校验。
    def _send_checked(self, sess, method, url, data, headers, timeout):
        """发送请求并对每一跳重定向做 SSRF 校验（禁用自动跟随）。"""
        cur = url
        for hop in range(MAX_REDIRECTS + 1):
            ok, why = url_is_public_resolved(cur)
            if not ok:
                where = "请求目标" if hop == 0 else f"第{hop}跳重定向"
                raise SSRFBlocked(f"{where}被拒绝: {why} ({cur[:120]})")
            # 连接层绑定（2026-09-10）：校验与实际建连使用同一份解析结果。
            # 校验后 DNS 被改写为内网时，连接仍发往已校验的公网 IP，
            # 消除 DNS 重绑定窗口；Host/SNI/证书校验仍按域名。
            # 2026-09-10 收敛（修复"绑定被跳过"的确证缺口）——pin 为**请求级
            # 实际调用**：
            #   · curl 会话：无论是否配置代理都写目标 RESOLVE（经代理建连时该
            #     条目不被使用、写入无害；且不改 CurlOpt.PROXY，专用代理协议仍
            #     走代理、绝不因此转直连）。避免 NO_PROXY 命中 / trust_env=False
            #     等"实际直连"被任意 env 代理错跳。
            #   · requests/cloudscraper 会话：按 requests 真实语义判定实际出站
            #     （URL scheme / no_proxy / trust_env / 显式代理），直连则挂同
            #     会话受控适配器把建连地址钉到已校验 IP（SNI/证书仍按域名），
            #     经代理则不钉（连接终点是代理，非直连）。
            # 失败语义与校验对齐：重解析到私网抛 SSRFBlocked（不发出请求）；解析
            # 失败抛 DNSResolveUnavailable；非 curl 无法建立受控绑定抛
            # PinUnavailable（可重试网络错误，绝不静默退回未绑定连接）。
            pin_curl_session(sess, cur)
            if method == 'POST' and hop == 0:
                resp = sess.post(cur, data=data, headers=headers,
                                 timeout=timeout, allow_redirects=False)
            else:
                # 303 及 301/302 对 POST 的实际行为均为转 GET
                resp = sess.get(cur, headers=headers, timeout=timeout,
                                allow_redirects=False)
            if resp.status_code not in (301, 302, 303, 307, 308):
                return resp
            loc = (resp.headers or {}).get('Location') \
                or (resp.headers or {}).get('location')
            if not loc:
                return resp
            cur = urljoin(cur, loc)
        raise SSRFBlocked(f"重定向次数超过上限({MAX_REDIRECTS}): {url[:120]}")

    # ── 请求主流程 ──
    def _request(self, method, url, data=None, source=None, timeout=30,
                 retries=3, extra_headers=None, deadline=None):
        """执行请求；deadline 为 time.monotonic() 的绝对截止时间。"""
        def _remaining():
            if deadline is None:
                return float(timeout)
            left = deadline - time.monotonic()
            if left <= 0:
                raise DeadlineExceeded(f"request deadline exceeded: {url}")
            return min(float(timeout), left)

        headers = self.parse_header(source)
        if extra_headers:
            headers.update(extra_headers)
        host = self._host_of(source)
        st = self._session_for(source)
        # UA：书源显式指定优先，否则用会话构建时选定的 UA（保持整会话指纹一致）
        if 'User-Agent' not in headers and 'user-agent' not in headers:
            headers['User-Agent'] = st.ua or random.choice(_UA_DESKTOP)
        # 浏览器常规头（部分站点校验 Accept / Accept-Language 缺失）
        headers.setdefault('Accept', 'text/html,application/xhtml+xml,'
                           'application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8')
        headers.setdefault('Accept-Language', 'zh-CN,zh;q=0.9,en;q=0.8')
        # R48: 不带 br(Brotli)——环境无 brotli 解码库,服务器若回 br 会得到乱码字节
        headers.setdefault('Accept-Encoding', 'gzip, deflate')
        # Referer：未指定时用站点首页（部分站校验防盗链）
        if 'Referer' not in headers and 'referer' not in headers:
            try:
                p = urlparse(url)
                if p.scheme and p.netloc:
                    headers['Referer'] = f'{p.scheme}://{p.netloc}/'
            except Exception:
                pass
        # 书源级 concurrentRate：'5/1000' → 每1000ms 5次；'500' → 每500ms 1次
        rl_n, rl_ms = self.RATE_LIMIT_N, self.RATE_LIMIT_MS
        try:
            cr = ((source or {}).get('concurrentRate') or '').strip()
            if cr and cr != '0':
                if '/' in cr:
                    _n, _ms = cr.split('/', 1)
                    rl_n, rl_ms = int(_n.strip()), int(_ms.strip())
                else:
                    rl_n, rl_ms = 1, int(cr.strip())
        except Exception:
            pass

        last = None
        for attempt in range(max(1, int(retries))):
            _attempt_t0 = time.time()
            try:
                self._throttle(st, host, n=rl_n, ms=rl_ms, deadline=deadline)
                # B01: 并发信号量获取纳入预算——池外排队不再无界等待；
                # 到期抛 DeadlineExceeded，不产生迟到的网络调用
                if deadline is None:
                    st.sem.acquire()
                else:
                    _sem_left = deadline - time.monotonic()
                    if _sem_left <= 0 or not st.sem.acquire(timeout=_sem_left):
                        raise DeadlineExceeded(
                            f"request deadline exceeded (host semaphore): {url}")
                try:
                    # 租约仅在 session 使用期间持有（含多跳重定向）；
                    # 拿到响应立即归还——退避 sleep/封锁判定期间不得占用
                    # 池成员锁，避免阻塞同域名其他请求
                    lease = self._lease_session(st, source, deadline=deadline)
                    try:
                        # B01: 拿到信号量+租约后重算剩余预算——排队耗时
                        # 不再从网络超时里偷跑；预算已耗尽则 _remaining()
                        # 直接抛错，根本不发请求
                        _timeout = _remaining()
                        resp = self._send_checked(lease.session, method, url,
                                                  data, headers, _timeout)
                    finally:
                        lease.release()
                finally:
                    st.sem.release()
                # ── 状态码封锁检测 ──
                if resp.status_code in _BLOCK_STATUS:
                    raise _Blocked(resp.status_code, resp.headers)
                resp.raise_for_status()
                text = self._decode(resp)
                # ── 挑战页检测 ──
                if self._looks_blocked(text):
                    raise _Blocked(200, resp.headers, note='challenge-page')
                self._on_success(st)
                self._proxy_ok(st.proxy)
                if st.proxy:
                    self._record_latency(st.proxy, time.time() - _attempt_t0)
                return text
            except DeadlineExceeded:
                raise
            except SSRFBlocked:
                # 安全策略拒绝：重试只会重复打内网，必须立即中止
                raise
            except _Blocked as b:
                last = b
                self._on_block(st, status=b.status if hasattr(b, 'status') else 403)
                self._proxy_failed(st.proxy)
                # R48: HTTP 反爬封锁(403/412/418/429/挑战页)即使首次也切代理——
                # 此类封锁多为 CF/DDOS-Guard 对直连 IP 的完整拦截,直连重试无意义;
                # 代理池里家宽/移动出口代理可过(实测 kudushu cloud+代理 200)。
                _http_anti = (getattr(b, 'status', None) in _HTTP_ANTI_STATUS) or \
                             (getattr(b, 'status', None) == 200
                              and getattr(b, 'note', '') == 'challenge-page')
                if _http_anti and not st.proxy and \
                        host not in self._direct_fail_hosts:
                    self._mark_direct_failed(host)
                    self._rebuild_session(host, source, reason='HTTP反爬→代理')
                    st.block_streak = 0
                elif _http_anti and st.proxy and attempt < retries - 1:
                    # 已走代理仍被拦(该代理 IP 被 CF 拒)→ 立即换下一个代理重试,
                    # 免费代理池命中率低,重试机会应花在不同代理上
                    self._rebuild_session(host, source, reason='代理被拦→换')
                    st.block_streak = 0
                elif attempt < retries - 1:
                    # 尊重 Retry-After，否则指数退避 + 抖动
                    ra = (b.headers or {}).get('Retry-After') or \
                         (b.headers or {}).get('retry-after')
                    try:
                        wait = float(ra) if ra else 0.0
                    except (TypeError, ValueError):
                        wait = 0.0
                    if wait <= 0:
                        if _http_anti:
                            # R48: 挑战站换代理重试为主,无需长指数退避
                            wait = 2.0 + random.uniform(0.3, 1.0)
                        else:
                            wait = min(20.0, 2.0 * (2 ** attempt)) + random.uniform(0.5, 2.0)
                    print(f"[fetcher] {host} 被封锁({b.status}) 等待 {wait:.1f}s "
                          f"后重试({attempt + 1}/{retries})", flush=True)
                    if deadline is not None:
                        wait = min(wait, max(0.0, deadline - time.monotonic()))
                    if wait > 0:
                        time.sleep(wait)
                    _remaining()
                    # 连续封锁 → 重建会话（换 TLS 指纹/Cookie/UA）
                    if st.block_streak >= self.REBUILD_AFTER_BLOCKS:
                        self._rebuild_session(host, source,
                                              reason=f'连续封锁×{st.block_streak}')
                        st.block_streak = 0
            except Exception as e:
                last = e
                st.net_streak += 1
                # 判定是否为 IP 级封锁：SSL EOF/连接重置 是典型特征（服务器静默丢包）；
                # 普通 ReadTimeout/HTTP 错误不是 → 不切代理（避免正常源被误切）
                _es = str(e)
                _ip_block = any(k in _es for k in (
                    "SSL", "Connection reset", "Connection aborted",
                    "RemoteDisconnected", "Connection closed"))
                # 直连失败且判定为 IP 封锁（连续 2 次）→ 切代理
                if not st.proxy and _ip_block and st.net_streak >= 2:
                    self._mark_direct_failed(host)
                    self._rebuild_session(host, source, reason='IP封锁→换代理')
                    st.net_streak = 0
                self._proxy_failed(st.proxy)
                if attempt < retries - 1:
                    # R48: 代理连接级错误（死代理/被拒）→ 立即重建换下一个代理，
                    # 不等连续错误计数（免费代理池死代理多，重试机会应花在不同代理上）
                    _proxy_err = any(k in _es for k in (
                        "ProxyError", "CONNECT aborted", "proxy"))
                    if st.proxy and _proxy_err:
                        self._rebuild_session(host, source, reason='代理失效→换')
                        st.net_streak = 0
                        continue
                    wait = 1.5 * (attempt + 1) + random.uniform(0.5, 1.5)
                    if deadline is not None:
                        wait = min(wait, max(0.0, deadline - time.monotonic()))
                    if wait > 0:
                        time.sleep(wait)
                    _remaining()
                    # 连续网络错误（连接重置/SSL 中断常为 IP 级封锁）→ 换引擎重建
                    if st.net_streak >= self.REBUILD_AFTER_BLOCKS:
                        self._rebuild_session(host, source,
                                              reason=f'连续网络错误×{st.net_streak}')
                        st.net_streak = 0
        raise last

    # ── 解码（与 v1 一致）──
    def _decode(self, resp):
        # 1) 显式 charset
        ctype = resp.headers.get('Content-Type', '')
        m = re.search(r'charset=([\w-]+)', ctype, re.I)
        if m:
            try:
                return resp.content.decode(m.group(1), 'replace')
            except Exception:
                pass
        # 2) 尝试 utf-8
        try:
            return resp.content.decode('utf-8')
        except Exception:
            pass
        # 3) 嗅探 HTML meta charset
        head = resp.content[:2048].decode('latin1', 'ignore')
        m = re.search(r'charset=["\']?([\w-]+)', head, re.I)
        if m:
            try:
                return resp.content.decode(m.group(1), 'replace')
            except Exception:
                pass
        # 4) gbk 兜底（多数中文小说站）
        try:
            return resp.content.decode('gbk', 'replace')
        except Exception:
            return resp.content.decode('utf-8', 'replace')


class _Blocked(Exception):
    """被反爬封锁（状态码封锁或挑战页），触发退避+会话重建"""
    def __init__(self, status, headers=None, note=''):
        super().__init__(f'blocked: HTTP {status} {note}'.strip())
        self.status = status
        self.headers = headers or {}
        self.note = note


def _pick_impersonate():
    """curl_cffi 指纹：优先通用 chrome 别名，失败用具体版本"""
    return 'chrome'


# 进程退出时关闭全部域名池内 session（释放 keep-alive 连接/句柄）
atexit.register(Fetcher.close_all)
