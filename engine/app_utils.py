# -*- coding: utf-8 -*-
"""通用工具函数（从 app.py 拆出，减少单体复杂度）：
纯函数，无 Flask 依赖（_safe_seg 依赖 abort 保留在 app.py）。
"""
from contextlib import suppress as _suppress
from datetime import datetime
import hashlib
import json
import os
import re
import tempfile
import threading

try:
    from opencc import OpenCC
    _CC = OpenCC('t2s')
except Exception:
    _CC = None


class StageTimeout(Exception):
    """阶段超过**硬性时限**（不是源站回了错误，是我们主动放弃等待）。

    为什么需要它：适配器自己的 timeout 不可靠——`urllib`/`requests` 在目标域有多个
    地址（IPv4/IPv6、多条 A 记录）时会**逐个**去连，每地址各等一次超时；
    再叠加"换关键词重试"，一次验证能等几分钟（实测漫画侧 nhentai 单源 120 秒）。
    """


def call_bounded(fn, seconds):
    """有时限地调用：超时抛 `StageTimeout`，被放弃的线程是 daemon 自己收尾。

    两个验证流程（漫画 `engine.manga.verify`、小说 `engine.source_verify`）共用这一份，
    避免"只给一侧加了时限、另一侧忘了"。
    """
    import threading as _th
    box = {}

    def _work():
        try:
            box["v"] = fn()
        except Exception as e:                                   # noqa: BLE001
            box["e"] = e
    t = _th.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout=max(1.0, float(seconds)))
    if t.is_alive():
        raise StageTimeout("阶段超过硬性时限 %.0fs" % seconds)
    if "e" in box:
        raise box["e"]
    return box.get("v")


def pick_title_match(items, kw):
    """从搜索结果里挑"这次要读的那本书"：**标题精确匹配 > 包含关键词 > 第一条**。

    不能盲目取第一条：源站的模糊匹配会把同名前缀的书排到前面。2026-09-17 实测
    ixdzs8 对「剑来」的第一条是《青冥等剑来》（89 章），而《剑来》本身（1278 章）
    掉到第 6 条——按"第一条"判定会把它误报成"目录退化"。
    """
    rows = [x for x in (items or []) if isinstance(x, dict)]
    if not rows:
        return {}
    kw = (kw or "").strip()
    if kw:
        for x in rows:
            if (x.get("name") or "").strip() == kw:
                return x
        for x in rows:
            if kw in (x.get("name") or ""):
                return x
    return rows[0]


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _norm(s):
    """简繁归一化：转简体 + 去空白"""
    if not s:
        return ''
    s = re.sub(r'\s+', '', str(s))
    if _CC:
        try:
            return _CC.convert(s)
        except Exception:
            return s
    return s


# 书名分组归一的常见后缀（多源同书不同写法）
_BOOK_SUFFIX_RE = re.compile(
    r'(?:[·:：—\-–\s]*)(?:第?[一二三四五六七八九十百\d]+卷|'
    r'第?[一二三四五六七八九十百\d]+集|第?[一二三四五六七八九十百\d]+部|'
    r'精校|全文|全集|完本|TXT|txt|无删|修订|实体书|典藏|有声)'
    r'[\s·:：—\-–]*$')


def _group_key(name):
    """书名归一化：简繁 + 去后缀 + 去书名号/括号 + 去分类前缀，用于多源分组合并。
    修复：同书不同源标题带《》/【科幻】/[科幻]/「科幻」/科幻： 等差异时无法归并的问题"""
    n = _norm(name)
    n = _BOOK_SUFFIX_RE.sub('', n)
    # 去成对括号内容（圆/方/书名号/角括号），如（同名影视）【科幻】[科幻]
    n = re.sub(r'[（(【\[「『<][^）)】\]」』>]*[）)】\]」』>]', '', n)
    # 去书名号（《剑来》 → 剑来）
    n = re.sub(r'[《》]', '', n)
    # 去分类前缀：形如 "科幻：" / "科幻·" / "科幻，" / "科幻_"（前缀 1-6 字，后跟分隔符）
    n = re.sub(r'^(?:[^：:·_，,]{1,6}?)[：:·_，,]+', '', n)
    # 去首尾空白与残留分隔符
    n = n.strip(' \t·:：_，,')
    # 截断（书名过长视为异常）
    return n[:24]


