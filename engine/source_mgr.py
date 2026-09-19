#!/usr/bin/env python3
"""书源管理：导入（含校验）、存取、启用/禁用"""
import os
import re
import json
import copy
import time
import uuid
import threading

from .rules import RuleEngine
from .fetcher import Fetcher
from .urlsec import url_is_public

# 书源目录唯一事实源在 engine/config.py（WR_SOURCES_DIR 覆盖优先；
# 安卓落 WR_DATA_DIR/sources 并 bootstrap 拷贝；桌面默认项目内 sources/）
from .config import SOURCES_DIR  # noqa: F401  # 保留模块级名供测试 monkeypatch


# R29(技术评审4.3): UID 净化——只允许安全字符，禁止路径穿越
_UID_RE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff._-]{1,80}$")


def _safe_uid(uid):
    """净化并校验书源 UID：非法（含 .. / 路径分隔符 / 超长）返回 None"""
    if not uid or not isinstance(uid, str):
        return None
    uid = uid.strip()
    if not _UID_RE.match(uid) or ".." in uid or uid.startswith("."):
        return None
    return uid


def _source_path(uid):
    uid = _safe_uid(uid)
    if uid is None:
        raise ValueError(f"书源 UID 非法: {uid!r}")
    p = os.path.join(SOURCES_DIR, f"{uid}.json")
    # 双保险：realpath 校验目标必须位于 sources 目录内（防符号链接逃逸）
    _real = os.path.realpath(p)
    _base = os.path.realpath(SOURCES_DIR)
    if not _real.startswith(_base + os.sep):
        raise ValueError("书源路径越界")
    return p


def _atomic_write(path, data):
    """R47: 收敛到 app_utils.atomic_write 单一实现（此前为无 fsync/无清理
    的简化副本）"""
    # 隔离保护（指南 P1-2）：网络探针默认 WR_SOURCES_READONLY=1，
    # 拒绝写书源目录 —— 书源是**用户运行数据**，不该被随手运行的工具改掉
    # （历史工具 verify_sources_live.py 会直接写 sources/ 去"禁用无效源"）。
    if os.environ.get("WR_SOURCES_READONLY") == "1":
        try:
            _p = os.path.abspath(path)
            _root = os.path.abspath(SOURCES_DIR) + os.sep
            if _p.startswith(_root):
                raise RuntimeError(
                    "书源目录只读（探针隔离保护）：确需写入请设 WR_ALLOW_SOURCE_WRITES=1")
        except RuntimeError:
            raise
        except Exception:                                        # noqa: BLE001
            pass

    from .app_utils import atomic_write
    atomic_write(path, data)


# ── P2-2: 书源列表进程内缓存 ──────────────────
# load_all() 此前每次 listdir + 全量读 ~30 个 JSON（书源管理/搜索/任务
# 每个请求都触发）。改为 (name, mtime_ns, size) 指纹感知：指纹不变直接
# 复用缓存列表；书源管理 API 写 sources/*.json 后主动失效（指纹机制兜底）。
_SOURCES_CACHE_LOCK = threading.Lock()
_SOURCES_CACHE = None          # list[dict] | None
_SOURCES_FINGERPRINT = None    # tuple((name, mtime_ns, size), ...) | None


def _sources_fingerprint():
    """SOURCES_DIR 下 *.json 的 (name, mtime_ns, size) 清单指纹；目录缺失返回 None"""
    try:
        return tuple(sorted(
            (f, st.st_mtime_ns, st.st_size)
            for f in os.listdir(SOURCES_DIR) if f.endswith('.json')
            for st in [os.stat(os.path.join(SOURCES_DIR, f))]))
    except OSError:
        return None


def invalidate_sources_cache():
    """写 sources/*.json 后主动失效进程内缓存（mtime 指纹本身也能兜住）"""
    global _SOURCES_CACHE, _SOURCES_FINGERPRINT
    with _SOURCES_CACHE_LOCK:
        _SOURCES_CACHE = None
        _SOURCES_FINGERPRINT = None


