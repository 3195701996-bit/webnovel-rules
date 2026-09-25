# -*- coding: utf-8 -*-
"""漫画图片下载器：下载与阅读共用
- 缓存：data/manga/<key>/<comic>/images/<chapter>/<idx>.<ext>
- 防盗链：adapter.image_headers + image_url 覆盖
- 失败重试 + on_image_failed 动态刷新配置
- 并发：per-adapter 信号量（concurrent 字段）
- 节流：每图最小间隔（防 IP 级风控标记，实现边下载边看）
- SSRF：fetch_image_checked —— 请求前 DNS 解析校验 + 逐跳重定向校验
  （与 engine/fetcher.py `_send_checked` 同模式；fetcher 属小说域且由
  其他代理维护，故在漫画域内实现独立小助手，三处图片通道共用）
- 落盘：atomic_write_image —— tmp + os.replace，读端只查存在性，
  直写目标文件会让并发读者读到半图
"""
import json
import os
import threading
import time
from urllib.parse import urljoin

from .. import netproxy as _netproxy
from ..urlsec import (MAX_REDIRECTS, SSRFBlocked, DNSResolveUnavailable,
                       PinUnavailable, url_is_public_resolved,
                       pin_curl_session,
                       _pin_requests_session as pin_requests_session)
from .base import MangaError


class BadImageError(MangaError):
    """源站坏页/占位图（永久性）：返回内容过小或非图片——
    重试无意义，直接快速失败，不消耗重试与节流预算"""


# 处理版本标记：记录"这一话的图片是用哪个版本的还原算法处理过的"。
# 算法一变（如 jm 由"按固定 5 字符切扩展名"改为 splitext），旧缓存就不可信——
# 有了标记才能**只重建受影响的章节**，而不是让用户清数据/重下整个书库
# （0.51.0 风险评估 §2.3 第 4、5 条）。
PROCESS_MARKER = "_processed.json"
_marked_dirs = set()


def process_version_of(adapter):
    v = getattr(adapter, "PROCESS_VERSION", None)
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def chapter_process_version(adapter, chapter_id=None, comic_id=None):
    """该章节目录要求的**处理版本**（0.65.0）。

    默认就是适配器的 PROCESS_VERSION；适配器可以提供 `process_version_for()`
    做"按章节收敛"：jm 的单话相册（chapter_id 就是相册 id）在 0.65.0 之前被
    "跳过块还原"写坏过，所以**只有这一类**目录要求新版本；其余章节的好缓存
    继续沿用旧版本号，避免一次版本提升把用户已经下好的图全部作废重下
    （实测每页 CDN 约 2.8s，整库重下代价极高）。
    """
    fn = getattr(adapter, "process_version_for", None)
    if callable(fn):
        try:
            v = int(fn(chapter_id, comic_id))
            if v > 0:
                return v
        except Exception:
            pass
    return process_version_of(adapter)


def mark_processed(cache_root, adapter, comic_id, chapter_id):
    """把当前处理版本写进章节目录（每次进程内每目录只写一次）"""
    return mark_processed_for_dir(
        os.path.join(cache_root, str(comic_id), str(chapter_id)), adapter,
        chapter_id=chapter_id, comic_id=comic_id)


def _processed_marker_version(d, adapter):
    """读取章节目录中该源的处理版本；缺失或损坏标记均视为未知。"""
    try:
        with open(os.path.join(d, PROCESS_MARKER), encoding="utf-8") as f:
            data = json.load(f) or {}
        return (data.get(getattr(adapter, "key", "?")) or {}).get("version")
    except Exception:
        return None


def is_processed_dir_current(d, adapter, chapter_id=None, comic_id=None):
    """目录是否可直接读取。

    没有图片后处理的源不受处理版本约束。带处理版本的源只有 marker 与
    当前算法一致时才能直出，避免旧算法写下的花图在修复后仍被长期缓存复用。
    """
    want = chapter_process_version(adapter, chapter_id, comic_id)
    return want <= 0 or _processed_marker_version(d, adapter) == want


