# -*- coding: utf-8 -*-
"""server.state —— 共享状态与领域助手（R47 从 app.py 拆出）

纪律：本模块不 import server 兄弟模块（单向依赖：routes → state → engine）。
"""
import hashlib
import json
import os
import re
import threading
import time

from flask import jsonify, g, request, abort

from engine.source_mgr import (get_by_uid, find_source)
from engine.crawler import SourceCrawler, CrawlTask
from engine.app_utils import (now_iso, _norm, _group_key, _parse_time,
                              _parse_words, book_key_of, atomic_write,
                              load_json, cache_key_of)
from engine.config import (SEARCH_CACHE_TTL, TOC_CACHE_TTL, BOOK_PROGRESS_FILE,
                           TOC_PAGE_CAP,
                           MANGA_DIR, MANGA_LIBRARY_FILE, MANGA_TASKS_FILE, MANGA_CACHE_DIR,
                           MANGA_DOWNLOADS_DIR, MANGA_STATE_DIR,
                           ST_RUNNING, ST_PAUSED, ST_STOPPED, ST_ERROR,
                           ST_DONE)
from engine.search_service import SlowSourceTracker, SearchWorker
from engine.manga.download_manager import DownloadManager

# ── 目录（WR_DATA_DIR 可覆盖：外置磁盘/测试隔离）──
HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("WR_DATA_DIR", "").strip() \
    or os.path.join(HUB_DIR, "data")
BOOKS_DIR = os.path.join(DATA_DIR, "books")
TASKS_DIR = os.path.join(DATA_DIR, "tasks")
for _d in (BOOKS_DIR, TASKS_DIR):
    os.makedirs(_d, exist_ok=True)

_cache_key = cache_key_of  # 领域助手内部使用的本地别名


# ══════════════════════════════════════════════
#  任务管理
# ══════════════════════════════════════════════
_tasks = {}


_crawlers = {}


_check_tocs = {}   # task_id -> preset_chapters（检查更新增量目录）


_check_jobs = {}   # book_key -> {status, new_chapters, retry_failed, task_id, message, error, ts}


_check_missing = {}  # book_key -> {book_url, source_uid, chapters:[...], ts}（检查出的缺失章节，待确认）


_toc_cache = {}    # book_url -> {count, name, updated, ts}（搜索章节数缓存，落盘持久化）


_search_cache = {} # (q, source, sort, type) -> {ts, payload}（搜索级缓存，落盘持久化）


_toc_lock = threading.Lock()


_toc_fetching = {}  # book_url -> threading.Event（单飞：同书目录并发抓取合并为一次）


_lock = threading.RLock()


_slow_tracker = SlowSourceTracker()


_search_worker = SearchWorker(_slow_tracker)


# 兼容旧引用（保持 _slow_sources/_source_latency 可用）
_slow_sources = _slow_tracker._slow


_source_latency = _slow_tracker._latency


TOC_CACHE_FILE = os.path.join(DATA_DIR, "toc_cache.json")


SEARCH_CACHE_FILE = os.path.join(DATA_DIR, "search_cache.json")


# 搜索缓存版本：适配器/解析规则变更时递增，旧版本缓存整体失效
# （否则 1h 磁盘缓存会抵消数据修复——如作者字段修正后仍命中脏缓存）
SEARCH_CACHE_VERSION = 2


# 部分结果（还有源没跑完）的短缓存时长：与漫画端 _MANGA_SEARCH_PARTIAL_TTL 同一口径。
# 理由：耐心上限/断网会让搜索带着"部分结果"提前收尾；若按完整结果缓存 1 小时，
# 用户在这一小时里反复搜索都拿不到那几个慢源的内容（"怎么总是这几本"）。
NOVEL_PARTIAL_TTL = 60


def _cache_ok(v):
    """缓存条目校验：版本匹配且未过期。

    部分结果（payload.partial=True）只短暂可用：过后重跑，慢源才有机会补上。
    """
    if not v:
        return False
    if v.get("ver") != SEARCH_CACHE_VERSION:
        return False
    ttl = SEARCH_CACHE_TTL
    if (v.get("payload") or {}).get("partial"):
        ttl = NOVEL_PARTIAL_TTL
    return time.time() - (v.get("ts") or 0) < ttl


def _err_response(e, code=500, hint="操作失败"):
    """统一错误响应：异常详情只进服务端日志，客户端拿脱敏文案 + request_id。
    直接回传 f"{type(e).__name__}: {e}" 会泄露本地路径/内网主机名/库版本，
    并放大 SSRF 等探测型漏洞的可利用性。"""
    import traceback
    traceback.print_exc()
    _rid = getattr(g, "request_id", "")
    print(f"[error] rid={_rid} {hint}: {type(e).__name__}: {e}", flush=True)
    return jsonify({"error": hint, "code": "INTERNAL_ERROR",
                    "request_id": _rid}), code


def device_offline_hint():
    """客户端（Android）在本机联网状态上的**设备级**信号：True / None。

    审查意见（2026-09-15 P1-网络归因）要求优先用 Android 网络状态作为辅助
    信号，而不是只靠"某个源连不上"来推断整机断网。App 侧用
    ConnectivityManager 判断"没有活动网络/没有 INTERNET 能力"时带上
    请求头 X-Device-Net: offline（见 android/.../NetState.kt）。

    语义严格限定为**辅助证据**：
      · True  → 设备自称没有可用网络，可以据此说"本机当前没有网络"；
      · None  → 设备说在线或没带这个头（含网页端/桌面端）→ 不做任何推断，
                由服务端自己的失败路径判据决定（engine/neterr.py）。
    不能反过来用"设备说在线"去否定实际的连接失败。
    """
    try:
        v = (request.headers.get("X-Device-Net") or "").strip().lower()
    except Exception:
        return None
    return True if v == "offline" else None


def _err_json(msg, code=400):
    """轻量错误响应：纯 {"error": msg} + HTTP 状态码（无额外字段）。
    形状与历史直写 jsonify({"error": ...}), code 完全一致——仅收敛重复代码，
    不改变任何响应契约（前端/安卓客户端按此形状解析）。带额外字段的
    错误响应（code/task_id/ok/books/token_set 等）不走此助手，保持原样。"""
    return jsonify({"error": msg}), code


