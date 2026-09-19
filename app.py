#!/usr/bin/env python3
"""
webnovel_rules — 书源驱动的网页爬虫系统（组合根）
===============================================
基于 legado（阅读 App）书源规则：导入书源 → 校验 → 搜索 → 爬取 → 阅读。
不硬编码任何站点，全部解析由书源 JSON 规则驱动。

R47 结构：本文件只保留 Flask app 装配、鉴权/同源防护、页面路由、
后台守护线程与 main()。领域代码在 server/ 包：
  server/state.py     共享状态与领域助手
  server/novel_api.py 小说域 API（蓝图 novel）
  server/manga_api.py 漫画域 API（蓝图 manga）

启动:
  venv/bin/python app.py            # http://127.0.0.1:8766
"""
import os
import sys
import json
import time
import shutil
import threading
import hashlib
import uuid

from flask import Flask, jsonify, render_template, request, g, redirect

HUB_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HUB_DIR)

# 导入期副作用闸门（设计 §5）：移动端用 WR_DEFER_INIT=1 声明"由宿主显式
# initialize()"，此时导入模块只装配对象、不清理、不恢复、不起线程、不联网。
_DEFER_INIT = os.environ.get("WR_DEFER_INIT") == "1"


def _purge_pycache():
    """卷时间戳异常会导致 Python 误用陈旧 pyc：启动时清理一次（本地、幂等）。"""
    for _d in (HUB_DIR, os.path.dirname(HUB_DIR)):
        _pc = os.path.join(_d, "__pycache__")
        if os.path.isdir(_pc):
            shutil.rmtree(_pc, ignore_errors=True)
    return True


if not _DEFER_INIT:
    _purge_pycache()

from engine.source_mgr import load_all
from engine.crawler import SourceCrawler
from engine.urlsec import (url_is_public as _urlsec_url_is_public,
                           safe_target_url as _urlsec_safe_target_url)
from engine.app_utils import now_iso, _read_json, _write_json
from engine.app_utils import _norm, _group_key  # noqa: F401 测试兼容 re-export
from engine.app_utils import _parse_time, _parse_words  # noqa: F401 同上
from engine.config import (ST_RUNNING, ST_ERROR,
                           MANGA_LIBRARY_FILE, MANGA_CACHE_DIR,
                           IS_MOBILE as _IS_MOBILE, PROFILE as _PROFILE)


def _is_mobile():
    """当前运行 profile 是否为 mobile（显式 WR_PROFILE=mobile，不做平台猜测）"""
    return _IS_MOBILE

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True  # 模板改动即时生效（开发期无需重启）

# ── 领域模块装配（R47）：DATA_DIR/BOOKS_DIR/TASKS_DIR 单一来源在 state ──
from server.state import (DATA_DIR, BOOKS_DIR, TASKS_DIR,          # noqa: F401
                          _tasks, _crawlers, _lock, _toc_lock, _toc_cache,
                          _search_cache, _save_task, _log, _scan_books,
                          _run_check_update, _err_response, _safe_seg,
                          _load_manga_adapters, _manga_dl, _clean_caches,
                          _cache_footprint, _manga_dl_key, _norm_comic_id,
                          _check_jobs, _dir_size)  # 部分为测试兼容 re-export
from server import novel_api as _novel_api
from server import manga_api as _manga_api
from server import capabilities as _capabilities
from server import storage_api as _storage_api
from server import net_api as _net_api
from server.manga_api import api_manga_proxy  # noqa: F401 兼容测试直接调用
app.register_blueprint(_novel_api.bp)
app.register_blueprint(_manga_api.bp)
app.register_blueprint(_storage_api.bp)
app.register_blueprint(_net_api.bp)


# 覆盖修复事务恢复（2026-09-10）：崩溃在"旧目录改名备份"与"暂存改名正式"
# 之间时，备份是唯一可读版本——启动即按持久事务记录收敛（回滚或完成切换）。
# 放在**模块级**而非仅 main()：WSGI/导入式启动（gunicorn 等）只加载模块、
# 不执行 main()，此前这类部署下恢复从不运行；同时置于下方后台线程之前，
# 避免恢复与后台下载并发。recover 内部持全局恢复锁，与运行期按章修复互斥。
_boot_repair_recovered = False
_boot_repair_lock = threading.Lock()


def _boot_recover_repair_transactions():
    """覆盖修复事务恢复（启动恢复）。

    幂等仅在**成功**后置位：恢复抛错时保持未置位，后续调用（main() 或
    下次触发）会重试，绝不因一次失败而"永不恢复"。并发调用由锁串行化，
    避免同进程重复扫描。"""
    global _boot_repair_recovered
    if _boot_repair_recovered:
        return
    with _boot_repair_lock:
        if _boot_repair_recovered:
            return
        try:
            from server.manga_api import recover_repair_transactions
            recover_repair_transactions()
        except Exception as e:  # 启动路径尽力而为，恢复失败不得阻断服务
            print(f"[startup] 覆盖修复事务恢复失败（保留待重试）: "
                  f"{type(e).__name__}: {e}", flush=True)
            return
        _boot_repair_recovered = True


if not _DEFER_INIT:
    _boot_recover_repair_transactions()