def load_all():
    """加载全部书源（含 disabled 标记），返回列表。
    P2-2: 指纹不变时复用进程内缓存；返回深拷贝，防调用方就地改源
    （如 set_enabled 改 enabled 字段）污染缓存"""
    global _SOURCES_CACHE, _SOURCES_FINGERPRINT
    if not os.path.isdir(SOURCES_DIR):
        return []
    with _SOURCES_CACHE_LOCK:
        fp = _sources_fingerprint()
        if fp is not None and fp == _SOURCES_FINGERPRINT \
                and _SOURCES_CACHE is not None:
            return copy.deepcopy(_SOURCES_CACHE)
    out = []
    for f in sorted(os.listdir(SOURCES_DIR)):
        # 跳过点文件：sources/.seed-removed.json 是**删除墓碑**，不是书源
        # （不加这层过滤，它会被当成一个 uid=".seed-removed" 的假书源显示出来）
        if not f.endswith('.json') or f.startswith('.'):
            continue
        try:
            with open(os.path.join(SOURCES_DIR, f), encoding='utf-8') as fh:
                d = json.load(fh)
            d.setdefault('uid', f[:-5])
            out.append(d)
        except Exception:
            continue
    with _SOURCES_CACHE_LOCK:
        # 读取期间目录可能已被改写：仅当指纹仍匹配才回填缓存
        if fp is not None and fp == _sources_fingerprint():
            _SOURCES_CACHE = out
            _SOURCES_FINGERPRINT = fp
    return copy.deepcopy(out)


def load_enabled():
    # 注：valid=False 的源仍参与搜索是有意为之——校验失败多为临时网络/解析
    # 问题（实测 19 源中 2 个属此类），按 valid 过滤会误伤用户已启用的源。
    # 恶意源的防线不在这里：engine/urlsec 已在 Fetcher 层统一拦截内网目标，
    # 持续失败的源另由 _source_health_check 自动禁用。
    #
    # 同 uid 多文件只保留一份（0.74.19，实测缺陷）：仓库里实测 34 文件 / 30 个
    # uid（4 组重复，内容逐字相同）。**这里返回的列表会被逐个发请求**——
    # 不去重就是同一站点被搜两遍、被检验两遍：实测流式搜索 total=18 而唯一
    # uid 只有 15，白花时间也更容易触发源站限流；界面上的"已返回 N/M 个源"
    # 也对不上用户实际拥有的源数。
    # 去重只作用于本函数；load_all() 保持原样，书源页仍能照实提示
    # "有 N 个文件同 uid（配置重复）"。
    out, seen = [], set()
    for s in load_all():
        if not s.get('enabled', True):
            continue
        uid = s.get('uid') or ''
        if uid and uid in seen:
            continue
        if uid:
            seen.add(uid)
        out.append(s)
    return out


def get_by_uid(uid):
    for s in load_all():
        if s.get('uid') == uid:
            return s
    return None


def find_source(key):
    """按 key 定位书源：精确 uid → URL 完全相等 → URL 包含(最短) → 名称包含。
    供搜索/任务等复用（替代各处重复的 _find_source 闭包）"""
    if not key:
        return None
    alls = load_all()
    # 1) 精确 uid
    for x in alls:
        if x.get('uid') == key:
            return x
    # 2) URL 完全相等
    for x in alls:
        if (x.get('bookSourceUrl') or '').rstrip('/') == key.rstrip('/'):
            return x
    # 3) URL 包含（取最短匹配，避免多源同域取错）
    hits = [x for x in alls if key in (x.get('bookSourceUrl') or '')]
    if hits:
        return min(hits, key=lambda x: len(x.get('bookSourceUrl') or ''))
    # 4) 名称包含
    hits = [x for x in alls if key in (x.get('bookSourceName') or '')]
    if hits:
        return min(hits, key=lambda x: len(x.get('bookSourceName') or ''))
    return None


