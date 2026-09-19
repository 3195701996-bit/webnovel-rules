# -*- coding: utf-8 -*-
"""存储占用与选择性清理（路线 §7 P1-2："按源/作品/章节查看占用，选择性清除错误缓存"）。

原则（与产品基线一致，写死在代码里）：
  · **已下载内容只在用户明确点名时才删**——小说章节正文、漫画已下载图片都属于
    "用户的数据"，不能被一个笼统的"清理缓存"顺手清掉；
  · 数字必须是真的：占用按磁盘实际大小扫出来，释放量按**删除前后实测差**给出，
    删不掉的东西进 `kept` 并说明原因，不谎报；
  · 删掉缓存后状态不得撒谎：小说的 `completed` 同步剔除被删章节
    （配合 crawler 的自愈，下次"继续下载"会重新抓回来）。

对外只有两个入口：`usage()`（看）与 `clear(target)`（删）。
"""
import json
import os
import shutil
import time

from engine.app_utils import cache_key_of
from engine.config import (BOOKS_DIR, DATA_DIR, MANGA_CACHE_DIR,
                           MANGA_DOWNLOADS_DIR, MANGA_LIBRARY_FILE,
                           SEARCH_CACHE_FILE, TOC_CACHE_FILE)
from server.state import load_book_state

# 分类：哪些是"可再生"、哪些是"用户的数据"。界面据此决定红色警示与二次确认。
CATEGORIES = [
    {"key": "novel_books", "label": "小说正文缓存",
     "kind": "user_data", "regenerable": False,
     "note": "已下载的小说章节，删了要重新联网抓；不在书中时不影响书架与阅读进度"},
    {"key": "manga_downloads", "label": "漫画已下载（离线阅读）",
     "kind": "user_data", "regenerable": False,
     "note": "离线阅读用的图片；删除后需重新下载才能离线看"},
    {"key": "manga_cache", "label": "漫画临时缓存",
     "kind": "cache", "regenerable": True,
     "note": "浏览/取图过程中的临时文件，随时可删，已下载内容不受影响"},
    {"key": "regen", "label": "可再生数据（搜索/目录缓存、日志）",
     "kind": "cache", "regenerable": True,
     "note": "搜索与目录缓存、服务日志；删除只影响下次请求的快慢"},
    {"key": "trash", "label": "回收站（软删除内容）",
     "kind": "trash", "regenerable": False,
     "note": "从书架/书库删除的内容会先放到这里；清空后无法在应用内恢复"},
]

TRASH_DIR = os.path.join(DATA_DIR, "trash")


# ── 扫描 ──────────────────────────────────────────────────────────
def _file_size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def _dir_bytes(p):
    total = 0
    for root, _dirs, files in os.walk(p):
        for fn in files:
            total += _file_size(os.path.join(root, fn))
    return total


def _manga_titles():
    """comic_id → 书名（书库里才有），用于把占用显示成人能看懂的名字"""
    out = {}
    try:
        with open(MANGA_LIBRARY_FILE, encoding="utf-8") as f:
            for it in json.load(f) or []:
                if isinstance(it, dict) and it.get("comic_id"):
                    out[(it.get("source") or "", it["comic_id"])] = it.get("title") or ""
    except Exception:
        pass
    return out


def _novel_items():
    items = []
    if not os.path.isdir(BOOKS_DIR):
        return items
    for key in sorted(os.listdir(BOOKS_DIR)):
        d = os.path.join(BOOKS_DIR, key)
        if not os.path.isdir(d):
            continue
        state = load_book_state(os.path.join(d, "_state.json")) or {}
        chapters = state.get("chapters", []) or []
        failed = state.get("failed", {}) or {}
        caches = [f for f in os.listdir(d) if f.endswith(".cache")] if os.path.isdir(d) else []
        # 缓存文件 → 章节序号（用于"选择性清除"时给用户看清楚删的是哪几章）
        by_name = {cache_key_of(c.get("url", "")): i + 1 for i, c in enumerate(chapters)}
        idxs = sorted({by_name[n[:-6]] for n in caches if n[:-6] in by_name})
        info = state.get("book") or {}
        items.append({
            "key": key,
            "name": info.get("name") or key,
            "author": info.get("author", ""),
            "total": len(chapters),
            "cached": len(idxs),
            "cached_indexes": idxs,
            "failed": len(failed),
            "bytes": _dir_bytes(d),
            "stop_reason": state.get("stop_reason", ""),
            "checkpoint": state.get("checkpoint") or {},
        })
    items.sort(key=lambda x: -x["bytes"])
    return items