def invalidate_stale_processed_dir(d, adapter, chapter_id=None, comic_id=None):
    """清除旧处理版本的单章图片，返回是否执行了清理。

    必须在写入第一张新图前完成：若仅重下当前页就写新 marker，剩余旧页会被
    当作当前版本直接读出。只删图片和处理标记，不触碰书库、历史或任务元数据。
    调用方负责与同一下载器的写入互斥。
    """
    if is_processed_dir_current(d, adapter, chapter_id, comic_id):
        return False
    try:
        for name in os.listdir(d):
            if name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")) \
                    or name == PROCESS_MARKER:
                try:
                    os.remove(os.path.join(d, name))
                except OSError:
                    pass
    except OSError:
        pass
    _marked_dirs.discard((d, getattr(adapter, "key", "?")))
    return True


def mark_processed_for_dir(d, adapter, chapter_id=None, comic_id=None):
    """同上，但直接给章节目录（下载器手里只有图片的缓存路径，没有 comic_id）"""
    try:
        key = (d, getattr(adapter, "key", "?"))
        if key in _marked_dirs:
            return
        p = os.path.join(d, PROCESS_MARKER)
        data = {}
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f) or {}
            except Exception:
                data = {}
        want = chapter_process_version(adapter, chapter_id, comic_id)
        cur = (data.get(getattr(adapter, "key", "?")) or {}).get("version")
        if cur == want:
            _marked_dirs.add(key)
            return
        os.makedirs(d, exist_ok=True)
        data[getattr(adapter, "key", "?")] = {"version": want, "at": time.time()}
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)
        _marked_dirs.add(key)
    except Exception:
        pass          # 标记只是"重建依据"，写失败不影响图片本身


class StorageWriteError(MangaError):
    """**本地写入失败**（目录不可写 / 磁盘满 / 权限不足）。

    必须与网络错误区分开：实测（2026-09-15）目标目录不可写时，每张图都按
    "网络临时失败"处理并重试，任务长时间停在 running，用户看到的是"一直在下载"，
    而真正原因是**写不进去**——既不诚实也永远等不到结果。
    重试对写失败毫无意义，因此这里直接抛出、由上层终止任务并给出可读原因。
    """


def _valid_image_header(path):
    """校验图片文件头（JPEG/PNG/GIF/WEBP/AVIF），防缓存损坏文件"""
    try:
        with open(path, 'rb') as f:
            h = f.read(12)
    except OSError:
        return False
    if len(h) < 12:
        return False
    if h[:2] == b'\xff\xd8':
        return True
    if h[:8] == b'\x89PNG\r\n\x1a\n':
        return True
    if h[:3] in (b'GIF87a', b'GIF89a'):
        return True
    if h[:4] == b'RIFF' and h[8:12] == b'WEBP':
        return True
    # AVIF：ISO BMFF ftyp box，major brand 仅认 avif/avis——不泛化到任意
    # ftyp（mp4 等同框格式不能当图片缓存）。扩展白名单（.avif）早已允许
    # 该格式落盘，这里补上对应魔数，避免合法 AVIF 被判损坏。
    if h[4:8] == b'ftyp' and h[8:12] in (b'avif', b'avis'):
        return True
    return False


def _cached_bytes(cp, min_size=4096):
    """命中阅读缓存则返回字节，否则 None。

    损坏/过小（<=min_size）或图片头非法一律视为未命中（R11 加固）。
    跟随者复用 leader 结果时可传 min_size=0：那份文件刚由 leader 写入，
    只需"存在 + 魔数合法"，不套用 4096 阈值，否则窄条图会被误判失败。
    """
    try:
        if os.path.getsize(cp) <= min_size or not _valid_image_header(cp):
            return None
        with open(cp, 'rb') as f:
            return f.read()
    except OSError:
        return None


def _curl_engine_available():
    """curl_cffi 是否可用（Android 无可用 wheel，见设计 §4 与阶段 A 结论）"""
    try:
        import curl_cffi  # noqa: F401
        return True
    except Exception:
        return False