def import_sources(payload, validate=True):
    """
    导入书源。payload 可为：单对象 / 数组 / {"data":[...]} / {"sources":[...]}。
    validate=True 时逐源校验可用性（网络请求）。
    返回 (imported, results)
    """
    items = payload
    if isinstance(payload, dict):
        for k in ('data', 'sources', 'items'):
            if isinstance(payload.get(k), list):
                items = payload[k]
                break
    if not isinstance(items, list):
        items = [items]

    results = []
    imported = 0
    for src in items:
        if not isinstance(src, dict) or not src.get('bookSourceUrl'):
            results.append({'ok': False, 'name': (src or {}).get('bookSourceName', '?'),
                            'error': '缺少 bookSourceUrl'})
            continue
        # R35(SSRF): 导入即触发 validate_source 联网代拉，bookSourceUrl 此前完全
        # 未校验 —— 构造内网地址的书源可把服务变成内网扫描器/SSRF 跳板。
        # 必须在落盘与校验之前拦截。
        if not url_is_public(src.get('bookSourceUrl')):
            results.append({'ok': False,
                            'name': src.get('bookSourceName', '?'),
                            'url': src.get('bookSourceUrl'),
                            'error': '书源地址被拒绝（仅允许公网 http/https 地址）'})
            continue
        # 去重：同 bookSourceUrl 覆盖。
        # R29(技术评审4.3): UID 一律服务端净化生成——客户端传入的 uid 仅作参考，
        # 非法（../、路径分隔符、超长）时回退服务端生成，杜绝路径穿越写入
        uid = _safe_uid(src.get('uid')) or _uid_of(src)
        src['uid'] = uid
        src['enabled'] = src.get('enabled', True)
        src.setdefault('imported_at', time.strftime('%Y-%m-%d %H:%M:%S'))
        # 校验
        check = validate_source(src) if validate else {'ok': True, 'note': '未校验'}
        src['valid'] = check['ok']
        src['valid_error'] = check.get('error', '')
        src['valid_tested_at'] = check.get('tested_at', '')
        _path = _source_path(uid)
        _atomic_write(_path, src)
        # 用户重新加回这个文件 → 解除删除墓碑（否则种子逻辑会一直跳过它）
        clear_removed([os.path.basename(_path)])
        invalidate_sources_cache()  # P2-2: 写点后主动失效书源缓存
        imported += 1
        results.append({'ok': True, 'uid': uid,
                        'name': src.get('bookSourceName'),
                        'url': src.get('bookSourceUrl'),
                        'valid': check['ok'],
                        'error': check.get('error', '')})
    return imported, results


def _uid_of(src):
    name = src.get('bookSourceName') or 'source'
    url = re.sub(r'https?://', '', src.get('bookSourceUrl', ''))
    uid = re.sub(r'[^\w\u4e00-\u9fff.-]', '_', f"{name}_{url}")[:80]
    uid = _safe_uid(uid) or f"src_{uuid.uuid4().hex[:10]}"
    return uid


def _source_files_with_uid(uid):
    """返回**包含该 uid 的所有书源文件** [(路径, 数据)]。

    为什么必须按内容找：文件名不一定等于 uid。导入时按 uid 落盘，但手工放进
    sources/ 的文件常按域名命名（实测有 4 个：ixdzs8.com.json 的 uid 是
    「爱下书_ixdzs8__ixdzs8.com」等）。过去 set_enabled/delete_source 直接拼
    `sources/<uid>.json`，对这些源就**写到了另一个文件**（或删了个不存在的文件）——
    界面显示"已停用"但原文件的 enabled 没变，用户看到的和实际生效的不一致。
    """
    out = []
    if not os.path.isdir(SOURCES_DIR):
        return out
    for fn in sorted(os.listdir(SOURCES_DIR)):
        if not fn.endswith('.json') or fn.startswith('.'):
            continue
        p = os.path.join(SOURCES_DIR, fn)
        try:
            with open(p, encoding='utf-8') as fh:
                d = json.load(fh)
        except Exception:
            continue
        if (d.get('uid') or fn[:-5]) == uid:
            out.append((p, d))
    return out


