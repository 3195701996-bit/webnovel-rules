# -*- coding: utf-8 -*-
"""拷贝漫画网页版适配器（Playwright 渲染通道，绕过 APP API 的 IP 标记）
- 搜索：2026copy.com searchci API（页面上下文 fetch，同源带 cookie）
- 详情/章节：渲染详情页提取章节 DOM（395 章实测）
- 章节图片：渲染章节页提取图片 URL
- 图片下载：curl_cffi + Referer（sh.mangafunb.fun 防盗链）
"""
import json
import os
import threading
import time

from .base import Comic, ComicDetails, Chapter, MangaAdapter, MangaError

# R49f: 2026copy.com 域名失效(ERR_CONNECTION_CLOSED, 2026-09) →
# 官方 web 域多域池(copy4000.com 优先——用户实测随时稳定可用;
# mangacopy.com 曾出现连接重置; 自动探测可用域并缓存选定结果, 渲染失败自动换域)
_WEB_DOMAINS = ["https://www.copy4000.com", "https://www.mangacopy.com"]
WEB = _WEB_DOMAINS[0]          # 兼容旧引用(动态域经 _web_base() 获取)
_web_pick = {"base": "", "ts": 0.0}
_web_lock = threading.Lock()


def _web_base(force=False):
    """探测并返回当前可用 web 域(缓存 10 分钟; 失败轮换下一域)"""
    import random as _r
    with _web_lock:
        if not force and _web_pick["base"] and \
                time.time() - _web_pick["ts"] < 600:
            return _web_pick["base"]
        # 按序探测: 标题含 拷贝/拷貝 且非错误页
        from curl_cffi import requests as _creq
        for base in _WEB_DOMAINS:
            try:
                r = _creq.get(base + "/", timeout=8,
                              headers={"User-Agent": UA},
                              impersonate="chrome")
                if r.status_code == 200 and ("拷贝" in r.text[:4000]
                                             or "拷貝" in r.text[:4000]):
                    _web_pick["base"] = base
                    _web_pick["ts"] = time.time()
                    return base
            except Exception:
                continue
        _web_pick["base"] = _WEB_DOMAINS[0]   # 全失败回退首个
        _web_pick["ts"] = time.time()
        return _web_pick["base"]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _web_goto(pg, path, **kw):
    """带自动换域的页面跳转: 当前域网络失败(连接重置/超时等)时立即失效
    缓存并换下一可用域重试一次(copy4000.com 稳定, mangacopy 曾连接重置)"""
    for _attempt in range(2):
        _base = _web_base()
        try:
            return pg.goto(_base + path, **kw)
        except Exception:
            if _attempt == 0:
                with _web_lock:
                    _web_pick["base"] = ""
                    _web_pick["ts"] = 0.0
                continue
            raise


from ..config import MANGA_PAGE_SIZE as PAGE_SIZE  # 单页条数唯一事实源

_browser = None
_browser_lock = threading.Lock()
# Playwright sync API 要求同一 browser/context 只在【创建它的同一个线程】
# 使用（Flask 多线程下并发会报 cannot switch to a different thread）。
# R61: 多 worker 提速——每 worker 线程持有自己的 browser(thread-local),
#   互不串扰; 网页渲染/提取可并行(下载多章/检查多书时吞吐提升)。
#   worker 内每 _PW_RECYCLE 次取页后重建 browser, 防同一 Chrome 长跑变慢。
from concurrent.futures import ThreadPoolExecutor
# R66: 双 worker——下载任务逐章渲染(30-60s/章)会长期独占单 worker,
# 搜索/详情请求排队 → app 层 20s 超时被砍返回"空结果"(源站明明有)。
# thread-local browser 机制已支持多 worker(各持独立 Chrome/profile)。
# R72e: 3 worker 留冗余——pkill 恢复后部分线程的 greenlet 永久损坏
# (asyncio loop 错误, 无法修复), 冗余池 + 淘汰重建可吸收。
_pw_exec = ThreadPoolExecutor(max_workers=3, thread_name_prefix="cmweb-pw")
_CTX_ARGS = {"args": ["--no-sandbox"]}
_PW_RECYCLE = 8          # 每个 browser 最多服务多少次取页后重建
_tlocal = threading.local()   # thread-local browser 句柄


def _my_browser():
    return getattr(_tlocal, "browser", None)


def _set_my_browser(b):
    _tlocal.browser = b


def _bump_use():
    n = getattr(_tlocal, "uses", 0) + 1
    _tlocal.uses = n
    return n


_pw_lock = threading.Lock()
_pw_last_kill = [0.0]     # R72c: 全杀频率限制(防误伤连锁)
_PW_KILL_MIN_GAP = 30.0   # 两次全杀至少间隔 30s