# 降级路径的会话：thread-local，避免 Session 跨线程共享（requests.Session 非线程安全）。
# 同主机同 IP 时 `_mount_pinned` 会复用既有受控挂载，因此同一 worker 线程内
# 连续取图仍保持 keep-alive；换主机才会重新挂载。
_REQ_TLS = threading.local()


def _requests_session():
    sess = getattr(_REQ_TLS, "sess", None)
    if sess is None:
        import requests
        sess = requests.Session()
        sess.trust_env = False      # 不走环境/系统代理：绑定语义要求直连（经代理时 pin 会明确拒绝）
        _REQ_TLS.sess = sess
    return sess


def _fetch_image_checked_requests(url, headers, timeout=20):
    """**无 curl_cffi 时的图片取回**（设计 §4 规定的降级路径）。

    与 curl 路径**同一套安全契约**，不是"能联网就行"的宽松实现：
      - 每跳（含首跳）`url_is_public_resolved`：DNS 解析后必须全公网，
        封"公网域名解析到内网"与"302 跳内网"；
      - `pin_requests_session`：**直连时**把建连钉到已校验的 IP（防校验后重绑定），
        无法绑定/解析失败时抛 PinUnavailable / DNSResolveUnavailable，
        **绝不静默退回未绑定连接**；
      - **配了代理时语义不同（0.73.0）**：连接终点是代理而非目标站，目标 IP 绑定
        本就不适用（见 engine/netproxy 的安全边界），此时不因"未能绑定"判不可用，
        但 `url_is_public_resolved` 校验、逐跳重定向、协议限制一律照旧；
      - `allow_redirects=False` + 逐跳校验（跳数上限 MAX_REDIRECTS）；
      - `verify` 保持默认 True（证书校验），不提供关闭开关。

    返回 requests.Response：其 `.status_code/.headers/.content` 与 curl_cffi
    响应对调用方完全同构，故上层（下载器、代理路由、修复补页）无需改动。
    """
    sess = _requests_session()
    cur = url
    # 移动端（WR_PROFILE=mobile）：VPN/代理 App 的 fake-ip（198.18/15 被标准库
    # 判为私网）与 DNS 污染会让"解析校验 + IP 钉绑"误杀正常目标——而系统网络栈
    # （Coil/OkHttp 按域名交给 VPN 远端解析）完全正常。这正是"下载全灭但在线
    # 阅读秒开"的机制（2026-09-25 实测定位）。手机端威胁模型没有"局域网共享
    # 服务"，钉绑/解析失败时退回主机名直连（证书校验与逐跳重定向限制保留），
    # 并如实记日志；桌面严格语义一字不动。
    _mobile = (os.environ.get("WR_PROFILE") or "").strip().lower() == "mobile"
    for hop in range(MAX_REDIRECTS + 1):
        ok, why = url_is_public_resolved(cur)
        if not ok:
            if not _mobile:
                where = "请求目标" if hop == 0 else f"第{hop}跳重定向"
                raise SSRFBlocked(f"{where}被拒绝: {why} ({cur[:120]})")
            print(f"[img-fetch] 解析校验不适用（{why}），移动端退回系统网络栈: "
                  f"{cur[:100]}", flush=True)
        _proxy = _netproxy.current_proxy()
        if _mobile:
            # 钉绑尽力而为：失败不阻断（系统网络栈会正确处理）
            try:
                pinned = pin_requests_session(sess, cur, proxy=_proxy or None)
            except Exception as _pe:
                print(f"[img-fetch] IP 绑定不可用，退回主机名直连: "
                      f"{type(_pe).__name__} ({cur[:80]})", flush=True)
                pinned = False
        else:
            pinned = pin_requests_session(sess, cur, proxy=_proxy or None)
            if not pinned and not _proxy:
                # 直连时"未能绑定"原则上判不可用（与 curl 路径语义对齐，不假装已绑定）。
                # **但配了代理就是另一回事**：连接终点是代理，目标 IP 绑定本就不适用
                # （见 engine/netproxy 的边界说明）——旧实现在这里直接抛 PinUnavailable，
                # 结果是"一配代理，图片全部取不到"（用户要的就是代理解决可达性）。
                raise PinUnavailable(
                    f"requests 降级路径无法对该地址建立受控 IP 绑定: {cur[:120]}")
        try:
            resp = sess.get(cur, headers=headers, timeout=timeout,
                            proxies=_netproxy.proxy_dict(),
                            allow_redirects=False, verify=True)
        except Exception:
            if _mobile and pinned:
                # 钉到的 IP 可能已失效/被污染：解除钉绑，按主机名让系统栈重试一次
                print(f"[img-fetch] 钉绑连接失败，解除钉绑按主机名重试: {cur[:80]}",
                      flush=True)
                from ..urlsec import _unmount_pinned
                _unmount_pinned(sess)
                pinned = False
                resp = sess.get(cur, headers=headers, timeout=timeout,
                                proxies=_netproxy.proxy_dict(),
                                allow_redirects=False, verify=True)
            else:
                raise
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp
        loc = (resp.headers or {}).get("Location") or (resp.headers or {}).get("location")
        if not loc:
            return resp
        cur = urljoin(cur, loc)
    raise SSRFBlocked(f"重定向次数超过上限({MAX_REDIRECTS}): {url[:120]}")