def _parse_time(s):
    """解析更新时间字符串为时间戳"""
    if not s:
        return 0
    m = re.match(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', s)
    if m:
        try:
            return int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
        except Exception:
            return 0
    m = re.match(r'(\d{4})[-/](\d{1,2})', s)
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 100
    return 0


def _parse_words(s):
    """解析字数（'123万字' / '1234567'）为数字"""
    if not s:
        return 0
    m = re.search(r'([\d.]+)\s*万', s)
    if m:
        try:
            return int(float(m.group(1)) * 10000)
        except Exception:
            return 0
    m = re.search(r'(\d+)', s)
    return int(m.group(1)) if m else 0


def book_key_of(source_uid, book_url):
    h = hashlib.md5(book_url.encode()).hexdigest()[:10]
    return f"{source_uid}_{h}"


def atomic_write(path, data):
    """tmp + os.replace 原子写。

    dirname 为空表示裸文件名（相对当前目录），需回退为 "."，
    否则 makedirs("") 抛 FileNotFoundError。
    序列化失败时清理临时文件，避免留下 .tmp 垃圾。
    """
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # 必须捕获 BaseException：Ctrl+C / SIGTERM 触发的 KeyboardInterrupt
        # 不属于 Exception，只捕 Exception 会在中断时残留 .tmp 垃圾
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def atomic_write_text(path, text):
    """tmp + os.replace 原子写文本（章节缓存/txt 导出共用，语义同 atomic_write）"""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cache_key_of(url):
    """章节 URL → 缓存文件名键。app.py / crawler.py / 历史 fallback 共用——
    此前三处各自实现同一替换规则，任何一处漂移都会造成缓存失联。"""
    return re.sub(r"[^\w\u4e00-\u9fff-]", "_", url)[-60:]



_json_lock = threading.Lock()  # JSON 文件并发读写锁（Kimi 优化）


def _read_json(p, default):
    with _json_lock:
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return default


def _write_json(p, data):
    """原子写入用户数据（收藏/历史/阅读进度）。

    原实现用 open(p,"w") 直接截断目标文件：写入中途进程被杀或断电，
    文件会残留为 0 字节或半截 JSON，_read_json 解析失败后回退默认值，
    表现为收藏夹与阅读进度整体丢失。改为 tmp + os.replace 原子替换。

    注意：失败仅记录日志、不向调用者传播（历史契约，收藏等调用方依赖
    不抛异常）。需要感知写失败的场景请用 update_json。
    """
    with _json_lock:
        try:
            atomic_write(p, data)
        except Exception as e:
            print(f"[manga] 写文件失败 {p}: {e}", flush=True)


def update_json(p, mutator, default=None):
    """读-改-原子替换 在同一临界区内完成（修复读、写分别加锁导致的
    并发互相覆盖：两个请求各读旧值、各自合并、后写覆盖先写）。

    mutator(data) 接收当前数据（文件缺失/损坏时为 default），返回要写
    入的新数据；mutator 抛异常则不写盘并原样向上传播。
    写盘失败同样向上传播（atomic_write 抛什么就抛什么），由调用方
    决定如何响应——不同于 _write_json 的吞掉异常。

    注意 _json_lock 非重入：临界区内不能再调 _read_json / _write_json /
    update_json，故这里内联读写逻辑而非复用这两个函数。
    """
    with _json_lock:
        data = default
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
        new_data = mutator(data)
        atomic_write(p, new_data)
        return new_data



# ── 磁盘类写入错误的归因（2026-09-18 实测缺口）────────────────────────
# 现象：手机上磁盘写满时，小说任务以 "任务异常：OSError（详细错误见任务日志/
# 服务端日志）" 结束、漫画任务以 "下载过程异常中断，可重新启动续传" 结束——
# **两句都没说清是存储问题**，用户只会以为"又坏了"，反复重试同样失败。
# 发布门槛明确要求"存储不足…显示可行动原因"，故在此集中归因（脱敏：只输出
# 固定中文句子，绝不带路径/URL/异常原文）。
try:
    import errno as _errno_mod
except Exception:                                                # noqa: BLE001
    _errno_mod = None

_MSG_NO_SPACE = ("设备存储空间不足（磁盘已满）：请到「设置 → 存储管理」清理后"
                 "再点「继续」，已下载的内容不会丢")
_MSG_QUOTA = ("存储配额已满（系统限制了本应用可用的空间）：请到「设置 → 存储管理」"
              "清理后点「继续」")
_MSG_READONLY = ("无法写入存储（分区只读或权限被拒）：请检查系统存储状态后点「继续」")
_MSG_IO = "写入存储失败（磁盘 I/O 错误）：请稍后点「继续」，或重启设备后再试"


def disk_error_text(exc):
    """磁盘类写入错误 → 可行动中文说明；不是磁盘错误则返回 ""。

    判定顺序：先看 errno（最可靠），再用异常链文本兜底（不同 Python 绑定/
    平台可能不带 errno，实测 requests/chaquopy 下均可能出现）。"""
    if exc is None:
        return ""
    codes = {}
    if _errno_mod is not None:
        for name, msg in (("ENOSPC", _MSG_NO_SPACE), ("EDQUOT", _MSG_QUOTA),
                          ("EROFS", _MSG_READONLY), ("EACCES", _MSG_READONLY),
                          ("EPERM", _MSG_READONLY), ("EIO", _MSG_IO)):
            code = getattr(_errno_mod, name, None)
            if isinstance(code, int):
                codes[code] = msg
    seen, cur, depth = set(), exc, 0
    while cur is not None and depth < 6:
        if id(cur) in seen:
            break
        seen.add(id(cur))
        errno_v = getattr(cur, "errno", None)
        if isinstance(errno_v, int) and errno_v in codes:
            return codes[errno_v]
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
        depth += 1
    # 文本兜底要遍历**整条异常链**（外层常是"保存章节失败"这种包装异常，
    # 真正的原因在内层/__cause__ 里——只看最外层 args 会漏）
    texts, cur, depth = [], exc, 0
    seen2 = set()
    while cur is not None and depth < 6:
        if id(cur) in seen2:
            break
        seen2.add(id(cur))
        with _suppress(Exception):
            texts.append(type(cur).__name__)
            texts.append(str(cur))
        for a in (getattr(cur, "args", ()) or ()):
            if not isinstance(a, BaseException):
                with _suppress(Exception):
                    texts.append(str(a))
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
        depth += 1
    blob = " ".join(texts).lower()
    if "no space left" in blob or "disk full" in blob:
        return _MSG_NO_SPACE
    if "read-only file system" in blob or "permission denied" in blob:
        return _MSG_READONLY
    if "input/output error" in blob:
        return _MSG_IO
    return ""