# ── 删除墓碑：用户删掉的源不许被"随包内置源"在下次启动时又加回来 ──
#
# 实测（2026-09-15）：在 App 里清掉 4 个重复源文件后，**下次启动又被解出 4 个**
# （BundledSources.ensure() 见文件不存在就重新解压）——用户会看到"删了又回来"，
# 与方向基线 §6.4（内置源是产品资源，但用户改动优先）冲突。
# 因此删除动作要留墓碑：种子解压时跳过墓碑里的文件；用户重新导入同名文件即解碑。
TOMBSTONE_FILE = ".seed-removed.json"


def _tombstone_path():
    return os.path.join(SOURCES_DIR, TOMBSTONE_FILE)


def removed_names():
    """已被用户删除/清理的文件名集合（种子解压要跳过它们）"""
    try:
        with open(_tombstone_path(), encoding="utf-8") as f:
            d = json.load(f)
        return set((d or {}).get("files", {}).keys())
    except Exception:
        return set()


def note_removed(names, why="user-delete", backup=""):
    """记录删除墓碑（原子写）。names 为文件名列表（不是路径）。"""
    names = [os.path.basename(n) for n in (names or []) if n]
    if not names:
        return 0
    p = _tombstone_path()
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    d.setdefault("schema", 1)
    files = d.setdefault("files", {})
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    for n in names:
        files[n] = {"ts": ts, "why": why, "backup": backup}
    try:
        _atomic_write(p, d)
    except Exception as e:
        print(f"[sources] 写删除墓碑失败: {e}", flush=True)
        return 0
    return len(names)


def clear_removed(names):
    """用户重新导入/加回同名文件 → 解碑（否则种子逻辑会一直跳过它）"""
    names = [os.path.basename(n) for n in (names or []) if n]
    if not names:
        return 0
    p = _tombstone_path()
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        files = (d or {}).get("files") or {}
    except Exception:
        return 0
    n = 0
    for name in names:
        if name in files:
            files.pop(name, None)
            n += 1
    if n:
        try:
            _atomic_write(p, d)
        except Exception:
            return 0
    return n


def _removed_root():
    """删除/清理出来的源文件放这里（可恢复）。用 engine.config.DATA_DIR，与清理端点一致"""
    try:
        from engine.config import DATA_DIR
    except Exception:
        DATA_DIR = os.path.dirname(os.path.abspath(SOURCES_DIR))
    return os.path.join(DATA_DIR, "sources_removed")


def list_removed_batches():
    """列出"被清理/删除的源文件"批次（供 App 提供**恢复**入口）。

    背景：清理只移动不删除，但备份落在应用私有目录里，用户点不到——
    等于"清错了就找不回来"。这里把备份列出来，让 App 能给出一键恢复。
    """
    root = _removed_root()
    batches = []
    if os.path.isdir(root):
        for name in sorted(os.listdir(root), reverse=True):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            files = []
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".json") or fn.startswith("."):
                    continue
                files.append({"name": fn,
                              "size": os.path.getsize(os.path.join(d, fn)),
                              "already_back": os.path.exists(
                                  os.path.join(SOURCES_DIR, fn))})
            if files:
                batches.append({"dir": name, "files": files})
    return {"batches": batches,
            "total": sum(len(b["files"]) for b in batches),
            # 最近一批（App「一键恢复」真正会恢复的那一批）——界面必须能如实说出
            # "点一次恢复几个"，否则按钮上写"（9 个）"、点完只回来 3 个，
            # 用户会以为又坏了一次。
            "latest_dir": batches[0]["dir"] if batches else "",
            "latest_count": len(batches[0]["files"]) if batches else 0}