def _kill_my_chrome():
    """强杀当前进程的全部渲染 Chrome。playwright sync API 阻塞在
    Chrome 无响应/网络挂死时无法通过 ctx.close() 取消——必须 pkill 让
    驱动连接断开, 阻塞中的调用才会抛错返回, 线程才能真正释放。
    R72: 抗压加固——此前超时只 shutdown executor, 卡死的 worker 线程
    仍占着旧 Chrome(线程内 playwright 调用永不返回), 新建 executor 又
    杀不掉它(profile 带线程 id), 僵尸 Chrome 越积越多 → 系统整体卡死。
    R72b: 不能用 _tlocal.profile——那是 worker 线程的 thread-local,
    _run_pw 在调用线程执行 pkill 时取到的是空; 改按进程 pid 前缀
    清掉本进程全部 cm_web_profile_<pid>_* Chrome(异常恢复场景
    宁可全杀重建, 不留僵尸)"""
    import os as _os
    import subprocess as _sp
    _pat = f"cm_web_profile_{_os.getpid()}_"
    try:
        _sp.run(["pkill", "-9", "-f", _pat], capture_output=True, timeout=8)
    except Exception:
        pass
    # 清本进程所有 profile 的单例残留(Chrome 下次启动不冲突)
    try:
        for _d in _os.listdir("/tmp"):
            if _d.startswith(_pat):
                for _lk in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                    _p = f"/tmp/{_d}/{_lk}"
                    if _os.path.exists(_p):
                        try:
                            _os.remove(_p)
                        except OSError:
                            pass
    except Exception:
        pass
    # 本线程句柄也清掉(其它 worker 线程的句柄在各自任务失败时各自清理)
    _b = _my_browser()
    if _b is not None:
        try:
            _b["ctx"].close()
        except Exception:
            pass
        try:
            _b["pw"].stop()
        except Exception:
            pass
        _set_my_browser(None)


def _run_pw(fn):
    """在 Playwright 专属线程执行 fn() 并返回结果（异常原样抛出）。
    R72d(重写): 超时/驱动崩溃时**只 pkill Chrome、不重建 executor**——
    旧实现每次恢复都 shutdown+新建 executor → 新线程→新 profile→新 Chrome,
    旧线程卡死在 playwright 调用里永不退出, 高强度下线程/僵尸 Chrome
    持续累积(实测 76 线程/764 profile 目录)最终拖垮系统。
    pkill 后阻塞调用抛错 → worker 线程自然结束当前 fn 回池复用,
    executor 与线程数恒定(2), 恢复路径只是杀 Chrome + 等线程空闲 + 重试。"""
    global _pw_exec
    import concurrent.futures as _cf
    import time as _time

    def _wrapped():
        """在 worker 线程内执行 fn; 任何失败都清空本线程的 browser 句柄——
        pkill/崩溃后残留的 playwright 会话若不清理, 线程回池复用时会报
        "Playwright Sync API inside the asyncio loop"(实测成片失败)"""
        try:
            return fn()
        except BaseException:
            _b = _my_browser()
            if _b is not None:
                try:
                    _b["ctx"].close()
                except Exception:
                    pass
                try:
                    _b["pw"].stop()
                except Exception:
                    pass
            _set_my_browser(None)
            raise

    try:
        with _pw_lock:
            fut = _pw_exec.submit(_wrapped)
        return fut.result(timeout=150)
    except Exception as e:
        msg = str(e)
        # 仅超时/驱动层崩溃才是"本任务真凶", 值得杀 Chrome 恢复。
        _real_broken = ("cannot schedule new futures" in msg
                        or "cannot switch to a different thread" in msg
                        or "has exited" in msg or "asyncio loop" in msg)
        _timed_out = isinstance(e, _cf.TimeoutError)
        if not (_real_broken or _timed_out):
            # R76: "Target page/context/browser has been closed" 多数是另一
            # worker 触发全杀时的**受害者**(并发检查/下载/阅读共享 3 worker,
            # 一次全杀会误伤并行任务 → 一键检查偶发 1-2 部失败)。
            # 受害者不得再触发 pkill(连锁), 但应静默重试一次——等凶手
            # 重建后新 Chrome 可正常渲染, 误伤自愈(此前直接抛=整部失败)。
            # 重试仍 closed 则确认真故障, 抛出由上层记失败。
            if ("has been closed" in msg or "Target page" in msg
                    or "Target context" in msg):
                _time.sleep(3)      # 等凶手完成 pkill+重建, 避免抢 Chrome
                with _pw_lock:
                    fut = _pw_exec.submit(_wrapped)
                return fut.result(timeout=150)
            raise
        _kill_my_chrome()
        # 等 worker 线程从阻塞中退出(pkill 后 playwright 调用抛错, fn 结束)
        try:
            fut.result(timeout=15)
        except Exception:
            pass
        # 同一 executor 重试(线程已回池, 复用而非重建)
        try:
            with _pw_lock:
                fut = _pw_exec.submit(_wrapped)
            return fut.result(timeout=150)
        except Exception as e2:
            _kill_my_chrome()          # 二次失败也清场, 不留僵尸
            if isinstance(e2, _cf.TimeoutError):
                raise MangaError(f"网页渲染超时(两次尝试均无响应): {str(e2)[:120]}") from e2
            # R72e: asyncio loop 类错误 = worker 线程 greenlet 永久损坏,
            # 该线程再也不能跑 playwright——整体淘汰重建 executor(换新线程)。
            # 60s 频率限制防连锁。
            # R75: 淘汰重建前必须先 _kill_my_chrome()——旧实现只 shutdown
            # executor, 旧 worker 线程的 Chrome 变孤儿进程(实测累积到
            # 28 个 0% CPU 僵尸 Chrome, 系统 load 飙升, 新 Chrome launch
            # 直接失败 → 一键检查两部漫画失败)。
            if "asyncio loop" in str(e2):
                with _pw_lock:
                    _now2 = _time.time()
                    if _now2 - _pw_last_kill[0] >= 60:
                        _pw_last_kill[0] = _now2
                        try:
                            _kill_my_chrome()
                        except Exception:
                            pass
                        try:
                            _pw_exec.shutdown(wait=False, cancel_futures=True)
                        except Exception:
                            pass
                        _pw_exec = ThreadPoolExecutor(
                            max_workers=3, thread_name_prefix="cmweb-pw")
                try:
                    with _pw_lock:
                        fut = _pw_exec.submit(_wrapped)
                    return fut.result(timeout=150)
                except Exception as e3:
                    _kill_my_chrome()
                    if isinstance(e3, _cf.TimeoutError):
                        raise MangaError(f"网页渲染超时(三次尝试均无响应): {str(e3)[:120]}") from e3
                    raise
            raise