def fetch_image_checked(url, headers, timeout=20, acquire_timeout=None):
    """SSRF 校验 + 禁自动重定向逐跳校验的图片 GET（漫画图片通道共用）。

    模式对齐 engine/fetcher.py `_send_checked`（R35b）：每跳（含首跳）都过
    url_is_public_resolved——DNS 解析后所有地址必须为公网，封掉
    "公网域名解析到内网"（nip.io 等）与"公网 URL 302 跳内网"两类绕过；
    allow_redirects=False 手动跟随，跳数上限 urlsec.MAX_REDIRECTS。

    图片 URL 来自适配器/源站（半可信）：校验失败抛 SSRFBlocked
    （ValueError 子类），调用方记日志、按永久失败处理，不得上抛成 500。
    返回最终非 3xx 响应（status_code 由调用方判定）。

    R74: Session 池复用——逐图下载复用同域 TLS 连接，
    消除每图新建连接的握手开销（jm CDN 跨洋握手实测 ~2s/图，
    复用后 0.4s）。

    acquire_timeout: 借池成员的剩余时间预算（秒），None=不限（现状）。
    为 B01 全链路 deadline 传递预留：调用方把剩余预算换算后传入，
    超时抛 MangaError（本项不实现全链路 deadline）。
    """
    # 引擎选择：优先 curl_cffi（TLS 指纹 + 连接池）；不可用时走 requests 受控降级
    # （Android 无 curl_cffi wheel）。两条路径安全契约一致，见各自 docstring。
    if not _curl_engine_available():
        return _fetch_image_checked_requests(url, headers, timeout)
    _sess, _sess_lock = _acquire_session(timeout=acquire_timeout)
    try:
        cur = url
        for hop in range(MAX_REDIRECTS + 1):
            ok, why = url_is_public_resolved(cur)
            if not ok:
                where = "请求目标" if hop == 0 else f"第{hop}跳重定向"
                raise SSRFBlocked(f"{where}被拒绝: {why} ({cur[:120]})")
            # 连接层绑定（2026-09-10）：与校验同一份解析结果建连，
            # 防"校验后 DNS 改写为内网"的重绑定窗口。pin 为**请求级实际调用**：
            # curl 会话无论是否配置代理都写目标 RESOLVE（经代理建连时该条目不被
            # 使用、写入无害；不改 CurlOpt.PROXY）。失败语义与校验对齐：重新解析
            # 到私网抛 SSRFBlocked（不发出请求）；解析失败抛 DNSResolveUnavailable
            # （可重试，不硬封源）；非 curl 无法绑定则抛 PinUnavailable，
            # 绝不静默退回未绑定连接。
            pin_curl_session(_sess, cur, proxy=_netproxy.current_proxy() or None)
            resp = _sess.get(cur, headers=headers, impersonate="chrome",
                             proxies=_netproxy.proxy_dict(),
                             timeout=timeout, allow_redirects=False)
            if resp.status_code not in (301, 302, 303, 307, 308):
                return resp
            loc = (resp.headers or {}).get("Location") \
                or (resp.headers or {}).get("location")
            if not loc:
                return resp
            cur = urljoin(cur, loc)
        raise SSRFBlocked(f"重定向次数超过上限({MAX_REDIRECTS}): {url[:120]}")
    finally:
        _release_session(_sess_lock)


