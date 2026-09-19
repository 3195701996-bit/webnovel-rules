# -*- coding: utf-8 -*-
"""禁漫天堂适配器（完整：动态服务器列表 + 签名 + 响应解密 + 图片混淆还原）
转写自 venera-configs jm.js
- 动态域名：bytepluses.com 拉取 AES 加密服务器列表（手机 APP 同机制）
- 请求签名：token = md5(time + "18comicAPPContent")
- 响应解密：AES-ECB(md5(time + "185Hcomic3PAPP7R").hex, base64(data))
- 图片：块倒序混淆，PIL 还原
"""
import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

from .base import Comic, ComicDetails, Chapter, MangaAdapter, MangaError

UA = "Mozilla/5.0 (Linux; Android 10; K; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/130.0.0.0 Mobile Safari/537.36"
DOMAIN_URL = "https://rup4a04-c02.tos-cn-hongkong.bytepluses.com/newsvr-2025.txt"
DOMAIN_SECRET = "diosfjckwpqpdfjkvnqQjsik"
RESP_SECRET = "185Hcomic3PAPP7R"
AUTH_SECRET = "18comicAPPContent"
FALLBACK_SERVERS = ["www.cdnhjk.net", "www.cdngwc.cc", "www.cdngwc.net",
                    "www.cdngwc.club", "www.cdnutc.me"]
SCRAMBLE_ID = 220980

# 图片块还原算法的**处理版本**：算法/参数一变，旧缓存就不可信。
# 它会被写进章节目录的 _processed.json，用于"只重建受影响的章节缓存"，
# 而不是让用户清数据/重下整个书库（0.51.0 风险评估 §2.3 第 4 条）。
#   1 = 初版（按固定 5 字符切扩展名：.jpg 会切掉最后一位数字，query 串也算进哈希）
#   2 = 2026-09-15 修复：先剥 query/fragment，再 splitext 取 basename
PROCESS_VERSION = 2
IMG_DOMAIN = "https://cdn-msp.jmapinodeudzn.net"


class JmFormatError(MangaError):
    """源站响应格式不符合预期（信封缺 data、解密结果异常等）。

    与网络/HTTP 错误分开：格式错误**不该**被"重试换服务器/刷域名列表"掩盖——
    那会让"接口变了"表现成"网络不好"。
    """


def _aes_ecb_decrypt(b64data, secret):
    key = hashlib.md5(secret.encode()).hexdigest().encode()
    data = base64.b64decode(b64data)
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    dec = cipher.decryptor().update(data) + cipher.decryptor().finalize()
    txt = dec.decode("utf-8", "replace")
    start, end = txt.find("{"), txt.rfind("}")
    return txt[start:end + 1] if start >= 0 and end > start else txt


# ── 图片域质量（2026-09-14：实测"又变慢"的根因）──
# 实测（同一张 332,958B 的图）：
#   cdn-msp2.jmdanjonproxy.vip  11.0s   ← 引擎当时选中的域
#   cdn-msp.jmapiproxy1.cc      13.0s   ← /setting?app_img_shunt=0&express= 返回的域
#   cdn-msp3.jmapiproxy3.cc     18.3s
#   cdn-msp12.jmdanjonproxy.xyz  1.7s   ← 同一张图
#   cdn-msp.jmapinodeudzn.net    1.7s
# 即：图片内容完全一样，**慢在域**（差 6~10 倍），而 /setting 给出的域并不保证快。
# 旧实现一次定终身：进程启动时取一次 /setting 就再不改，碰上劣化域就整场都慢，
# 单图 20s 超时还会让阅读卡住。这里改为：候选域 + 慢/失败即换域 + 落盘记忆。
IMG_ATTEMPT_TIMEOUT = 15      # 单次取图尝试上限（比默认 20s 略短，仍能容纳 13s 的慢页）
IMG_BAD_COOLDOWN = 600        # 刚失败的域 10 分钟内不再切回去（避免来回跳）
IMG_SLOW_SECONDS = 4.0        # 单图耗时超过它 → 记为"偏慢"（要换域还需有实测更快的候选）
IMG_HOST_TTL = 900            # 候选域列表缓存时长
_IMG_SETTING_VARIANTS = (     # /setting 不同参数会给出不同的图片域（实测）
    "app_img_shunt=0&express=",
    "app_img_shunt=0&express=1",
    "app_img_shunt=1&express=",
)
_hosts_cache = {"ts": 0.0, "hosts": []}



# ── jm API 连接复用（0.69.0）──────────────────────────────────────────────
# 旧实现用 `urllib.request.urlopen()`：它**不复用连接**（默认 Connection: close），
# 于是每次 /search、/album、/chapter 都要重来一遍 TCP + TLS 握手。实测（直连）：
#   /album 首次 6.9s、再次 1.0–1.3s；/chapter 1.8s → 1.1s —— 这一两秒几乎全是握手。
# 改成**每线程一个 requests.Session**（自带连接池 + keep-alive），后续请求复用同一
# 条 TLS 连接；用 threading.local 而不是共享 Session：requests.Session 不是线程安全的。
_jm_api_local = threading.local()


def _jm_api_reset():
    """丢弃当前线程的 API 会话（切换代理后调用，避免继续走旧网络路径）。"""
    s = getattr(_jm_api_local, "sess", None)
    _jm_api_local.sess = None
    if s is not None:
        try:
            s.close()
        except Exception:
            pass


