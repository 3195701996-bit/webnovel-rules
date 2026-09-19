"""全本小说网（www.quanben.io）专属适配器。

与 quanben5.com（AMP 镜像）同源但独立成源：
- 内容域 = https://www.quanben.io（源配置域名，直连被 IP 封锁 → 走代理池）
- 目录页 = /amp/n/{slug}/list.html（单页完整列表，1633+ 章实测）
- 正文页 = /amp/n/{slug}/{n}.html（AMP 容器 articlebody）
- 搜索 = quanben.io 传统搜索接口不可靠（返回首页/固定热门），
  热门书走 HOT_SLUGS slug 兜底，其他走首页热门列表兜底

复用 quanben.py 的代理池/租约/预热逻辑（类级一次预热），解析器针对
quanben.io 的 AMP 结构（list.html 目录）实现。
"""
import re
import threading
import time

from . import BaseSourceAdapter

SEARCH_DOMAIN = "https://www.quanben.io"    # 搜索/内容主域（源配置域名）
CONTENT_DOMAIN = "https://www.quanben.io"

# 常用书 slug 表（搜索兜底：书名 → 拼音 slug；与 quanben5 同库共享）
HOT_SLUGS = [
    ("斗破苍穹", "doupocangqiong", "天蚕土豆"),
    ("剑来", "jianlai", "烽火戏诸侯"),
    ("凡人修仙传", "fanrenxiuxianchuan", "忘语"),
    ("遮天", "zhetian", "辰东"),
    ("完美世界", "wanmeishijie", "辰东"),
    ("诡秘之主", "guimizhizhu", "爱潜水的乌贼"),
    ("庆余年", "qingyunian", "猫腻"),
    ("雪中悍刀行", "xuezhonghandaoxing", "烽火戏诸侯"),
    ("赘婿", "zhuixu", "愤怒的香蕉"),
    ("一念永恒", "yinianyongheng", "耳根"),
    ("仙逆", "xianni", "耳根"),
    ("斗罗大陆", "douluodalu", "唐家三少"),
    ("大奉打更人", "dafengdagenren", "卖报小郎君"),
    ("夜的命名术", "yedemingmingshu", "会说话的肘子"),
    ("深空彼岸", "shenkongbian", "辰东"),
    ("元尊", "yuanzun", "天蚕土豆"),
    ("万古神帝", "wangushendi", "飞天鱼"),
    ("牧神记", "mushenji", "宅猪"),
    ("大道朝天", "dadaochaotian", "猫腻"),
    ("大王饶命", "dawangraoming", "会说话的肘子"),
]


class _NotFound(Exception):
    """内容不存在（404/410）：URL 失效，换代理重试无用，直接终止"""