# R74: curl_cffi Session 池(连接复用)。实测 jm CDN 连接复用后单图
# 0.37s vs 每次新连接 2s+——握手是主要成本。curl_cffi Session 非线程安全,
# 池内每 Session 配独立锁, 借出时独占; 池大小 = 典型并发(下载/阅读)。
# thread-local 方案在 Flask 每请求新线程下每请求都新建连接, 池化才能
# 真正复用跨请求的 keep-alive 连接。
import threading as _threading


def _safe_chapter_dir(chapter_id):
    """章节 ID 来自适配器/源站/_info.json 缓存，未经净化直接拼路径会越出
    cache_root（"../x"）。禁止路径分隔符、.. 与空值。"""
    cid = str(chapter_id or "")
    if not cid or cid in (".", "..") or "/" in cid or "\\" in cid \
            or ".." in cid or "\x00" in cid:
        raise MangaError(f"章节 ID 非法: {cid!r}")
    return cid


_session_pool = []
_session_pool_locks = []
_SESSION_POOL_SIZE = 6
# 同图单飞：跟随者等待 leader 的上限 = max(_FLIGHT_WAIT_MIN, timeout*倍数)。
# 正常单图几分钟内必出结果；超上限说明 leader 挂死，跟随者接管回源。
_FLIGHT_WAIT_MIN = 10.0
_FLIGHT_WAIT_FACTOR = 4
_pool_init_lock = _threading.Lock()
# B02: 统一借还条件变量——任何成员归还都 notify 唤醒等待者，
# 消除"全忙时只等 locks[0]"的队头阻塞。
_pool_avail = _threading.Condition()


def _ensure_pool():
    global _session_pool, _session_pool_locks
    if not _session_pool:
        with _pool_init_lock:
            if not _session_pool:
                from curl_cffi import requests as _creq
                # B02: 先在本局部量完整构建，再一次发布——其他线程要么看到
                # 空（走初始化），要么看到完整池，不会读到半初始化列表。
                sessions = [_creq.Session(impersonate="chrome")
                            for _ in range(_SESSION_POOL_SIZE)]
                locks = [_threading.Lock() for _ in range(_SESSION_POOL_SIZE)]
                _session_pool_locks = locks
                _session_pool = sessions


def _close_pool():
    """进程退出时关闭池内 Session（与 fetcher.Fetcher.close_all 对称）。

    只在能独占某成员时关闭：借出中（成员锁被持）的 Session 正被其它线程
    使用，直接 close 会打断在途请求。非阻塞尝试，拿不到即跳过（退出时
    放弃该连接可接受）。
    """
    for _lk, _s in zip(list(_session_pool_locks), list(_session_pool)):
        if not _lk.acquire(blocking=False):
            continue  # 借出中：不并发关闭正在使用的 Session
        try:
            _s.close()
        except Exception:
            pass
        finally:
            _lk.release()


import atexit as _atexit  # noqa: E402
_atexit.register(_close_pool)