def _manga_download_items():
    titles = _manga_titles()
    items = []
    if not os.path.isdir(MANGA_DOWNLOADS_DIR):
        return items
    for src in sorted(os.listdir(MANGA_DOWNLOADS_DIR)):
        sdir = os.path.join(MANGA_DOWNLOADS_DIR, src)
        if not os.path.isdir(sdir):
            continue
        for cid in sorted(os.listdir(sdir)):
            cdir = os.path.join(sdir, cid)
            if not os.path.isdir(cdir):
                continue
            images = 0
            for _root, _dirs, files in os.walk(cdir):
                images += sum(1 for f in files if f.lower().endswith(
                    (".jpg", ".jpeg", ".png", ".webp", ".gif")))
            items.append({
                "source": src, "comic_id": cid,
                "title": titles.get((src, cid)) or cid,
                "images": images, "bytes": _dir_bytes(cdir),
            })
    items.sort(key=lambda x: -x["bytes"])
    return items


def _manga_cache_items():
    items = []
    if not os.path.isdir(MANGA_CACHE_DIR):
        return items
    for src in sorted(os.listdir(MANGA_CACHE_DIR)):
        sdir = os.path.join(MANGA_CACHE_DIR, src)
        if not os.path.isdir(sdir):
            continue
        works = [w for w in os.listdir(sdir) if os.path.isdir(os.path.join(sdir, w))]
        items.append({"source": src, "works": len(works), "bytes": _dir_bytes(sdir)})
    items.sort(key=lambda x: -x["bytes"])
    return items


def _regen_items():
    items = [
        {"name": "搜索缓存", "path": SEARCH_CACHE_FILE, "bytes": _file_size(SEARCH_CACHE_FILE)},
        {"name": "目录缓存", "path": TOC_CACHE_FILE, "bytes": _file_size(TOC_CACHE_FILE)},
    ]
    logs = 0
    log_files = []
    if os.path.isdir(DATA_DIR):
        for fn in os.listdir(DATA_DIR):
            if fn.endswith(".log") or fn.endswith(".tmp"):
                p = os.path.join(DATA_DIR, fn)
                if os.path.isfile(p):
                    logs += _file_size(p)
                    log_files.append(fn)
    items.append({"name": "服务日志", "path": ",".join(log_files), "bytes": logs})
    return items


def _trash_items():
    items = []
    if not os.path.isdir(TRASH_DIR):
        return items
    for name in sorted(os.listdir(TRASH_DIR)):
        p = os.path.join(TRASH_DIR, name)
        if os.path.isdir(p):
            items.append({"name": name, "bytes": _dir_bytes(p)})
        elif os.path.isfile(p):
            # 软删除的正常形态是目录（整本书/整部漫画移进来）；散落文件也照实列出，
            # 否则它们占用空间却在界面里"看不见"
            items.append({"name": name, "bytes": _file_size(p)})
    items.sort(key=lambda x: -x["bytes"])
    return items