class Adapter(BaseSourceAdapter):
    uid_prefix = "quanben"
    # 类级预热标志：搜索/目录每次请求都新建实例，实例级 _warmed 会导致
    # 每次请求都重新并发测速代理池（约 8s 开销）→ 进程内只预热一次
    _warm_done = False
    _warm_lock = threading.Lock()

    def __init__(self, source, fetcher):
        super().__init__(source, fetcher)
        self.base = CONTENT_DOMAIN
        self._warmed = False

    def _warm_proxies(self):
        """预热：并发测速代理池，把真实延迟写入 Fetcher 画像（快代理优先）。
        类级一次预热（进程内共享），避免每次请求重复测速开销"""
        import requests as _rq
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from engine.fetcher import Fetcher as _F
        if Adapter._warm_done:
            return
        with Adapter._warm_lock:
            if Adapter._warm_done:
                return
            Adapter._warm_done = True
        try:
            pool = _F._load_proxy_pool()
            test_url = CONTENT_DOMAIN + "/amp/n/doupocangqiong/1.html"

            def _test(pr):
                t0 = time.time()
                try:
                    r = _rq.get(test_url, timeout=4,
                                proxies={"http": pr, "https": pr},
                                headers={"User-Agent": "Mozilla/5.0"})
                    if r.status_code == 200 and len(r.text) > 3000:
                        return pr, time.time() - t0
                except Exception:
                    pass
                return pr, None

            good = 0
            with ThreadPoolExecutor(max_workers=16) as ex:
                futs = [ex.submit(_test, p) for p in pool]
                for f in as_completed(futs, timeout=20):
                    pr, lat = f.result()
                    if lat is not None:
                        _F._proxy_latency[pr] = lat
                        good += 1
                        if lat > 8:
                            _F._proxy_fails[pr] = 3
                    else:
                        # 预热即失败（对 quanben 不可达）→ 直接停用，
                        # 避免无效代理反复进入候选拖慢请求
                        _F._proxy_fails[pr] = 3
            print(f"[adapter] quanben.io 代理预热完成（{len(pool)} 测速，{good} 可用）", flush=True)
        except Exception as e:
            print(f"[adapter] quanben.io 代理预热失败: {e}", flush=True)

    def _get(self, url, timeout=10, retries=3, **kw):
        """经代理获取（普通 requests + 代理池轮换；quanben.io 直连被 IP 封锁）。
        并发安全：每代理租约制——同一时刻一个代理只服务一个请求。"""
        import requests as _rq
        last = None
        tried = set()
        deadline = self._deadline
        for attempt in range(retries * 2):
            proxy = self._next_proxy()
            if not proxy:
                break
            pk = proxy.get("https", "")
            if pk in tried:
                continue
            tried.add(pk)
            self._lease_proxy(pk, timeout + 3)
            try:
                _timeout = self._remaining_timeout(timeout)
                r = _rq.get(url, timeout=_timeout, proxies=proxy,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                                     "Connection": "close"})
                if r.status_code == 200 and len(r.text) > 2000:
                    return r.text
                if r.status_code in (404, 410):
                    raise _NotFound(f"{r.status_code}: {url}")
                self._mark_proxy_dead(proxy)
                last = Exception(f"代理返回 {r.status_code}")
            except Exception as e:
                last = e
                self._mark_proxy_dead(proxy)
            finally:
                self._release_proxy(pk)
            wait = min(0.3, max(0.0, deadline - time.monotonic())) if deadline else 0.3
            if wait > 0:
                time.sleep(wait)
            if deadline is not None and time.monotonic() >= deadline:
                from ..fetcher import DeadlineExceeded
                raise DeadlineExceeded(f"adapter deadline exceeded: {url}")
        if last:
            raise last
        return None

    # 代理池（从 Fetcher 的公共池读取，带本适配器健康状态）
    _pool = []
    _pool_ts = 0.0
    _dead = {}          # proxy -> 停用截止时间戳（60s 后恢复）
    _busy = {}          # proxy -> 租约截止时间戳（并发占用，防止撞车）
    _pool_lock = threading.Lock()

    def _next_proxy(self):
        """从 Fetcher 代理池挑一个未停用**且未被并发占用**的代理（快代理优先）"""
        import random as _rand
        from engine.fetcher import Fetcher as _F
        self._warm_proxies()
        now = time.time()
        with self._pool_lock:
            expired = [k for k, v in self._dead.items() if v <= now]
            for k in expired:
                self._dead.pop(k, None)
            expired_b = [k for k, v in self._busy.items() if v <= now]
            for k in expired_b:
                self._busy.pop(k, None)
            if time.time() - self._pool_ts > 120 or not self._pool:
                pool = _F._load_proxy_pool()
                self._pool = [p for p in pool if p not in self._dead]
                self._pool_ts = time.time()
            candidates = [p for p in self._pool
                          if p not in self._dead and p not in self._busy]
            if not candidates:
                candidates = sorted(self._pool,
                                    key=lambda p: self._busy.get(p, 0))[:3]
        if not candidates:
            return None
        lat = _F._proxy_latency
        if lat:
            candidates.sort(key=lambda p: lat.get(p, 5.0))
            # 快代理候选：池大取前 1/3，池小取前一半（避免可用代理被过度缩窄）
            _n = len(candidates)
            fast = candidates[:max(1, min(_n, _n // 2 if _n <= 10 else _n // 3))]
            pick = _rand.choice(fast)
        else:
            pick = _rand.choice(candidates)
        return {"http": pick, "https": pick}

    def _lease_proxy(self, pk, secs):
        if not pk:
            return
        with self._pool_lock:
            self._busy[pk] = time.time() + max(1.0, secs)

    def _release_proxy(self, pk):
        if not pk:
            return
        with self._pool_lock:
            self._busy.pop(pk, None)

    def _mark_proxy_dead(self, proxy):
        if not proxy:
            return
        for k in ("http", "https"):
            pr = proxy.get(k)
            if pr:
                with self._pool_lock:
                    self._dead[pr] = time.time() + 60

    def _content_url(self, path):
        """把站内路径补全为 quanben.io 完整 URL"""
        if path.startswith("http"):
            return re.sub(r"https?://[^/]+", CONTENT_DOMAIN, path)
        return CONTENT_DOMAIN + path

    # ── 首页封面映射：{slug: cover}（list2 容器内 img itemprop=image + 书链接）──
    _home_covers = {}
    _home_ts = 0.0

    def _home_cover_map(self):
        """抓首页解析 slug→封面 映射（缓存 10 分钟，避免每次搜索都抓首页）"""
        now = time.time()
        if self._home_covers and now - self._home_ts < 600:
            return self._home_covers
        covers = {}
        try:
            html = self._get(CONTENT_DOMAIN + "/", timeout=15, retries=1)
            if html:
                # <div class="list2" ...> ... <img src="封面" itemprop="image" />
                #   <h3><a href="/n/{slug}/">书名</a></h3> ...
                for blk in re.finditer(
                        r'<div[^>]*class="list2"[^>]*>(.*?)</div>\s*</div>',
                        html, re.S):
                    seg = blk.group(1)
                    im = re.search(r'<img[^>]*src="([^"]+)"', seg)
                    if not im:
                        continue
                    cover = im.group(1)
                    if cover.startswith("//"):
                        cover = "https:" + cover
                    for a in re.finditer(r'href="(/n/[^"/]+/)"', seg):
                        slug = a.group(1).strip("/").split("/")[-1]
                        if slug and "/n/" + slug + "/" in seg:
                            covers[slug] = cover
                # 首页热门区块可能用其它容器，再宽松扫一遍
                if not covers:
                    for blk in re.finditer(
                            r'<img[^>]*src="([^"]+)"[^>]*itemprop="image"[^>]*>'
                            r'.{0,400}?href="(/n/[^"/]+/)"', html, re.S):
                        cover, u = blk.group(1), blk.group(2)
                        if cover.startswith("//"):
                            cover = "https:" + cover
                        covers[u.strip("/").split("/")[-1]] = cover
        except Exception as e:
            print(f"[adapter] quanben.io 首页封面解析失败: {e}", flush=True)
        self._home_covers = covers
        self._home_ts = now
        return covers

    def _hot_result(self, name, slug, author=""):
        cover = self._cover_for_slug(slug) or self._home_cover_map().get(slug, "")
        return {
            "name": name, "author": author, "intro": "", "kind": "",
            "cover": cover,
            "book_url": f"{CONTENT_DOMAIN}/n/{slug}/",
            "source_uid": self.source.get("uid", ""),
            "source_name": self.source.get("bookSourceName", ""),
        }

    # 详情页封面缓存（{slug: cover}）：热门书不在首页时抓详情页 og:image
    _detail_covers = {}

    def _cover_for_slug(self, slug):
        """抓详情页取封面（og:image / itemprop=image），内存缓存 1 小时"""
        if slug in self._detail_covers:
            return self._detail_covers[slug]
        cover = ""
        try:
            html = self._get(f"{CONTENT_DOMAIN}/n/{slug}/", timeout=12, retries=1)
            if html:
                m = re.search(
                    r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"', html) \
                    or re.search(r'<img[^>]*src="([^"]+)"[^>]*itemprop="image"', html)
                if m:
                    cover = m.group(1)
                    if cover.startswith("//"):
                        cover = "https:" + cover
        except Exception:
            pass
        self._detail_covers[slug] = cover
        return cover

    # ── 搜索：热门书 slug 兜底（秒回，带回封面）；其他走首页热门列表 ──
    def search(self, keyword, page=1):
        for name, slug, _auth in HOT_SLUGS:
            if keyword in name or name in keyword:
                return [self._hot_result(name, slug, _auth)]
        books = self._fallback_search(keyword)
        return books

    def _fallback_search(self, keyword):
        """热门书 slug 表匹配 → 首页热门书（带回封面）"""
        for name, slug, _auth in HOT_SLUGS:
            if keyword in name or name in keyword:
                return [self._hot_result(name, slug, _auth)]
        # 首页热门列表兜底
        try:
            html = self._get(CONTENT_DOMAIN + "/", timeout=15, retries=1)
        except Exception:
            return []
        covers = self._home_cover_map()
        books = []
        seen = set()
        for m in re.finditer(
                r'<a[^>]*href="(/n/[^"/]+/)"[^>]*>\s*([^<]{2,40})</a>', html):
            u, name = m.group(1), m.group(2).strip()
            full = self._content_url(u)
            if not name or full in seen:
                continue
            seen.add(full)
            slug = u.strip("/").split("/")[-1]
            books.append({
                "name": name, "author": "", "intro": "", "kind": "",
                "cover": covers.get(slug, ""), "book_url": full,
                "source_uid": self.source.get("uid", ""),
                "source_name": self.source.get("bookSourceName", ""),
            })
        return books[:20]

    # ── 详情 ──
    def get_book(self, book_url, fast=False):
        url = self._content_url(book_url)
        html = self._get(url, timeout=15 if not fast else 10, retries=2)
        if not html:
            return None
        name = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
        # 作者：优先 <title>《书名》作者</title>；meta description "作者:X,简介:..." 兜底时只取到简介前
        _author = ""
        _tm = re.search(r"<title>[^<]*》\s*([^<-]{2,30})", html)
        if _tm:
            _author = _tm.group(1).strip()
        if not _author:
            _dm = re.search(r"作者[:：]\s*([^<,，]{2,30})", html)
            if _dm:
                _author = _dm.group(1).strip()
        author = _author
        m = re.search(r"/n/([^/]+)/", url)
        slug = m.group(1) if m else ""
        # 详情页封面（og:image / 主封面，内存缓存）
        _cov = ""
        try:
            _cov = self._cover_for_slug(slug)
        except Exception:
            pass
        info = {
            "name": name.group(1).strip() if name else "",
            "author": author.strip() if author else "",
            "intro": "", "cover": _cov, "kind": "",
            "last_chapter": "", "word_count": "", "update_time": "",
            "toc_url": f"{CONTENT_DOMAIN}/amp/n/{slug}/list.html",
        }
        return info

    # ── 目录（/amp/n/{slug}/list.html 单页完整；章节 href 为 /amp/n/...）──
    def get_toc(self, book):
        m = re.search(r"/n/([^/]+)/", book.book_url)
        if not m:
            raise RuntimeError("quanben.io 书 URL 无法解析 slug")
        slug = m.group(1)
        url = f"{CONTENT_DOMAIN}/amp/n/{slug}/list.html"
        html = self._get(url, timeout=15, retries=2)
        if not html:
            raise RuntimeError("quanben.io 目录页获取失败")
        chapters = []
        seen = set()
        for m in re.finditer(
                r'<a[^>]*href="(/amp/n/[^/]+/\d+\.html)"[^>]*>([^<]{1,60})</a>',
                html):
            u, name = m.group(1), m.group(2).strip()
            full = self._content_url(u)
            if full in seen or not name:
                continue
            seen.add(full)
            chapters.append({"name": name, "url": full})
        if not chapters:
            raise RuntimeError("quanben.io 目录解析为空")
        return chapters

    # ── 正文（AMP content；章节 URL 已是 /amp/n/... 形式）──
    def get_content(self, chapter_url):
        url = re.sub(r"(https?://[^/]+)/n/", r"\1/amp/n/", chapter_url)
        url = self._content_url(url)
        html = self._get(url, timeout=15, retries=2)
        if not html:
            raise RuntimeError("quanben.io 章节页获取失败")
        m = re.search(r'<div[^>]*class="(?:articlebody|content)"[^>]*>(.*?)</div>', html, re.S)
        if not m:
            return ""
        return self._clean_content(m.group(1))