def restore_removed(batch=None, names=None, latest=False):
    """从备份恢复源文件：恢复到书源目录并**解除删除墓碑**。

    安全口径：
      · 同名文件已存在 → 跳过并如实报告（绝不覆盖用户当前的文件）；
      · 只从备份目录里取文件（不接受任意路径）；
      · 恢复后解除墓碑，否则种子逻辑会一直把它当"用户删过"的东西。

    `latest=True`：只恢复**最近一个仍有文件的批次**（App 一键恢复的口径）。
    为什么必须有这个参数：App 的确认弹窗写的是"将把**最近一次**清理/删除的源文件
    恢复"，客户端也照发 `{"latest": true}`，但服务端**从不读这个字段** —— 于是
    `batch=None` 落到"遍历所有批次"，把用户当初**故意删掉**的源也一并带回来并
    解除墓碑（内置源升级不再把它当"用户删过"）。用户点的是"恢复最近一次"，
    实际发生的是"恢复全部历史批次"，与弹窗承诺相反。

    返回 {"restored": [...], "skipped": [...], "tombstone_cleared": n, "batches": [...]}
    """
    import shutil
    root = _removed_root()
    restored, skipped = [], []
    batches = []
    if batch:
        batches = [batch]
    elif latest:
        # 与界面同口径：最近一批 = list_removed_batches() 的首个（它已按批次名倒序，
        # 且**只列仍有 .json 的批次**，所以空批次不会被选中）
        cand = list_removed_batches()["batches"]
        batches = [cand[0]["dir"]] if cand else []
    elif os.path.isdir(root):
        batches = sorted(os.listdir(root), reverse=True)
    want = set(names or [])
    used_batches = [b for b in batches if os.path.isdir(os.path.join(root, b))]
    for b in batches:
        d = os.path.join(root, b)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json") or fn.startswith("."):
                continue
            if want and fn not in want:
                continue
            dst = os.path.join(SOURCES_DIR, fn)
            if os.path.exists(dst):
                skipped.append({"name": fn, "why": "书源目录里已有同名文件（不覆盖）"})
                continue
            try:
                src_file = os.path.join(d, fn)
                shutil.copy2(src_file, dst)
                # 恢复成功即把备份那份移走：否则"可恢复"列表里会一直挂着它，
                # 用户看到"还能恢复"其实文件已经在书源目录里了（语义混乱）。
                # 文件已回到 sources/，删备份不丢数据；再删一次会重新生成备份。
                try:
                    os.remove(src_file)
                except OSError:
                    pass
                restored.append(fn)
            except Exception as e:
                skipped.append({"name": fn, "why": f"恢复失败：{e}"})
        # 批次目录空了就顺手删掉，避免留下一堆空目录
        try:
            if not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
    cleared = clear_removed(restored) if restored else 0
    if restored:
        invalidate_sources_cache()
    return {"restored": restored, "skipped": skipped,
            "tombstone_cleared": cleared,
            # 如实回报"这次动的是哪些批次"（处理前先记下来：空批次随后会被删掉，
            # 事后再看就看不到"刚恢复的那一批"了）
            "batches": list(used_batches)}


def delete_source(uid):
    """删除该 uid 的**所有**文件（同 uid 多文件时全部删掉，否则界面显示删了却没删干净）"""
    hits = _source_files_with_uid(uid)
    n = 0
    names = []
    for p, _d in hits:
        try:
            os.remove(p)
            n += 1
            names.append(os.path.basename(p))
        except OSError:
            pass
    if n:
        # 墓碑：否则下次启动种子解压会把它又加回来（实测"删了又回来"）
        note_removed(names, why="user-delete")
        invalidate_sources_cache()  # P2-2
    return n > 0


def set_enabled(uid, enabled):
    """设置启用状态，写入**真正含该 uid 的文件**（不是按 uid 拼出来的路径）"""
    hits = _source_files_with_uid(uid)
    if not hits:
        return False
    for p, d in hits:
        d['enabled'] = bool(enabled)
        _atomic_write(p, d)
    invalidate_sources_cache()  # P2-2
    return True