def _jm_api_session():
    """当前线程的 API 会话（懒建；带连接池与重试上限，不自动重试慢请求）。"""
    s = getattr(_jm_api_local, "sess", None)
    if s is None:
        import requests
        from requests.adapters import HTTPAdapter
        s = requests.Session()
        # 连接池：同一 host 复用；max_retries=0 —— 重试策略由上层（轮换服务器）负责，
        # 这里再叠一层会让"慢服务器"的等待翻倍。
        _ad = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        s.mount("https://", _ad)
        s.mount("http://", _ad)
        # 出站代理（0.73.0）：用户配了就经代理，否则直连
        try:
            from .. import netproxy as _np
            _p = _np.proxy_dict()
            if _p:
                s.proxies.update(_p)
        except Exception:
            pass
        _jm_api_local.sess = s
    return s


def _host_of(url):
    try:
        return urlsplit(url).netloc.lower()
    except Exception:
        return ""


# Android 兜底转码的质量（中间格式）。选 JPEG 而不是 PNG 的原因（2026-09-17 实测）：
#   · 源图本身就是**有损 WebP**（142–190KB/张），再转 PNG（~2MB）画质并不会变好，
#     只是体积大 10 倍、编码更慢；
#   · 中间用 JPEG q95（而不是 q92）是为了给"Pillow 置换后再存 JPEG q92"留出余量，
#     避免两代 q92 叠加出现可见损失。
_ANDROID_INTERMEDIATE_QUALITY = 95


def _decode_to_jpeg_via_android(img_bytes):
    """用 Android 自带解码器把任意可识别图片（含 **WebP**）转成 **JPEG** 字节。

    为什么需要它：Chaquopy 打包的 Pillow **没有 WebP 支持**（设备实测
    `PIL.features.check('webp') = False`），而禁漫图片只有 WebP 一种形态
    （同图 .jpg/.png 变体实测返回 502）——没有这条兜底，手机上每张禁漫图都会在
    `Image.open` 处失败，用户看到的就是"加载不了/特别慢"。

    为什么只"转码"、不在 Java 里做分块：设备实测 `Canvas`+`Rect` 的 drawBitmap
    在 Chaquopy 下抛 `IllegalArgumentException: Invalid ID, must be in the range
    [0..16)`；而把像素搬进 Python 列表再搬回去又慢到 30s 超时（1.5M 像素的
    列表转换）。**只做一次原生解码 + 原生编码**（都是毫秒级实现），几何逻辑仍
    交给验证过的 Pillow 路径，是这里最稳的组合。

    0.65.0：编码格式从 PNG 改为 **JPEG q95**（用户拍板）。理由见
    `_ANDROID_INTERMEDIATE_QUALITY`：源图本来就有损，PNG 只增大体积不提画质；
    转 JPEG 后整话体积约降到 1/4，设备上的编码与读图都更快。
    """
    from java import jclass

    BitmapFactory = jclass("android.graphics.BitmapFactory")
    Bitmap = jclass("android.graphics.Bitmap")
    ByteArrayOutputStream = jclass("java.io.ByteArrayOutputStream")

    bmp = BitmapFactory.decodeByteArray(img_bytes, 0, len(img_bytes))
    if bmp is None:
        raise MangaError("Android 解码器无法识别该图片（可能不是图片字节）")
    buf = ByteArrayOutputStream()
    if not bmp.compress(Bitmap.CompressFormat.JPEG, _ANDROID_INTERMEDIATE_QUALITY, buf):
        raise MangaError("Android 编码 JPEG 失败")
    return bytes(buf.toByteArray())


# 兼容旧名（有测试/外部脚本可能还在按老名字调用）
_decode_to_png_via_android = _decode_to_jpeg_via_android


def _permute_blocks_pillow(img_bytes, num):
    """按 jm.js 的分块规则把图片上下分块后**倒序**重排（纯 Pillow 实现）。

    输入字节必须能被 Pillow 解码（JPEG/PNG 都行，见 _decode_to_jpeg_via_android）。
    """
    from PIL import Image
    import io

    im = Image.open(io.BytesIO(img_bytes))
    w, h = im.size
    block = h // num
    rem = h % num
    blocks = []
    for i in range(num):
        start = i * block
        end = start + block + (0 if i != num - 1 else rem)
        blocks.append((start, end))
    out = Image.new(im.mode, (w, h))
    y = 0
    for i in range(len(blocks) - 1, -1, -1):
        s0, e0 = blocks[i]
        out.paste(im.crop((0, s0, w, e0)), (0, y))
        y += (e0 - s0)
    buf = io.BytesIO()
    # 保留输入格式（PNG→PNG 无损、JPEG→JPEG）：既有用例锁着"还原后逐像素等于原图"，
    # 一律转 JPEG 会引入有损误差（实测被该用例挡住）。
    _fmt = (im.format or "JPEG").upper()
    if _fmt == "JPEG":
        out.save(buf, format="JPEG", quality=92)
    else:
        out.save(buf, format=_fmt)
    return buf.getvalue()


# 单话相册（详情没有 series，chapter_id 就是相册 id）在 0.65.0 之前会被
# "整本相册不做块还原"这条捷径跳过还原，写下的图是**乱序**的。只有这一类目录
# 需要作废重建，所以给它一个更高的版本号；其余章节沿用 PROCESS_VERSION，
# 避免一次版本提升把用户已下好的图全部重下（实测每页 CDN 约 2.8s）。
ALBUM_ONLY_PROCESS_VERSION = 3