def usage():
    """完整占用明细（每项都来自磁盘实扫；不估算、不四舍五入到 MB）"""
    novel = _novel_items()
    md = _manga_download_items()
    mc = _manga_cache_items()
    regen = _regen_items()
    trash = _trash_items()
    cat_bytes = {
        "novel_books": sum(i["bytes"] for i in novel),
        "manga_downloads": sum(i["bytes"] for i in md),
        "manga_cache": sum(i["bytes"] for i in mc),
        "regen": sum(i["bytes"] for i in regen),
        "trash": sum(i["bytes"] for i in trash),
    }
    cats = [dict(c, bytes=cat_bytes[c["key"]],
                 count={"novel_books": len(novel), "manga_downloads": len(md),
                        "manga_cache": len(mc), "regen": len(regen),
                        "trash": len(trash)}[c["key"]])
            for c in CATEGORIES]
    return {
        "total_bytes": sum(cat_bytes.values()),
        "categories": cats,
        "novel_books": novel,
        "manga_downloads": md,
        "manga_cache": mc,
        "regen": regen,
        "trash": trash,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ── 备份范围（"完整备份范围明确"）──────────────────────────────────
# 备份 = 用户数据里**体积小、丢了最麻烦、又无法从源站重新拿到**的那部分：
# 书源配置、启用状态、阅读进度、漫画书库与历史。正文与图片明确不进：
# 它们体积大（桌面实测 11.4 GB）且可以从书源重新下载。
BACKUP_INCLUDE = [
    {"what": "书源配置与启用状态", "why": "丢了要重新一个个加；源站变化后未必找得回来"},
    {"what": "小说阅读进度（读到第几章/章内位置）", "why": "丢了要从头找位置"},
    {"what": "漫画书库与阅读历史", "why": "丢了不知道看过哪些"},
]
BACKUP_EXCLUDE = [
    {"what": "小说正文缓存", "why": "可以从书源重新下载",
     "category": "novel_books"},
    {"what": "漫画已下载图片", "why": "可以从源站重新下载；体积最大",
     "category": "manga_downloads"},
    {"what": "临时缓存与日志", "why": "可再生、无价值", "category": "regen"},
    {"what": "回收站内容", "why": "本来就是已删除的东西", "category": "trash"},
]


def backup_scope():
    """备份范围报告：**包含什么、明确不含什么**，各自带真实体积。

    界面据此能说"备份将包含 34 个书源、6 本阅读进度（约 2 MB）；
    不含正文与图片（本机 11.4 GB）"——而不是只说一句"不含正文"。
    """
    u = usage()
    by = {c["key"]: c["bytes"] for c in u["categories"]}

    # 书源目录来自 engine.config（单一来源）；不要用"books 的兄弟目录"猜
    from engine.config import SOURCES_DIR
    src_dir = SOURCES_DIR
    src_files = []
    if os.path.isdir(src_dir):
        src_files = [f for f in os.listdir(src_dir)
                     if f.endswith(".json") and not f.startswith(".")]
    src_bytes = sum(_file_size(os.path.join(src_dir, f)) for f in src_files)

    progress_files = []
    total_bytes = src_bytes
    for rel in ("book_progress.json", os.path.join("manga", "_library.json"),
                os.path.join("manga", "_history.json")):
        p = os.path.join(DATA_DIR, rel)
        if os.path.isfile(p):
            sz = _file_size(p)
            total_bytes += sz
            progress_files.append({"name": rel, "bytes": sz})

    return {
        "include": BACKUP_INCLUDE,
        "exclude": BACKUP_EXCLUDE,
        "include_detail": {
            "sources": {"files": len(src_files), "bytes": src_bytes},
            "progress_files": progress_files,
            "bytes": total_bytes,
        },
        "exclude_detail": {c["category"]: by.get(c["category"], 0)
                           for c in BACKUP_EXCLUDE},
        "excluded_total_bytes": sum(by.get(c["category"], 0) for c in BACKUP_EXCLUDE),
        "note": "备份是配置与进度，不是离线全文副本；正文与图片请用「离线阅读」功能。",
    }


# ── 清理 ──────────────────────────────────────────────────────────
def _rm_path(p, deleted, kept, label):
    """删除单个路径并记录实测释放量；失败进 kept（带原因），不谎报成功"""
    if not os.path.exists(p):
        return 0
    if os.path.isdir(p):
        size = _dir_bytes(p)
        try:
            shutil.rmtree(p, ignore_errors=False)
        except Exception as e:                      # noqa: BLE001
            kept.append({"what": label, "reason": f"{type(e).__name__}: {e}"})
            return 0
    else:
        size = _file_size(p)
        try:
            os.remove(p)
        except Exception as e:                      # noqa: BLE001
            kept.append({"what": label, "reason": f"{type(e).__name__}: {e}"})
            return 0
    if os.path.exists(p):
        # 删完还在（权限/占用）→ 不能算释放
        kept.append({"what": label, "reason": "删除后文件仍存在"})
        return 0
    deleted.append({"what": label, "bytes": size})
    return size


def _clear_regen(deleted, kept):
    """可再生数据：搜索缓存、目录缓存、日志。内存里的缓存对象也要清，
    否则删了文件界面还在用旧数据（下次请求又写回去）。"""
    freed = 0
    for p in (SEARCH_CACHE_FILE, TOC_CACHE_FILE):
        freed += _rm_path(p, deleted, kept, os.path.basename(p))
    if os.path.isdir(DATA_DIR):
        for fn in os.listdir(DATA_DIR):
            if fn.endswith(".log") or fn.endswith(".tmp"):
                freed += _rm_path(os.path.join(DATA_DIR, fn), deleted, kept, fn)
    # 内存缓存同步失效：否则"清理后搜索还是秒回旧结果"，用户会以为没清掉
    try:
        from server.state import _search_cache, _toc_cache, _search_cache_save, _toc_cache_save
        _search_cache.clear()
        _toc_cache.clear()
        _search_cache_save()
        _toc_cache_save()
    except Exception as e:                          # noqa: BLE001
        kept.append({"what": "内存搜索/目录缓存", "reason": f"{type(e).__name__}: {e}"})
    return freed


def _running_novel_book_keys():
    """正在跑的小说任务对应的 book_key 集合（用于拒绝"边下边删"）"""
    try:
        from server.state import _tasks, ST_RUNNING
        return {t.get("book_key") for t in _tasks.values()
                if t and t.get("status") == ST_RUNNING and t.get("book_key")}
    except Exception:
        return set()


def _running_manga_tasks():
    """正在跑的漫画下载任务（source, comic_id）集合"""
    out = set()
    try:
        from server.state import _manga_dl
        for key, job in (_manga_dl.all_tasks() or {}).items():
            if (job or {}).get("status") == "running":
                out.add(tuple(str(key).split(":", 1)))
    except Exception:
        pass
    return out


def clear(target):
    """按 target 选择性清理。target 至少要有一个已知 scope（不做"清空所有"的隐式行为）

    并发护栏：**正在下载的书/源不许清它的缓存**——删到正在写的中间文件，会让那一章
    白失败一次（有原因、可重试，不丢数据，但用户会莫名其妙）。宁可当场拒绝并说明。
    """
    target = target or {}
    scope = (target.get("scope") or "").strip()
    deleted, kept, notes = [], [], []
    freed = 0

    if scope == "regen":
        freed = _clear_regen(deleted, kept)
        notes.append("已下载的小说章节与漫画图片未受影响")

    elif scope == "manga_cache":
        src = (target.get("source") or "").strip()
        cid = (target.get("comic_id") or "").strip()
        running = _running_manga_tasks()
        busy = {k for k in running if (not src or k[0] == src)}
        if busy:
            return {"ok": False, "freed_bytes": 0,
                    "error": f"有 {len(busy)} 个漫画下载任务正在跑，等它结束再清临时缓存"
                             "（避免删到正在写的中间文件）"}
        if src and cid:
            # 单作品临时缓存
            base = os.path.join(MANGA_CACHE_DIR, src)
            freed += _rm_path(os.path.join(base, cid), deleted, kept, f"{src}/{cid}")
            freed += _rm_path(os.path.join(base, f"{cid}_imgs.json"), deleted, kept,
                              f"{src}/{cid}_imgs.json")
        elif src:
            freed += _rm_path(os.path.join(MANGA_CACHE_DIR, src), deleted, kept, src)
        else:
            freed += _rm_path(MANGA_CACHE_DIR, deleted, kept, "漫画临时缓存")
            os.makedirs(MANGA_CACHE_DIR, exist_ok=True)
        notes.append("只删临时缓存；manga/downloads 下的离线图片未受影响")

    elif scope == "trash":
        freed += _rm_path(TRASH_DIR, deleted, kept, "回收站")
        os.makedirs(TRASH_DIR, exist_ok=True)
        notes.append("回收站内容已清空，应用内无法再恢复（这是用户明确要求的操作）")

    elif scope == "book_chapters":
        key = (target.get("key") or "").strip()
        if not key or "/" in key or "\\" in key or key.startswith("."):
            return {"ok": False, "error": "书籍 key 不合法", "freed_bytes": 0}
        if key in _running_novel_book_keys():
            return {"ok": False, "freed_bytes": 0,
                    "error": "这本书正在下载，等它停下再清章节缓存"
                             "（避免删到正在写的缓存文件）"}
        d = os.path.join(BOOKS_DIR, key)
        if not os.path.isdir(d):
            return {"ok": False, "error": "书籍不存在", "freed_bytes": 0}
        state = load_book_state(os.path.join(d, "_state.json")) or {}
        chapters = state.get("chapters", []) or []
        failed = state.get("failed", {}) or {}
        only = (target.get("only") or "").strip()
        wanted = set()
        if only == "failed":
            # "错误缓存"：抓取失败的章节 + 内容为空的损坏缓存
            for i, c in enumerate(chapters):
                u = c.get("url", "")
                if u in failed:
                    wanted.add(i + 1)
                p = os.path.join(d, cache_key_of(u) + ".cache")
                if os.path.exists(p):
                    try:
                        with open(p, encoding="utf-8") as f:
                            if not f.read().strip():
                                wanted.add(i + 1)
                    except (OSError, UnicodeDecodeError):
                        wanted.add(i + 1)
        elif isinstance(target.get("chapters"), (list, tuple)):
            for v in target["chapters"]:
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    continue
                if 1 <= n <= len(chapters):
                    wanted.add(n)
        else:
            return {"ok": False, "error": "缺少 only=failed 或 chapters 列表（防止误删全部）",
                    "freed_bytes": 0}
        removed_urls = []
        for idx in sorted(wanted):
            u = chapters[idx - 1].get("url", "")
            p = os.path.join(d, cache_key_of(u) + ".cache")
            freed += _rm_path(p, deleted, kept, f"{key} 第{idx}章")
            if not os.path.exists(p):
                removed_urls.append(u)
        # 状态不撒谎：completed 里剔除已删章节 → 下次"继续下载"会重新抓
        if removed_urls:
            _completed = state.get("completed", [])
            _kept_completed = [u for u in _completed if u not in set(removed_urls)]
            if len(_kept_completed) != len(_completed):
                state["completed"] = _kept_completed
            for u in removed_urls:
                failed.pop(u, None)
            state["failed"] = failed
            try:
                from engine.app_utils import atomic_write
                from server.state import invalidate_book_state
                atomic_write(os.path.join(d, "_state.json"), state)
                invalidate_book_state(os.path.join(d, "_state.json"))
            except Exception as e:                  # noqa: BLE001
                notes.append(f"缓存已删，但状态回写失败（继续下载时会自愈）：{e}")
        notes.append("已下载的其他章节与书架记录未受影响；被删章节可在下载页点「继续」重抓")
        if not wanted:
            notes.append("没有匹配到需要清理的章节（失败章节没有缓存、也没有空缓存）")

    else:
        return {"ok": False, "freed_bytes": 0,
                "error": f"未知的清理范围「{scope}」（可选：regen/manga_cache/trash/book_chapters）"}

    return {"ok": True, "scope": scope, "freed_bytes": freed,
            "deleted": deleted, "kept": kept, "notes": notes,
            "total_bytes_after": usage()["total_bytes"]}