def _load_persisted_state():
    """显式装载持久状态（任务记录、toc/搜索缓存、漫画统计快照）。

    技术指南 §8.1：import 阶段不得有副作用——此前这三件事写在 server/state.py
    的模块级，import 一次就会读盘、甚至**写盘**（把 running 任务标成中断）。
    现在统一由运行时初始化钩子调用（桌面与移动同一条链）。"""
    from server.state import load_persisted_state
    load_persisted_state()
    return {"ok": True}


def _recover_tasks():
    """两类任务恢复（设计 §5）：小说中断任务归一化 + 漫画任务装载。

    只做本地状态收敛（running → stopped，可点击恢复），**不自动续跑**：
    是否继续由用户或既定策略决定。幂等，可重复调用。"""
    from server.state import _load_disk_tasks
    _load_disk_tasks()
    try:
        _manga_dl.load()
    except Exception as e:
        print(f"[startup] 漫画任务装载失败: {type(e).__name__}: {e}", flush=True)
    return {"novel_tasks": len(_tasks),
            "manga_tasks": len(_manga_dl.all_tasks())}


# ══════════════════════════════════════════════
#  自动缓存清理（无动作 12h / 容量超限触发；已下载内容永不删除）
# ══════════════════════════════════════════════
AUTO_CLEAN_IDLE_SECS = 12 * 3600     # 无动作 12 小时 → 自动清理缓存


AUTO_CLEAN_SIZE_LIMIT = 1 << 30      # data/ 总占用 ≥ 1GB → 触发自动清理


AUTO_CLEAN_SIZE_TARGET = 768 << 20   # 容量清理目标：降至 768MB 以下


AUTO_CLEAN_CHECK_SECS = 300          # 检查节流：每 5 分钟最多检查一次


LAST_ACTIVITY_FILE = os.path.join(DATA_DIR, ".last_activity")


_last_activity_ts = 0.0          # 最近一次用户/系统活动时间戳


# 活动时间戳落盘节流（P1-2 审计）：此前 after_request 每请求 open+write
# .last_activity 一次（读图高峰期每请求一次磁盘写）。现在活动时间戳只在
# 内存即时更新，落盘 60s 节流——崩溃最多丢 60s 活动记录，不影响 12h
# 空闲判定语义（判定读本请求到来前的内存值，见 _after_request 注释）。
ACTIVITY_PERSIST_SECS = 60


_last_activity_persist_ts = 0.0  # 上次活动时间戳落盘时间


_last_auto_check_ts = 0.0        # 上次自动清理检查时间


_auto_clean_lock = threading.Lock()


def _load_last_activity():
    """启动时从磁盘恢复最近活动时间（跨重启保持 12h 无动作语义）"""
    global _last_activity_ts
    try:
        with open(LAST_ACTIVITY_FILE, encoding="utf-8") as f:
            _last_activity_ts = float(f.read().strip() or 0)
    except Exception:
        _last_activity_ts = time.time()


def _touch_activity():
    global _last_activity_ts
    _last_activity_ts = time.time()


def _write_activity_file():
    """实际落盘（独立函数便于测试计数；失败静默——只是活动时间记录）"""
    try:
        with open(LAST_ACTIVITY_FILE, "w", encoding="utf-8") as f:
            f.write(f"{_last_activity_ts:.0f}")
    except Exception:
        pass


def _persist_activity(force=False):
    """60s 节流落盘；force=True 立即落盘（自动清理重置计时等关键节点）"""
    global _last_activity_persist_ts
    now = time.time()
    if not force and now - _last_activity_persist_ts < ACTIVITY_PERSIST_SECS:
        return
    _last_activity_persist_ts = now
    _write_activity_file()


_load_last_activity()   # 恢复最近活动时间（自动清理的 12h 无动作判定）


# 后台线程总开关：WR_DISABLE_BACKGROUND=1 时不启动任何会产生外部副作用的
# 模块级后台线程——搜索预热（向本服务 HTTP 自调，pytest 时会误打生产 8766）、
# 书源健康校验与自动追更（真实访问源站）。测试在 conftest.py 中置位；
# 任务 watchdog 是纯本地状态修正，不受此开关影响，始终运行。
# 兼容保留：真正的后台启停判定已收敛到 server.runtime.worker_enabled()
# （支持 WR_BG_<NAME> 单项开关与 mobile profile 默认值）
_BACKGROUND_DISABLED = os.environ.get("WR_DISABLE_BACKGROUND") == "1"


def _prewarm_search_cache(stop_event=None):
    """后台预热：用热门关键词搜索，填充搜索缓存（用户搜索时秒回）。

    受管工作线程：等待一律用 stop_event.wait（可唤醒），停止时立即返回。"""
    import random as _rand
    if stop_event is not None and stop_event.wait(4):
        return          # 等 app.run 起来（原实现模块加载即启动，前几次必失败）
    hot_words = ["剑来", "斗破苍穹", "赘婿", "雪中悍刀行", "凡人修仙传",
                 "诡秘之主", "庆余年", "完美世界", "遮天", "一念永恒",
                 "仙逆", "斗罗大陆", "大奉打更人", "深空彼岸", "夜的命名术"]
    _rand.shuffle(hot_words)
    for _w in hot_words[:6]:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            with _toc_lock:
                if (_w, "", "default", "all") in _search_cache:
                    continue
            # 直接调用搜索核心逻辑（走完整流程以填充缓存）
            import urllib.parse
            # 用内部函数避免 HTTP 开销
            _q = urllib.parse.quote(_w)
            import requests as _req
            try:
                _r = _req.get(f"http://127.0.0.1:{os.environ.get('PORT', 8766)}/api/search?q={_q}",
                              timeout=40)
                if _r.status_code == 200:
                    print(f"[prewarm] {_w} 完成", flush=True)
            except Exception:
                pass
        except Exception:
            pass
    print("[prewarm] 预热完成", flush=True)