class Jm(MangaAdapter):
    # 类属性：能力/版本查询走这里，避免为了读元信息而实例化适配器（见 manager.adapter_meta）
    PROCESS_VERSION = PROCESS_VERSION
    key = "jm"
    name = "禁漫天堂"
    version = "1.4.0"
    concurrent = 2

    def __init__(self, state_dir=None):
        super().__init__(state_dir)
        self._servers = list(FALLBACK_SERVERS)
        self._img_domain = IMG_DOMAIN
        self._img_refreshed = False
        self._img_bad_hosts = {}      # host → 劣化计数（仅本进程，用于域切换决策）
        self._domain_probed = False   # 本进程是否已用真实图片探过域质量
        self._srv_cost = {}           # API 服务器 → 本进程实测最快耗时
        self._host_cost = {}          # 图片域 → 本进程实测最快耗时
        self._host_bad_ts = {}        # 图片域 → 最近失败时间（冷却期内不切回去）
        self._load_state()

    def _ensure_img_domain(self):
        """首次使用时确定图片域。

        与旧实现的区别：**不再无条件采信 /setting**。实测 /setting 会给出劣化域
        （同一张图 13s vs 1.7s），无条件覆盖会把"本来快的域"换成慢的。现在把
        /setting 的返回当作**候选**之一，只有在"当前域未知/被标记劣化"时才切换。
        """
        if self._img_refreshed:
            return
        self._img_refreshed = True
        # 关键：**不再在搜索/详情里同步等 /setting**——实测这一步在劣化时段要
        # 十几秒到几十秒，而它只是"拿候选图片域"，对搜索和详情没有必要。
        # 只有当连一个域都没有时（首次运行且状态文件为空）才同步取一次；
        # 其余情况留到真正要取图时（images()）再探，那里有真实图片可以测速。
        if self._img_domain:
            return
        try:
            setting_host = self._fetch_setting_host()
        except Exception:
            setting_host = ""
        if setting_host:
            self._remember_host(setting_host)
            self._img_domain = setting_host if setting_host.startswith("http") \
                else "https://" + setting_host
            self._save_state()
            print(f"[jm] 图片域初始化 → {self._img_domain}", flush=True)

    def _note_server(self, host, seconds):
        """记录 API 服务器耗时（本进程）：快的排前面，慢的沉底"""
        if not host:
            return
        self._srv_cost[host] = min(self._srv_cost.get(host, 99.0), seconds)

    def _rotate_server(self, host):
        """把服务器列表里**比当前更快**的那台换到第一位，返回新服务器名。

        实测同一个 /chapter 请求：www.cdnhjk.net 2.3s、www.cdngwc.net 5.7s、
        www.cdngwc.club 6.3s、www.cdngwc.cc 10.1s——固定用第一台会在它变慢时
        让整场阅读都慢。这里按已记录的耗时挑一台更快的，没有则用下一台。
        """
        if len(self._servers) < 2:
            return ""
        cur = host or self._servers[0]
        rest = [s for s in self._servers if s != cur]
        if not rest:
            return ""
        faster = sorted(rest, key=lambda s: self._srv_cost.get(s, 99.0))
        nxt = faster[0]
        if self._srv_cost.get(nxt, 99.0) >= self._srv_cost.get(cur, 99.0):
            # 没有更快的记录 → 至少轮换一台，避免一直撞同一台
            nxt = rest[0]
        self._servers = [nxt] + [s for s in self._servers if s != nxt]
        self._save_state()
        print(f"[jm] API 服务器轮换：{cur} → {nxt}", flush=True)
        return nxt

    def _fetch_setting_host(self):
        """取 /setting 的 img_host（带候选域缓存：TTL 内不重复请求）"""
        now = time.time()
        if _hosts_cache["hosts"] and now - _hosts_cache["ts"] < IMG_HOST_TTL:
            return _hosts_cache["hosts"][0]
        hosts = []
        for qs in _IMG_SETTING_VARIANTS[:2]:      # 最多两个变体：够了就停，别耗时间
            try:
                j = json.loads(self._get(f"{self.base_url}/setting?{qs}", timeout=6))
                h = (j.get("img_host") or "").strip()
                if h:
                    if not h.startswith("http"):
                        h = "https://" + h
                    if h not in hosts:
                        hosts.append(h)
                if len(hosts) >= 2:
                    break
            except Exception:
                continue
        if hosts:
            _hosts_cache["hosts"] = hosts
            _hosts_cache["ts"] = now
        return hosts[0] if hosts else ""

    def candidate_img_hosts(self):
        """候选图片域（当前域优先，其次 /setting 的各个变体）"""
        out = []
        for h in [_host_of(self._img_domain)] + [_host_of(u) for u in _hosts_cache["hosts"]]:
            if h and h not in out:
                out.append(h)
        return out

    def _remember_host(self, host):
        h = _host_of(host) or host
        if not h:
            return
        if h not in _hosts_cache["hosts"]:
            _hosts_cache["hosts"].insert(0, ("https://" + h) if not h.startswith("http") else h)
            _hosts_cache["ts"] = time.time()

    def prefer_img_host(self, host, reason=""):
        """切换当前图片域（并落盘，重启后仍生效）"""
        if not host:
            return False
        if not host.startswith("http"):
            host = "https://" + host
        if host == self._img_domain:
            return False
        old = self._img_domain
        self._img_domain = host
        # 旧域也要记进候选：切换前已经发出去的图片 URL 仍指向它，
        # image_url() 要能把它们改写到新域（否则读者手里那批地址永远走慢域）
        self._remember_host(old)
        self._save_state()
        print(f"[jm] 图片域切换：{old} → {host}（{reason or '更优'}）", flush=True)
        return True

    def image_url(self, image_url):
        """把已知图片域上的 URL 改写到**当前首选域**。

        这样切换域之后，阅读通道与后台预热（都走下载器 → image_url）会立即改用新域，
        不需要改任何调用方。非图片域（或未知域）的 URL 原样返回。
        """
        host = _host_of(image_url)
        if not host or host in ("", "localhost"):
            return image_url
        cur = _host_of(self._img_domain)
        known = set(self.candidate_img_hosts()) | {cur}
        known.discard("")
        if host in known and cur and host != cur:
            return urlsplit(image_url)._replace(netloc=cur).geturl()
        return image_url

    def note_image_result(self, url, seconds, ok, switched=None):
        """域质量反馈：**失败**→ 换域；**只是慢**→ 仅当有实测明显更快的候选才换。

        为什么这么保守：实测 jm 的图片 CDN 会整体性间歇劣化（同一张图 1.7s ↔ 18s ↔
        超时）。全都在慢的时候来回换域毫无收益，还会让每个域名都背上一次失败记录；
        所以"慢"只在有**实测**更快的候选时切换，"失败"才无条件换。
        """
        if switched is not None:
            return switched
        host = _host_of(url)
        cur = _host_of(self._img_domain)
        if not host:
            return False
        if ok and seconds is not None:
            self._host_cost[host] = min(self._host_cost.get(host, 99.0), seconds)
            if seconds <= IMG_SLOW_SECONDS:
                return False
        else:
            self._host_bad_ts[host] = time.time()
        if host != cur:
            return False
        now = time.time()
        # 冷却期内的域不作为切换目标（刚失败过）
        cands = [h for h in self.candidate_img_hosts()
                 if h != cur and now - self._host_bad_ts.get(h, 0) > IMG_BAD_COOLDOWN]
        if not cands:
            return False
        if ok:
            # 慢：要有实测更快的候选
            cur_cost = self._host_cost.get(cur, 99.0)
            faster = [h for h in cands
                      if self._host_cost.get(h, 99.0) < min(cur_cost, IMG_SLOW_SECONDS)]
            if not faster:
                return False
            nxt = min(faster, key=lambda h: self._host_cost.get(h, 99.0))
            return self.prefer_img_host(
                nxt, reason="当前域 %.1fs 偏慢，备选实测更快" % seconds)
        nxt = min(cands, key=lambda h: self._host_cost.get(h, 99.0))
        return self.prefer_img_host(nxt, reason="当前域取图失败")

    def image_timeout_hint(self):
        """给下载器的单次尝试超时建议。

        取 15s（默认 20s 略短）：既能容纳实测 13s 的慢页，又不至于在真正卡死的
        域上白等 20s×3 次重试。**不再用更短的 8s**——那会把"慢但能成"变成失败，
        实测在 13~15s 的时段会直接给用户坏图。
        """
        return IMG_ATTEMPT_TIMEOUT

    def _state_path(self):
        return os.path.join(self.state_dir, "jm_state.json") if self.state_dir else None

    def _load_state(self):
        p = self._state_path()
        if p and os.path.exists(p):
            try:
                st = json.load(open(p, encoding="utf-8"))
                if st.get("servers"):
                    self._servers = st["servers"]
                if st.get("img_domain"):
                    self._img_domain = st["img_domain"]
            except Exception:
                pass

    def _save_state(self):
        p = self._state_path()
        if not p:
            return
        os.makedirs(os.path.dirname(p), exist_ok=True)
        json.dump({"servers": self._servers, "img_domain": self._img_domain},
                  open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    def refresh_domains(self):
        """拉取并解密当前可用服务器列表（手机 APP 同机制）"""
        try:
            req = urllib.request.Request(DOMAIN_URL, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=12) as r:
                raw = r.read().decode("utf-8-sig", "replace")
            clean = re.sub(r"\s+", "", raw)
            j = json.loads(_aes_ecb_decrypt(clean, DOMAIN_SECRET))
            servers = j.get("Server", [])[:5]
            if servers:
                self._servers = servers
                self._save_state()
                print(f"[jm] 服务器更新: {servers}", flush=True)
                return True
        except Exception as e:
            print(f"[jm] 域名刷新失败: {e}", flush=True)
        return False

    def refresh_img_domain(self):
        """从 /setting 获取图片域"""
        try:
            res = self._get(f"{self.base_url}/setting?app_img_shunt=0&express=")
            j = json.loads(res)
            if j.get("img_host"):
                self._img_domain = j["img_host"]
                self._save_state()
        except Exception:
            pass

    @property
    def base_url(self):
        return f"https://{self._servers[0]}"

    def _get(self, url, timeout=15):
        t = int(time.time())
        token = hashlib.md5(f"{t}{AUTH_SECRET}".encode()).hexdigest()
        headers = {
            "User-Agent": UA, "token": token, "tokenparam": f"{t},2.0.16",
            "Referer": "https://localhost/", "Origin": "https://localhost",
            "Accept": "*/*", "X-Requested-With": "com.example.app",
        }
        _t0 = time.time()
        try:
            # 会话复用（keep-alive）：省掉每次请求的 TCP+TLS 握手
            _r = _jm_api_session().get(url, headers=headers, timeout=timeout)
            if _r.status_code != 200:
                raise MangaError(f"HTTP {_r.status_code}")
            body = json.loads(_r.content)
            # 顺手记录服务器耗时：慢的服务器要沉底，别让每次请求都等它
            self._note_server(_host_of(url), time.time() - _t0)
        except Exception as e:
            # 先**轮换服务器**（同一次运行内就有多个可用域，实测 /chapter 在不同
            # 服务器上 2.3s ~ 10.1s），再考虑刷新域名列表——旧实现一失败就去刷
            # 域名列表，遇到慢服务器要等满超时（实测最坏 50s+）。
            nxt = self._rotate_server(_host_of(url))
            if nxt:
                url2 = re.sub(r"https://[^/]+", f"https://{nxt}", url)
                try:
                    _r2 = _jm_api_session().get(url2, headers=headers, timeout=timeout)
                    if _r2.status_code != 200:
                        raise MangaError(f"HTTP {_r2.status_code}")
                    body = json.loads(_r2.content)
                    self._note_server(nxt, time.time() - _t0)
                    # **必须与正常路径同口径**：正常路径返回的是"解密后的明文 JSON 文本"。
                    # 这里原先直接 return body（未解密的信封 dict），于是调用方的
                    # json.loads(...) 撞上 dict → TypeError，
                    # 表现为"某个服务器慢的时候，禁漫的章节/图片整段失败"
                    # （0.74.3 验证流程实测到：TypeError: the JSON object must be
                    #  str, bytes or bytearray, not dict）。类型与内容都要一致。
                    return self._decrypt_body(body, t)
                except JmFormatError:
                    # 换域后**格式仍然不对**：这是真问题（源站改版/接口变了），
                    # 不能被"首台服务器失败"这句掩盖，也不能再往下刷域名列表。
                    raise
                except Exception:
                    pass
            if self.refresh_domains():
                url = re.sub(r"https://[^/]+", self.base_url, url)
                return self._get(url, timeout)
            raise MangaError(f"禁漫请求失败: {e}") from e
        return self._decrypt_body(body, t)

    @staticmethod
    def _decrypt_body(body, t):
        """统一出口：解密信封 → 明文 JSON 文本。

        正常路径与"换服务器重试"路径**必须**都走这里——两处各写一遍必然漂移，
        而这次漂移的代价是"慢服务器一出现，禁漫章节/图片就 TypeError"
        （见 _get 里的注释与 tests/test_jm_server_rotate_decode.py）。
        """
        if isinstance(body, dict) and isinstance(body.get("data"), str):
            return _aes_ecb_decrypt(body["data"], f"{t}{RESP_SECRET}")
        raise JmFormatError("禁漫响应格式异常（信封里没有 data 字段）")

    # ── 搜索 ──
    # 原站排序参数（APP API 实证有效）：
    #   mr=最近更新  mv=最多观看  mp=最多图片  tf=最多点赞
    #   tr=评分  md=评论
    ORDER_PARAMS = {
        "mr": "最近更新", "mv": "最多观看", "mp": "最多图片",
        "tf": "最多点赞", "tr": "评分", "md": "评论",
    }

    def _comic_from_item(self, c):
        """列表项 → Comic。搜索结果与分类浏览（/categories/filter）字段完全一致，
        因此共用同一套映射——两处各写一遍必然漂移（封面/标签漏一个就看不出来）。"""
        cid = str(c.get("id", ""))
        if not cid:
            return None
        author = c.get("author") or ""
        if not isinstance(author, str):
            author = str(author)
        tags = []
        cat = c.get("category") or {}
        sub = c.get("category_sub") or {}
        if cat.get("title"):
            tags.append(cat["title"])
        if sub.get("title"):
            tags.append(sub["title"])
        return Comic(id=cid, title=c.get("name", ""), author=author,
                     cover=self.get_cover_url(cid), tags=tags,
                     source_key=self.key)

    def _comics_from_body(self, body):
        out = []
        for c in (body.get("content") or []):
            comic = self._comic_from_item(c)
            if comic is not None:
                out.append(comic)
        return out

    def search(self, keyword, page=1, order="mr"):
        self._ensure_img_domain()
        o = order if order in self.ORDER_PARAMS else "mr"
        kw = urllib.parse.quote(keyword).replace("%20", "+")
        url = f"{self.base_url}/search?search_query={kw}&o={o}"
        if page > 1:
            url += f"&page={page}"
        return self._comics_from_body(json.loads(self._get(url)))

    # ── 排行 / 分类浏览 ──
    # 端点（APP API 实测）：
    #   GET /categories          → 顶层分类（带 slug / 子分类）
    #   GET /categories/filter?page=N&o=<order>&c=<slug>  → 漫画列表（content[]）
    # 排序参数 o 与搜索一致（mr/mv/mp/tf/tr/md）；c=<slug> 过滤顶层分类。
    # 只暴露**顶层分类**：子分类 slug 在不同父类下重名（「漢化」在 同人/單本/短篇
    # 下都有），单独当筛选条件会串味（实测 c=chinese 返回的是韓漫），宁缺勿错。
    ORDER_GROUP = "排行（全站）"
    CATEGORY_GROUP = "分类"

    # 分类清单是**每次拉探索页都会被问到**的（清单接口要为每个适配器算一遍），
    # 而它本身极少变：这里做进程内短 TTL 缓存，否则探索页会被 jm 的一次网络往返
    # （最坏 15s 超时）拖住——源站抖一下整页都慢。
    _CATS_CACHE = {"at": 0.0, "data": []}
    _CATS_TTL = 900

    def categories(self):
        """可浏览入口：全站排行（o=mr/mv/...）+ 顶层分类（c=<slug>）。"""
        cats = [{"key": f"o:{k}", "name": self.ORDER_PARAMS[k], "group": self.ORDER_GROUP}
                for k in ("mr", "mv", "mp", "tf", "tr", "md")]
        seen = set()
        for c in self._top_categories():
            slug = (c.get("slug") or "").strip()
            if not slug or slug in seen:
                continue          # slug 为空的那条就是"最新A漫"（= o:mr），不重复列
            seen.add(slug)
            cats.append({"key": f"c:{slug}", "name": c.get("name") or slug,
                         "group": self.CATEGORY_GROUP})
        return cats

    def _top_categories(self, timeout=15):
        """顶层分类原始列表（失败就返回空——分类入口拿不到不该让整页报错）。"""
        cache = type(self)._CATS_CACHE
        now = time.time()
        if cache["data"] and (now - cache["at"]) < self._CATS_TTL:
            return cache["data"]
        try:
            body = json.loads(self._get(f"{self.base_url}/categories", timeout=timeout))
        except Exception as e:
            print(f"[jm] 分类列表获取失败: {type(e).__name__}: {e}", flush=True)
            return []
        cats = [c for c in (body.get("categories") or []) if isinstance(c, dict)]
        if cats:
            cache["data"], cache["at"] = cats, now
        return cats

    def _probe_domains_once(self, sample_url):
        """首个章节取址时，用**真实图片**探一次域质量（每进程一次）。

        为什么值得花这一两秒：实测劣化域单图 11~18s，整话几十页就是几分钟；
        先花 1~2s 选对域，后面每张都只要 1.7s。当前域够快时只多一个往返。
        """
        if self._domain_probed or not sample_url:
            return
        self._domain_probed = True
        if len(self.candidate_img_hosts()) < 2:
            # 候选不足 → 这时才去要 /setting（有真实图片可测速，代价可接受）
            try:
                h = self._fetch_setting_host()
                if h:
                    self._remember_host(h)
            except Exception:
                pass
        cands = self.candidate_img_hosts()
        cur = _host_of(self._img_domain)
        order = [cur] + [h for h in cands if h != cur]
        best = None
        for h in order[:3]:
            u = urlsplit(sample_url)._replace(netloc=h).geturl()
            t = time.time()
            try:
                from .downloader import fetch_image_checked
                r = fetch_image_checked(u, self.image_headers(u),
                                        timeout=IMG_ATTEMPT_TIMEOUT)
                dt = time.time() - t
                if getattr(r, "status_code", 0) == 200 and len(getattr(r, "content", b"")) > 500:
                    if best is None or dt < best[1]:
                        best = (h, dt)
                    if dt <= IMG_SLOW_SECONDS:
                        break        # 够快就定了，不再继续探
            except Exception:
                continue
        if best:
            self._host_cost[best[0]] = min(self._host_cost.get(best[0], 99.0), best[1])
        if best and best[0] != cur:
            cur_cost = self._host_cost.get(cur, 99.0)
            # 只有"明显更快"才值得换（否则换过去也一样慢，还多一次往返）
            if best[1] <= max(IMG_SLOW_SECONDS, cur_cost * 0.5):
                self.prefer_img_host(best[0], reason="实测更快 %.1fs" % best[1])
            else:
                print("[jm] 备选域 %s 实测 %.1fs，不比当前域(%s %.1fs)快，保持不动"
                      % (best[0], best[1], cur, cur_cost), flush=True)
        elif best:
            print("[jm] 图片域实测可用：%s（%.1fs）" % (cur, best[1]), flush=True)

    def browse(self, category="o:mr", page=1):
        """按排行/分类浏览。category 形如 "o:mv"（排行）或 "c:doujin"（分类）。"""
        self._ensure_img_domain()
        try:
            page = max(1, int(page or 1))
        except (TypeError, ValueError):
            page = 1
        key = str(category or "o:mr")
        if key.startswith("c:"):
            slug = key[2:].strip()
            url = (f"{self.base_url}/categories/filter?page={page}"
                   f"&c={urllib.parse.quote(slug)}&o=mr")
        else:
            o = key[2:] if key.startswith("o:") else key
            if o not in self.ORDER_PARAMS:
                o = "mr"
            url = f"{self.base_url}/categories/filter?page={page}&o={o}"
        return self._comics_from_body(json.loads(self._get(url)))

    @staticmethod
    def normalize_id(comic_id):
        """禁漫码归一化：JM1234567 / jm1234567 / 1234567 指向同一 album。
        原实现用 startswith("jm") 逐处剥前缀——大小写敏感（JM 前缀漏剥，
        导致 album?id=JM123 请求失败），且散落多处易漏改。"""
        s = str(comic_id or "").strip()
        m = re.match(r"^(?:jm)?\s*(\d+)$", s, re.I)
        return m.group(1) if m else s

    def get_cover_url(self, cid):
        return f"{self._img_domain}/media/albums/{self.normalize_id(cid)}_3x4.jpg"

    # ── 详情与章节 ──
    def comic_info(self, comic_id):
        self._ensure_img_domain()
        cid = self.normalize_id(comic_id)
        j = json.loads(self._get(f"{self.base_url}/album?id={cid}"))
        chapters = []
        for e in sorted((j.get("series") or []), key=lambda x: x.get("sort", 0)):
            eid = str(e.get("id", ""))
            if not eid:
                continue
            name = (e.get("name") or "").strip() or f"第{e.get('sort')}話"
            chapters.append(Chapter(id=eid, name=name, group="正篇"))
        if not chapters:
            # 详情里没有 series：这一话的 id 其实是**相册 id**，与系列的 ep id 不是
            # 同一套编号。记下来，图片还原时不做块倒序（不拿它去算哈希）。
            chapters.append(Chapter(id=cid, name="第1話"))
            try:
                self._album_only_seen().add(str(cid))
            except Exception:
                pass
        tags = [t for t in (j.get("tags") or []) if t]
        author = j.get("author") or []
        if isinstance(author, list):
            author = ", ".join(str(a) for a in author)
        upd = ""
        try:
            upd = time.strftime("%Y-%m-%d", time.localtime(int(j.get("addtime", 0))))
        except Exception:
            pass
        return ComicDetails(
            id=cid, title=j.get("name", ""), cover=self.get_cover_url(cid),
            sub_title=self.name, description=j.get("description", ""),
            author=str(author), tags=tags,
            views=str(j.get("total_views", "")),
            likes=str(j.get("likes", "")), update_time=upd,
            chapters=chapters)

    def chapters(self, comic_id):
        return self.comic_info(comic_id).chapters

    def images(self, comic_id, chapter_id):
        # 图片按 chapter_id 取，comic_id 不参与请求（原 cid 变量未被使用）
        j = json.loads(self._get(f"{self.base_url}/chapter?id={chapter_id}"))
        imgs = j.get("images") or []
        urls = [self.get_image_url(chapter_id, img) for img in imgs]
        # 拿到真实图片地址后探一次域质量（每进程一次）：选对了域，整话都快
        try:
            self._probe_domains_once(urls[0] if urls else "")
        except Exception:
            pass
        if urls:
            urls = [self.image_url(u) for u in urls]     # 探完可能已换域
        return urls

    def get_image_url(self, ep_id, image_name):
        return f"{self._img_domain}/media/photos/{ep_id}/{image_name}"

    def image_headers(self, image_url):
        return {"User-Agent": UA, "Referer": "https://localhost/",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}

    # ── 图片混淆还原（块倒序）──

    @staticmethod
    def picture_name(image_url):
        """从图片地址取"参与哈希的文件名"（不含扩展名、不含 query/fragment）。

        这是 0.51.0 风险评估 P0-1 的核心修复点。旧实现是

            image_url.rstrip("/").split("/")[-1][:-5]

        它假定扩展名恒为 5 个字符，于是：
          · `00001.jpg`（4 字符扩展名）会**连最后一位数字一起切掉** → 哈希输入错，
            块数错，倒序拼接按错误边界执行 → 图片出现条带/拼图式错位（用户截图）；
          · `00001.jpg?token=x` 会把 query 串当成文件名的一部分 → 同样错；
          · `.webp/.jpeg` 只是碰巧长度对，属于"看起来对"的巧合。
        正确做法：剥 query/fragment → 取 basename → splitext 去扩展名 → 解码百分号转义。
        """
        if not image_url:
            return ""
        try:
            path = urlsplit(image_url).path or image_url
        except Exception:
            path = image_url.split("?")[0].split("#")[0]
        base = path.rstrip("/").split("/")[-1]
        name = os.path.splitext(base)[0]
        try:
            return urllib.parse.unquote(name)
        except Exception:
            return name

    def _is_album_only(self, chapter_id):
        """是不是"整本相册"（详情里没有 series，章节列表由相册 id 兜底生成）。

        这种 chapter_id 是**相册 id**，和系列的 ep id 不是同一套编号；把它当 ep_id
        去算块数属于"悄悄算了个错的"（风险评估 §2.3 第 2 条）。这里明确不还原，
        并把原因写进日志，而不是让它参与哈希。
        """
        try:
            return str(chapter_id) in self._album_only_seen()
        except Exception:
            return False

    def _album_only_seen(self):
        """惰性初始化"整本相册 id"集合（适配器实例级；线程安全用 GIL 兜底）"""
        try:
            return self._album_only_ids
        except AttributeError:
            self._album_only_ids = set()
            return self._album_only_ids

    def process_version_for(self, chapter_id=None, comic_id=None):
        """该章节目录要求的处理版本（按章节收敛，见 downloader.chapter_process_version）。

        只有"单话相册"需要 3：它的 chapter_id 就是相册 id，旧代码在下载流程里
        把它登记成"整本相册"后**跳过块还原**，存下的是乱序图。判据用
        `chapter_id == comic_id`（单话相册必然如此；连载作品的第 1 话也可能相同，
        那只会多花一次重下，不会漏判）。
        """
        try:
            if chapter_id and comic_id and str(chapter_id) == str(comic_id):
                return ALBUM_ONLY_PROCESS_VERSION
            if chapter_id and self._is_album_only(chapter_id):
                return ALBUM_ONLY_PROCESS_VERSION
        except Exception:
            pass
        return PROCESS_VERSION

    def image_scramble_num(self, ep_id, image_url):
        """计算图片混淆块数（与 jm.js onImageLoad 一致）"""
        try:
            ep = int(str(ep_id).strip())
        except (TypeError, ValueError):
            print(f"[jm] 章节 id 不是数字，跳过块还原：{ep_id!r}", flush=True)
            return 0
        # 0.65.0（**关键修复**）：「整本相册」（详情无 series，chapter_id 就是相册 id）
        # 以前在这里直接 return 0 = **完全不做块还原**，把乱序图原样存盘。
        # 下载流程恰好会先取详情（同一实例登记了相册 id）再取图，于是下载下来的
        # 单话相册**永远是乱的**，还被标记成"当前处理版本"，界面不会给任何提示
        # （用户 2026-09-17 反馈"用禁漫源图片依旧错乱"）。
        #
        # 实测证明这条捷径是错的：拿相册 id 去算哈希是**对的**——
        #   · 1463559（相册型）：原图在 12 块网格上断层 13.7，我们公式给 12；
        #     倒序(12) 后断层降到 1.3–2.4（连续）；
        #   · 1469591（相册型）：原图在 6 块网格上断层 19.5，我们公式给 6；
        #     倒序(6) 后断层 1.1–1.7（连续）；错用 2 块则残留 8.7。
        # 所以相册型**照常还原**，只在日志里说明它不是 series 话。
        if self._is_album_only(ep_id):
            print(f"[jm] 该话为整本相册（详情无 series），仍按相册 id 计算块数还原：{ep_id}",
                  flush=True)
        picture_name = self.picture_name(image_url)
        if ep < SCRAMBLE_ID:
            return 0
        if ep < 268850:
            return 10
        h = hashlib.md5(f"{ep}{picture_name}".encode()).hexdigest()
        rem = ord(h[-1]) % (8 if ep > 421926 else 10)
        return rem * 2 + 2

    def unscramble_image(self, img_bytes, ep_id, image_url):
        """按块倒序还原图片（PIL）。

        失败**必须抛出**（0.51.0 风险评估 §2.3 第 3 条）：旧实现 except 里
        `return img_bytes`，于是没还原成功的乱序图被当成功写进缓存——用户看到的
        就是花图，而且缓存里那份"错误内容"会一直被复用。
        """
        num = self.image_scramble_num(ep_id, image_url)
        if num <= 1 or image_url.split("?")[0].endswith(".gif"):
            return img_bytes
        try:
            return _permute_blocks_pillow(img_bytes, num)
        except Exception as e:
            # 0.63.0：APK 里的 Pillow 缺 WebP（禁漫只给 WebP）→ 先用 Android 解码器
            # 转成 PNG，再走同一条 Pillow 置换路径。
            try:
                _mid = _decode_to_jpeg_via_android(img_bytes)
                _out = _permute_blocks_pillow(_mid, num)
                print("[jm] 图片经 Android 解码器转 JPEG 后还原成功（Pillow 缺 WebP 兜底）",
                      flush=True)
                return _out
            except Exception as e2:                    # noqa: BLE001
                if not isinstance(e2, MangaError):
                    print(f"[jm] Android 兜底转码失败: {type(e2).__name__}: {e2}", flush=True)
            print(f"[jm] 图片还原失败: {e}", flush=True)
            raise MangaError(f"图片块还原失败（{type(e).__name__}: {e}）") from e

    def _unused_legacy_unscramble(self, img_bytes, num):
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(img_bytes))
            w, h = im.size
            block = h // num
            rem = h % num
            # 按 jm.js 分块（最后一块含 remainder）
            blocks = []
            for i in range(num):
                start = i * block
                end = start + block + (0 if i != num - 1 else rem)
                blocks.append((start, end))
            out = Image.new(im.mode, (w, h))
            y = 0
            for i in range(len(blocks) - 1, -1, -1):
                s, e = blocks[i]
                cur = e - s
                out.paste(im.crop((0, s, w, e)), (0, y))
                y += cur
            buf = io.BytesIO()
            out.save(buf, format=im.format or "JPEG")
            return buf.getvalue()
        except Exception as e:
            # 0.63.0（用户反馈"禁漫加载过慢"实测定位）：**APK 里的 Pillow 不带 WebP**
            # （设备实测 PIL.features.check('webp') = False），而禁漫只提供 WebP
            # （同图 .jpg/.png 变体返回 502）→ 每张图都在这里解码失败。
            # 桌面 Pillow 带 libwebp，所以桌面一切正常、只有手机会坏。
            # 兜底：用 **Android 自带的解码器**（BitmapFactory 支持 WebP）做同样的
            # 分块倒序——引擎逻辑仍在 Python 侧，不把解混淆搬到 UI 层。
            try:
                _out = _unscramble_via_android(img_bytes, num)
                if _out:
                    print("[jm] 图片用 Android 解码器还原成功（Pillow 缺 WebP 兜底）",
                          flush=True)
                    return _out
            except Exception as e2:                    # noqa: BLE001
                print(f"[jm] Android 兜底还原也失败: {type(e2).__name__}: {e2}", flush=True)
            print(f"[jm] 图片还原失败: {e}", flush=True)
            raise MangaError(f"图片块还原失败（{type(e).__name__}: {e}）") from e