def _setup_page(pg):
    """新页面通用设置: 超时 + 资源拦截。
    R58: 详情/章节页只需要 DOM(章节链接/图片 data-src 文本), 不需要真实
    图片/字体/CSS 字节——拦截可把渲染从 12s+ 压到 2-4s, 不影响懒加载
    DOM 注入(lazysizes 只是改写 data-src, 图片字节由下载器另行抓取)"""
    pg.set_default_timeout(45000)
    try:
        def _block(route):     # sync API: handler 必须同步
            try:
                route.abort()
            except Exception:
                pass
        pg.route("**/*.{png,jpg,jpeg,webp,gif,avif,ico,css,woff,woff2,ttf,eot,svg}",
                 _block)
    except Exception:
        pass


def _get_page():
    """(当前 worker 线程)获取页面——每 worker 持有自己的 browser(thread-local),
    互不串扰。R61: 同一 Chrome 连续渲染会累积变慢——每 _PW_RECYCLE 次取页
    后主动重建浏览器, 让渲染速度回到起步水平"""
    _b = _my_browser()
    if _b is not None:
        _tlocal.uses = getattr(_tlocal, "uses", 0) + 1
        if _tlocal.uses >= _PW_RECYCLE:
            _tlocal.uses = 0
            try:
                _b["ctx"].close()
            except Exception:
                pass
            _set_my_browser(None)
        else:
            try:
                pg = _b["ctx"].new_page()
                _setup_page(pg)
                return pg
            except Exception:
                # context 已关闭 → 重建
                try:
                    _b["ctx"].close()
                except Exception:
                    pass
                _set_my_browser(None)
    # 慢路径：本 worker 线程重建浏览器
    _launch_browser()
    _b = _my_browser()
    if _b is not None:
        try:
            pg = _b["ctx"].new_page()
            _setup_page(pg)
            return pg
        except Exception:
            pass
    raise MangaError("网页版浏览器启动失败")


def _launch_browser():
    """(当前 worker 线程)启动 Chrome——thread-local 独立 profile:
    多渲染 worker 各自持有独立 user-data-dir 与 playwright 实例, 不互抢。
    长操作(pkill/sleep/launch)不持锁。
    R70: profile 名带 pid——多进程(服务器+批量修复脚本)各自 worker 的
    threading.get_ident() 可能相同, 纯线程 id 会让 pkill 误杀对方 Chrome"""
    import time as _time
    import os as _os
    _profile = f"/tmp/cm_web_profile_{os.getpid()}_{threading.get_ident()}"
    _tlocal.profile = _profile
    if _my_browser() is not None:
        return
    # 1) 杀掉占用本 profile 的残留 Chrome（process-singleton 互抢）
    try:
        import subprocess as _sp
        _sp.run(["pkill", "-9", "-f", _os.path.basename(_profile)],
                capture_output=True, timeout=10)
    except Exception:
        pass
    _time.sleep(0.5)
    # 2) 清理残留单例锁
    for _lk in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            _p = f"{_profile}/{_lk}"
            if _os.path.exists(_p):
                _os.remove(_p)
        except Exception:
            pass
    try:
        # 懒加载：playwright 为可选依赖（未安装时回落 HTTP/渲染通道），
        # 缺失时抛 MangaError 而非 ImportError，由上层按"网页版不可用"处理
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        _set_my_browser(None)
        raise MangaError("网页版浏览器启动失败") from e
    try:
        _old = _my_browser()
        if _old:
            try:
                _old.get("pw").stop()
            except Exception:
                pass
            _set_my_browser(None)
        _pw = sync_playwright().start()
        try:
            _ctx = _pw.chromium.launch_persistent_context(
                _profile, headless=True,
                executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                **_CTX_ARGS)
        except Exception:
            try:
                _pw.stop()
            except Exception:
                pass
            raise
        _set_my_browser({"pw": _pw, "ctx": _ctx})
    except Exception:
        _set_my_browser(None)
        raise