def _worker_prewarm(stop_event):
    """工作线程入口：热门搜索预热（打本机 API/源站，mobile profile 默认关闭）"""
    _prewarm_search_cache(stop_event)


def _worker_manga_stats_verify(stop_event):
    """B03: 书库统计后台低频核对——以真实文件校正持久化快照

    （纯本地目录扫描，不打源站；两平台默认都开）"""
    from server.state import _manga_stats_verify_loop
    _manga_stats_verify_loop(stop_event=stop_event)


# ── 自动追更：每 30 分钟对书架书籍后台检查更新，有更新时书库显示徽标 ──
AUTO_FOLLOW_INTERVAL = 30 * 60      # 轮次间隔（秒）


def _auto_follow(stop_event=None):
    """受管循环：等待一律用 stop_event.wait（可唤醒），停止时立即返回。"""
    import time as _t
    if stop_event is not None:
        if stop_event.wait(25):      # 等服务与健康校验就绪
            return
        while not stop_event.is_set():
            _auto_follow_round(stop_event)
            if stop_event.wait(AUTO_FOLLOW_INTERVAL):
                return
        return
    _t.sleep(25)
    while True:
        _auto_follow_round(None)
        _t.sleep(AUTO_FOLLOW_INTERVAL)


def _auto_follow_round(stop_event=None):
    import time as _t
    if True:
        try:
            for bk in _scan_books():
                key = bk["key"]
                # 未下完且无失败章节的书不自动追更：缺失章节是"还没下完"，
                # 检查只会误报"有更新"且白打源站；有失败章节的书仍需检查（自动重试）
                if bk["percent"] < 100 and not bk["failed"]:
                    continue
                # 跳过正在检查的
                with _lock:
                    if _check_jobs.get(key, {}).get("status") == "checking":
                        continue
                # 已有更新标记且距上次检查 < 2h → 跳过（避免反复打源）
                with _lock:
                    _j = _check_jobs.get(key)
                    if _j and _j.get("missing_count") and _j.get("status") == "done":
                        from datetime import datetime as _dt
                        try:
                            _last = _dt.strptime(_j.get("ts", ""), "%Y-%m-%d %H:%M:%S")
                            if (_t.time() - _last.timestamp()) < 7200:
                                continue
                        except Exception:
                            pass
                try:
                    _run_check_update(key)
                except Exception:
                    pass
                if stop_event is not None:
                    if stop_event.wait(3):   # 书间间隔，避免打爆源站
                        return
                else:
                    _t.sleep(3)
        except Exception:
            pass


def _worker_auto_follow(stop_event):
    """自动追更（会真实访问源站；mobile profile 默认关闭，保留显式触发入口）"""
    _auto_follow(stop_event)


# ── 任务 watchdog（R29，技术评审4.4补充）：running 任务长时间无心跳
# （进程崩溃/线程异常死亡）自动转 failed，防任务永久卡在运行中 ──
WATCHDOG_INTERVAL = 600            # 看护轮次间隔（秒）


def _task_watchdog(stop_event=None):
    """任务心跳看护（设计 §5）：running 任务长时间无心跳→failed。

    与旧实现的区别：① 停止时可立即唤醒退出；② **区分系统暂停**——若本轮与上轮
    间隔远超预期（Doze/冻结/机器休眠），说明是我们被挂起而非任务卡死，
    这一轮只刷新基准、不做失败判定。"""
    import time as _t
    if stop_event is not None:
        if stop_event.wait(60):      # 等服务就绪
            return
    else:
        _t.sleep(60)
    while True:
        suspended = _RUNTIME.begin_round("task-watchdog", WATCHDOG_INTERVAL)
        if suspended:
            print("[watchdog] 检测到进程被挂起（轮次间隔异常），本轮跳过失败判定",
                  flush=True)
        else:
            _watchdog_round()
        if stop_event is not None:
            if stop_event.wait(WATCHDOG_INTERVAL):
                return
        else:
            _t.sleep(WATCHDOG_INTERVAL)


def _watchdog_round():
    if True:
        try:
            _now = time.time()
            from datetime import datetime as _dt
            with _lock:
                for _tid, _tt in list(_tasks.items()):
                    if _tt.get("status") != ST_RUNNING:
                        continue
                    _up = _tt.get("updated_at") or _tt.get("created_at") or ""
                    try:
                        _ts = _dt.strptime(_up, "%Y-%m-%d %H:%M:%S").timestamp()
                    except Exception:
                        _ts = _now
                    if _now - _ts > 1800:  # 30 分钟无心跳
                        _tt["status"] = ST_ERROR
                        _tt["error"] = "任务无响应（可能进程崩溃），已自动标记失败"
                        _tt["finished_at"] = now_iso()
                        # P2-6: pop 前先置停止标记——否则旧爬虫线程仍活着，
                        # 之后 _launch_task 可再起线程，造成同书双线程并发写
                        # 同一 book_dir。stop_callback 每章检查一次该标记。
                        _ctl = _crawlers.get(_tid)
                        if isinstance(_ctl, dict) and _ctl.get("stop") is not None:
                            _ctl["stop"]["v"] = True
                        _crawlers.pop(_tid, None)
                        _save_task(_tt)
                        print(f"[watchdog] 任务 {_tid} 无心跳 30 分钟，"
                              f"自动标记失败", flush=True)
        except Exception:
            pass