# 判定"两份文件是不是同一份源、只是文件名不同"时要忽略的字段：
#   enabled —— 启用状态本来就可能被用户在某一份上改过，不算"规则不同"
#   valid / valid_error / valid_tested_at —— 本机校验结果，随环境变化
_DUP_IGNORE_FIELDS = ("enabled", "valid", "valid_error", "valid_tested_at")


def _dup_signature(data):
    """除忽略字段外的规范化指纹（用于判断两份文件内容是否一致）"""
    import json as _json
    clean = {k: v for k, v in (data or {}).items() if k not in _DUP_IGNORE_FIELDS}
    return _json.dumps(clean, sort_keys=True, ensure_ascii=False)


def find_duplicate_sources():
    """找出**重复的源文件**（同一 uid 且同一站点地址的两份文件）并给出处置建议。

    背景：内置源目录里实测有 4 组这样的"双份文件"（同一 uid、同一 bookSourceUrl），
    同一 uid 有两个文件会让"启用/删除"只作用于其中一个，用户看到的行为自相矛盾。
    此前 App 只能提示"建议在网页端或文件系统里清理"——那等于要求用户去用电脑，
    与方向基线 §8.A（源的问题不应要求访问电脑）冲突，所以这里给出可执行的结论。

    安全口径（只提议、不猜）：
      · 除上面忽略字段外**内容完全一致** → 安全：建议保留"已启用的那份"，
        其余列为可清理（清理动作会把文件**移到备份目录**，不是直接删除）；
      · 内容不一致（规则真的不同）→ needs_review：不提议清理，如实说明需要人工确认。

    返回 {"groups": [...], "safe_groups": n, "removable": n, "review_groups": n}
    """
    from collections import OrderedDict
    groups = OrderedDict()
    for fn, data in load_all_with_files():
        uid = (data or {}).get("uid") or fn[:-5]
        url = ((data or {}).get("bookSourceUrl") or "").rstrip("/")
        groups.setdefault((uid, url), []).append((fn, data or {}))
    out = []
    for (uid, url), items in groups.items():
        if len(items) < 2:
            continue
        sigs = {_dup_signature(d) for _, d in items}
        files = [{"name": fn, "enabled": bool(d.get("enabled", True)),
                  "size": len(_json_dumps(d))} for fn, d in items]
        if len(sigs) == 1:
            # 只差启用状态（或完全一致）：保留已启用的那份；都同状态 → 保留 canonical 名字
            enabled = [f for f in files if f["enabled"]]
            if len(enabled) == 1:
                keep = enabled[0]["name"]
            else:
                # 都不启用（或都启用）：保留**名字更短**的那份（通常是域名命名的原始文件，
                # uid.json 风格），名字相同长度时按字典序，保证结果确定、可解释
                cand = [f["name"] for f in files]
                keep = sorted(cand, key=lambda n: (len(n), n))[0]
            remove = [f["name"] for f in files if f["name"] != keep]
            out.append({"uid": uid, "url": url, "files": files, "keep": keep,
                        "remove": remove, "safe": True,
                        "reason": "除启用状态外内容完全一致，保留"
                                  + ("已启用的那份" if len(enabled) == 1
                                     else "规范命名的那份")})
        else:
            out.append({"uid": uid, "url": url, "files": files, "keep": "",
                        "remove": [], "safe": False,
                        "reason": "两份文件的规则内容不同，需人工确认后再处理（不自动清理）"})
    out.sort(key=lambda g: (not g["safe"], g["uid"]))
    return {"groups": out,
            "safe_groups": sum(1 for g in out if g["safe"]),
            "review_groups": sum(1 for g in out if not g["safe"]),
            "removable": sum(len(g["remove"]) for g in out)}