def _http_chapter_images(comic_id, chapter_id):
    """HTTP 快速通道：curl_cffi 直抓章节页 → AES 解密内嵌 contentKey 得全量
    图片 URL（P1-2 调查结论：页面内嵌 var cct / var contentKey，站点 JS
    comic_content_pass*.js 用 AES-128-CBC 解密——key=cct(16 字符)，
    iv=contentKey 前 16 字符，密文=其余 hex，Pkcs7 去填充后为 JSON 数组
    [{"url":...}]；无需 JS 执行/滚动注入，实测 ~1s/话 vs 渲染 10-30s/话）。
    网络失败自动换域重试一次（与 _web_goto 同策略）；
    数据缺失/解密失败抛 MangaError，由调用方降级 Playwright 渲染"""
    import re as _re
    from curl_cffi import requests as _creq
    try:
        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes)
        from cryptography.hazmat.primitives import padding as _padding
    except ImportError as _ie:  # 未安装 cryptography 的环境
        raise MangaError("无 AES 库，HTTP 快速通道不可用") from _ie
    base = _web_base()
    last = None
    for _attempt in range(2):
        try:
            r = _creq.get(f"{base}/comic/{comic_id}/chapter/{chapter_id}",
                          headers={"User-Agent": UA}, impersonate="chrome",
                          timeout=15)
            if r.status_code != 200:
                raise MangaError(f"章节页 HTTP {r.status_code}")
            _t = r.text
            _m1 = _re.search(r"var cct = '([^']+)'", _t)
            _m2 = _re.search(r"var contentKey = '([^']+)'", _t)
            if not (_m1 and _m2):
                raise MangaError("章节页无内嵌图片数据(需渲染)")
            _key, _ck = _m1.group(1), _m2.group(1)
            _ct = bytes.fromhex(_ck[16:])
            _dec = Cipher(algorithms.AES(_key.encode("utf-8")),
                          modes.CBC(_ck[:16].encode("utf-8"))).decryptor()
            _padded = _dec.update(_ct) + _dec.finalize()
            _u = _padding.PKCS7(128).unpadder()
            _plain = _u.update(_padded) + _u.finalize()
            _data = json.loads(_plain.decode("utf-8").strip())
            imgs = [x.get("url", "") for x in _data
                    if isinstance(x, dict) and x.get("url")]
            # 与渲染通道同口径过滤：只保留漫画图片 CDN，排除站内静态/广告
            imgs = [u for u in imgs
                    if ("mangafunb" in u or "copymanga" in u)
                    and "/static/" not in u]
            if not imgs:
                raise MangaError("解密结果无图片(需渲染)")
            return imgs
        except MangaError:
            raise
        except Exception as e:
            last = e
            if _attempt == 0:
                # 换域重试一次（当前域连接重置/超时等网络失败）
                with _web_lock:
                    _web_pick["base"] = ""
                    _web_pick["ts"] = 0.0
                base = _web_base()
                continue
            raise MangaError(f"HTTP 章节图片提取失败: {last}")