def _worker_task_watchdog(stop_event):
    """任务看护（两平台默认都开）"""
    _task_watchdog(stop_event)


# ── 启动时源健康校验：失效源自动禁用（防死源拖慢搜索；源生命周期保障）──
_HEALTH_FAILS_FILE = os.path.join(DATA_DIR, "source_health.json")


def _source_health_check(stop_event=None):
    """后台校验各启用源搜索可达性：连续 N 次失败 → 自动禁用（enabled=false），
    避免每次搜索都等死源超时（实测恢复的旧源约一半已失效）。

    R47 修复：失败计数此前是函数局部变量且每次启动只跑一轮，fails[uid]
    最大为 1，`>= 2 自动禁用` 永远不会触发——功能名存实亡。现失败计数
    持久化到 source_health.json，跨启动累计，"连续失败"语义真实生效；
    校验成功即清零。成功源会从禁用恢复由用户手动操作（不自动恢复，
    避免误判抖动）。"""
    import time as _t
    if stop_event is not None:
        if stop_event.wait(10):      # 等服务就绪（可唤醒）
            return
    else:
        _t.sleep(10)
    try:
        from engine.source_mgr import load_enabled, set_enabled
        from concurrent.futures import ThreadPoolExecutor as _TPE2
        fails = _read_json(_HEALTH_FAILS_FILE, {}) or {}
        srcs = [s for s in load_enabled() if s.get("searchUrl")]
        changed = False

        def _check(s):
            uid = s.get("uid", "")
            name = s.get("bookSourceName", "")[:12]
            t0 = _t.time()
            try:
                c = SourceCrawler(s)
                books = c.search("斗破")
                return uid, name, bool(books), _t.time() - t0
            except Exception:
                return uid, name, False, _t.time() - t0
        _results = []
        with _TPE2(max_workers=2 if _is_mobile() else 6) as _ex:
            _results = list(_ex.map(_check, srcs))
        _n_ok = sum(1 for _r in _results if _r[2])
        _n_all = len(_results)
        # 断网/系统暂停保护（设计 §5）：整轮几乎全失败时更可能是本机没网或刚
        # 被系统挂起，而不是"这批源全死了"——此时**不累加失败计数**，
        # 避免把一次网络中断固化成"源永久失效"（连续失败 2 次就自动禁用）。
        if _n_all >= 3 and _n_ok == 0:
            print(f"[health] 本轮 {_n_all} 个源全部失败，判定为断网/挂起，"
                  f"不计入失败计数", flush=True)
            return
        for uid, name, ok, el in _results:
                if ok:
                    if fails.pop(uid, None) is not None:
                        changed = True
                    print(f"[health] ✓ {name} 搜索OK ({el:.1f}s)", flush=True)
                else:
                    fails[uid] = fails.get(uid, 0) + 1
                    changed = True
                    print(f"[health] ✗ {name} 搜索失败 ({el:.1f}s) 累计{fails[uid]}", flush=True)
                    if fails[uid] >= 2:
                        try:
                            set_enabled(uid, False)
                            fails.pop(uid, None)   # 已禁用，清零防重复处理
                            print(f"[health] ⛔ {name} 连续失败，已自动禁用", flush=True)
                        except Exception:
                            pass
        # 清理已不存在/已禁用源的陈旧计数
        live = {s.get("uid", "") for s in load_all()}
        for _k in [k for k in fails if k not in live]:
            fails.pop(_k, None)
            changed = True
        if changed:
            _write_json(_HEALTH_FAILS_FILE, fails)
    except Exception as e:
        print(f"[health] 源健康校验异常: {e}", flush=True)


def _worker_source_health(stop_event):
    """启动即全源健康校验（会真实请求源站；mobile profile 默认关闭）"""
    _source_health_check(stop_event)


# ═══════ R9 请求日志 + request-id 追踪（Kimi 产出）═══════
# ══════════════════════════════════════════════
#  访问鉴权（可选）：data/auth.json 存在且含 "password" 时启用
#  所有页面/API 需登录（cookie token），防止局域网裸奔泄露书库/历史
# ══════════════════════════════════════════════
AUTH_FILE = os.path.join(DATA_DIR, "auth.json")


AUTH_PASSWORD = os.environ.get("WR_AUTH_PASSWORD", "").strip()


_auth_enabled = False


_auth_token = ""


_login_attempts = {}   # ip -> {fails, locked_until, ts}（登录爆破防护）


LOGIN_ATTEMPT_WINDOW = 1800    # 失败记录保留窗口（= 最大锁定时长 30 分钟）


LOGIN_ATTEMPTS_MAX = 1000      # 表总量上限：超出时按最后失败时间丢弃最旧


def _prune_login_attempts(now):
    """清理 _login_attempts，防按 IP 只增不清导致内存无限增长。
    ① 未在锁定中且最后失败距今超出窗口 → 删除；
    ② 总量超 LOGIN_ATTEMPTS_MAX → 从最旧开始丢弃。调用方须已持有 _lock。"""
    for ip in [k for k, r in _login_attempts.items()
               if r.get("locked_until", 0) <= now
               and now - r.get("ts", 0) > LOGIN_ATTEMPT_WINDOW]:
        _login_attempts.pop(ip, None)
    overflow = len(_login_attempts) - LOGIN_ATTEMPTS_MAX
    if overflow > 0:
        for ip, _r in sorted(_login_attempts.items(),
                             key=lambda kv: kv[1].get("ts", 0))[:overflow]:
            _login_attempts.pop(ip, None)