def _acquire_session(timeout=None):
    """借出一个独占 Session（独占语义不变：借出期间其他线程不可复用）。

    非阻塞轮询所有成员；全忙时在 _pool_avail 上等待——任何成员经
    _release_session 归还都会唤醒等待者，等待者醒来后重新扫描空闲成员，
    不再固定等 locks[0]（队头阻塞修复）。

    timeout: 剩余时间预算（秒）。None=不限（现状，向后兼容）；
    超时抛 MangaError。为 B01 全链路 deadline 传递预留的接口。
    """
    _ensure_pool()
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        # 快照当前已发布的列表（初始化后不再变化；测试可整体替换）
        sessions = _session_pool
        locks = _session_pool_locks
        for i, lk in enumerate(locks):
            if lk.acquire(blocking=False):
                return sessions[i], lk
        with _pool_avail:
            # 双重检查：归还通知可能落在"扫描"与"wait"之间——持
            # Condition 锁期间再扫一遍，防丢失唤醒后永久等待。
            for i, lk in enumerate(locks):
                if lk.acquire(blocking=False):
                    return sessions[i], lk
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not _pool_avail.wait(remaining):
                    raise MangaError(f"图片会话池借出超时(预算 {timeout}s 已耗尽)")
            else:
                _pool_avail.wait()


def _release_session(lock):
    """归还成员并唤醒等待者。

    成员锁的释放与 notify 同在 Condition 锁内完成：等待者要么在
    wait 中被唤醒（重新扫描必然看到空闲成员），要么在 wait 前的
    双重检查里直接借到——不存在丢失唤醒窗口。
    """
    with _pool_avail:
        lock.release()
        _pool_avail.notify_all()