def _web_chapter_images(comic_id, chapter_id):
    """渲染网页版章节页提取图片 URL（不受 APP IP 风控）
    章节页图片为懒加载：分段滚动到底触发全部加载，收集 data-src/src"""
    def _work():
        pg = _get_page()
        try:
            # R60: 章节图片页必须正常加载 css/图片——资源拦截会破坏页面布局,
            # 使懒加载(IntersectionObserver)失效, 实测只能取到首屏 3-4 张
            try:
                pg.unroute("**/*.{png,jpg,jpeg,webp,gif,avif,ico,css,woff,woff2,ttf,eot,svg}")
            except Exception:
                pass
            _web_goto(pg, f"/comic/{comic_id}/chapter/{chapter_id}",
                      timeout=45000, wait_until="domcontentloaded")
            pg.wait_for_timeout(2500)
            # R69: 章节页图片按 scroll 事件逐张注入(站点 JS: scrollY < 列表高/3
            # 时每次 scroll 事件 append 一张, 全部 URL 来自 AES 解密的 contentKey)。
            # 旧 R60 逻辑向下滚动——scrollY 一旦超过列表高 1/3 注入即停,
            # 长章节只能抓到前 ~2/3(实测 37 页章只抓到 24-25 张)。
            # 正确做法: 在顶部反复触发 scroll 事件, 让每次注入条件恒成立,
            # 直到 comicCount 显示的总页数全部注入。
            pg.evaluate("""async () => {
              const sleep = ms => new Promise(r => setTimeout(r, ms));
              const ul = document.querySelector('.comicContent-list');
              if (!ul) return 0;
              const countEl = document.querySelector('.comicCount');
              const total = parseInt((countEl ? countEl.innerText || countEl.textContent : '') || '0', 10);
              window.scrollTo(0, 0);
              const MAX = Math.max(total + 10, 60);
              let stall = 0;
              for (let i = 0; i < MAX; i++) {
                const before = ul.children.length;
                window.dispatchEvent(new Event('scroll'));
                await sleep(50);
                // 重新读到注入所需: 部分页面注入依赖 scrollY 保持在低位
                if (window.scrollY > 0) window.scrollTo(0, 0);
                const after = ul.children.length;
                if (after <= before) { stall++; if (stall >= 3 && after >= total) break; }
                else stall = 0;
                if (total && after >= total) break;
              }
              window.scrollTo(0, 0);
              await sleep(300);
              return ul.children.length;
            }""")
            # 收集图片 URL：data-src 优先（lazyload 真实地址），去重保序
            imgs = pg.evaluate("""() => {
              const out = [];
              const seen = new Set();
              document.querySelectorAll('img').forEach(img => {
                const src = (img.getAttribute('data-src') || img.currentSrc || img.src || '').trim();
                if (!src || src.startsWith('data:') || src.includes('loading')) return;
                if (src.includes('placeholder') || src.includes('loading.jpg')) return;
                if (seen.has(src)) return;
                seen.add(src);
                out.push(src);
              });
              return out;
            }""")
            # 过滤：只保留漫画图片 CDN（sq.mangafunb.fun 等），
            # 排除站内广告图(/static/ads/ 等静态区,如 tsugumomo800_130.jpg)
            imgs = [u for u in imgs
                    if ("mangafunb" in u or "copymanga" in u)
                    and "/static/" not in u]
            return imgs
        except Exception as e:
            raise MangaError(f"网页章节图片提取失败: {e}") from e
        finally:
            _release_page(pg)
    return _run_pw(_work)


def _release_page(pg):
    try:
        pg.close()
    except Exception:
        pass