def _auth_load():
    global _auth_enabled, _auth_token
    _auth_enabled = False
    _auth_token = ""
    pwd = AUTH_PASSWORD
    try:
        if os.path.exists(AUTH_FILE):
            _d = json.load(open(AUTH_FILE, encoding="utf-8"))
            pwd = pwd or str(_d.get("password", "")).strip()
    except Exception:
        pass
    if pwd:
        _auth_enabled = True
        _auth_token = hashlib.sha256(("wr" + pwd).encode()).hexdigest()


def _auth_ok():
    if not _auth_enabled:
        return True
    return request.cookies.get("wr_auth") == _auth_token


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if not _auth_enabled:
        return redirect("/")
    if request.method == "POST":
        # 登录速率限制：按 IP 记失败次数，5 次失败锁定 60s，
        # 之后每累计 5 次失败锁定时间翻倍（上限 30 分钟），防爆破
        _ip = request.remote_addr or "?"
        with _lock:
            _prune_login_attempts(time.time())   # P3-4: 记录前清理过期/超限条目
            _rec = _login_attempts.get(_ip) or {"fails": 0, "locked_until": 0,
                                                "ts": 0}
        if _rec["locked_until"] > time.time():
            _wait = int(_rec["locked_until"] - time.time()) + 1
            return render_template("login.html",
                                   error=f"尝试次数过多，请 {_wait} 秒后重试"), 429
        pwd = (request.form.get("password") or "").strip()
        if pwd and hashlib.sha256(("wr" + pwd).encode()).hexdigest() == _auth_token:
            with _lock:
                _login_attempts.pop(_ip, None)
            resp = redirect("/")
            resp.set_cookie("wr_auth", _auth_token, max_age=30 * 86400,
                            httponly=True, samesite="Lax")
            return resp
        with _lock:
            _rec = _login_attempts.get(_ip) or {"fails": 0, "locked_until": 0,
                                                "ts": 0}
            _rec["fails"] += 1
            _rec["ts"] = time.time()
            if _rec["fails"] >= 5 and _rec["fails"] % 5 == 0:
                _rec["locked_until"] = time.time() + min(
                    1800, 60 * (2 ** (_rec["fails"] // 5 - 1)))
            _login_attempts[_ip] = _rec
        return render_template("login.html", error="密码错误"), 401
    return render_template("login.html", error="")


# ══════════════════════════════════════════════
#  R35 同源防护：CSRF + DNS rebinding
#  本服务无 CORS 配置且大量写操作走 POST（简单请求无预检），
#  仅绑定 127.0.0.1 并不能防住恶意网页跨站提交表单打本机服务。
#  因此对所有状态变更方法强制校验 Origin/Referer 与 Host 同源。
# ══════════════════════════════════════════════
_UNSAFE_METHODS = frozenset(("POST", "PUT", "PATCH", "DELETE"))


# 额外放行的 Host（内网穿透域名等），逗号分隔：WR_ALLOWED_HOSTS=a.example.com,b.example.com
ALLOWED_HOSTS = frozenset(
    h.strip().lower() for h in os.environ.get("WR_ALLOWED_HOSTS", "").split(",")
    if h.strip()
)


def _host_only(netloc):
    """从 Host/Origin 的 netloc 中取出主机名（去端口，剥 IPv6 方括号）"""
    netloc = (netloc or "").strip().lower()
    if netloc.startswith("["):                      # [::1]:8766
        return netloc[1:netloc.index("]")] if "]" in netloc else netloc[1:]
    return netloc.rsplit(":", 1)[0] if ":" in netloc else netloc


def _host_allowed(netloc):
    """Host 白名单：本机 / 私网 IP / .local(mDNS) / 显式配置的穿透域名。

    公网域名默认拒绝，防 DNS rebinding（把域名解析到 127.0.0.1 后
    用受害者浏览器读写本机服务）。穿透场景请配置 WR_ALLOWED_HOSTS。
    """
    host = _host_only(netloc)
    if not host:
        return False
    if host in ALLOWED_HOSTS:
        return True
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost"):
        return True
    if host.endswith(".local"):                     # mDNS，仅局域网可解析
        return True
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False                                # 未配置的域名一律拒绝
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


def _same_origin_ok():
    """状态变更请求的同源校验。

    Origin 缺失时回落 Referer；两者皆无 → 判定为非浏览器客户端（curl 等 /
    curl / 脚本）放行——浏览器对本文涉及的所有跨站写请求都会带上其中之一。
    """
    origin = request.headers.get("Origin")
    if origin:
        if origin == "null":                        # sandbox iframe / file://
            return False
        from urllib.parse import urlparse as _up
        return _up(origin).netloc.lower() == (request.host or "").lower()
    referer = request.headers.get("Referer")
    if referer:
        from urllib.parse import urlparse as _up
        return _up(referer).netloc.lower() == (request.host or "").lower()
    return True


@app.before_request
def _before_request():
    if request.path.startswith('/static/'):
        return
    # R35: Host 白名单（防 DNS rebinding）——先于鉴权，未通过不泄露任何信息
    if not _host_allowed(request.host):
        return jsonify({"error": "请求 Host 不被允许",
                        "code": "HOST_NOT_ALLOWED",
                        "hint": "内网穿透/自定义域名请设置 WR_ALLOWED_HOSTS"}), 403
    # R35: 跨站写操作拦截（CSRF）
    if request.method in _UNSAFE_METHODS and not _same_origin_ok():
        return jsonify({"error": "跨站请求被拒绝", "code": "CSRF_BLOCKED"}), 403
    # 鉴权：登录页放行，其余全部校验
    if not _auth_enabled or request.path == "/login":
        pass
    elif not _auth_ok():
        if request.path.startswith("/api/"):
            return jsonify({"error": "未登录", "code": "UNAUTHORIZED"}), 401
        return redirect("/login")
    g.request_id = uuid.uuid4().hex[:12]
    g.start = time.time()


@app.after_request
def _after_request(response):
    if request.path.startswith('/static/'):
        return response
    # P1-2: 图片类路径（阅读期每页一请求）豁免请求日志——与 Werkzeug 日志
    # 叠加后一话 30 页 ≈ 60+ 行，淹没有效日志；活动时间戳照常被记录
    _quiet = "/img/" in request.path or request.path.endswith("/proxy")
    try:
        elapsed_ms = int((time.time() - g.start) * 1000)
    except Exception:
        elapsed_ms = 0
    rid = getattr(g, 'request_id', '-')
    if not _quiet:
        print(f"[req] {rid} {request.method} {request.path} {response.status_code} {elapsed_ms}ms",
              flush=True)
    # 失败请求进有界环形缓冲：脱敏诊断报告（/api/diagnostics/report）据此说明
    # "哪一层返回了什么状态码"，不用翻日志。记录本身失败绝不影响响应。
    try:
        if response.status_code >= 400:
            from server import diag as _diag
            _diag.record_request(request.method, request.path,
                                 response.status_code, elapsed_ms)
    except Exception:
        pass
    # 自动缓存清理：记录活动 + 后台惰性检查（不阻塞响应）
    if request.path != '/api/health':   # 健康检查不算"用户活动"
        # BUG-3 修复：空闲判定必须基于"本请求到来之前"的上次活动时间，
        # 先 touch 再判定会让 now - _last_activity_ts 恒 ≈ 0，12h 清理永不触发。
        # 语义：只有上一个请求距今 ≥12h，才在本请求到来时触发清理。
        _idle_secs = time.time() - _last_activity_ts
        _touch_activity()
        # P1-2: touch 只更新内存；落盘 60s 节流（崩溃最多丢 60s 活动记录，
        # 跨重启 12h 无动作语义不变）。自动清理重置计时的位置强制落盘。
        _persist_activity()
        try:
            if time.time() - _last_auto_check_ts >= AUTO_CLEAN_CHECK_SECS:
                threading.Thread(target=_maybe_auto_clean, args=(_idle_secs,),
                                 daemon=True).start()
        except Exception:
            pass
    return response


@app.route("/api/health")
def api_health():
    """健康检查：系统状态 + 各漫画源可用性"""
    from engine.manga.manager import list_adapters
    _load_manga_adapters()
    sources = [{"key": s["key"], "name": s["name"]} for s in list_adapters()]
    _rt = _RUNTIME.status()
    return jsonify({
        "ok": True, "ts": time.time(),
        "books_dir": os.path.isdir(BOOKS_DIR),
        "manga_sources": sources,
        "manga_dl_tasks": len(_manga_dl.all_tasks()),
        "server_time": now_iso(),
        # 生命周期与工作线程状态（设计 §5.3：就绪信息由控制通道返回；
        # 只给状态，不给数据目录/凭据/书源秘密）
        "runtime": {"state": _rt["state"], "initialized": _rt["initialized"],
                    "profile": _rt["profile"], "instance_id": _rt["instance_id"],
                    "workers": _rt["workers"], "uptime_s": _rt["uptime_s"]},
        # 能力台账（阶段 C）：这台运行时上哪些可选依赖/通道真的可用。
        # 设计 §11 阶段 C 要求逐项 supported/degraded/unsupported 与原因。
        "capabilities": _capabilities.summary(),
    })


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sources")
def sources_page():
    return render_template("sources.html")


@app.route("/library")
def library_page():
    return render_template("library.html")


@app.route("/tasks_page")
def tasks_page():
    return render_template("tasks.html")


@app.route("/reader/<book_key>")
def reader(book_key):

    book_key = _safe_seg(book_key, "书籍")
    return render_template("reader.html", book_key=book_key)


@app.route("/task/<task_id>")
def task_page(task_id):
    return render_template("task.html", task_id=task_id)


@app.route("/manga")
def manga_page():
    return render_template("manga.html")


@app.route("/novel_detail")
def novel_detail_page():
    """小说详情页（对齐漫画详情设计）：
    书库模式 ?key=xxx → /api/books/<key>（章节+下载状态）
    搜索模式 ?source_uid=&book_url=&name= → /api/search-detail（下载入口）"""
    return render_template("novel_detail.html")


@app.route("/manga_detail")
def manga_detail_page():
    _ref = request.headers.get("Referer", "")
    if "undefined" in (request.args.get("source") or "") or \
       "undefined" in (request.args.get("id") or ""):
        print(f"[debug] manga_detail undefined 来源: {_ref} args={dict(request.args)}", flush=True)
    return render_template("manga_detail.html")


@app.route("/manga_reader")
def manga_reader_page():
    return render_template("manga_reader.html")


@app.route("/manga_download")
def manga_download_page():
    return render_template("manga_download.html")


def _maybe_auto_clean(idle_secs):
    """惰性自动清理入口（after_request 调用）：
    ① 无动作 ≥12h → 全量清理（保护已下载）；
    ② data/ 总占用 ≥ 1GB → 清理最旧的未下载缓存直至低于目标值。
    节流检查（AUTO_CLEAN_CHECK_SECS）+ 加锁防并发。

    idle_secs：触发请求到来前的空闲时长（在 _touch_activity 之前测得），
    空闲判定必须用它而非 _last_activity_ts（后者已被本请求刷新）。"""
    global _last_auto_check_ts
    now = time.time()
    if now - _last_auto_check_ts < AUTO_CLEAN_CHECK_SECS:
        return
    _last_auto_check_ts = now
    if not _auto_clean_lock.acquire(blocking=False):
        return
    try:
        # ① 无动作 12h → 全量清理
        if idle_secs >= AUTO_CLEAN_IDLE_SECS:
            freed, cleaned = _clean_caches("cache")
            _touch_activity()   # 清理后重置计时，避免连续触发
            _persist_activity(force=True)   # 关键节点立即落盘（绕过 60s 节流）
            if freed:
                print(f"[autoclean] 无动作≥12h 自动清理，释放 {freed/1048576:.1f}MB",
                      flush=True)
            return
        # ② 容量超限 → 只清"最旧未下载缓存"至目标水位
        total = _cache_footprint()
        if total >= AUTO_CLEAN_SIZE_LIMIT:
            freed = 0
            target = total - AUTO_CLEAN_SIZE_TARGET
            # 按 mtime 从旧到新删除未下载漫画缓存
            if os.path.isdir(MANGA_CACHE_DIR):
                protected = set()
                try:
                    with open(MANGA_LIBRARY_FILE, encoding='utf-8') as _f:
                        lib = json.load(_f)
                    for item in lib:
                        if item.get('status') == 'done' and item.get('source') and item.get('comic_id'):
                            protected.add((item['source'], item['comic_id']))
                except Exception:
                    pass
                cands = []
                for src_name in os.listdir(MANGA_CACHE_DIR):
                    src_dir = os.path.join(MANGA_CACHE_DIR, src_name)
                    if not os.path.isdir(src_dir):
                        continue
                    for cid in os.listdir(src_dir):
                        comic_dir = os.path.join(src_dir, cid)
                        if not os.path.isdir(comic_dir):
                            continue
                        if (src_name, cid) in protected:
                            continue
                        try:
                            mt = os.path.getmtime(comic_dir)
                        except OSError:
                            mt = 0
                        cands.append((mt, comic_dir))
                cands.sort(key=lambda x: x[0])
                for _mt, comic_dir in cands:
                    if freed >= target:
                        break
                    sz = _dir_size(comic_dir)
                    shutil.rmtree(comic_dir, ignore_errors=True)
                    freed += sz
            # 若仍超限 → 再清数据缓存（可再生）
            if _cache_footprint() >= AUTO_CLEAN_SIZE_LIMIT:
                f2, _ = _clean_caches("cache")
                freed += f2
            if freed:
                print(f"[autoclean] 缓存 {total/1048576:.0f}MB 超限，释放 {freed/1048576:.1f}MB",
                      flush=True)
    finally:
        _auto_clean_lock.release()


# ── URLPolicy（R29，技术评审 4.2/7.1）：统一 SSRF 防护 ──
# 所有"用户可控 URL 由服务器代拉"的入口（任务创建/目录/详情/漫画代理/书源导入）
# 必须通过本函数校验：只允许公网 http/https，禁止回环/私网/链路本地/保留地址。
# R35: 实现下沉至 engine/urlsec.py —— engine 层（书源导入/校验）此前无任何
# SSRF 校验，两处各自实现必然漂移，故统一为单一实现源，此处仅做别名转发。
_url_is_public = _urlsec_url_is_public


_safe_target_url = _urlsec_safe_target_url


# ── 全局错误处理（统一 JSON）──
@app.errorhandler(404)
def _err_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "资源不存在", "code": "NOT_FOUND",
                        "request_id": getattr(g, 'request_id', '')}), 404
    return e


@app.errorhandler(500)
def _err_500(e):
    if request.path.startswith("/api/"):
        # R29(技术评审7.4): 客户端只返回稳定错误码+request_id，
        # 完整异常详情进服务端日志（防信息泄漏）
        import traceback
        traceback.print_exc()
        return jsonify({"error": "服务器内部错误",
                        "code": "INTERNAL_ERROR",
                        "request_id": getattr(g, 'request_id', '')}), 500
    return e


@app.errorhandler(Exception)
def _err_unhandled(e):
    """兜底：未捕获异常统一 JSON（带 request_id，防 HTML 泄漏；
    R29: 不再把异常文本原样返回客户端；abort() 的 HTTP 错误按原状态返回）"""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        if request.path.startswith("/api/"):
            _code = e.code or 500
            _msg = (e.description or e.name) if _code < 500 else "服务器内部错误"
            return jsonify({"error": _msg,
                            "code": "HTTP_" + str(_code),
                            "request_id": getattr(g, 'request_id', '')}), _code
        return e
    if request.path.startswith("/api/"):
        import traceback
        traceback.print_exc()
        return jsonify({"error": "服务器内部错误",
                        "code": "INTERNAL_ERROR",
                        "request_id": getattr(g, 'request_id', '')}), 500
    return e


# ══════════════════════════════════════════════
#  启动
# ══════════════════════════════════════════════
_auth_load()          # 读取访问密码（data/auth.json 或 WR_AUTH_PASSWORD）


# ══════════════════════════════════════════════
#  生命周期控制器装配（阶段 B）
#
#  导入模块**不再**启动任何后台线程/联网：所有初始化与工作线程都经由这个控制器
#  显式触发（桌面 main()、移动端 mobile_entry、测试各自调用）。
#  每个工作线程独立开关：WR_BG_<NAME>=0/1 > WR_DISABLE_BACKGROUND=1 > profile 默认。
# ══════════════════════════════════════════════
from server import runtime as _runtime_mod


def _on_runtime_stop(reason):
    """服务/引擎停止：把**运行中**的下载任务标成可解释的中断（路线 P1-1）。

    为什么必须有这一步：手机上前台服务会被系统停止（dataSync 配额/超时、被回收），
    进程也可能直接被结束——那时没人来得及写"为什么停了"。引擎正常关闭这条路径
    是**最后的机会**：把原因落盘，用户回到应用就能看到原因并点「继续」。
    进程被强杀时这条钩子也不会跑到，由下次启动的 `_load_disk_tasks` 兜底
    （status 仍是 running → 记 process_restart）。"""
    from server.state import (interrupt_running_tasks, service_stop_reason,
                              STOP_KIND_SERVICE)
    text = service_stop_reason(reason)
    novel_n = interrupt_running_tasks(text, kind=STOP_KIND_SERVICE)
    manga_n = 0
    try:
        manga_n = _manga_dl.interrupt_running(text)
    except Exception as e:      # noqa: BLE001 - 关闭路径不得因漫画任务出错而中断
        print(f"[shutdown] 漫画任务中断标记失败: {type(e).__name__}: {e}", flush=True)
    if novel_n or manga_n:
        print(f"[shutdown] 已标记中断：小说 {novel_n} 个 / 漫画 {manga_n} 个（{reason}）",
              flush=True)
    return {"novel": novel_n, "manga": manga_n, "reason": reason}


_RUNTIME = _runtime_mod.RuntimeController(
    name="webnovel",
    initialize_hooks=(("purge_pycache", _purge_pycache),
                      ("load_persisted_state", _load_persisted_state),
                      ("repair_transactions", _boot_recover_repair_transactions),
                      ("recover_tasks", _recover_tasks)),
    shutdown_hooks=(("mark_interrupted", _on_runtime_stop),),
    workers={"prewarm": _worker_prewarm,
             "manga-stats-verify": _worker_manga_stats_verify,
             "auto-follow": _worker_auto_follow,
             "task-watchdog": _worker_task_watchdog,
             "source-health": _worker_source_health},
)


def runtime_status():
    """服务/工作线程状态（供 /api/health 与移动端控制通道复用）"""
    return _RUNTIME.status()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="webnovel_rules 服务")
    # R35: 条件默认 —— 未启用鉴权时默认仅本机(127.0.0.1)，启用鉴权后默认开放
    # 局域网(0.0.0.0)。此前无条件默认 0.0.0.0 + 一行启动警告，实际等同于
    # 默认让同网段任何设备可读写书库/删除数据。显式 --host 始终优先。
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    host_explicit = args.host is not None
    if not host_explicit:
        # R36b: 家庭局域网 + 手机 App/浏览器是主要使用场景——默认开放监听
        # （此前"未鉴权→仅本机"导致手机端无法访问，三次回退教训）；
        # 安全兜底：开放监听且未鉴权时下方打印醒目警告。
        # 仅本机使用：显式 --host 127.0.0.1
        args.host = "0.0.0.0"

    # 预热线程按实际端口自调 API（原硬编码 8766，换端口后预热静默失效）
    os.environ['PORT'] = str(args.port)
    # 显式生命周期：initialize（事务/任务恢复）→ 受管工作线程 → 状态置 READY。
    # HTTP 由 Flask 的 app.run 托管（http_mode=external），控制器如实记录该事实。
    _st = _RUNTIME.start(wsgi_app=None, host=args.host, port=args.port,
                         start_workers=True)
    if _st.get("state") == _runtime_mod.STATE_FAILED:
        print(f"⚠️  初始化失败（服务仍会启动，后台能力可能受限）: {_st.get('error')}",
              flush=True)
    print(f"后台工作线程: {_RUNTIME.status()['workers']}", flush=True)
    if args.debug:
        print("⚠️  --debug 已开启：Flask 调试模式会暴露 Werkzeug 调试器，"
              "仅限本地开发，禁止在局域网/公网使用", flush=True)
    if args.host in ("0.0.0.0", "::") and not _auth_enabled:
        # 用户显式要求开放监听但未设密码：尊重选择，但警告要足够醒目
        print("=" * 64, flush=True)
        print("⚠️  已开放局域网监听但未启用访问鉴权！", flush=True)
        print("    局域网内其他设备可读取书库/阅读历史/漫画收藏，"
              "并可删除数据。", flush=True)
        print("    启用鉴权：设置 WR_AUTH_PASSWORD 环境变量，"
              "或创建 data/auth.json", flush=True)
        print("=" * 64, flush=True)

    print(f"📚 webnovel_rules 启动: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug,
            threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()