def _json_dumps(d):
    import json as _json
    try:
        return _json.dumps(d, ensure_ascii=False)
    except Exception:
        return ""


def load_all_with_files():
    """[(文件名, 数据)] —— 需要按**文件**粒度操作时用（同一 uid 可能有多个文件）"""
    out = []
    if not os.path.isdir(SOURCES_DIR):
        return out
    for fn in sorted(os.listdir(SOURCES_DIR)):
        # 点文件不是书源（.seed-removed.json 为删除墓碑）
        if not fn.endswith('.json') or fn.startswith('.'):
            continue
        p = os.path.join(SOURCES_DIR, fn)
        try:
            with open(p, encoding='utf-8') as fh:
                out.append((fn, json.load(fh)))
        except Exception:
            continue
    return out


def set_enabled_files(filenames, enabled):
    """按**文件名**批量设置启用状态。

    与 set_enabled_bulk 的区别：这里只动指定的文件，**不牵连同 uid 的其它文件**。
    "按当前状态筛选"（filter=enabled/disabled）必须用这个口径，否则恒等调用会
    顺手把同 uid 的重复文件也一起启用（实测：恒等调用 changed=4，多启用了 4 个重复文件，
    等于悄悄改变了每条源的抓取次数）。
    """
    want = bool(enabled)
    changed = 0
    for fn in filenames:
        p = os.path.join(SOURCES_DIR, fn)
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding='utf-8') as fh:
                d = json.load(fh)
        except Exception:
            continue
        if bool(d.get('enabled', True)) == want:
            continue
        d['enabled'] = want
        _atomic_write(p, d)
        changed += 1
    if changed:
        invalidate_sources_cache()  # P2-2
    return {"changed": changed}


def set_enabled_bulk(uids, enabled):
    """按 uid 批量设置启用状态。

    返回 {"changed": 实际改动的**文件**数, "matched": 命中的 uid 数,
          "missing": [没找到的 uid]}——条数如实回传，界面才能说清"改了几个"。
    """
    want = bool(enabled)
    changed = 0
    matched = 0
    missing = []
    for uid in uids:
        hits = _source_files_with_uid(uid)
        if not hits:
            missing.append(uid)
            continue
        matched += 1
        for p, d in hits:
            if bool(d.get('enabled', True)) == want:
                continue          # 已经是目标状态：不算改动，也不重写文件
            d['enabled'] = want
            _atomic_write(p, d)
            changed += 1
    if changed:
        invalidate_sources_cache()  # P2-2
    return {"changed": changed, "matched": matched, "missing": missing}


# ── 书源校验 ────────────────────────────────

DEFAULT_CHECK_KEYWORD = "剑来"
# R47: CHECK_KEYWORDS 统一从 config 导入（此前与 config.CHECK_KEYWORDS 双份定义）
from .config import CHECK_KEYWORDS  # noqa: E402


def _search_one(fetcher, engine, src, url, cfg, kw, timeout):
    """执行一次搜索，返回 (html 文本或 None, 错误)"""
    base = src.get('bookSourceUrl', '').rstrip('/')
    try:
        clean = url
        if '{{' in clean:
            clean = clean.replace('{{key}}', kw).replace('{{searchKey}}', kw) \
                         .replace('{{page}}', '1')
        if not clean.startswith('http'):
            clean = base + clean
        # R35(SSRF): searchUrl 可为绝对地址，绕过 bookSourceUrl 的公网校验；
        # 拼接后的最终目标必须再校验一次（导入与手动 validate 共用此路径）。
        if not url_is_public(clean):
            return None, "目标地址被拒绝（仅允许公网 http/https 地址）"
        retry = min(int(cfg.get('retry') or 0) + 1, 2)
        headers = cfg.get('headers') if isinstance(cfg.get('headers'), dict) else None
        if str(cfg.get('method', '')).upper() == 'POST':
            body = str(cfg.get('body') or '')
            data = {}
            for kv in body.split('&'):
                if '=' in kv:
                    k, v = kv.split('=', 1)
                    data[k.strip()] = v.replace('{{key}}', kw) \
                                        .replace('{{searchKey}}', kw)
            return fetcher.post(clean, data=data, source=src,
                                timeout=timeout, retries=retry,
                                extra_headers=headers), None
        return fetcher.get(clean, source=src, timeout=timeout, retries=retry,
                           extra_headers=headers), None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:60]}"