class CopyMangaWeb(MangaAdapter):
    key = "copymanga_web"
    name = "拷贝漫画(网页版)"
    version = "1.0.0"
    concurrent = 1  # 网页版慢，单并发

    # ── 搜索：页面上下文 fetch searchci API ──
    def search(self, keyword, page=1):
        def _work():
            pg = _get_page()
            try:
                _web_goto(pg, "", timeout=45000, wait_until="domcontentloaded")
                pg.wait_for_timeout(3000)
                # R34: 真分页——page 参数 → offset=(page-1)*30，每页 30 条。
                # 此前固定拉 offset 0/30/60 三页 = 永远最多 90 条，
                # 前端"加载更多"翻页拿到的是同一批结果
                try:
                    _pg = max(1, int(page))
                except (TypeError, ValueError):
                    _pg = 1
                off = (_pg - 1) * PAGE_SIZE
                out = []
                seen_ids = set()
                # 关键词经参数传入而非拼接进 JS 源码：拼接只转义单引号挡不住
                # 反斜杠/换行/U+2028，可打断字面量在站点上下文执行任意 JS
                r = pg.evaluate("""async ([kw, limit, off]) => {
                    // R64: 站内搜索 API 需 platform=2&q_type= 参数(网页搜索页同款),
                    // 缺失时对部分关键词(如"姐姐的朋友")返回空结果
                    const res = await fetch('/api/kb/web/searchci/comics?q=' +
                        encodeURIComponent(kw) + '&limit=' + limit + '&offset=' + off +
                        '&platform=2&q_type=');
                    if (!res.ok) return {status: res.status};
                    return {status: res.status, data: await res.json()};
                }""", [keyword, PAGE_SIZE, off])
                if r and r.get("status") == 200 and r.get("data"):
                    items = (r["data"].get("results") or {}).get("list", [])
                    _total = int((r["data"].get("results") or {}).get("total") or 0)
                    for item in items:
                        comic = item.get("comic") or item
                        cid = comic.get("path_word") or comic.get("id") or ""
                        if not cid or cid in seen_ids:
                            continue
                        seen_ids.add(cid)
                        # R65: 同名不同 cid 是不同作品，作者一并带回，前端卡片可区分
                        _au = comic.get("author") or []
                        if isinstance(_au, dict):
                            _au = [_au]
                        _author = ""
                        if isinstance(_au, list) and _au:
                            _first = _au[0]
                            _author = _first.get("name", "") if isinstance(_first, dict) else str(_first)
                        out.append(Comic(
                            id=cid, title=comic.get("name", ""),
                            author=_author, cover=comic.get("cover", ""),
                            tags=[t.get("name", "") for t in (comic.get("theme") or [])],
                            source_key=self.key, total=_total))
                return out
            except Exception as e:
                raise MangaError(f"网页版搜索失败: {e}") from e
            finally:
                _release_page(pg)
        try:
            return _run_pw(_work)
        except MangaError:
            raise
        except Exception as e:
            raise MangaError(f"网页版搜索失败: {e}") from e

    # ── 详情：渲染详情页提取章节 DOM ──
    def comic_info(self, comic_id):
        def _work():
            pg = _get_page()
            try:
                _resp = _web_goto(pg, f"/comic/{comic_id}", timeout=45000,
                                 wait_until="commit")
                # 快速 404 检测①：HTTP 状态码
                if _resp is not None and _resp.status >= 400:
                    raise MangaError(f"漫画不存在: {comic_id}")
                # R58: commit 后即轮询章节 DOM 就绪(通常 1-3s; 不再固定等
                # 9s+, goto 用 commit 省 domcontentloaded 资源等待);
                # 404 页 title 立即可见 → 轮询中同步判 404 快速失败
                _ch_ready = pg.evaluate("""async (cid) => {
                  const sleep = ms => new Promise(r => setTimeout(r, ms));
                  let prev = -1, stable = 0;
                  for (let i = 0; i < 24; i++) {
                    const t = (document.title || '').trim();
                    if (t.includes('404') || t.includes('不存在') || t.includes('无法访问')) return '404';
                    const n = document.querySelectorAll('a[href*="/comic/' + cid + '/chapter/"]').length;
                    if (n > 5 && n === prev) { stable++; if (stable >= 2) return true; }
                    else stable = 0;
                    prev = n;
                    await sleep(300);
                  }
                  return prev > 5;
                }""", comic_id)
                if _ch_ready == '404':
                    raise MangaError(f"漫画不存在: {comic_id}")
                if not _ch_ready:
                    pg.wait_for_timeout(3000)
                # 标题/封面/简介 + 作者/标签/更新时间（页面 meta + DOM 文本）
                info = pg.evaluate("""async () => {
                  const out = {title: document.title.split('-')[0] || '', cover: '', desc: '',
                               author: '', tags: [], update_time: ''};
                  const desc = document.querySelector('meta[name="description"]');
                  if (desc) out.desc = desc.content.slice(0, 200);
                  const body = document.body.innerText || '';
                  const aM = body.match(/作者：\\s*([^\\n]{1,30})/);
                  if (aM) out.author = aM[1].trim();
                  const uM = body.match(/最後更新：\\s*([0-9\\-]{8,10})/);
                  if (uM) out.update_time = uM[1].trim();
                  const tM = body.match(/題材：\\s*([^\\n]{1,80})/);
                  if (tM) out.tags = (tM[1].match(/#[^\\s#]+/g) || []).map(t => t.slice(1));
                  // 封面：og:image 优先；网页详情页无 og:image（实测），
                  // 回退到页面第一个漫画封面图（CDN cover 路径，含 /cover/ 且非广告）
                  const findCover = () => {
                    const og = document.querySelector('meta[property="og:image"]');
                    if (og && og.content) return og.content;
                    const img = [...document.querySelectorAll('img')].find(i => {
                      const s = (i.getAttribute('data-src') || i.src || '').trim();
                      // R68: data-src 优先——lazyload 未触发时 img.src 仍是
                      // 占位图, 旧逻辑先取 src 再取 data-src 会短路拿到占位,
                      // 导致封面漏取(实测姐姐的朋友 cover 恒为空)
                      return s.indexOf('/cover/') > 0 && s.indexOf('/static/') < 0
                        && !s.startsWith('data:');
                    });
                    if (img) return img.getAttribute('data-src') || img.src || '';
                    return '';
                  };
                  out.cover = findCover();
                  // 仍无封面: lazyload 可能未注入 → 等一批再取
                  if (!out.cover) {
                    const sleep2 = ms => new Promise(r => setTimeout(r, ms));
                    window.scrollTo(0, 0);
                    await sleep2(1800);
                    out.cover = findCover();
                  }
                  return out;
                }""")
                # 章节列表: 前述就绪轮询后 DOM 已含全部章节(实测 1s 内全量,
                # 无需滚动加载)——直接提取; R58 提速(去掉 4s+ 滚动等待)
                # P2-9: comic_id 参数化传入（与 search/就绪轮询一致），
                # 不再 f-string 拼进 JS 源码（JS 注入风险）
                _EX = """(cid) => {
                  const raw = [];
                  document.querySelectorAll('a[href*="/comic/' + cid + '/chapter/"]').forEach(a => {
                    const href = a.href;
                    const m = href.match(/chapter\\/([a-f0-9-]+)/);
                    if (m) raw.push({id: m[1], name: (a.textContent||'').trim(), href: href});
                  });
                  // 过滤"開始閱讀"等按钮后再按 id 去重（避免按钮占用章节 id）
                  const out = [];
                  const seen = new Set();
                  raw.forEach(c => {
                    const nm = c.name || '';
                    if (nm === '開始閱讀' || nm === '开始阅读' || nm === '閱讀') return;
                    if (seen.has(c.id)) return;
                    seen.add(c.id);
                    out.push(c);
                  });
                  return out;
                }"""
                chs = pg.evaluate(_EX, comic_id)
                if len(chs) < 5:
                    # 兜底: 极慢渲染时滚一次再试
                    pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    pg.wait_for_timeout(2500)
                    chs = pg.evaluate(_EX, comic_id)
                # 快速 404 检测：错误 comic_id 时页面无章节链接或标题为 404
                if not chs:
                    _pg_title = (pg.title() or "").strip()
                    if "404" in _pg_title or "不存在" in _pg_title or "无法访问" in _pg_title:
                        raise MangaError(f"漫画不存在: {comic_id}")
                # 完整性检查：网页详情页应有全部章节（含"第01话"）。
                # 若提取数偏少，可能是页面异步渲染未完 → 回滚到顶等待重提
                for _retry in range(3):
                    if len(chs) >= 85:
                        break
                    pg.evaluate("window.scrollTo(0, 0)")
                    pg.wait_for_timeout(3000)
                    pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    pg.wait_for_timeout(2500)
                    chs = pg.evaluate(_EX, comic_id)
                # 过滤：只排除"開始閱讀"等非章节按钮链接；
                # 所有有名称的章节（话/卷/番外/特别篇）全部保留，避免章节缺失
                chapters = []
                for i, c in enumerate(chs):
                    nm = c["name"] or ""
                    if nm in ("開始閱讀", "开始阅读", "閱讀"):
                        continue
                    chapters.append(Chapter(id=c["id"], name=nm or f"第{len(chapters)+1}话"))
                if not chapters:
                    chapters = [Chapter(id=c["id"], name=c["name"] or f"第{i+1}话")
                                for i, c in enumerate(chs)]
                # 网页版 DOM 完整（含"第01话"），无需 APP 交叉补齐——
                # APP comic2/chapters 接口在 IP 风控时会重试拖垮整页加载。
                # 若仍缺章节（极少见），保留网页数据即可。
                # 相关推荐（详情页"相关推荐"区，链接 /comic/{path_word}）
                rec = pg.evaluate("""() => {
                  const out = [];
                  const seen = new Set();
                  document.querySelectorAll('a[href*="/comic/"]').forEach(a => {
                    const href = a.href;
                    const m = href.match(/\\/comic\\/([^\\/?#]+)/);
                    if (!m) return;
                    if (seen.has(m[1])) return;
                    seen.add(m[1]);
                    const img = a.querySelector('img');
                    out.push({id: m[1], title: (a.getAttribute('title') || a.textContent || '').trim().slice(0, 40),
                              cover: img ? (img.getAttribute('data-src') || img.src || '') : ''});
                  });
                  return out.slice(0, 12);
                }""")
                recs = [Comic(id=r["id"], title=r["title"] or r["id"],
                              cover=r["cover"], source_key=self.key)
                        for r in rec if r["id"] != comic_id]
                return ComicDetails(
                    id=comic_id, title=info.get("title", comic_id),
                    cover=info.get("cover", ""), sub_title=self.name,
                    description=info.get("desc", ""),
                    author=info.get("author", ""),
                    tags=info.get("tags") or [],
                    update_time=info.get("update_time", ""),
                    chapters=chapters, recommend=recs[:8])
            except MangaError:
                raise
            except Exception as e:
                raise MangaError(f"网页版详情失败: {e}") from e
            finally:
                _release_page(pg)
        try:
            return _run_pw(_work)
        except MangaError:
            raise
        except Exception as e:
            raise MangaError(f"网页版详情失败: {e}") from e

    def chapters(self, comic_id):
        return self.comic_info(comic_id).chapters

    # ── 章节图片：HTTP 快速通道（AES 解密 contentKey，~1s/话）优先；
    #    失败/数据缺失降级 Playwright 网页渲染（不受 APP IP 风控）──
    def images(self, comic_id, chapter_id):
        # 图片 URL 缓存（data/manga/_cache/.../<chapter>_imgs.json，1 天 TTL）
        _imgs_p = None
        if self.state_dir:
            _imgs_p = os.path.join(self.state_dir, "..", "_cache", "copymanga", comic_id,
                                   f"{chapter_id}_imgs.json")
            try:
                if os.path.exists(_imgs_p) and time.time() - os.path.getmtime(_imgs_p) < 86400:
                    _cached = json.load(open(_imgs_p, encoding="utf-8"))
                    # R70: 缓存版本化 {"v":2,"urls":[...]}——R69 之前的裸数组缓存
                    # 是 R60 滚动逻辑抓的半截列表(实测 37 页章只存 25), 一旦命中
                    # 会让复查/补页/阅读都拿坏列表当"应有数"。旧格式一律视为无效,
                    # 强制用 R69 全量提取重写。
                    _urls = []
                    if isinstance(_cached, dict) and _cached.get("v") == 2:
                        _urls = _cached.get("urls") or []
                    elif isinstance(_cached, list):
                        _urls = []          # R70: 旧坏缓存(无版本)不再信任
                    if _urls:
                        return _urls
            except Exception:
                pass
        imgs = None
        # P1-2: HTTP 快速通道（页面内嵌 contentKey 直接解密，实测 ~1s/话，
        # 渲染通道 10-30s/话）；失败/数据缺失时降级 Playwright 渲染兜底
        try:
            imgs = _http_chapter_images(comic_id, chapter_id)
        except Exception as _he:
            print(f"[copymanga_web] HTTP 快速通道失败({_he})，降级网页渲染",
                  flush=True)
        if not imgs:
            # R59: 全面禁用 APP 通道——纯网页滚动提取(copy4000)。
            # (APP chapter2 曾一次返回全部图, 但持续触发 IP 风控(210),
            #  用户明确禁止走 APP 端)
            try:
                imgs = _web_chapter_images(comic_id, chapter_id)
            except Exception as _we:
                print(f"[copymanga_web] 网页图片提取失败({_we})", flush=True)
        if imgs:
            if _imgs_p:
                try:
                    os.makedirs(os.path.dirname(_imgs_p), exist_ok=True)
                    json.dump({"v": 2, "urls": imgs},
                              open(_imgs_p, "w", encoding="utf-8"))
                except Exception:
                    pass
            return imgs
        raise MangaError("章节图片获取失败（网页提取失败）")

    def image_headers(self, image_url):
        # 图片防盗链：Referer 用网页版站点
        return {"User-Agent": UA, "Referer": _web_base() + "/"}

    def image_url(self, image_url):
        return image_url