def atomic_write_image(cp, content):
    """tmp + os.replace 原子写图。

    阅读端只查目标文件存在性，直写 cp 会让并发读者读到半图。
    写入 cp+'.tmp' 后校验非空且过 _valid_image_header，再 os.replace；
    任何失败清理 tmp。内容非法抛 BadImageError（永久失败，不固化坏缓存，
    也不消耗重试/节流预算）。
    """
    tmp = cp + ".tmp"
    d = os.path.dirname(cp)
    if d:
        os.makedirs(d, exist_ok=True)  # 防御：正常流程 cache_path 已建目录
    try:
        with open(tmp, "wb") as f:
            f.write(content)
        if len(content) == 0 or not _valid_image_header(tmp):
            raise BadImageError("图片内容校验失败（空文件或文件头非法）")
        os.replace(tmp, cp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ImageDownloader:
    def __init__(self, adapter, cache_root, max_workers=4, min_interval=3.0,
                 concurrency=None):
        self.adapter = adapter
        self.cache_root = cache_root
        # 并发：显式 concurrency 优先（阅读通道可放开源限制）；
        # 默认 源 concurrent*2（下载任务节流防风控）
        _conc = concurrency if concurrency else \
            getattr(adapter, 'concurrent', 2) * 2
        self._sem = threading.Semaphore(max(2, _conc))
        self._lock = threading.Lock()
        self._min_interval = min_interval  # 同实例每图最小间隔秒
        self._last_dl_ts = [0.0]
        self._dl_lock = threading.Lock()
        # 同图单飞：预热线程与 /img 请求（或下载任务重试）可能同时要同一张图。
        # 不合并就是**同一张图抓两遍**——jm 这类 CDN 每 IP 带宽/风控受限，
        # 重复抓取直接拖慢用户可见页，也让风控计数翻倍。
        self._flight_lock = threading.Lock()
        self._flight = {}          # cache_path -> Event（leader 完成即 set）

    def _throttle(self):
        """图片下载节流：同源串行间隔，防止高频请求触发 IP 风控标记"""
        if self._min_interval <= 0:
            return
        with self._dl_lock:
            gap = time.time() - self._last_dl_ts[0]
            if gap < self._min_interval:
                time.sleep(self._min_interval - gap)
            self._last_dl_ts[0] = time.time()

    def cache_path(self, comic_id, chapter_id, idx, ext='.webp'):
        # 调用方提供完整根（downloads/{source}/{comic_id} 或 _cache/{source}/{comic_id}），
        # 这里只拼 chapter 目录，与 _manga_media_root 读取路径一致
        chapter_id = _safe_chapter_dir(chapter_id)
        d = os.path.join(self.cache_root, chapter_id)
        _root = os.path.realpath(self.cache_root)
        if not os.path.realpath(d).startswith(_root + os.sep):
            raise MangaError(f"章节 ID 非法: {chapter_id!r}")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{idx:04d}{ext}")

    def get(self, image_url, comic_id, chapter_id, idx, timeout=20):
        """获取图片字节（缓存优先；R11 加固：有效头校验 + 损坏重下）"""
        ext = os.path.splitext(image_url.split('?')[0])[1] or '.webp'
        if ext.lower() not in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.avif'):
            ext = '.webp'
        cp = self.cache_path(comic_id, chapter_id, idx, ext)
        # 混淆源的旧算法缓存即使图片头合法也可能是花图。先在下载器实例锁内
        # 清理整章旧版本，再判断本页命中；不能只重下当前页后写新 marker。
        with self._lock:
            invalidate_stale_processed_dir(os.path.dirname(cp), self.adapter,
                                           chapter_id=chapter_id)
            _hit = _cached_bytes(cp)
        if _hit is not None:
            return _hit, cp
        # ── 同图单飞：同 cache_path 只允许一个回源者，其余等它的结果 ──
        # 语义与 /img 的 P1-4 单飞一致：跟随者不做第二个同图生产者——
        # 命中即直出，未命中即明确失败，由上层冷却/重试处理。
        _mine = threading.Event()      # 本次登记进 flight 表的事件（leader 用）
        with self._flight_lock:
            _wait_ev = self._flight.get(cp)
            _leader = _wait_ev is None
            if _leader:
                self._flight[cp] = _mine
        _pre = None
        if _leader:
            # 清理"损坏/过小文件"只能 leader 做：跟随者此时动手会把 leader
            # 刚写入的文件删掉（实测窄条图 <4096B 会被这样误删 → 用户报错）。
            # 清理前再复核一次缓存：上一个 leader 可能刚好写完。
            _pre = _cached_bytes(cp)
            if _pre is None and os.path.exists(cp):
                try:
                    os.remove(cp)   # 损坏/过小 → 重下
                except OSError:
                    pass
        else:
            # 跟随者：等 leader 落盘（超时只作兜底，避免被挂死链路拖住）
            _wait_ev.wait(max(_FLIGHT_WAIT_MIN, timeout * _FLIGHT_WAIT_FACTOR))
            # leader 刚写入的文件直接直出：只校验"存在 + 图片头"，不套用
            # 4096 的缓存有效性阈值——否则窄条图会被判"同图请求未成功"，
            # 明明文件已在磁盘上却对用户报错
            _pre = _cached_bytes(cp, min_size=0)
            if _pre is None:
                if _wait_ev.is_set():
                    raise MangaError("图片下载失败: 同图请求未成功")
                # leader 远超正常时长仍无结果（挂死）→ 接管：登记**新**事件，
                # 否则之后来的跟随者还在等那个永远不会 set 的旧事件
                _mine = threading.Event()
                with self._flight_lock:
                    self._flight[cp] = _mine
                _leader = True
        try:
            if _pre is not None:
                return _pre, cp          # 复用别人刚落盘的结果，零重复回源
            with self._sem:
                self._throttle()
                # R29(技术评审5.4): 章节/URL 作为显式参数传入——不再写实例属性
                # _cur_ep/_cur_url（多线程并发下载时互相覆盖，导致按章节还原的
                # 图片用错上下文）
                return self._download(image_url, cp, timeout, chapter_id, image_url)
        finally:
            if _leader:
                # 仅当登记的还是本次的事件才摘除（被接管的场景交给接管者）
                with self._flight_lock:
                    if self._flight.get(cp) is _mine:
                        self._flight.pop(cp, None)
                _mine.set()

    def _download(self, image_url, cp, timeout, chapter_id=None, orig_url=None):
        last = None
        # 适配器可给出"单次尝试超时建议"（jm：卡住的图片域要快速失败，交给换域逻辑，
        # 不要一条慢链路硬等 20s 让阅读卡住）
        _hint = getattr(self.adapter, "image_timeout_hint", None)
        if callable(_hint):
            try:
                timeout = min(timeout, int(_hint()))
            except Exception:
                pass
        for attempt in range(3):
            _t0 = time.time()
            try:
                headers = self.adapter.image_headers(image_url)
                url = self.adapter.image_url(image_url)
                # P0-1: 请求前 url_is_public_resolved + 逐跳重定向校验
                # （禁自动跟随，封 302 跳内网）
                resp = fetch_image_checked(url, headers, timeout=timeout)
                if resp.status_code != 200:
                    # on_image_failed 动态刷新（token 过期等）
                    cfg = self.adapter.on_image_failed(image_url)
                    if cfg and attempt < 2:
                        headers = cfg.get('headers', headers)
                        url = cfg.get('url', url)
                        continue
                    raise MangaError(f"HTTP {resp.status_code}")
                ct = resp.headers.get('Content-Type', '')
                content = resp.content
                if len(content) < 500:
                    # R38: 源站坏页/占位图（实测 80-452B 的 1px 条图）——
                    # 永久性失败，不重试直接抛 BadImageError
                    raise BadImageError(f"源站坏页/占位图({len(content)}B)")
                if 'image' not in ct and len(content) < 1000:
                    raise BadImageError(f"非图片响应 {ct}")
                # 源级图片后处理（如 jm 块倒序还原）——上下文由显式参数传入。
                # **失败必须当坏页**（永久失败、不写盘）：旧实现 `except: pass` 会把
                # 没还原成功的乱序图当成功写进缓存，用户看到花图且缓存里那份错误内容
                # 会被一直复用（0.51.0 风险评估 P0-1 §2.3 第 3 条）。
                _post = getattr(self.adapter, "unscramble_image", None)
                if _post:
                    try:
                        content = _post(content, chapter_id or "", orig_url or image_url)
                    except Exception as e:
                        raise BadImageError(f"图片还原失败：{e}") from e
                    # 处理版本按**图片所在目录**记录（下载器没有 comic_id 参数，
                    # 之前误用未定义的 comic_id，被 except 吞掉后标记从来没写成——
                    # 被 tests/test_jm_unscramble_ingest.py 当场抓到）
                    mark_processed_for_dir(os.path.dirname(cp), self.adapter,
                                           chapter_id=chapter_id)
                with self._lock:
                    # P2-7: tmp + 校验 + os.replace 原子落盘，
                    # 读端只查存在性，直写会读到半图
                    try:
                        atomic_write_image(cp, content)
                    except BadImageError:
                        raise
                    except OSError as _we:
                        # 目录不可写/磁盘满：重试没有意义，且必须让上层**立刻**
                        # 知道原因（原实现会当成网络失败反复重试，任务长期卡在 running）
                        raise StorageWriteError(
                            "本地写入失败（存储不可写或空间不足）：%s"
                            % (_we.strerror or _we.__class__.__name__)) from _we
                self._note_image(image_url, time.time() - _t0, True)
                return content, cp
            except BadImageError:
                raise  # 永久失败：不重试
            except StorageWriteError:
                raise  # 本地写不进去：重试无意义，交给上层立刻如实报错
            except SSRFBlocked as e:
                # 半可信图片 URL 被 SSRF 策略拒绝：记日志、快速失败（不重试），
                # 对外只暴露脱敏文案（URL/解析细节留服务端日志）
                print(f"[manga-dl] 图片地址被 SSRF 策略拒绝: {e}", flush=True)
                raise MangaError("图片下载失败: 目标地址被安全策略拒绝") from e
            except Exception as e:
                last = e
                # 失败也回报一次：jm 会据此把劣化域换掉，下一次取图即用新域
                self._note_image(image_url, time.time() - _t0, False)
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        raise MangaError(f"图片下载失败: {last}")

    def _note_image(self, image_url, seconds, ok):
        """把取图耗时/成败回传给适配器（宽松钩子：没有该方法的适配器不做任何事）。

        jm 用它做**图片域质量反馈**：实测同一张图在不同图片域上 1.7s vs 13s，
        而 /setting 给的域并不保证快——慢/失败就换域，避免整场阅读都卡在劣化域上。
        """
        hook = getattr(self.adapter, "note_image_result", None)
        if not callable(hook):
            return
        try:
            hook(image_url, seconds, ok)
        except Exception:
            pass