def _search_cache_load():
    """启动时从磁盘加载搜索缓存（仅加载未过期的）"""
    try:
        with open(SEARCH_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if _cache_ok(v):
                _search_cache[tuple(json.loads(k))] = v
    except Exception:
        pass


# ── 缓存落盘异步化（P0-4）──
# 此前 _search_cache_save/_toc_cache_save 在请求路径上同步对整个缓存 dict 做
# json.dump(indent=1) + 原子写 + fsync（search_cache.json 实测 2.1MB，每次
# 50-200ms 同步阻塞，直接加在响应延迟上并阻塞其他并发请求）。
# 现在：请求路径只在锁内更新内存 dict 并标记 dirty；序列化 + 落盘移到后台
# 线程做，2s 防抖合并（连续变更只落盘一次）。
# 崩溃窗口（≤2s 防抖 + 写盘耗时）内丢失未落盘缓存是可接受的——只是缓存，
# 重启后按 TTL 重新爬取即可。进程无 atexit/清理钩子（R47 起即如此），
# 退出时未落盘的 dirty 数据同样直接丢弃（同为可接受的缓存丢失）。
_CACHE_FLUSH_DEBOUNCE = 2.0


_cache_flush_lock = threading.Lock()


_cache_flush_dirty = {"search": False, "toc": False}


_cache_flush_timer = None  # threading.Timer | None（防抖计时器，daemon）


def _cache_flush_schedule(kind):
    """请求路径调用：锁内标 dirty + 启动/复用防抖计时器（耗时 ≈ 0，不做 IO）"""
    global _cache_flush_timer
    with _cache_flush_lock:
        _cache_flush_dirty[kind] = True
        if _cache_flush_timer is None or not _cache_flush_timer.is_alive():
            _cache_flush_timer = threading.Timer(_CACHE_FLUSH_DEBOUNCE,
                                                 _cache_flush_worker)
            _cache_flush_timer.daemon = True  # 不阻塞进程退出
            _cache_flush_timer.start()


def _cache_flush_worker():
    """后台线程：取出 dirty 标记，对对应缓存做一次性快照落盘。
    快照在 _toc_lock 内复制（锁内只做轻量 dict 复制），json 序列化 + 原子写
    在锁外执行；写盘失败只记日志，不影响任何请求。"""
    global _cache_flush_timer
    with _cache_flush_lock:
        kinds = [k for k, v in _cache_flush_dirty.items() if v]
        for k in kinds:
            _cache_flush_dirty[k] = False
        _cache_flush_timer = None
    for kind in kinds:
        try:
            if kind == "search":
                with _toc_lock:
                    data = {json.dumps(k, ensure_ascii=False): v
                            for k, v in _search_cache.items()}
                atomic_write(SEARCH_CACHE_FILE, data)
            else:
                with _toc_lock:
                    data = dict(_toc_cache)
                atomic_write(TOC_CACHE_FILE, data)
        except Exception as _e:
            print(f"[cache-flush] {kind} 落盘失败: "
                  f"{type(_e).__name__}: {_e}", flush=True)


def _search_cache_save():
    """异步落盘搜索缓存：只标 dirty，2s 防抖后由后台线程合并落盘"""
    _cache_flush_schedule("search")


def _toc_cache_load():
    """启动时从磁盘加载 toc 缓存（仅加载未过期的）"""
    try:
        with open(TOC_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        for k, v in data.items():
            if now - (v.get("ts") or 0) < TOC_CACHE_TTL:
                _toc_cache[k] = v
    except Exception:
        pass


def _toc_cache_save():
    """异步落盘 toc 缓存：只标 dirty，2s 防抖后由后台线程合并落盘"""
    _cache_flush_schedule("toc")


def _save_task(t):
    atomic_write(os.path.join(TASKS_DIR, t["id"] + ".json"), t)


def _log(t, msg):
    t.setdefault("log", []).append({"t": now_iso(), "msg": msg})
    if len(t["log"]) > 300:
        t["log"] = t["log"][-200:]


# ── P1-1（0.56.0）：任务"为什么停了"必须是结构化事实，而不是只有日志里一句话 ──
# 停止原因的取值是**稳定枚举**（客户端据此决定给什么按钮），reason 是给用户看的话：
#   user_pause / user_stop / service_stopped / process_restart / error / done
STOP_KIND_USER_PAUSE = "user_pause"
STOP_KIND_USER_STOP = "user_stop"
STOP_KIND_SERVICE = "service_stopped"
STOP_KIND_RESTART = "process_restart"
STOP_KIND_ERROR = "error"
STOP_KIND_DONE = "done"

# 前台服务停止原因（Android 侧 runtime.stop(reason) 的 reason）→ 用户能看懂的话。
# 关键：**系统限制**与**任务失败**要分开说——前者不该让用户以为是自己下载坏了。
SERVICE_STOP_REASONS = {
    "fgs_timeout": "系统停止了前台服务（Android 对 dataSync 后台时长有限额）。"
                   "这是系统行为，不是下载失败；回到应用点「继续」即可接着下。",
    "user_notification": "你从通知里停止了本机服务，下载随之暂停。",
    "service_destroy": "本机服务被销毁（应用退出或系统回收），下载随之中断。",
    "start_failed": "本机服务启动失败，下载未开始。",
}


def service_stop_reason(reason):
    """把服务停止原因码翻译成用户可读文案（未知 reason 也照实带出来）"""
    _r = (reason or "").strip()
    # 移动端会带前缀（mobile_entry.stop 传 "mobile:<reason>"），先剥掉再查表，
    # 否则用户看到的是"原因：mobile:fgs_timeout"这种内部串
    while ":" in _r and _r.split(":", 1)[0] in ("mobile", "runtime"):
        _r = _r.split(":", 1)[1]
    for _key in (_r, _r.split(":", 1)[0]):
        if _key in SERVICE_STOP_REASONS:
            return SERVICE_STOP_REASONS[_key]
    if _r in ("requested", "api", "user"):
        return "本机服务被停止，下载随之中断（已下载内容保留，可点「继续」）。"
    if not _r:
        return "本机服务已停止，下载随之中断（原因未记录）。"
    return f"本机服务已停止（原因：{_r}），下载随之中断。"


def _checkpoint(t):
    """断点信息：下次继续会从哪里接上（数字全部来自服务端进度，不估算）"""
    p = t.get("progress") or {}
    return {
        "done": int(p.get("completed") or 0),
        "total": int(p.get("total") or 0),
        "failed": int(p.get("failed_chapters") or p.get("failed") or 0),
        "current": p.get("current") or "",
    }


def _set_stop(t, kind, reason, resumable=True):
    """写入结构化停止原因（幂等；不改 status——调用方负责状态迁移）"""
    t["stop_kind"] = kind
    t["stop_reason"] = reason
    t["resumable"] = bool(resumable)
    t["stopped_at"] = now_iso()
    t["checkpoint"] = _checkpoint(t)
    return t


def interrupt_running_tasks(reason, kind=STOP_KIND_SERVICE):
    """把**正在运行**的小说任务标记为可解释的中断（服务/引擎停止时调用）。

    保守实现：只置停止标志 + 写原因，不 join 线程（关闭流程不能被下载阻塞）。
    线程真正退出时 `_run_task` 的收尾逻辑看到 progress.status=stopped 会保持
    STOPPED，不会把原因覆盖成"已完成"。返回被标记的任务数。
    """
    marked = 0
    with _lock:
        for tid, t in list(_tasks.items()):
            if t.get("status") != ST_RUNNING:
                continue
            ctl = _crawlers.get(tid)
            if ctl:
                try:
                    ctl["stop"]["v"] = True
                except Exception:
                    pass
            t["status"] = ST_STOPPED
            t["finished_at"] = t.get("finished_at") or now_iso()
            _set_stop(t, kind, reason, resumable=True)
            _log(t, reason)
            _save_task(t)
            marked += 1
    return marked


def _load_disk_tasks():
    if not os.path.isdir(TASKS_DIR):
        return
    for f in os.listdir(TASKS_DIR):
        if not f.endswith(".json"):
            continue
        t = load_json(os.path.join(TASKS_DIR, f))
        if not t:
            continue
        if t.get("status") == ST_RUNNING:
            t["status"] = ST_STOPPED
            t["finished_at"] = t.get("finished_at") or now_iso()
            # P1-1: 磁盘上还是 running 说明进程/服务在上次运行中被结束了
            # （正常暂停/停止/完成都会落终态）。原因写清楚，别只说"中断"。
            _set_stop(t, STOP_KIND_RESTART,
                      "上次运行被系统结束（应用进程或前台服务已停止），"
                      "下载进度已保存，可点「继续」从断点接着下。", resumable=True)
            _log(t, "服务重启，任务中断（可点击启动恢复）")
            _save_task(t)
        _tasks[t["id"]] = t


# P2-4: 爬取进度落盘节流状态（task_id -> [上次落盘时间, 上次落盘时完成章数]）
# 此前每章完成都 _save_task（整任务 JSON 原子写+fsync）+ 写 _progress.json；
# 改为每 5 章或每 5s（先到为准）落盘一次，终态立即落盘。
_PROGRESS_FLUSH = {}


def _run_task(task_id):
    """后台线程：执行 CrawlTask"""
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        source_uid, book_url, book_key = t["source_uid"], t["book_url"], t["book_key"]
    book_dir = os.path.join(BOOKS_DIR, book_key)
    source = get_by_uid(source_uid)
    if not source:
        with _lock:
            t = _tasks.get(task_id)
            if t:
                t["status"] = ST_ERROR
                t["error"] = "书源不存在"
                _set_stop(t, STOP_KIND_ERROR,
                          "任务失败：书源不存在（可能已被删除或停用）", resumable=False)
                t["finished_at"] = now_iso()
                _save_task(t)
        return

    def on_progress(p):
        with _lock:
            tt = _tasks.get(task_id)
            if tt is None:
                return
            tt["progress"] = p
            tt["updated_at"] = now_iso()
            if p.get("status") == ST_ERROR:
                tt["status"] = ST_ERROR
                _set_stop(tt, STOP_KIND_ERROR,
                          "任务失败：" + (p.get("message") or "爬取过程报错（详见任务日志）"),
                          resumable=True)
                tt["finished_at"] = now_iso()
            # P2-4: 落盘节流——每 5 章或每 5s（先到为准）一次；
            # 终态（done/stopped/error/discover 等非 running）立即落盘。
            # 崩溃窗口 ≤5s 的进度丢失可接受：已下载章节有磁盘 .cache，
            # 重启续爬 is_done = completed ∪ 磁盘缓存（crawler.py）兜底，不重下
            _done = int(p.get("completed") or 0)
            _fs = _PROGRESS_FLUSH.setdefault(task_id, [0.0, -1])
            _flush = p.get("status") != "running" \
                or _done - _fs[1] >= 5 or time.time() - _fs[0] >= 5
            if _flush:
                _fs[0] = time.time()
                _fs[1] = _done
                _save_task(tt)
        if _flush:
            # 同步书籍实时进度
            atomic_write(os.path.join(book_dir, "_progress.json"), p)

    stop_flag = {"v": False}
    preset = None
    with _lock:
        preset = _check_tocs.pop(task_id, None)
    task = CrawlTask(source, book_url, book_dir, resume=True,
                     progress_callback=on_progress,
                     stop_callback=lambda: stop_flag["v"])
    # 必须在 crawl() 前注册，暂停/取消才能找到任务
    with _lock:
        _crawlers[task_id] = {"task": task, "stop": stop_flag}
    # R29(技术评审4.4): 整个任务生命周期包入 try/except/finally——
    # crawl() 在进入 try 前抛异常会留下永久 running 状态且 _crawlers 不清理
    try:
        if preset is not None:
            task.crawl(preset_chapters=preset)
        else:
            task.crawl()
        # B06: 先定终态再决定是否合并——merge_txt 延迟到任务明确完成
        # （done）才执行；暂停/停止路径不做全量合并（merge_txt 自身还有
        # 内容 revision 守卫兜底幂等，重复暂停不会重复合并）
        _do_merge = False
        with _lock:
            if task_id in _crawlers:
                del _crawlers[task_id]
            tt = _tasks.get(task_id)
            if tt and tt["status"] == ST_RUNNING:
                if tt.get("pause_requested"):
                    tt["status"] = ST_PAUSED
                    _set_stop(tt, STOP_KIND_USER_PAUSE,
                              "你在 App 里暂停了任务（进度已保存，可点「继续」接着下）")
                    _log(tt, "已暂停（进度已保存）")
                elif (tt.get("progress") or {}).get("status") == "stopped":
                    tt["status"] = ST_STOPPED
                    # 服务/进程级中断的原因由 interrupt_running_tasks / 启动装载写入，
                    # 更具体，不能被这里的笼统文案覆盖
                    if tt.get("stop_kind") not in (STOP_KIND_SERVICE, STOP_KIND_RESTART):
                        _set_stop(tt, STOP_KIND_USER_STOP,
                                  "你在 App 里停止了任务（已下载的章节保留，可点「继续」续爬）")
                    _log(tt, "爬取已停止（可点击启动续爬）")
                else:
                    tt["status"] = ST_DONE
                    _do_merge = True
                    # 完成但存在失败章节 → 显著告警（避免读者读到那章才发现坑）
                    _pf = (tt.get("progress") or {})
                    _fail_n = int(_pf.get("failed_chapters") or 0)
                    _set_stop(tt, STOP_KIND_DONE, "", resumable=False)
                    if _fail_n:
                        _log(tt, f"⚠ 爬取完成，但有 {_fail_n} 个失败章节（书库可补下/单章重爬）")
                        tt["has_failed_warning"] = True
                tt["finished_at"] = now_iso()
                _save_task(tt)
        if _do_merge:
            # 锁外执行全量合并，避免 IO 阻塞任务锁；合并失败不颠覆已完成
            # 的爬取结果（章节缓存齐全，book.txt 可在完成路径/单章重爬时重建）
            try:
                task.merge_txt()
            except Exception as _me:
                with _lock:
                    tt = _tasks.get(task_id)
                    if tt:
                        _log(tt, f"合并全文失败: {type(_me).__name__}: {_me}")
                        _save_task(tt)
    except Exception as e:
        # 磁盘类错误要给**可行动原因**（2026-09-18 实测缺口）：写盘失败时旧实现
        # 只给"任务异常：OSError（详细错误见任务日志）"，用户看不出是磁盘满了，
        # 只会反复重试同样失败。disk_error_text 只返回固定中文句子（脱敏）。
        from engine.app_utils import disk_error_text as _disk_text
        _disk = _disk_text(e)
        with _lock:
            if task_id in _crawlers:
                del _crawlers[task_id]
            tt = _tasks.get(task_id)
            if tt:
                tt["status"] = ST_ERROR
                # 客户端可见 error 只存脱敏文案（异常原文含本地路径/内网地址）；
                # 完整异常进任务日志与服务端日志
                tt["error"] = _disk or "爬取失败（详细错误见任务日志/服务端日志）"
                # 磁盘文案自身已含"点「继续」"的指引，不再拼接以免重复两遍
                _set_stop(tt, STOP_KIND_ERROR,
                          _disk or f"任务异常：{type(e).__name__}"
                                   "（详细错误见任务日志/服务端日志）",
                          resumable=True)
                _log(tt, f"任务异常: {type(e).__name__}: {e}")
                tt["finished_at"] = now_iso()
                _save_task(tt)
        print(f"[task {task_id}] ERROR: {type(e).__name__}: {e}", flush=True)
    finally:
        # 兜底清理：任何路径退出都移除 crawler 注册，防止任务永久 running
        with _lock:
            _crawlers.pop(task_id, None)
            _PROGRESS_FLUSH.pop(task_id, None)


def _create_task(source_uid, book_url, check_toc=None):
    """创建并启动爬取任务(R53: 单源下载——多源混合已取消)

    check_toc：增量下载的预置章节目录（检查更新→补充下载）。必须在持锁
    创建任务、启动线程**之前**写入 _check_tocs——否则 worker 先 pop 到 None，
    增量下载退化为整书全量重爬（TOCTOU）。"""
    with _lock:
        for t in _tasks.values():
            if t["source_uid"] == source_uid and t["book_url"] == book_url \
                    and t["status"] == ST_RUNNING:
                return None, "该书已有任务在运行", t["id"]
        task = {
            "id": "t" + hashlib.md5((source_uid + book_url + str(time.time())).encode()).hexdigest()[:10],
            "source_uid": source_uid,
            "book_url": book_url,
            "book_key": book_key_of(source_uid, book_url),
            "status": ST_RUNNING,
            "created_at": now_iso(),
            "finished_at": None,
            "progress": None,
            "pause_requested": False,
            "log": [{"t": now_iso(), "msg": f"任务创建：{source_uid}"}],
        }
        _tasks[task["id"]] = task
        _save_task(task)
        if check_toc is not None:
            _check_tocs[task["id"]] = check_toc
        th = threading.Thread(target=_run_task, args=(task["id"],),
                              daemon=True, name=f"crawl-{task['id']}")
        th.start()
        return task["id"], None, None


def _launch_task(task_id):
    with _lock:
        t = _tasks.get(task_id)
        if t is None:
            return None, "任务不存在"
        if t["status"] == ST_RUNNING:
            return None, "任务已在运行中"
        if task_id in _crawlers:
            return None, "任务仍在收尾中，请稍候"
        t["status"] = ST_RUNNING
        t["pause_requested"] = False
        t["finished_at"] = None
        t["error"] = None
        # 开始跑就不该再显示上一轮的停止原因（resumable 也复位）
        t["stop_kind"] = ""
        t["stop_reason"] = ""
        t["stopped_at"] = ""
        t["resumable"] = False
        _log(t, "启动/恢复爬取（失败章节优先重试）…")
        _save_task(t)
        th = threading.Thread(target=_run_task, args=(task_id,),
                              daemon=True, name=f"crawl-{task_id}")
        th.start()
        return task_id, None


def _task_snapshot_synthetic(t):
    """按**真实快照的字段口径**合成一条（供离线用例核对 title/type 等契约）。

    只调用与真实路径相同的取名字函数 `_task_title`，不触碰全局任务表；
    用途是"字段契约"测试，不替代端到端验证。"""
    return {
        "id": t.get("id"), "source_uid": t.get("source_uid"),
        "title": _task_title(t), "type": "novel",
        "book_url": t.get("book_url"), "book_key": t.get("book_key"),
        "status": t.get("status"),
    }


def _task_title(t):
    """任务显示名：书目录里的书名 → 书 URL 末段 → 任务 id。

    为什么必须有：客户端下载页渲染 `title.ifBlank { id }`，而**小说任务的快照
    此前根本不发 title**（漫画任务一直发）——于是小说任务在下载页显示的是
    `t34328ff4f1` 这种内部 id，暂停/删除的提示也变成"已请求暂停：t34328ff4f1"。
    两端口径不一致，用户看到的是机器串。
    """
    try:
        d = os.path.join(BOOKS_DIR, str(t.get("book_key") or ""))
        st = load_book_state(os.path.join(d, "_state.json")) or {}
        nm = str((st.get("book") or {}).get("name") or "").strip()
        if nm and nm != (t.get("book_url") or "").strip():
            return nm[:80]
    except Exception:                                            # noqa: BLE001
        pass
    u = str(t.get("book_url") or "").strip()
    if u:
        seg = u.rstrip("/").rsplit("/", 1)[-1] or u
        return seg[:80]
    return str(t.get("id") or "")


def _task_snapshot(tid):
    t = _tasks.get(tid)
    if t is None:
        return None
    return {
        "id": t["id"], "source_uid": t["source_uid"],
        # 客户端按 title 显示（缺省回退到 id）；小说任务此前缺失，见 _task_title
        "title": _task_title(t), "type": "novel",
        "book_url": t["book_url"], "book_key": t["book_key"],
        "status": t["status"], "created_at": t.get("created_at"),
        "finished_at": t.get("finished_at"), "progress": t.get("progress"),
        "error": t.get("error"), "log": t.get("log", [])[-60:],
        "running": tid in _crawlers,
        # P1-1: 结构化停止/中断原因（客户端据此显示"为什么停了"+给不给继续按钮）
        "stop_kind": t.get("stop_kind", ""),
        "stop_reason": t.get("stop_reason", ""),
        "resumable": bool(t.get("resumable", t.get("status") in (ST_PAUSED, ST_STOPPED))),
        "checkpoint": t.get("checkpoint") or _checkpoint(t),
        "stopped_at": t.get("stopped_at", ""),
    }


def load_persisted_state():
    """显式装载持久状态（**不再在 import 时自动执行**）。

    技术指南 §8.1/§19 要求"Python import 无启动副作用"：模块级直接调用会带来
    两个真实问题——
      · `_load_disk_tasks()` 会**写文件**（把 running 任务标成中断并落盘），
        import 一次就改一次用户数据；
      · Android 端 import 发生在 Chaquopy 初始化线程里，任何写盘/长扫描都可能
        拖慢首启，且无法被宿主控制。
    现在由运行时初始化钩子显式调用（desktop main / mobile_entry 都走同一条链）。
    幂等：重复调用只做重复读取。
    """
    _load_disk_tasks()
    _toc_cache_load()
    _search_cache_load()
    _manga_stats_load()


def _clean_book(b):
    """清洗单本书：作者前缀、简介残留"""
    _author_raw = b.get('author', '') or ''
    _author_clean = re.sub(r'^\s*(作者|作\s*者)[:：\s]+', '', _author_raw).strip()
    if not _author_clean:
        _author_clean = _author_raw.strip()
    _intro_raw = b.get('intro', '') or ''
    if re.search(r'\$1|\}\}|^$1', _intro_raw) or len(_intro_raw) < 12:
        _intro_clean = ''
    else:
        _intro_clean = re.sub(r'<br\s*/?>|</?[a-z]+>', '', _intro_raw).strip()
    b['author'] = _author_clean
    b['intro'] = _intro_clean
    return b


def _build_groups(all_books, kw, search_type, sort_mode, _cache=None):
    """把原始书籍列表分组/过滤/排序 → groups 列表（与 api_search 一致）
    P2-3: `_cache`（可选 dict，一次搜索会话内复用）按书籍身份
    (name|author|book_url) 缓存 _norm/_group_key/_score/_parse_time/
    _parse_words 的计算结果——流式端点每完成一个源全量重建分组，
    同一本书此前每轮重复跑 5-6 次 OpenCC 繁简转换 + 4 条正则 +
    2 次打分；缓存后整轮只算一次。kwc 在会话内固定故 score 稳定；
    不传 _cache 时行为与原实现完全一致（每次调用独立计算）。"""
    if _cache is None:
        _cache = {}
    qq = kw.strip()
    author_mode = qq.startswith("作者") or qq.startswith("作者:")
    title_mode = qq.startswith("书名") or qq.startswith("书名:")
    kwc = re.sub(r'^(作者|书名)[:：\s]*', '', qq).strip()
    nkw = _norm(kwc)

    def _score(b):
        name = (b.get('name') or '').strip()
        author = (b.get('author') or '').strip()
        intro = (b.get('intro') or '')
        s_ = 0
        if kwc:
            if name == kwc:
                s_ = 200   # 精确同名（本体）→ 稳居第一，避免被同人蹭名书挤沉
            elif kwc in name:
                s_ = 85
            elif author == kwc:
                s_ = 75
            elif kwc in author:
                s_ = 70
            elif kwc in intro:
                s_ = 30
            elif any(w in name for w in kwc if len(w) >= 2):
                s_ = 55
            else:
                s_ = 10
        if author_mode:
            s_ = 100 if (author and kwc in author) else (s_ - 30 if s_ else 0)
        if title_mode:
            s_ = 100 if (name and kwc in name) else (s_ - 30 if s_ else 0)
        if author:
            s_ += 8
        if intro:
            s_ += 7
        if b.get('cover'):
            s_ += 5
        return s_

    # P2-3: 按书籍身份缓存贵计算（OpenCC/正则/打分/时间字数解析）。
    # 值 = (norm_name, norm_author, group_key, score, update_ts, word_num)
    def _bi(b):
        k = (b.get('name') or '', b.get('author') or '', b.get('book_url') or '')
        v = _cache.get(k)
        if v is None:
            v = (_norm(b.get('name', '')), _norm(b.get('author', '')),
                 _group_key(b.get('name', '')), _score(b),
                 _parse_time(b.get('update_time', '')),
                 _parse_words(b.get('word_count', '')))
            _cache[k] = v
        return v

    # 组级归一化同样按组名缓存（每轮重建对全部组重跑 _group_key/_norm）
    def _gk(n):
        k = ('\x00g', n or '')
        v = _cache.get(k)
        if v is None:
            v = _group_key(n or '')
            _cache[k] = v
        return v

    def _gn(n):
        k = ('\x00n', n or '')
        v = _cache.get(k)
        if v is None:
            v = _norm(n or '')
            _cache[k] = v
        return v

    # 过滤（含模糊容错）
    def _fuzzy_hit(b):
        name, author, gkey = _bi(b)[0], _bi(b)[1], _bi(b)[2]
        if nkw in name or nkw in author:
            return 0
        if nkw in gkey:
            return 1
        if len(nkw) >= 2 and nkw in name:
            return 1
        return -1

    if search_type == 'name':
        kept = []
        for b in all_books:
            fz = _fuzzy_hit(b)
            if fz >= 0 and (fz == 0 or nkw in _bi(b)[0]):
                kept.append(b)
        all_books = kept
    elif search_type == 'author':
        all_books = [b for b in all_books if nkw in _bi(b)[1]]
    else:
        kept = []
        for b in all_books:
            fz = _fuzzy_hit(b)
            if fz >= 0:
                if fz == 1:
                    b['fuzzy'] = True
                kept.append(b)
        all_books = kept
    all_books.sort(key=lambda b: (_bi(b)[3] - (50 if b.get('fuzzy') else 0)))
    for b in all_books:
        b.pop('fuzzy', None)

    # 分组
    groups = {}
    for b in all_books:
        _nname, _nauthor, _gname, _sc, _ts, _wn = _bi(b)
        _gauthor = _nauthor or '佚名'
        key = (_gname, _gauthor)
        g = groups.setdefault(key, {
            'name': b.get('name', ''), 'author': b.get('author', ''),
            'intro': b.get('intro', ''), 'cover': b.get('cover', ''),
            'score': _sc, 'update_ts': 0, 'word_num': 0,
            'sources': []})
        if _sc > g.get('score', 0):
            g['score'] = _sc  # 同名组保留最高分（精确同名书不被同人先占位）
        g['update_ts'] = max(g['update_ts'], _ts)
        g['word_num'] = max(g['word_num'], _wn)
        g['sources'].append({
            'source_uid': b.get('source_uid', ''), 'source_name': b.get('source_name', ''),
            'book_url': b.get('book_url', ''),
            'last_chapter': b.get('last_chapter', ''),
            'update_time': b.get('update_time', ''),
            'word_count': b.get('word_count', ''),
            'chapter_count': b.get('chapter_count', 0),
        })
    glist = list(groups.values())

    # 同名作者空组归并
    _by_gname = {}
    for g in glist:
        _by_gname.setdefault(_gk(g.get('name', '')), []).append(g)
    glist = []
    for _gname, _gs in _by_gname.items():
        if not _gname:
            continue
        _named = [g for g in _gs if g.get('author') and g['author'] != '佚名']
        _anon = [g for g in _gs if not (g.get('author') and g['author'] != '佚名')]
        if not _named:
            _base = _gs[0]
            for g in _gs[1:]:
                _base['sources'].extend(g['sources'])
            glist.append(_base)
        else:
            glist.extend(_named)
            if _anon:
                _base = _named[0]
                for g in _anon:
                    _base['sources'].extend(g['sources'])
    glist = [g for g in glist if _gk(g.get('name', ''))]

    if sort_mode == 'update':
        glist.sort(key=lambda g: -g['update_ts'])
    elif sort_mode == 'word':
        glist.sort(key=lambda g: -g['word_num'])
    else:
        for g in glist:
            g['completeness'] = (1 if g.get('author') else 0) * 8 + \
                                (1 if g.get('intro') else 0) * 7 + \
                                (1 if g.get('cover') else 0) * 5
        # 精确同名（书名==关键词）→ 强制置顶：不被同人蹭名书挤沉
        glist.sort(key=lambda g: (0 if _gn(g.get('name', '')) == nkw else 1,
                                  -(g.get('score', 0) + g.get('completeness', 0))))
    # 组内源按章节数降序
    for g in glist:
        g['sources'].sort(key=lambda s: -(s.get('chapter_count') or 0))
    return glist


# ══════════════════════════════════════════════
#  书籍 API
# ══════════════════════════════════════════════
# ── P1-5: _state.json 解析缓存（进程内，mtime_ns+size 指纹失效，LRU 20 本）──
# 书籍详情/章节 GET/书单扫描此前每次都全量 json.load 整本 _state.json
# （3000 章书可达数百 KB~MB 级）。爬取进度保存（CrawlTask._save_state）等
# 写路径直接改写文件——以 (mtime_ns, size) 为指纹自动失效；本进程内的
# 写点（novel_api 单章重爬）写后主动 invalidate_book_state。
from collections import OrderedDict as _OrderedDict

_BOOK_STATE_CACHE_MAX = 20
_book_state_cache = _OrderedDict()   # path -> ((mtime_ns, size), data)
_book_state_lock = threading.Lock()


def load_book_state(path, default=None):
    """带解析缓存的 _state.json 读取（语义同 load_json，失败返回 default）。
    返回的是共享对象——调用方只读；需要修改的调用方（单章重爬）改完
    落盘后必须 invalidate_book_state。"""
    try:
        st = os.stat(path)
    except OSError:
        with _book_state_lock:
            _book_state_cache.pop(path, None)
        return default
    fp = (st.st_mtime_ns, st.st_size)
    with _book_state_lock:
        ent = _book_state_cache.get(path)
        if ent is not None and ent[0] == fp:
            _book_state_cache.move_to_end(path)
            return ent[1]
    data = load_json(path, default)
    with _book_state_lock:
        _book_state_cache[path] = (fp, data)
        _book_state_cache.move_to_end(path)
        while len(_book_state_cache) > _BOOK_STATE_CACHE_MAX:
            _book_state_cache.popitem(last=False)
    return data


def invalidate_book_state(path):
    """_state.json 写点后主动失效（指纹机制兜底，主动失效防同 ns 同尺寸极端情形）"""
    with _book_state_lock:
        _book_state_cache.pop(path, None)


def _scan_books():
    books = []
    if not os.path.isdir(BOOKS_DIR):
        return books
    # R30: 全局阅读进度（book_key → {idx, pct, ts}）
    try:
        with open(BOOK_PROGRESS_FILE, encoding="utf-8") as _f:
            _book_progress = json.load(_f) or {}
    except Exception:
        _book_progress = {}
    # _check_jobs 所有写入均在 _lock 内；这里取一次浅快照替代循环内无锁读
    with _lock:
        _check_jobs_snap = {k: dict(v) for k, v in _check_jobs.items()
                            if isinstance(v, dict)}
    for key in sorted(os.listdir(BOOKS_DIR)):
        d = os.path.join(BOOKS_DIR, key)
        if not os.path.isdir(d):
            continue
        state = load_book_state(os.path.join(d, "_state.json"))
        if not state:
            continue
        chapters = state.get("chapters", [])
        completed = state.get("completed", [])
        failed = state.get("failed", {}) or {}
        total = len(chapters)
        if total == 0:
            # 爬取中断残留（从未成功获取目录）：不在书籍列表展示
            continue
        # 已完成 = 有缓存的章节数（缓存即真实数据；失败章节无缓存不计入）
        # 0.55.0: completed 不再直接计入——它只是"爬取时成功过"的历史记录，
        # 缓存被清理后仍会谎报已完成（书库显示"已下载"而正文空白）。
        comp_set = set(completed)
        if chapters and len(chapters) < 5000:
            # R47 性能：每书一次 listdir 建立缓存文件名集，替代逐章
            # os.path.exists（50 书 × 2500 章 ≈ 12.5 万次 stat/请求 → 50 次 listdir）
            try:
                _cache_files = {f[:-6] for f in os.listdir(d)
                                if f.endswith(".cache")}
            except OSError:
                _cache_files = set()
            done = sum(1 for c in chapters
                       if _cache_key(c.get('url', '')) in _cache_files)
        else:
            done = len(comp_set)
        done = min(done, total)
        # R30: 最近阅读时间戳来自全局阅读进度 book_progress.json
        # 2026-09-13: 一并带出**阅读进度**（读到第几章/章内百分比/章名）——
        # 书库为每本小说标注进度（与"下载进度 percent"是两件事）
        _pr = {}
        try:
            _pr = _book_progress.get(key) or {}
        except Exception:
            _pr = {}
        _lr = _pr.get("ts") or 0.0
        _ridx = int(_pr.get("idx") or 0)
        _rpct = int(_pr.get("pct") or 0)
        books.append({
            "key": key,
            "name": (state.get("book") or {}).get("name") or key,
            "author": (state.get("book") or {}).get("author", ""),
            "source_uid": (state.get("book") or {}).get("source_uid")
                          or key.split("_")[0],
            "total": total,
            "done": done,
            "failed": len(failed),
            "percent": round(done / total * 100) if total else 0,
            "updated_at": state.get("updated_at", ""),
            "last_read_ts": _lr,        # 最近阅读（epoch 秒，0=未读）
            "read_idx": _ridx,          # 读到第几章（1 起，0=未读）
            "read_pct": _rpct,          # 该章章内滚动百分比
            "read_name": _pr.get("name") or "",
            "read_ratio": (round(_ridx / total * 100)
                           if (total and _ridx) else 0),   # 章级进度 %
            "has_txt": os.path.exists(os.path.join(d, "book.txt")),
            # 自动追更标记：最近一次检查发现有缺失/新章节。
            # **只在书已下完时亮徽标**——未下完的书缺失章节是"还没下完"，
            # 不是"有更新"（第二轮报告：未下完书误亮 🔔 有更新）
            "has_update": (done >= total and total > 0)
                          and bool((_check_jobs_snap.get(key) or {}).get("missing_count")),
        })
    return books


def _dir_size(p):
    if not os.path.isdir(p):
        return 0
    total = 0
    for root, _, files in os.walk(p):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def _clean_caches(scope="cache"):
    """清理各类缓存（漫画临时缓存/搜索缓存/目录缓存/日志），返回 (freed, cleaned)。
    原则：**已下载内容永不删除**——只清理未下载的临时缓存与可再生的数据缓存。
    scope 可选：'cache'（默认，保护已下载）/ 'all'（额外清软删除垃圾 trash）。"""
    import shutil as _sh
    freed = 0
    cleaned = []

    def _rm(p):
        nonlocal freed
        if os.path.isdir(p):
            sz = _dir_size(p)
            _sh.rmtree(p, ignore_errors=True)
            freed += sz
            cleaned.append((os.path.basename(p), sz))

    # ── 1) 漫画缓存：只清理"未下载"的临时缓存，保护已下载漫画 ──
    if os.path.isdir(MANGA_CACHE_DIR):
        # 已下载漫画：library 中 status=done 的 (source, comic_id) → 图片保留
        protected = set()
        try:
            with open(MANGA_LIBRARY_FILE, encoding='utf-8') as _f:
                lib = json.load(_f)
            for item in lib:
                if item.get('status') == 'done' and item.get('source') and item.get('comic_id'):
                    protected.add((item['source'], item['comic_id']))
        except Exception:
            pass
        # 遍历缓存目录，删除"不在保护名单"的漫画缓存
        for src_name in os.listdir(MANGA_CACHE_DIR):
            src_dir = os.path.join(MANGA_CACHE_DIR, src_name)
            if not os.path.isdir(src_dir):
                continue
            for cid in os.listdir(src_dir):
                comic_dir = os.path.join(src_dir, cid)
                if not os.path.isdir(comic_dir):
                    continue
                if (src_name, cid) in protected:
                    continue  # 已下载 → 保留
                _rm(comic_dir)
        # 清空后移除空源目录
        for src_name in os.listdir(MANGA_CACHE_DIR):
            sd = os.path.join(MANGA_CACHE_DIR, src_name)
            if os.path.isdir(sd) and not os.listdir(sd):
                try:
                    os.rmdir(sd)
                except OSError:
                    pass
    # 2) 搜索缓存（原地清空：novel_api 等模块 import 时固化了 dict 引用，
    #    重绑定 _search_cache = {} 会让它们继续读写旧 dict，清理无效）
    with _toc_lock:
        _search_cache.clear()
    if os.path.exists(SEARCH_CACHE_FILE):
        sz = os.path.getsize(SEARCH_CACHE_FILE)
        os.remove(SEARCH_CACHE_FILE)
        freed += sz
        cleaned.append(("search_cache.json", sz))
    # 3) 目录缓存（同上：原地 clear，保留 import 方引用）
    with _toc_lock:
        _toc_cache.clear()
    if os.path.exists(TOC_CACHE_FILE):
        sz = os.path.getsize(TOC_CACHE_FILE)
        os.remove(TOC_CACHE_FILE)
        freed += sz
        cleaned.append(("toc_cache.json", sz))
    # P0-4: 复位异步落盘 dirty 标记——否则清理前 pending 的防抖 flush
    # 会把刚清空的缓存（或清理前的快照）重新写回磁盘，清理形同虚设。
    # （已进入快照阶段的在途 flush 仍有极小竞态窗口，影响只是缓存文件
    # 重现，属可接受的缓存语义）
    with _cache_flush_lock:
        _cache_flush_dirty["search"] = False
        _cache_flush_dirty["toc"] = False
    # 4) 服务器日志（保留当前）
    _log_f = os.path.join(DATA_DIR, "logs", "server.log")
    if os.path.exists(_log_f):
        sz = os.path.getsize(_log_f)
        os.remove(_log_f)
        freed += sz
        cleaned.append(("server.log", sz))
    # 5) scope=all 时清软删除垃圾
    if scope == "all":
        _trash = os.path.join(DATA_DIR, "trash")
        if os.path.isdir(_trash):
            _rm(_trash)
    cleaned.sort(key=lambda x: -x[1])
    return freed, cleaned


def _cache_footprint():
    """缓存占用：漫画临时缓存 + 搜索/目录缓存（不含 downloads/books 永久内容）。"""
    total = _dir_size(MANGA_CACHE_DIR)
    for _f in (SEARCH_CACHE_FILE, TOC_CACHE_FILE):
        try:
            total += os.path.getsize(_f)
        except OSError:
            pass
    return total


def _toc_chapter(ch, i):
    """兼容 get_toc 返回的 dict 章节与对象章节"""
    if isinstance(ch, dict):
        return {"index": i, "name": ch.get("name", ""), "url": ch.get("url", "")}
    return {"index": i, "name": getattr(ch, "name", ""), "url": getattr(ch, "url", "")}


# R39b: 目录抓取总时限——源站不稳时快速失败(504)而非挂死 40s+；
# 超时后线程在后台继续完成并写缓存，下次请求直接命中秒回
_TOC_FETCH_DEADLINE = 35


# P0-5: 目录爬取全局并发上限——前端 autoLoadToc / 批量详情并发时，超出的
# 抓取在后台线程内排队等待信号量，而非各自起爬取线程同时打源站与本地
# 连接池（实测 60+ 行并发可形成 ~200s 的请求风暴，并与 _toc_lock 互踩）。
# 排队发生在后台线程内，请求路径只受 _TOC_FETCH_DEADLINE 约束：
# 排队超 35s 的请求按既有超时语义返回 504，后台线程继续完成并写缓存。
_TOC_FETCH_MAX_CONCURRENT = 4


_toc_fetch_sem = threading.Semaphore(_TOC_FETCH_MAX_CONCURRENT)


def _toc_fetch_bounded(src, book_url):
    """带总时限的目录抓取。返回 (True, info) 成功 / (False, None) 超时 /
    (False, exception) 失败。线程完成/失败都会写缓存或清 fetching 标记"""
    done_ev = threading.Event()
    box = {}

    def _work():
        try:
            # 全局并发上限：只包裹真正的网络爬取段，缓存写入/标记清理不占额度
            with _toc_fetch_sem:
                c = SourceCrawler(src)
                book = c.get_book(book_url, fast=True)
                book.chapters = []
                c.get_toc(book)
            chs = [_toc_chapter(ch, i + 1) for i, ch in enumerate(book.chapters)]
            info = {"chapter_count": book.chapter_count,
                    "last_chapter": book.last_chapter,
                    "name": book.name, "chapters": chs, "ts": time.time()}
            with _toc_lock:
                _toc_cache[book_url] = info
                if len(_toc_cache) > 2000:
                    for k in sorted(_toc_cache,
                                    key=lambda x: _toc_cache[x].get("ts", 0))[:1000]:
                        _toc_cache.pop(k, None)
                _toc_fetching.get(book_url, threading.Event()).set()
                _toc_fetching.pop(book_url, None)
            _toc_cache_save()
            box["data"] = info
        except Exception as e:
            box["error"] = e
            with _toc_lock:
                _toc_fetching.get(book_url, threading.Event()).set()
                _toc_fetching.pop(book_url, None)
        finally:
            done_ev.set()

    threading.Thread(target=_work, daemon=True).start()
    if not done_ev.wait(timeout=_TOC_FETCH_DEADLINE):
        return False, None
    if "data" in box:
        return True, box["data"]
    return False, box.get("error")


def _run_check_update(book_key):
    """后台线程：真正执行检查更新（get_book → get_toc → 对比 → 创建任务）"""
    try:
        d = os.path.join(BOOKS_DIR, book_key)
        state = load_book_state(os.path.join(d, "_state.json")) or {}
        info = state.get("book") or {}
        book_url = info.get("book_url")
        if not book_url:
            raise ValueError("书籍数据不完整（无 book_url）")
        source_uid = info.get("source_uid") or book_key.rsplit("_", 1)[0]
        src = get_by_uid(source_uid)
        # 旧书 uid 可能是中文名（如"爱笔楼_m.biqutu.info_xxx"）→ 用 book_url 域名找源
        if not src:
            _bu = info.get("book_url") or ""
            src = find_source(_bu) if _bu else None
        if not src:
            # R53: 书源已失效的书——明确告知而非每次报"书源不存在"错误
            with _lock:
                _job = _check_jobs.get(book_key)
                if _job:
                    _job.update(status="done", error="",
                                message="该书源已失效（源站已删除/下架），无法检查更新；"
                                        "本地已下载内容仍可正常阅读",
                                ts=now_iso())
            print(f"[check-update] {book_key}: 书源不存在(源已失效), 跳过", flush=True)
            return

        crawler = SourceCrawler(src)
        # 缺失检测：纯本地对比（chapters 目录 vs 已下载 completed）——
        # 不依赖源站实时访问（源站不可达也能检测缺失）
        local_urls = {c.get("url") for c in state.get("chapters", [])}
        done_urls = set(state.get("completed", []) or [])
        failed = state.get("failed", {}) or {}
        missing = [c for c in state.get("chapters", [])
                   if c.get("url") not in done_urls]
        # 尝试源站拉新章节（可选增强；失败不阻塞缺失检测）
        new_ch = []
        _toc_partial = False       # 目录没取全 → 不能说"已是最新"
        _toc_failed = False
        try:
            book = crawler.get_book(book_url)
            # 上限用 TOC_PAGE_CAP（与目录页同一口径），不再写死 30：
            # 实测各启用源目录都是单页或适配器源（不受该参数影响），
            # 但写死 30 对新章在**末页**的分页源是静默漏检——新章永远发现不了。
            # 代价可控：单页目录零成本，分页目录受 TOC_PAGE_CAP 与 60s 预算双重约束。
            new_toc = crawler.get_toc(book, max_pages=TOC_PAGE_CAP)
            new_ch = [c for c in new_toc if c.get("url") not in local_urls]
            _toc_partial = bool(getattr(crawler, "toc_truncated", False))
            if _toc_partial:
                print(f"[check-update] {book_key}: 目录分页撞上限，"
                      f"新章检测可能不完整（已取 {len(new_toc)} 章）", flush=True)
        except Exception as _ne:
            _toc_failed = True
            print(f"[check-update] {book_key}: 源站新章节检测失败（忽略）: {_ne}", flush=True)
        # 合并：缺失（本地未下载）+ 源站新章节 + 失败章节
        all_missing = list(missing)
        seen_urls = {c.get("url") for c in all_missing}
        # 失败章节里**本机已有正文的不算缺失**（_state.json 的 failed 可能是旧记录；
        # 实测有 2 章正文已下载 1 万字却仍被劝"下载缺失章节"）。
        from engine.app_utils import cache_key_of as _cko
        _bkdir = os.path.join(BOOKS_DIR, book_key)
        _failed_real = {u for u in failed
                        if not (os.path.exists(os.path.join(_bkdir, _cko(u) + ".cache"))
                                and os.path.getsize(os.path.join(_bkdir, _cko(u) + ".cache")) > 0)}
        for c in new_ch + [c for c in state.get("chapters", [])
                           if c.get("url") in _failed_real]:
            if c.get("url") not in seen_urls:
                seen_urls.add(c.get("url"))
                all_missing.append(c)
        with _lock:
            # 只检测不下载：返回缺失清单，前端询问后调 download-missing
            _check_missing[book_key] = {
                "book_url": book_url, "source_uid": source_uid,
                "chapters": all_missing, "ts": now_iso()}
            _check_jobs[book_key] = {
                "status": "done", "new_chapters": len(new_ch),
                "retry_failed": len(failed),
                "missing_count": len(all_missing),
                "task_id": None, "message":
                    ("、".join(x for x in (
                        (f"发现 {len(all_missing)} 个缺失章节" if all_missing
                         else "已是最新，无缺失章节"),
                        ("目录未取全（撞分页上限），可能仍有新章未检出" if _toc_partial
                         else ""),
                        ("源站目录本次不可达，新章未核对" if _toc_failed else ""),
                    ) if x)),
                "error": "", "ts": now_iso()}
    except Exception as e:
        print(f"[check-update] {book_key}: {type(e).__name__}: {e}", flush=True)
        with _lock:
            # 脱敏：异常原文（本地路径/内网地址）只进服务端日志
            _check_jobs[book_key] = {
                "status": "error", "new_chapters": 0, "retry_failed": 0,
                "task_id": None,
                "message": "检查更新失败（详细错误见服务端日志）",
                "error": "检查更新失败", "ts": now_iso()}


# ══════════════════════════════════════════════
#  漫画板块 API
# ══════════════════════════════════════════════
os.makedirs(MANGA_DIR, exist_ok=True)


_manga_adapters_loaded = False


_manga_adapter_lock = threading.Lock()


def _load_manga_adapters():
    global _manga_adapters_loaded
    with _manga_adapter_lock:
        if _manga_adapters_loaded:
            return
        try:
            from engine.manga.copymanga import CopyManga
            from engine.manga.copymanga_web import CopyMangaWeb
            from engine.manga.mangadex import MangaDex
            from engine.manga.baozi import Baozi
            from engine.manga.jm import Jm
            from engine.manga.nhentai import Nhentai
            from engine.manga.manager import register_adapter
            # 拷贝漫画优先注册网页版（更稳定，APP API 仅作兜底）
            register_adapter(CopyMangaWeb)
            register_adapter(CopyManga)
            register_adapter(MangaDex)
            register_adapter(Baozi)
            register_adapter(Jm)
            register_adapter(Nhentai)
            # Komiic 适配器已编写（engine/manga/komiic.py），当前网络环境 TLS 中断暂不注册
            _manga_adapters_loaded = True
        except Exception as e:
            print(f"[manga] 适配器加载失败: {e}", flush=True)


_manga_adapter_instances = {}


# R15: 详情失败冷却（刷新/构建失败后短时间内不再重试回源）
_detail_swr_fail_ts = {}   # (source, comic_id) -> 失败时间戳


DETAIL_SWR_FAIL_COOLDOWN = 120  # 失败后 2 分钟内不再重试


# B04: 漫画详情回源单飞注册表——所有详情构建（冷启动同步等待、快速路径与
# SWR 后台刷新）统一经此按 (source, comic_id) 合并：同书仅一个在飞任务，
# 并发请求共享同一轮 Future 结果；失败入冷却（秒级~分钟级），注册表有界
# （上限 + 挂死淘汰），完成后即移除，不泄漏。
_DETAIL_FLIGHT_MAX = 64      # 在飞详情任务上限（超限淘汰最旧）


_DETAIL_FLIGHT_STALE = 300   # 在飞条目超过 5 分钟视为挂死，允许被取代/清理


class DetailUnavailable(Exception):
    """详情暂不可用：同轮失败冷却中，或等待同书在飞构建超时"""


class _DetailFlight:
    """详情单飞句柄：done 事件 + 同轮共享的结果/异常（不持久缓存）"""
    __slots__ = ("done", "result", "error", "ts")

    def __init__(self):
        self.done = threading.Event()
        self.result = None
        self.error = None
        self.ts = time.time()


_detail_flights = {}          # (source, comic_id) -> _DetailFlight


_detail_flight_guard = threading.Lock()


def _detail_flight_begin(source, comic_id):
    """注册/加入详情单飞。返回 (flight, is_leader, cooling)。
    - 无在飞且无冷却 → leader：调用方负责执行构建，并最终调用
      _detail_flight_finish（无论成败，必须调用以唤醒等待者并释放条目）
    - 已有在飞 → 跟随者：等待 flight.done 后读 flight.result / flight.error
    - 冷却中（上轮刚失败）→ cooling=True，本轮不再回源
    注册表有界：先清挂死条目，再按插入序淘汰最旧至上限以内。"""
    key = (source, comic_id)
    now = time.time()
    with _detail_flight_guard:
        f = _detail_flights.get(key)
        if f is not None:
            return f, False, False
        if now - _detail_swr_fail_ts.get(key, 0) < DETAIL_SWR_FAIL_COOLDOWN:
            return None, False, True
        # 有界清理：挂死条目（leader 线程意外死亡未 finish）释放等待者后移除
        for _k, _v in list(_detail_flights.items()):
            if now - _v.ts > _DETAIL_FLIGHT_STALE:
                _v.done.set()
                _detail_flights.pop(_k, None)
        while len(_detail_flights) >= _DETAIL_FLIGHT_MAX:
            _k = next(iter(_detail_flights))
            _detail_flights[_k].done.set()
            _detail_flights.pop(_k, None)
        f = _DetailFlight()
        _detail_flights[key] = f
        return f, True, False


def _detail_flight_finish(source, comic_id, flight, result, error):
    """leader 完成：写入同轮共享结果，失败记冷却，移除注册并唤醒等待者。
    失败结果不持久缓存——仅供同轮等待者共享 + 短期冷却，冷却后新一轮
    请求可重试（对齐 Mihon/Kotatsu：源站抖动不固化"无结果"）。"""
    key = (source, comic_id)
    flight.result = result
    flight.error = error
    with _detail_flight_guard:
        if error is not None or result is None:
            _detail_swr_fail_ts[key] = time.time()
            # 失败时间戳表有界（防无限增长）
            if len(_detail_swr_fail_ts) > 500:
                for _k in sorted(_detail_swr_fail_ts,
                                 key=lambda k: _detail_swr_fail_ts[k]
                                 )[:len(_detail_swr_fail_ts) - 500]:
                    _detail_swr_fail_ts.pop(_k, None)
        if _detail_flights.get(key) is flight:
            _detail_flights.pop(key, None)
    flight.done.set()


def _detail_fetch_singleflight(source, comic_id, builder, timeout=120):
    """冷启动详情构建单飞：leader 同步执行 builder，并发跟随者等待并共享
    同一轮结果（成功 dict / 失败 None / 异常）；冷却中或等待超时返回
    (None, DetailUnavailable)——明确"暂不可用"，不绕过 leader 自行回源。
    返回 (result, error)：error 为 None 表示本轮执行成功（result 仍可能为
    None = 软失败：拉取与降级均无数据）。"""
    flight, is_leader, cooling = _detail_flight_begin(source, comic_id)
    if cooling:
        return None, DetailUnavailable("详情刚获取失败，冷却中，请稍后重试")
    if is_leader:
        result, error = None, None
        try:
            result = builder()
        except Exception as e:
            error = e
            print(f"[manga-detail] 详情构建失败: {e}", flush=True)
        _detail_flight_finish(source, comic_id, flight, result, error)
        return result, error
    # 跟随者：等待同一轮结果，绝不自行回源（原递归重入已消除）
    if flight.done.wait(timeout=timeout):
        return flight.result, flight.error
    return None, DetailUnavailable("同书详情构建中，等待超时，请稍后重试")


def _detail_refresh_async(source, comic_id, builder, delay=0.0):
    """后台详情刷新单飞（快速路径 / SWR 不过期阻塞响应）：仅当无在飞且无
    冷却时提交一个刷新线程；已在飞或冷却中直接返回 False（不重复提交
    短命线程）。返回是否提交了新任务。"""
    flight, is_leader, _cooling = _detail_flight_begin(source, comic_id)
    if not is_leader:
        return False

    def _run():
        result, error = None, None
        try:
            if delay:
                time.sleep(delay)
            result = builder()
        except Exception as e:
            error = e
            print(f"[manga-detail] 后台刷新失败: {e}", flush=True)
        _detail_flight_finish(source, comic_id, flight, result, error)

    threading.Thread(target=_run, daemon=True).start()
    return True


def _manga_adapter(key):
    """获取漫画源适配器（单例缓存：避免每次 new 实例导致初始化请求绕过限流/状态混乱）"""
    from engine.manga.manager import get_adapter
    _load_manga_adapters()
    if key not in _manga_adapter_instances:
        state_dir = MANGA_STATE_DIR
        _manga_adapter_instances[key] = get_adapter(key, state_dir=state_dir)
    return _manga_adapter_instances.get(key)


# 阅读通道适配器实例（R22）：与常规单例分离——阅读实例绕过源令牌桶限流
# （copymanga 4s/请求 × 39 张 = 在线阅读每章 2 分半+，App 逐张读图完全不可用；
# 下载任务仍用 throttle=True 实例，防风控标记）
_manga_read_instances = {}


def _manga_read_adapter(key):
    """阅读通道适配器（throttle=False）：img/urls 端点专用，逐张读图不串行"""
    from engine.manga.manager import get_adapter
    _load_manga_adapters()
    if key not in _manga_read_instances:
        ad = get_adapter(key, state_dir=MANGA_STATE_DIR)
        if ad is not None:
            try:
                ad._throttle = False  # 适配器鸭子类型开关（copymanga 等）
            except Exception:
                pass
        _manga_read_instances[key] = ad
    return _manga_read_instances.get(key)


def _manga_read_images(ad, source, comic_id, chapter_id):
    """阅读通道取章节图片列表：copymanga APP 通道被风控(210)时
    自动降级 copymanga_web 通道（APP API 的 210 是 IP 级标记 TTL≈1h，
    web 通道不受影响；R22 防止 35s×N 重试链挂死阅读）。
    返回 (adapter, imgs)——降级时同时换适配器，图片下载同步走 web 通道"""
    try:
        return ad, _get_chapter_images(ad, source, comic_id, chapter_id)
    except Exception as _e:
        _es = str(_e)
        if source == "copymanga" and any(k in _es for k in
                                         ("210", "风控", "冷却", "标记")):
            print(f"[manga-read] copymanga APP 通道风控({_es[:50]})，"
                  f"阅读降级 copymanga_web", flush=True)
            ad2 = _manga_read_adapter("copymanga_web")
            if ad2:
                return ad2, _get_chapter_images(ad2, source, comic_id, chapter_id)
        raise


# 漫画搜索缓存（按 q+source+page，5 分钟 TTL）
_manga_search_cache = {}


_MANGA_SEARCH_TTL = 600  # R19: 全源搜索~6s，10min 缓存减源站压力


_MANGA_SEARCH_LOCK = threading.Lock()


# R39: 批量检查更新结果缓存（(source, comic_id) → {ts, source, title, missing}）
# P1-4: key 含 source——不同书源 comic_id 可能相同，只用 comic_id 会相互覆盖，
# 导致"全部补充下载"把 A 源的缺失章节下到 B 源同名 id 上
_mcache = {}


def _manga_search_cached(key, builder):
    """漫画搜索缓存。R63: 空结果不缓存/不命中——一次实时失败(超时/源站
    抖动)留下的空结果若被缓存 1h, 会让"源站明明有却一直搜不到"
    (实测拷贝源带 order=mr 的前端路径被旧空缓存污染)。
    R66: 带 errors(部分源失败/超时)的结果也不缓存——结果不完整时
    用户重试应真正重跑失败源, 而不是命中缓存继续看旧错误。"""
    now = time.time()
    with _MANGA_SEARCH_LOCK:
        hit = _manga_search_cache.get(key)
        if hit and (hit[1] or {}).get("results"):
            _ttl = (_MANGA_SEARCH_PARTIAL_TTL
                    if key in _manga_search_partial else _MANGA_SEARCH_TTL)
            if now - hit[0] < _ttl:
                return hit[1], True
    data = builder()
    if (data or {}).get("results") and not (data or {}).get("errors"):
        with _MANGA_SEARCH_LOCK:
            _manga_search_cache[key] = (now, data)
    elif (data or {}).get("results") and (data or {}).get("errors"):
        # 部分结果（有源超时/仍在查询）：也缓存**一小段**。
        # R66 的本意是"用户重试时真要重跑失败源"——那是分钟级重试；
        # 而实测的痛点是"每次搜索都正好 20s"（一个不可达源拖满期限），
        # 连连续重搜都要再等一轮。这里给 60s 短缓存：连续重搜/翻页秒回，
        # 一分钟后的重试仍会真正重跑（errors 一起缓存并在界面照实显示）。
        with _MANGA_SEARCH_LOCK:
            _manga_search_cache[key] = (now, data)
            _manga_search_partial[key] = now
            # 超限淘汰最旧到上限以内（旧版只清过期条目，TTL 内条数可无限涨）
            if len(_manga_search_cache) > 200:
                for _k in sorted(_manga_search_cache,
                                 key=lambda k: _manga_search_cache[k][0]
                                 )[:len(_manga_search_cache) - 200]:
                    _manga_search_cache.pop(_k, None)
                    _manga_search_partial.pop(_k, None)
    return data, False


_MANGA_SEARCH_FETCHING = {}  # key -> _SearchFlight（P2-1: 同 key 并发搜索单飞）


_MANGA_SEARCH_PARTIAL_TTL = 60   # 带 errors 的部分结果短缓存（见 _manga_search_cached）


_manga_search_partial = {}       # key -> 写入时间（用于区分"完整/部分"两种 TTL）


_MANGA_SEARCH_FAIL_COOLDOWN = 5  # B05: 失败结果秒级冷却——同轮等待者共享 +
                                 # 防瞬时重试风暴；冷却后新一轮用户操作可重试


_manga_search_fail = {}  # key -> (ts, payload, error)（失败短期记忆，不入主缓存）


_MANGA_SEARCH_FAIL_MAX = 200  # 失败短期记忆上限（防无限增长）


_MANGA_SEARCH_WAIT_TIMEOUT = 60  # 跟随者等待同轮结果上限（秒；全源 deadline ~25s）


class _SearchFlight:
    """搜索单飞句柄：done 事件 + 同轮共享的 (payload, cached) 或异常。
    B05: 成功与失败结果都随 Future 共享给所有等待者——不再让跟随者在
    leader 失败后递归重入、各自重跑一遍全源搜索。"""
    __slots__ = ("done", "result", "error")

    def __init__(self):
        self.done = threading.Event()
        self.result = None   # (payload, cached)
        self.error = None


def _evict_manga_search_fail():
    """失败短期记忆有界淘汰（调用方须持 _MANGA_SEARCH_LOCK）"""
    if len(_manga_search_fail) > _MANGA_SEARCH_FAIL_MAX:
        for _k in sorted(_manga_search_fail,
                         key=lambda k: _manga_search_fail[k][0]
                         )[:len(_manga_search_fail) - _MANGA_SEARCH_FAIL_MAX]:
            _manga_search_fail.pop(_k, None)


def _manga_search_singleflight(key, builder):
    """P2-1: 漫画搜索缓存 + 同 key 单飞合并（B05 加固为 Future 共享结果）。
    仅用于「未命中需实际构建」的读取路径（/api/manga/search）：首个请求实际
    跑全源搜索，并发同 key 的跟随者等待并共享同一轮结果——成功命中缓存；
    失败（空结果/带 errors/异常）也由同轮等待者共享，不再递归重入重跑。
    失败仅秒级短期记忆（_MANGA_SEARCH_FAIL_COOLDOWN），不长期固化
    "源站失败=无结果"：冷却后新一轮用户操作正常重试。
    等待超时返回明确"暂不可用"，不绕过 leader 自行回源。
    SSE 流式端点末尾的纯写入路径（结果已算完、仅落缓存）仍走
    _manga_search_cached——builder 瞬时返回，不应被单飞等待阻塞。"""
    with _MANGA_SEARCH_LOCK:
        now = time.time()
        hit = _manga_search_cache.get(key)
        if hit and now - hit[0] < _MANGA_SEARCH_TTL \
                and (hit[1] or {}).get("results"):
            return hit[1], True
        f = _MANGA_SEARCH_FETCHING.get(key)
        if f is None:
            # 秒级失败冷却：上一轮刚失败 → 直接共享同轮结果，不立刻再回源
            _fail = _manga_search_fail.get(key)
            if _fail and now - _fail[0] < _MANGA_SEARCH_FAIL_COOLDOWN:
                if _fail[2] is not None:
                    raise _fail[2]
                return _fail[1], False
            f = _SearchFlight()
            _MANGA_SEARCH_FETCHING[key] = f
            leader = True
        else:
            leader = False
    if leader:
        try:
            res = _manga_search_cached(key, builder)
            f.result = res
            _payload = res[0] if res else None
            # "不缓存"类失败（空结果或带 errors）→ 秒级短期记忆，
            # 同轮跟随者与冷却期内的瞬时重试共享，防重试风暴
            if not (_payload or {}).get("results") or (_payload or {}).get("errors"):
                with _MANGA_SEARCH_LOCK:
                    _manga_search_fail[key] = (time.time(), _payload, None)
                    _evict_manga_search_fail()
            return res
        except Exception as e:
            f.error = e
            with _MANGA_SEARCH_LOCK:
                _manga_search_fail[key] = (time.time(), None, e)
                _evict_manga_search_fail()
            raise
        finally:
            with _MANGA_SEARCH_LOCK:
                if _MANGA_SEARCH_FETCHING.get(key) is f:
                    _MANGA_SEARCH_FETCHING.pop(key, None)
            f.done.set()
    # 跟随者：等待并共享同一轮结果；leader 失败→同轮错误；超时→明确不可用，
    # 均不绕过 leader 自行回源（原"深度≤2"递归重入已消除）
    if f.done.wait(timeout=_MANGA_SEARCH_WAIT_TIMEOUT):
        if f.error is not None:
            raise f.error
        return f.result
    raise RuntimeError("搜索暂不可用（同关键词搜索进行中，等待超时）")


def _sort_split_chapters(chapters):
    """章节列表 → (episodes, volumes)：统一按话号正序排序 + 卷/话分离。
    R47 收敛：_refresh_detail_cache 与 _api_detail_fallback 此前各抄一份。
    - 排序与下载任务 _sort_chapters 一致（修各适配器 reverse 策略不一的乱序）
    - 含"卷"字样的条目归入 volumes（合集/整卷），其余为 chapters——
      卷不参与话的阅读序列
    """
    try:
        from engine.manga.download_manager import _sort_chapters
        ch_sorted = _sort_chapters(
            [{"id": c.id, "name": c.name, "group": c.group} for c in chapters])
    except Exception:
        ch_sorted = [{"id": c.id, "name": c.name, "group": c.group}
                     for c in chapters]
    volumes, episodes = [], []
    for _c in ch_sorted:
        _nm = _c.get("name") or ""
        if re.search(r"(?:第\s*\d+\s*卷|Vol\.?\s*\d+|卷\s*\d+|\d+\s*卷)", _nm, re.I):
            volumes.append(_c)
        else:
            episodes.append(_c)
    return episodes, volumes


# 章节图片列表缓存（避免逐图请求源站 images，jm 混淆源懒下载时 N+1 查询）
# P1-1: 三级读取——内存热层(300s,加锁) → 磁盘持久层(1 天,全源覆盖) →
# 回源 adapter.images()（回源成功后写两层）；
# 单飞合并：同章并发取图只回源一次（参照 _toc_fetching 模式）
_CHAPTER_IMAGES_CACHE = {}


_CHAPTER_IMAGES_TTL = 300  # 5 分钟


_CHAPTER_IMAGES_DISK_TTL = 86400  # 磁盘持久层 1 天（沿用 copymanga_web _imgs.json 约定）


_CHAPTER_IMAGES_LOCK = threading.Lock()


_CHAPTER_IMAGES_FETCHING = {}  # key -> threading.Event（单飞：同章并发取图合并为一次）


def _chapter_images_disk_path(source, comic_id, chapter_id):
    """章节图片 URL 列表磁盘缓存路径（沿用既有漫画缓存目录约定：
    data/manga/_cache/<source>/<comic_id>/<chapter_id>_imgs.json）"""
    return os.path.join(MANGA_CACHE_DIR, source, comic_id,
                        f"{chapter_id}_imgs.json")


def _chapter_images_disk_load(source, comic_id, chapter_id):
    """磁盘持久层读取：仅认 {"v":2,"urls":[...]}（ts 缺失时按 mtime 判期，
    兼容 copymanga_web 历史写入）；R70 旧裸数组(R60 半截列表)一律不信任"""
    p = _chapter_images_disk_path(source, comic_id, chapter_id)
    try:
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if not (isinstance(d, dict) and d.get("v") == 2):
            return None
        urls = d.get("urls") or []
        if not urls:
            return None
        ts = d.get("ts") or os.path.getmtime(p)
        if time.time() - ts >= _CHAPTER_IMAGES_DISK_TTL:
            return None
        return urls
    except Exception:
        return None


def _chapter_images_disk_save(source, comic_id, chapter_id, urls):
    """磁盘持久层写入：tmp+os.replace 原子写（app_utils.atomic_write）；
    失败只记日志，不影响请求"""
    try:
        atomic_write(_chapter_images_disk_path(source, comic_id, chapter_id),
                     {"v": 2, "ts": time.time(), "urls": urls})
    except Exception as _e:
        print(f"[manga-imgs] {source}/{comic_id}/{chapter_id} 图片列表落盘失败: "
              f"{_e}", flush=True)


def _known_chapter_pages(source, comic_id, chapter_id):
    """本章**权威页数**（内存热层 → 磁盘持久层）；未知返回 None。

    只读缓存、零源站请求。用途：/urls 判断"本地已有文件是否等于整章"。
    只按"本地文件连续"判断会把**部分缓存**（加载中刷新只落前 8 页、预热跑
    一半）误判成"整章 8 页"，客户端因此永远不知道还有第 9 页——后续页面
    再也不加载，且不会自愈。"""
    key = (source, comic_id, chapter_id)
    with _CHAPTER_IMAGES_LOCK:
        c = _CHAPTER_IMAGES_CACHE.get(key)
        if c and time.time() - c[0] < _CHAPTER_IMAGES_TTL:
            return len(c[1] or [])
    _u = _chapter_images_disk_load(source, comic_id, chapter_id)
    return len(_u) if _u else None


def _get_chapter_images(ad, source, comic_id, chapter_id):
    """章节图片 URL 列表：内存 300s 热层 → 磁盘 1 天持久层（全源）→
    回源 ad.images()（成功写两层）；同 key 并发单飞合并"""
    key = (source, comic_id, chapter_id)
    now = time.time()
    _hot = None
    with _CHAPTER_IMAGES_LOCK:
        c = _CHAPTER_IMAGES_CACHE.get(key)
        if c and now - c[0] < _CHAPTER_IMAGES_TTL:
            _hot = c[1]
        if _hot is None:
            ev = _CHAPTER_IMAGES_FETCHING.get(key)
            if ev is None:
                ev = threading.Event()
                _CHAPTER_IMAGES_FETCHING[key] = ev
                leader = True
            else:
                leader = False
    if _hot is not None:
        # 内存命中也要保证**磁盘层**有这份列表：/urls 的本地分支只读磁盘/内存
        # 的 URL 列表且刻意不回源（R49c），磁盘缺失时它只能用"本地已有页数"
        # 推断整章页数——部分缓存（加载中刷新只落了前几页）会被截断成
        # "整章只有 N 页"，用户看到后续页面永远不再加载。这里补写一次即可。
        if _chapter_images_disk_load(source, comic_id, chapter_id) is None:
            _chapter_images_disk_save(source, comic_id, chapter_id, _hot)
        return _hot
    if not leader:
        # 跟随者：等首个请求完成后直接命中内存热层；
        # 首个请求失败/超时未留缓存时，自己重走完整三级读取（成为新 leader，
        # 深度 ≤2，失败即向上抛，不会无限递归）
        ev.wait(timeout=60)
        with _CHAPTER_IMAGES_LOCK:
            c = _CHAPTER_IMAGES_CACHE.get(key)
            if c and time.time() - c[0] < _CHAPTER_IMAGES_TTL:
                return c[1]
        return _get_chapter_images(ad, source, comic_id, chapter_id)
    try:
        imgs = _chapter_images_disk_load(source, comic_id, chapter_id)
        if not imgs:
            imgs = ad.images(comic_id, chapter_id)
            if not imgs:
                # R79: 空图片列表一律视为**失败**，绝不缓存、绝不落盘。
                # jm 的 images() 实现是 `[url for img in (j.get("images") or [])]`
                # ——源站风控/异常响应里没有 images 字段时返回的是空列表而不是
                # 异常；旧实现把它写进 300s 内存热层，于是接下来 5 分钟每次
                # /urls 都是 count=0（整章没有图），**刷新页面也无效**（缓存在
                # 服务端进程里），用户只能等过期或重启服务。一次"加载中刷新"
                # 产生的重复请求最容易触发源站这一路。
                from engine.manga.base import MangaError
                raise MangaError(
                    f"源站未返回本章图片列表（可能被限流，请稍后重试）: "
                    f"{source}/{comic_id}/{chapter_id}")
            _chapter_images_disk_save(source, comic_id, chapter_id, imgs)
        with _CHAPTER_IMAGES_LOCK:
            _CHAPTER_IMAGES_CACHE[key] = (time.time(), imgs)
            # 超限淘汰最旧到上限以内（旧版只清过期条目，TTL 内条数可无限涨）
            if len(_CHAPTER_IMAGES_CACHE) > 200:
                for _k in sorted(_CHAPTER_IMAGES_CACHE,
                                 key=lambda k: _CHAPTER_IMAGES_CACHE[k][0]
                                 )[:len(_CHAPTER_IMAGES_CACHE) - 200]:
                    _CHAPTER_IMAGES_CACHE.pop(_k, None)
        return imgs
    finally:
        with _CHAPTER_IMAGES_LOCK:
            _CHAPTER_IMAGES_FETCHING.pop(key, None)
            ev.set()


# P1-2: 阅读期后台预取——/urls 返回当前话后，异步预热下一话图片列表缓存
# 2026-09-10: 预取范围延伸到"下一话前几页图片"（服务器端流水线预热）
_NEXT_CHAPTER_WARM_PAGES = 5
_prefetch_lock = threading.Lock()


_prefetch_inflight = set()   # (source, comic_id, next_chapter_id)（预取单飞）


def _prefetch_next_chapter_images(source, comic_id, chapter_id):
    """异步预热下一话 _get_chapter_images（含 HTTP/渲染通道），结果落缓存；
    用户翻到下一话时秒回。预取单飞（同话不重复）+ 异常静默（只记日志）。"""
    try:
        # 章节顺序来自详情缓存（7 天 TTL，/urls 前用户必经详情页，通常已在）
        _info_p = os.path.join(MANGA_CACHE_DIR, source, comic_id,
                               "_info_full.json")
        if not os.path.exists(_info_p):
            return
        with open(_info_p, encoding="utf-8") as f:
            _chs = (json.load(f).get("data") or {}).get("chapters") or []
        _ids = [_c.get("id") for _c in _chs if _c.get("id")]
        if chapter_id not in _ids:
            return
        _ni = _ids.index(chapter_id) + 1
        if _ni >= len(_ids):
            return  # 已是最后一话
        _next = _ids[_ni]
        # 下一话已下载（本地直出，不经 _get_chapter_images）→ 无需预取
        _nd = _manga_media_root(source, comic_id, _next)
        if os.path.isdir(_nd) and any(
                f.lower().endswith((".webp", ".jpg", ".jpeg", ".png",
                                    ".gif", ".avif"))
                for f in os.listdir(_nd)):
            return
    except Exception:
        return
    _pf_key = (source, comic_id, _next)
    _urls_cached = False
    with _prefetch_lock:
        if _pf_key in _prefetch_inflight:
            return
        # 内存热层/磁盘持久层已命中 → 无需再回源拿 URL 列表（避免下载任务期间
        # 重复请求）；但**图片预热仍要做**——列表有缓存 ≠ 图在本地，早退会让
        # "下一话首屏"退回逐张等往返（实测 0.17s/页）。
        _c = _CHAPTER_IMAGES_CACHE.get(_pf_key)
        if _c and time.time() - _c[0] < _CHAPTER_IMAGES_TTL:
            _urls_cached = True
        elif _chapter_images_disk_load(source, comic_id, _next) is not None:
            _urls_cached = True
        _prefetch_inflight.add(_pf_key)

    def _work():
        try:
            ad = _manga_read_adapter(source)
            if ad:
                # 走阅读通道（copymanga 210 时自动降级 web 通道，与正读一致）
                if not _urls_cached:
                    _manga_read_images(ad, source, comic_id, _next)
                # 2026-09-10: 只预热 URL 列表还不够——切话后每一页仍要各等一次
                # 源站往返。继续把下一话**前若干页图片**送进阅读缓存，翻到下一话
                # 时首屏直接命中本地（预热器自身低并发 + 切话代际守卫，
                # 不与当前话抢带宽）。
                try:
                    from server.manga_api import _warm_chapter_images
                    _warm_chapter_images(source, comic_id, _next,
                                         limit=_NEXT_CHAPTER_WARM_PAGES,
                                         guard_chapter=chapter_id)
                except Exception:
                    pass
        except Exception as _e:
            print(f"[manga-prefetch] {source}/{comic_id}/{_next} 预取失败: "
                  f"{type(_e).__name__}: {_e}", flush=True)
        finally:
            with _prefetch_lock:
                _prefetch_inflight.discard(_pf_key)

    _t = threading.Thread(target=_work, daemon=True,
                          name=f"manga-prefetch-{source}")
    _t.start()


# ══════════════════════════════════════════════════════════════
# 本地封面（2026-09-13）
#
# 背景：书库里的 cover 只有**源站 URL**，本地从不保存封面文件。断网后浏览器
# 取不到 → 卡片封面空白（"未联网时书库不显示封面"）。这里提供本地封面的
# 读写：封面按漫画存放在 downloads/<源>/<cid>/cover.<ext>（已下载）或
# _cache/<源>/<cid>/cover.<ext>（仅阅读缓存），服务端本地优先直出，离线可看。
# ══════════════════════════════════════════════════════════════
_COVER_EXTS = (".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif")


def _local_cover_path(source, comic_id):
    """本地封面文件路径（downloads 优先，其次 _cache）；没有则 None。

    两个根目录都查：已下载漫画（downloads 存在）的封面可能是浏览时缓存在
    _cache 的，反之亦然——只查 _manga_media_root 会漏掉另一种。
    """
    for _root in (MANGA_DOWNLOADS_DIR, MANGA_CACHE_DIR):
        base = os.path.join(_root, source, str(comic_id))
        for _ext in _COVER_EXTS:
            _p = os.path.join(base, "cover" + _ext)
            try:
                if os.path.getsize(_p) > 1000:   # 同图片缓存判定：过小视为坏文件
                    return _p
            except OSError:
                continue
    return None


def _cover_ext_of(data):
    """按魔数判定封面扩展名（与 downloader 的图片头校验一致）"""
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return ".avif"
    return ".webp"


def _save_cover_bytes(source, comic_id, data):
    """封面字节落盘到本地（原子写 + 图片头校验）；返回路径或 None。

    落盘失败（磁盘满/只读）只记日志并返回 None——调用方回内存字节即可，
    不影响本次显示。"""
    if not data or len(data) < 1000:
        return None
    base = _manga_media_root(source, comic_id)
    ext = _cover_ext_of(data)
    try:
        os.makedirs(base, exist_ok=True)
        from engine.manga.downloader import atomic_write_image, BadImageError
        p = os.path.join(base, "cover" + ext)
        try:
            atomic_write_image(p, data)
        except BadImageError:
            return None
        return p
    except Exception as _e:
        print(f"[manga-cover] {source}/{comic_id} 封面落盘失败: "
              f"{type(_e).__name__}: {_e}", flush=True)
        return None


_manga_total_cache = {}          # (source, comic_id) -> (ts, total)
_MANGA_TOTAL_TTL = 300           # 详情缓存里的总话数，5 分钟记忆


def _manga_cached_chapters(source, comic_id):
    """详情缓存里的章节列表 `[{id, name, group}, …]`；没有缓存返回 []。

    为什么要列表而不只是总数（0.64.0）：阅读进度的**续读落点**不能再用下标
    （章节目录会变：站方加更、删章、插入「推特杂图」这类非正话条目），必须按
    **章节身份**（id → 标签 → 话号）在当前列表里定位。书库行要在零源站请求下
    给出"到底会打开哪一话"，所以这里提供一次性解析出的列表，并做短 TTL 记忆。
    """
    key = (source, str(comic_id))
    now = time.time()
    c = _manga_chapters_cache.get(key)
    if c and now - c[0] < _MANGA_TOTAL_TTL:
        return c[1]
    chs = []
    try:
        p = os.path.join(MANGA_CACHE_DIR, source, str(comic_id),
                         "_info_full.json")
        with open(p, encoding="utf-8") as f:
            chs = (json.load(f).get("data") or {}).get("chapters") or []
        if not isinstance(chs, list):
            chs = []
    except Exception:
        chs = []
    _manga_chapters_cache[key] = (now, chs)
    if len(_manga_chapters_cache) > 200:      # 有界（列表比计数大，缓存更小）
        for _k in sorted(_manga_chapters_cache,
                         key=lambda k: _manga_chapters_cache[k][0])[:80]:
            _manga_chapters_cache.pop(_k, None)
    return chs


_manga_chapters_cache = {}       # (source, comic_id) -> (ts, chapters)


def _manga_total_chapters(source, comic_id):
    """该漫画已知总话数（读详情缓存 _info_full.json）；未知返回 0。

    书库为每部漫画标注"读到第 N 话 / 共 M 话"的进度条需要 M。详情缓存是阅读
    器/详情页本来就会写的，这里零源站请求、只读磁盘；短 TTL 记忆避免书库轮询
    时反复解析同一个 JSON。"""
    key = (source, str(comic_id))
    now = time.time()
    c = _manga_total_cache.get(key)
    if c and now - c[0] < _MANGA_TOTAL_TTL:
        return c[1]
    total = 0
    try:
        p = os.path.join(MANGA_CACHE_DIR, source, str(comic_id),
                         "_info_full.json")
        with open(p, encoding="utf-8") as f:
            chs = (json.load(f).get("data") or {}).get("chapters") or []
        total = len(chs)
    except Exception:
        total = 0
    _manga_total_cache[key] = (now, total)
    if len(_manga_total_cache) > 500:      # 有界
        for _k in sorted(_manga_total_cache,
                         key=lambda k: _manga_total_cache[k][0])[:200]:
            _manga_total_cache.pop(_k, None)
    return total


def _scan_downloaded_chapters(source, comic_id):
    """扫描已下载章节 id 集合（目录内含图片文件 = 已下载）"""
    base = _manga_media_root(source, comic_id)
    if not os.path.isdir(base):
        return []
    out = []
    for _n in os.listdir(base):
        # A01 双保险②：覆盖修复的崩溃残留备份目录不算章节
        # （含图片会被误统计为幽灵章节、章节数 +1）
        if _n.endswith(".repair_old"):
            continue
        _p = os.path.join(base, _n)
        if not os.path.isdir(_p):
            continue
        # 目录内有图片文件才算已下载（空目录/仅 URL 缓存不算）
        try:
            _has_img = any(f.lower().endswith((".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif"))
                           for f in os.listdir(_p))
        except Exception:
            _has_img = False
        if _has_img:
            out.append(_n)
    return out


def _manga_media_root(source, comic_id, chapter_id=None):
    """定位漫画图片目录：已下载( downloads/，永久)优先，其次临时缓存( _cache/ )。
    返回目录绝对路径；都不存在返回 downloads 路径（新下载写入处）。"""
    d_root = os.path.join(MANGA_DOWNLOADS_DIR, source, comic_id,
                          chapter_id) if chapter_id else \
        os.path.join(MANGA_DOWNLOADS_DIR, source, comic_id)
    c_root = os.path.join(MANGA_CACHE_DIR, source, comic_id,
                          chapter_id) if chapter_id else \
        os.path.join(MANGA_CACHE_DIR, source, comic_id)
    if os.path.isdir(d_root):
        return d_root
    if os.path.isdir(c_root):
        return c_root
    return d_root


def _local_chapter_images(source, comic_id, chapter_id):
    """已下载章节的图片 URL 列表（本地优先，避免请求源站拖慢阅读）
    - 图片 URL 顺序取自 _imgs.json 缓存（源站 words 重排后的顺序）
    - 返回 None 表示该章节无本地数据（需走源站）
    """
    base = _manga_media_root(source, comic_id, chapter_id)
    if not os.path.isdir(base):
        return None
    # 本地已下载文件（按序号排序）
    files = sorted(f for f in os.listdir(base)
                   if f.lower().endswith((".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif")))
    if not files:
        return None
    # URL 顺序：_imgs.json 缓存（源站重排后顺序）；无则用本地文件推断
    imgs_p = os.path.join(_manga_media_root(source, comic_id),
                          f"{chapter_id}_imgs.json")
    urls = []
    if os.path.exists(imgs_p):
        try:
            _u = json.load(open(imgs_p, encoding="utf-8"))
            if isinstance(_u, dict) and _u.get("v") == 2:
                # R70: 新版版本化缓存 {"v":2,"urls":[...]}
                urls = _u.get("urls") or []
            elif isinstance(_u, list):
                # R70: 旧裸数组缓存(R60 半截列表)不可信 → 本地文件为准
                urls = []
            else:
                urls = []
        except Exception:
            urls = []
    if not urls:
        # 无 URL 缓存：返回本地占位（前端直接走 /img/ 接口读磁盘）
        return [""] * len(files)
    # 返回完整 URL 列表：已下载部分用原 URL（前端 /img/ 会命中磁盘缓存），
    # 缺失部分保留 URL 让前端按需补拉——直接全量返回 urls
    return urls


_manga_dl = DownloadManager(
    state_file=MANGA_TASKS_FILE)


_manga_dl.configure(
    cache_root=MANGA_CACHE_DIR,
    state_dir=MANGA_STATE_DIR,
    library_file=MANGA_LIBRARY_FILE,
    downloads_root=MANGA_DOWNLOADS_DIR)


# P1-1: 下载 worker 的章节图片列表也走统一三级缓存（内存→磁盘→回源）
import engine.manga.download_manager as _dm_mod  # noqa: E402
_dm_mod.images_resolver = _get_chapter_images


def _manga_dl_key(source, comic_id):
    # 归一化后再拼键：否则 jm123 与 123 会被当作两个任务重复下载
    return f"{source}:{_norm_comic_id(source, comic_id)}"


def _manga_adapter_name(source):
    ad = _manga_adapter_instances.get(source)
    if ad:
        return ad.name
    return source


# ══════════════════════════════════════════════
#  B03: 书库统计快照（图片数/章节数持久化 + 条目 revision）
# ══════════════════════════════════════════════
# 背景：/api/manga/library 曾在请求路径上对每部漫画 os.walk 实扫图片
# （copymanga 双源互查还要再扫备选源），书库页 5s 轮询 → 持续全库目录遍历。
# 现在：计数持久化到 _library_stats.json；下载完成/删除/修复只在对应条目上
# 增量重扫（锁外扫描单部，绝不全库遍历）；后台线程低频核对真实文件——
# 核对以真实 os.walk 结果为准，不用"目录 mtime 一定反映所有后代变化"的
# 假设做唯一判据（原子替换/rename/深层文件写不一定冒泡到根目录 mtime）。

MANGA_LIBRARY_STATS_FILE = os.path.join(MANGA_DIR, "_library_stats.json")


_MANGA_IMG_EXTS = (".webp", ".jpg", ".png")


_manga_stats_lock = threading.Lock()


_manga_lib_stats = {}    # "source\x00comic_id" -> {images, chapters, rev, ts, ...}


_manga_lib_rev = 0       # 全局书库 revision（任何条目增删改 +1，随快照持久化）


_manga_stats_pending = set()   # 请求路径发现快照缺失 → 排队由后台优先核对


_MANGA_VERIFY_INTERVAL = 600   # 后台低频核对周期（秒）


_MANGA_VERIFY_STAGGER = 0.5    # 每部核对间隔，摊开 IO


def _manga_stats_skey(source, comic_id):
    return source + "\x00" + comic_id


def _manga_stats_load():
    """启动时恢复快照（图片数/章节数/条目 revision 跨重启保持）"""
    global _manga_lib_rev
    try:
        with open(MANGA_LIBRARY_STATS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in (data.get("entries") or {}).items():
            if isinstance(v, dict):
                _manga_lib_stats[k] = v
        _manga_lib_rev = int(data.get("rev") or 0)
    except Exception:
        pass


def _manga_stats_save_locked():
    """持锁内调用：快照原子落盘（条目少、体积小，直接写）"""
    try:
        atomic_write(MANGA_LIBRARY_STATS_FILE,
                     {"rev": _manga_lib_rev, "entries": _manga_lib_stats})
    except Exception as _e:
        print(f"[manga-stats] 快照落盘失败: {type(_e).__name__}: {_e}",
              flush=True)


def _manga_count_comic(source, comic_id):
    """实扫单部漫画 → (图片数, 章节数)。增量更新与后台核对共用的唯一扫描入口。
    口径与旧请求路径一致：章节数 = 图片根下子目录数；图片数含所有后代目录。"""
    base = _manga_media_root(source, comic_id)
    images = 0
    chapters = 0
    if os.path.isdir(base):
        try:
            # 历史修复备份既不计章节，也不扫描其图片。
            chapters = sum(1 for _n in os.listdir(base)
                           if not _n.endswith(".repair_old")
                           and os.path.isdir(os.path.join(base, _n)))
        except OSError:
            chapters = 0
        for _r, _ds, _fs in os.walk(base):
            _ds[:] = [name for name in _ds if not name.endswith(".repair_old")]
            images += sum(1 for f in _fs if f.endswith(_MANGA_IMG_EXTS))
    return images, chapters


def _manga_stats_compute_entry(source, comic_id):
    """重扫单部（copymanga/copymanga_web 备选源于扫描时一并统计，供源自动纠正）"""
    images, chapters = _manga_count_comic(source, comic_id)
    ent = {"images": images, "chapters": chapters, "ts": time.time()}
    if source in ("copymanga", "copymanga_web"):
        alt = "copymanga_web" if source == "copymanga" else "copymanga"
        ent["alt_source"] = alt
        ent["alt_images"] = _manga_count_comic(alt, comic_id)[0]
    return ent


def _manga_stats_note_change(source, comic_id, removed=False):
    """增量更新入口：下载完成（DownloadManager.library_change_hook）/覆盖修复/
    补页/删除后调用。只重扫这一部（锁外），绝不全库遍历。
    removed=True 时直接摘除条目。"""
    global _manga_lib_rev
    k = _manga_stats_skey(source, comic_id)
    if removed:
        with _manga_stats_lock:
            _manga_stats_pending.discard(k)
            if k in _manga_lib_stats:
                _manga_lib_stats.pop(k, None)
                _manga_lib_rev += 1
                _manga_stats_save_locked()
        return
    ent = _manga_stats_compute_entry(source, comic_id)   # 锁外扫描
    with _manga_stats_lock:
        old = _manga_lib_stats.get(k)
        ent["rev"] = int((old or {}).get("rev") or 0) + 1
        _manga_lib_stats[k] = ent
        _manga_lib_rev += 1
        _manga_stats_pending.discard(k)
        _manga_stats_save_locked()


def _manga_stats_get(source, comic_id):
    """请求路径读取快照（纯内存，无 IO）。返回 dict 副本或 None（快照未建）。"""
    with _manga_stats_lock:
        ent = _manga_lib_stats.get(_manga_stats_skey(source, comic_id))
        return dict(ent) if ent else None


def _manga_stats_request(source, comic_id):
    """快照缺失：排队由后台核对优先处理——请求路径不扫描、不阻塞。"""
    with _manga_stats_lock:
        _manga_stats_pending.add(_manga_stats_skey(source, comic_id))


def manga_library_revision():
    """全局书库 revision：客户端据此判断无变化时跳过重绘"""
    with _manga_stats_lock:
        return _manga_lib_rev


def _manga_stats_verify_once():
    """低频核对：以真实文件（os.walk 实扫）为准校正全部书库条目。
    返回校正条数。后台线程与测试均可直接调用。"""
    global _manga_lib_rev
    try:
        lib = json.load(open(MANGA_LIBRARY_FILE, encoding="utf-8"))
    except Exception:
        lib = []
    entries = [(x.get("source", ""), x.get("comic_id", "")) for x in lib
               if x.get("source") and x.get("comic_id")]
    with _manga_stats_lock:
        prio = set(_manga_stats_pending)
    entries.sort(key=lambda sc: 0 if _manga_stats_skey(*sc) in prio else 1)
    valid = {_manga_stats_skey(*sc) for sc in entries}
    changed = 0
    for source, comic_id in entries:
        k = _manga_stats_skey(source, comic_id)
        with _manga_stats_lock:
            old = _manga_lib_stats.get(k)
            _manga_stats_pending.discard(k)
        # 本周期内刚被增量更新（下载/修复钩子本身即真实扫描）→ 跳过
        if old is not None and k not in prio and \
                time.time() - (old.get("ts") or 0) < _MANGA_VERIFY_INTERVAL:
            continue
        ent = _manga_stats_compute_entry(source, comic_id)
        with _manga_stats_lock:
            cur = _manga_lib_stats.get(k)
            if cur is None or cur.get("images") != ent["images"] \
                    or cur.get("chapters") != ent["chapters"] \
                    or cur.get("alt_images") != ent.get("alt_images"):
                ent["rev"] = int((cur or {}).get("rev") or 0) + 1
                _manga_lib_stats[k] = ent
                _manga_lib_rev += 1
                changed += 1
            else:
                cur["ts"] = ent["ts"]
        time.sleep(_MANGA_VERIFY_STAGGER)
    # 书库记录已被外部删除的条目 → 快照同步摘除
    with _manga_stats_lock:
        stale = [k for k in _manga_lib_stats if k not in valid]
        for k in stale:
            _manga_lib_stats.pop(k, None)
        if stale:
            _manga_lib_rev += 1
        if changed or stale:
            _manga_stats_save_locked()
    return changed


def _manga_stats_verify_loop(stop_event=None):
    """后台低频核对（受管工作线程；由 server.runtime 按开关启动）。

    stop_event 不为空时：等待一律用 wait（可唤醒），收到停止信号立即返回，
    不必睡满一整轮才退出（设计 §5）。"""
    if stop_event is not None:
        if stop_event.wait(8):      # 启动后尽快建首轮快照
            return
        while not stop_event.is_set():
            try:
                _manga_stats_verify_once()
            except Exception as _e:
                print(f"[manga-stats] 核对异常: {type(_e).__name__}: {_e}",
                      flush=True)
            if stop_event.wait(_MANGA_VERIFY_INTERVAL):
                return
        return
    time.sleep(8)
    while True:
        try:
            _manga_stats_verify_once()
        except Exception as _e:
            print(f"[manga-stats] 核对异常: {type(_e).__name__}: {_e}",
                  flush=True)
        time.sleep(_MANGA_VERIFY_INTERVAL)


_manga_stats_load()

# B03: 下载完成 → 书库统计增量更新（engine 不反向依赖 server，钩子注入）
_dm_mod.library_change_hook = _manga_stats_note_change


# ── R49k 已下载章节完整性校验（检查更新附带）────────────────
_integrity_running = {}


_integrity_lock = threading.Lock()


def _integrity_path(source, comic_id):
    return os.path.join(MANGA_DIR, "_cache", source, comic_id,
                        "_integrity.json")


def _integrity_summary(source, comic_id):
    """读最近一次完整性校验结果概要(无结果返回 None)"""
    p = _integrity_path(source, comic_id)
    try:
        d = json.load(open(p, encoding="utf-8"))
        return {
            "status": d.get("status"),
            "scanned_at": d.get("scanned_at"),
            "total": d.get("total", 0),
            "checked": d.get("checked", 0),
            "incomplete": (d.get("incomplete") or [])[:100],
            "progress": d.get("progress"),
        }
    except Exception:
        return None


def _manga_integrity_scan(source, comic_id):
    """后台线程: 对已下载章节逐章校验完整性。
    基准 = copymanga_web.images(APP chapter2 优先 3-6s/章, 210 时降级网页慢);
    每章间隔 1.2s 防触发风控。结果写 _integrity.json。"""
    try:
        # 收集已下载章节 id(两个源目录合并; 本地文件所在目录)
        chids = []
        seen = set()
        for _s in ("copymanga", "copymanga_web"):
            base = os.path.join(MANGA_DOWNLOADS_DIR, _s, comic_id)
            if not os.path.isdir(base):
                continue
            for _n in os.listdir(base):
                _p = os.path.join(base, _n)
                if os.path.isdir(_p) and _n not in seen:
                    ok = any(f.lower().endswith(
                        (".webp", ".jpg", ".jpeg", ".png"))
                        for f in os.listdir(_p))
                    if ok:
                        seen.add(_n)
                        chids.append(_n)
        out = {"status": "running", "scanned_at": now_iso(),
               "scanned_at_ts": time.time(),
               "total": len(chids), "checked": 0, "incomplete": [],
               "chapters": [], "progress": ""}
        try:
            os.makedirs(os.path.dirname(_integrity_path(source, comic_id)),
                        exist_ok=True)
        except Exception:
            pass
        # 章名映射(两源 _info.json 取一份)
        _names = {}
        for _s in ("copymanga", "copymanga_web"):
            try:
                _li = json.load(open(os.path.join(
                    MANGA_DOWNLOADS_DIR, _s, comic_id, "_info.json"),
                    encoding="utf-8"))
                for c in (_li.get("chapters") or []):
                    _names[c.get("id", "")] = c.get("name", "")
            except Exception:
                pass
        try:
            from engine.manga.copymanga_web import CopyMangaWeb
            from engine.manga.manager import register_adapter, get_adapter
            register_adapter(CopyMangaWeb)
            ad = get_adapter("copymanga_web", state_dir=MANGA_STATE_DIR)
        except Exception:
            ad = None
        if ad is None:
            out["status"] = "error"
            out["progress"] = "网页适配器不可用"
        else:
            for i, chid in enumerate(chids):
                # 本地实际目录(downloads 优先)
                _dir = _manga_media_root(source, comic_id, chid)
                have = 0
                if os.path.isdir(_dir):
                    have = sum(1 for f in os.listdir(_dir)
                               if f.lower().endswith(
                                   (".webp", ".jpg", ".jpeg", ".png")))
                total = None
                err = ""
                try:
                    # 基准须为源站全量: 删除可能的过时提取缓存(网页降级可能
                    # 漏页, 如第03话缓存50 < 实际65), 强制 APP chapter2 重拉
                    _cp0 = os.path.join(MANGA_DIR, "_cache", "copymanga",
                                        comic_id, f"{chid}_imgs.json")
                    try:
                        if os.path.exists(_cp0):
                            os.remove(_cp0)
                    except Exception:
                        pass
                    imgs = ad.images(comic_id, chid)
                    total = len(imgs or [])
                except Exception as e:
                    err = f"{type(e).__name__}: {str(e)[:50]}"
                _row = {"id": chid, "name": (_names.get(chid) or chid)[:30],
                        "have": have, "total": total, "ok": None}
                if err:
                    _row["ok"] = False
                    _row["err"] = err
                elif total is not None and have < total:
                    _row["ok"] = False
                    out["incomplete"].append(_row)
                else:
                    _row["ok"] = True
                out["chapters"].append(_row)
                out["checked"] = i + 1
                out["progress"] = (f"校验 {i+1}/{len(chids)}: "
                                   f"{_row['name'][:16]} "
                                   f"{have}/{total or '?'}")
                if (i + 1) % 3 == 0:
                    try:
                        atomic_write(_integrity_path(source, comic_id), out)
                    except Exception:
                        pass
                time.sleep(1.2)   # 节流防 210
            out["status"] = "done"
            out["progress"] = (f"校验完成: {len(chids) - len(out['incomplete'])}"
                               f"/{len(chids)} 完整")
        try:
            atomic_write(_integrity_path(source, comic_id), out)
        except Exception:
            pass
    except Exception as e:
        print(f"[manga-integrity] 扫描异常: {type(e).__name__} {e}",
              flush=True)
    finally:
        with _integrity_lock:
            _integrity_running.pop((source, comic_id), None)


def _manga_check_one(source, comic_id, force_refresh=False):
    """单漫画检查更新（源站/缓存章节 vs 本地已下载）。source/comic_id 须已净化。
    force_refresh=True：跳过本地章节快照缓存，强制请求源站最新章节
    （供"一键检查书库更新"使用，真正发现连载新章）。
    返回 dict：ok/has_update/missing_count/latest/... 或 error"""
    # R57: copymanga/copymanga_web 检查统一走 copymanga_web(网页渲染)——
    # 与 APP 同库(cid 一致)但网页通道不受 APP 210 风控影响;
    # 检查不再请求 APP API, 从根源消除"检查触发 210 风控"
    ad = (_manga_adapter("copymanga_web")
          if source in ("copymanga", "copymanga_web")
          else _manga_adapter(source))
    if not ad:
        # R47 修复：此处此前返回 (jsonify, 404) 元组——调用方一律
        # jsonify(dict) 再包一层会 TypeError → 500。本函数契约是返回 dict。
        return {"ok": False, "error": "源不存在"}
    try:
        # 优先用本地缓存章节列表（详情 7 天缓存 / 下载 _info.json），
        # 避免每次检查都全量渲染源站（copymanga_web 13s+）。
        # **优先级：下载目录 _info.json > 详情缓存** —— 下载 worker 拉取的是
        # 同一来源，保证 check-update 的 missing id 与 download-new 时
        # worker 拉取的章节 id 一致（旧详情缓存可能导致 id 不匹配 → 误报）
        cur_chapters = None
        # force_refresh：跳过快照缓存，直接源站（批量一键检查用）
        _dl_info = (os.path.join(MANGA_DOWNLOADS_DIR, source, comic_id,
                                 "_info.json") if not force_refresh else "")
        if _dl_info and os.path.exists(_dl_info):
            try:
                _ci = json.load(open(_dl_info, encoding="utf-8"))
                if _ci.get("chapters"):
                    cur_chapters = [{"id": c["id"], "name": c["name"]}
                                    for c in _ci["chapters"]]
            except Exception:
                pass
        if cur_chapters is None:
            _info_full = (os.path.join(MANGA_CACHE_DIR, source, comic_id,
                                       "_info_full.json") if not force_refresh else "")
            if _info_full and os.path.exists(_info_full):
                try:
                    _c = json.load(open(_info_full, encoding="utf-8"))
                    _cd = _c.get("data") or {}
                    if _cd.get("chapters"):
                        cur_chapters = [{"id": c["id"], "name": c["name"]}
                                        for c in _cd["chapters"]]
                except Exception:
                    pass
        if cur_chapters is None:
            _info_p = (os.path.join(MANGA_CACHE_DIR, source, comic_id,
                                    "_info.json") if not force_refresh else "")
            if _info_p and os.path.exists(_info_p):
                try:
                    _ci = json.load(open(_info_p, encoding="utf-8"))
                    if _ci.get("chapters"):
                        cur_chapters = [{"id": c["id"], "name": c["name"]}
                                        for c in _ci["chapters"]]
                except Exception:
                    pass
        if cur_chapters is None and source in ("copymanga", "copymanga_web"):
            # R58: 检查统一走网页渲染(copy4000)——放弃 APP 端后检查不再
            # 请求 APP API(210 触发源消除); 渲染结果 10 分钟短缓存复用
            cur_chapters = _web_chlist_get("copymanga_web", comic_id)
        if cur_chapters is None:
            # R57: 统一网页渲染(copy4000 域, 稳定); copymanga 与 web 同库
            d = ad.comic_info(comic_id)
            cur_chapters = [{"id": c.id, "name": c.name} for c in d.chapters]
            if source in ("copymanga", "copymanga_web"):
                _web_chlist_set("copymanga_web", comic_id, cur_chapters)
        # 只排除**纯整卷合集**（"第X卷"整名，不可下载）；"01卷番外/02卷宣傳圖"
        # 等卷附加内容是实际可下载章节，必须保留——与下载 worker 过滤一致
        from engine.manga.download_manager import _is_volume_only as _vol_only
        cur_chapters = [c for c in cur_chapters
                        if not _vol_only(c.get("name") or "")]
        # **统一按话号排序**：_info.json 存的是源站原始乱序（如"第44話"在首位），
        # 不排序则 latest 会取到"第01卷/第08卷"等错误章节
        from engine.manga.download_manager import _sort_chapters as _sort_ch
        cur_chapters = _sort_ch(cur_chapters)
        # latest 取**正片最新章节**（第X话升序最后一项）——附加章节
        # （特别篇/番外/贺图/动画化决定等）是附带内容，不算"最新章节"
        _ep_re = __import__("re").compile(r"第\s*\d+(?:\.\d+)?\s*(?:话|話|回|章|集|话数|話数)")
        _latest = next((c["name"] for c in reversed(cur_chapters)
                        if _ep_re.search(c.get("name") or "")), "")
        latest = _latest or (cur_chapters[-1]["name"] if cur_chapters else "")
        # 本地已下载章节（含图片文件的目录）
        downloaded = set(_scan_downloaded_chapters(source, comic_id))
        if source in ("copymanga", "copymanga_web"):
            for _alt in ("copymanga_web", "copymanga"):
                if _alt != source:
                    downloaded |= set(_scan_downloaded_chapters(_alt, comic_id))
        # 缺失 = 源站全部 - 已下载
        missing = [c for c in cur_chapters if c["id"] not in downloaded]
        # 已下载过（说明下载过本漫画）但当前源无章节 → 无记录
        has_dl = bool(downloaded)
        # **下载中/排队中的漫画不算"有更新"**：缺失章节是任务还没下完，
        # 不是源站更新（第二轮报告：未下完书误亮徽标）
        _dl_st = _manga_dl.status(_manga_dl_key(source, comic_id))
        _dl_active = _dl_st.get("status") in ("running", "queued", "paused")
        # R58: 检查不再附带触发完整性扫描——其逐章 APP chapter2 重拉(3-6s/
        # 章×全部已下载章)是 bulk 检查拖慢与 210 风控的元凶; 完整性校验
        # 只在下载任务收尾/修复入口执行。此处仅带上已有概要(只读, 不扫描)
        _integrity = None
        try:
            _integrity = _integrity_summary(source, comic_id)
        except Exception:
            _integrity = None
        return {
            "ok": True,
            "has_update": bool(missing) and has_dl and not _dl_active,
            "downloading": _dl_active,
            "old_total": len(downloaded),
            "new_total": len(cur_chapters),
            "missing_count": len(missing),
            "missing": missing[:200],
            "latest": latest,
            "integrity": _integrity,
        }
    except Exception as e:
        # 客户端可见 error 脱敏（异常原文含本地路径/内网地址，只进服务端日志）。
        # 风控类异常保留稳定识别词——_manga_check_worker 依此做源级短路，
        # 不能用不含"风控/冷却"字样的文案，否则 210 短路失效
        _es = str(e)
        print(f"[manga-check] {source}/{comic_id}: "
              f"{type(e).__name__}: {e}", flush=True)
        if any(k in _es for k in ("210", "冷却", "风控")):
            return {"ok": False, "error": "源站风控冷却中，请稍后重试"}
        return {"ok": False, "error": "检查更新失败（详细错误见服务端日志）"}


_manga_check_state = {"running": False, "total": 0, "results": [],
                      "started_at": 0.0, "finished_at": 0.0, "error": ""}


_manga_check_lock = threading.Lock()


# R55: copymanga_web 渲染章节列表短缓存(10min)——网页渲染 ~10-30s/部,
# 一键检查不应每次都全量渲染; 缓存期内复用(源站章节列表分钟级不变)
_web_chlist_cache = {}


_web_chlist_lock = threading.Lock()


_WEB_CHLIST_TTL = 600


def _web_chlist_get(source, comic_id):
    with _web_chlist_lock:
        _v = _web_chlist_cache.get((source, comic_id))
        if _v and time.time() - _v[0] < _WEB_CHLIST_TTL:
            return _v[1]
    return None


def _web_chlist_set(source, comic_id, chapters):
    with _web_chlist_lock:
        _web_chlist_cache[(source, comic_id)] = (time.time(), chapters)
        # 超限淘汰最旧到上限以内（旧版只清过期条目，TTL 内条数可无限涨）
        if len(_web_chlist_cache) > 200:
            for _k in sorted(_web_chlist_cache,
                             key=lambda k: _web_chlist_cache[k][0]
                             )[:len(_web_chlist_cache) - 200]:
                _web_chlist_cache.pop(_k, None)


def _manga_check_worker(items):
    import concurrent.futures as _cf

    _skip = {}   # R54: 源级风控短路 {src: msg}——210 后该源剩余漫画不再请求
    def _one(it):
        src, cid, title = it
        if src in _skip:
            return {"comic_id": cid, "source": src, "title": title,
                    "has_update": False, "missing_count": 0, "latest": "",
                    "missing": [], "error": _skip[src], "skipped": True}
        for _attempt in range(2):
            # R77: worker 级重试一次——渲染被并发全杀误伤(R72d)时
            # _manga_check_one 返回脱敏 error, 源站本身正常; 重试可自愈
            try:
                r = _manga_check_one(src, cid, force_refresh=True)
                err = r.get("error") or ""
                if any(k in err for k in ("210", "冷却", "风控")):
                    _skip[src] = f"{err[:100]}（本次一键检查该源剩余漫画已跳过）"
                    return {"comic_id": cid, "source": src, "title": title,
                            "has_update": False, "missing_count": 0,
                            "latest": "", "missing": [], "error": err,
                            "skipped": True}
                if not err:
                    return {"comic_id": cid, "source": src, "title": title,
                            "has_update": bool(r.get("has_update")),
                            "missing_count": r.get("missing_count", 0),
                            "latest": r.get("latest", ""),
                            "downloading": bool(r.get("downloading")),
                            "missing": r.get("missing") or [],
                            "error": ""}
                # 失败: 非末次尝试则短暂等待后重试(渲染误伤自愈窗口)
                if _attempt == 0:
                    print(f"[manga-check] {src}/{cid} 首次失败({err[:60]})"
                          f"，重试一次", flush=True)
                    time.sleep(3)
                    continue
                return {"comic_id": cid, "source": src, "title": title,
                        "has_update": False, "missing_count": 0,
                        "latest": "", "missing": [],
                        "error": err or "检查失败（详细错误见服务端日志）"}
            except Exception as e:
                print(f"[manga-check] {src}/{cid} worker 兜底异常: "
                      f"{type(e).__name__}: {e}", flush=True)
                if _attempt == 0:
                    time.sleep(3)
                    continue
                return {"comic_id": cid, "source": src, "title": title,
                        "has_update": False, "missing_count": 0,
                        "latest": "", "error": "检查失败（详细错误见服务端日志）"}
        return {"comic_id": cid, "source": src, "title": title,
                "has_update": False, "missing_count": 0,
                "latest": "", "error": "检查失败（详细错误见服务端日志）"}

    # P3-3: executor 构建 / submit / wait / shutdown 全部纳入最外层 try——
    # 此前 ex/submit 在 try 之外，构建失败或 submit 抛错会直接跳过 finally，
    # running 永久 True，一键检查从此恒定 409。
    ex = None
    _futs = []
    try:
        _dl = time.time() + 360   # R56: 整体 deadline 用 wait 周期检查(见书源 worker 注)
        try:
            ex = _cf.ThreadPoolExecutor(max_workers=3)
            _futs = [ex.submit(_one, it) for it in items]
            while _futs and time.time() < _dl:
                _done, _futs = _cf.wait(_futs, timeout=5,
                                        return_when=_cf.FIRST_COMPLETED)
                for f in _done:
                    try:
                        r = f.result()
                    except Exception:
                        continue
                    with _manga_check_lock:
                        _manga_check_state["results"].append(r)
        finally:
            for f in _futs:
                f.cancel()
            if ex is not None:
                ex.shutdown(wait=False)
        # R39: 检查结果缓存 10 分钟——"全部补充下载"直接复用缺失章节
        try:
            with _manga_check_lock:
                results = list(_manga_check_state["results"])
            _now = time.time()
            _fresh = {}
            for _r in results:
                if _r.get("has_update") and _r.get("missing_count"):
                    # P1-4: key 含 source，见模块级 _mcache 注释
                    _fresh[(_r["source"], _r["comic_id"])] = {
                        "ts": _now, "source": _r["source"], "title": _r["title"],
                        "missing": [m.get("id") for m in
                                    (_r.get("missing") or [])]}
            # _mcache 增删与请求线程读取同锁（check-updates-download）
            with _manga_check_lock:
                _mcache.update(_fresh)
                for _k in [k for k, v in _mcache.items()
                           if _now - v["ts"] > 600]:
                    _mcache.pop(_k, None)
        except Exception:
            pass
    finally:
        with _manga_check_lock:
            _manga_check_state["running"] = False
            _manga_check_state["finished_at"] = time.time()


# ── 参数校验工具（R7 集中化）──
def _safe_int_arg(name, default=1, lo=1, hi=10 ** 6):
    """从 query 取整数参数：非法/越界回退默认"""
    v = request.args.get(name)
    if v is None:
        return default
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _safe_str_arg(name, default="", maxlen=200):
    """从 query 取字符串参数：去空白 + 限长"""
    v = request.args.get(name)
    if v is None:
        return default
    return str(v).strip()[:maxlen]


# ── 路径参数安全校验（防路径穿越/目录逃逸）──
_SAFE_SEG_RE = re.compile(r"^[^/\\]{1,128}$")


def _safe_seg(name, what="参数"):
    """校验 URL 路径段：允许任意非斜杠/反斜杠字符（含中文 book_key），
    禁止 ..（路径穿越）与空值。不合法 → 404。"""
    if not name or not _SAFE_SEG_RE.match(name) or ".." in name:
        abort(404, f"{what}非法")
    return name


def _norm_comic_id(source, comic_id):
    """漫画 ID 归一化（当前仅 jm 需要）。

    禁漫码 JM1234567 与 1234567 是同一部漫画，前缀只是书写差异。
    不归一化时同一部漫画会按两种 ID 各自建缓存目录、各写一条书库/历史，
    表现为"下载过却显示未下载""收藏出现重复项"。
    """
    if source == "jm":
        from engine.manga.jm import Jm
        return Jm.normalize_id(comic_id)
    return comic_id


def _safe_comic_id(source, comic_id):
    """校验并归一化漫画 ID（漫画路由统一入口）"""
    return _norm_comic_id(source, _safe_seg(comic_id, "漫画"))