def _app_chapter2_images(comic_id, chapter_id):
    """APP chapter2 接口一次返回全部图片（快且完整，受 IP 风控）"""
    import base64, hashlib, hmac, re as _re
    from curl_cffi import requests as creq
    from .copymanga import (CopyManga, _generate_device_info, _generate_device,
                            _generate_pseudoid, SECRET_B64, COPY_VERSION)
    from .base import MangaError as _ME
    cm = CopyManga()
    _api = cm._api
    _reqid = ""
    try:
        _reqid = cm._get_request_id()
    except Exception:
        _reqid = "web"
    ts = str(int(time.time()))
    secret = base64.b64decode(SECRET_B64)
    sig = hmac.new(secret, ts.encode(), hashlib.sha256).hexdigest()
    headers = {
        "User-Agent": f"COPY/{COPY_VERSION}", "source": "copyApp", "platform": "3",
        "referer": f"com.copymanga.app-{COPY_VERSION}", "version": COPY_VERSION,
        "Accept": "application/json", "region": "0",
        "deviceinfo": _generate_device_info(), "device": _generate_device(),
        "pseudoid": _generate_pseudoid(), "authorization": "Token",
        "umstring": "b4c89ca4104ea9a97750314d791520ac",
        "x-auth-timestamp": ts, "x-auth-signature": sig,
        "dt": time.strftime("%Y.%m.%d"),
    }
    url = (f"https://{_api}/api/v3/comic/{comic_id}/chapter2/{chapter_id}"
           f"?in_mainland=true&request_id={_reqid}")
    resp = creq.get(url, headers=headers, impersonate="chrome", timeout=15)
    if resp.status_code != 200:
        raise _ME(f"章节图片接口 {resp.status_code}")
    data = resp.json()
    chapter = (data.get("results") or {}).get("chapter", {}) or {}
    contents = chapter.get("contents", [])
    words = chapter.get("words", [])
    urls = [c.get("url", "") for c in contents]
    urls = [_re.sub(r"([./])c\d+x\.[a-zA-Z]+$", r"\1c1500x.webp", u) for u in urls]
    ordered = [""] * len(urls)
    for i, pos in enumerate(words):
        if 0 <= pos < len(ordered):
            ordered[pos] = urls[i]
    imgs = [u for u in ordered if u]
    if not imgs:
        raise _ME("章节无图片（可能需登录/VIP）")
    return imgs