def validate_source(src, timeout=25):
    """
    校验书源可用性：
      1. 命中专属适配器 → 用适配器搜索链路校验（规则引擎不识别适配器结构，
         直接套规则会误报"解析失败/无结果"，实测精华书阁等被误报异常）
      2. 有 searchUrl → 用多个热门关键词轮换搜索，任一能解析出书籍（名称+链接）即通过
      3. 无 searchUrl → 标记"不支持搜索"（valid=True 但 note 说明）
    返回 {'ok': bool, 'error': str, 'tested_at': str, 'sample': {...}}
    """
    kw0 = src.get('ruleSearch', {}).get('checkKeyWord') or DEFAULT_CHECK_KEYWORD
    keywords = [kw0] + [k for k in CHECK_KEYWORDS if k != kw0]

    # ── 适配器源优先：走适配器真实搜索（维护修复：规则引擎对适配器源误报）──
    try:
        from .crawler import SourceCrawler
        _c = SourceCrawler(src)
        if _c.adapter is not None:
            last_err = ''
            for kw in keywords:
                try:
                    books = _c.adapter.search(kw, page=1)
                except Exception as e:
                    last_err = f"适配器搜索异常: {type(e).__name__}: {str(e)[:80]}"
                    continue
                if not books:
                    last_err = f"搜索无结果（关键词：{kw}）"
                    continue
                b0 = books[0]
                return {'ok': True, 'error': '', 'note': '适配器校验',
                        'tested_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                        'sample': {'name': b0.get('name', ''),
                                   'author': b0.get('author', ''),
                                   'book_url': b0.get('book_url', ''),
                                   'keyword': kw}}
            return {'ok': False, 'error': last_err or '适配器校验失败',
                    'tested_at': time.strftime('%Y-%m-%d %H:%M:%S')}
    except Exception:
        pass  # 适配器链路不可用时退回规则校验

    fetcher = Fetcher()
    engine = RuleEngine(source=src)
    base = src.get('bookSourceUrl', '').rstrip('/')

    search_url = src.get('searchUrl')
    if not search_url:
        return {'ok': True, 'error': '', 'note': '该书源未配置搜索',
                'tested_at': time.strftime('%Y-%m-%d %H:%M:%S')}

    from .rules import parse_search_config
    clean_url, cfg = parse_search_config(search_url)
    rs = src.get('ruleSearch') or {}
    last_err = ''
    for kw in keywords:
        html, err = _search_one(fetcher, engine, src, clean_url, cfg, kw, timeout)
        if html is None:
            last_err = err
            continue
        try:
            elems = engine.get_elements(rs.get('bookList', ''), html)
        except Exception as e:
            last_err = f"规则解析异常: {e}"
            continue
        if not elems:
            last_err = f"搜索无结果（关键词：{kw}）"
            continue
        # 解析第一本
        first = elems[0]
        name = engine.get_string(rs.get('name', ''), first, base_url=base)
        book_url = engine.get_string(rs.get('bookUrl', ''), first, base_url=base)
        author = engine.get_string(rs.get('author', ''), first, base_url=base)
        if not name or not book_url:
            last_err = f"解析结果缺少书名/链接（name={name!r} url={book_url!r}）"
            continue
        return {'ok': True, 'error': '', 'tested_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'sample': {'name': name, 'author': author, 'book_url': book_url,
                           'keyword': kw}}
    return {'ok': False, 'error': last_err or '校验失败',
            'tested_at': time.strftime('%Y-%m-%d %H:%M:%S')}
