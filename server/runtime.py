# -*- coding: utf-8 -*-
"""服务生命周期控制器（阶段 B：共享后端可控运行）

设计依据：《手机独立服务器 APK 设计方案》§5。要点：

- 状态机：STOPPED → STARTING → READY → DRAINING → STOPPED；STARTING/READY → FAILED
- initialize() **只做本地初始化**（目录校验、事务恢复、任务恢复），不联网、不起线程
- start()/start_workers() 创建唯一的服务与受管后台工作线程；同一实例只初始化一次
- stop() 幂等、有界、可重复；绝不无限 join，绝不靠"睡满一整轮"才能退出
- 每个后台工作线程**独立开关**（而不是一个全局开关粗暴全禁），profile 提供默认值：
  mobile 默认关闭"启动即全源健康检查"与"热门搜索预热"，保留显式触发
- 长循环一律用可唤醒的 stop event（`stop_event.wait(timeout)`），不用 time.sleep
- 提供"是否被系统暂停"的判定助手：挂起后的一轮不把任务误判为业务卡死
"""
import os
import sys
import threading
import time
import uuid

from engine.config import PROFILE, IS_MOBILE

STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_DRAINING = "draining"
STATE_FAILED = "failed"

# 后台工作线程：name → (桌面默认, mobile 默认)
WORKER_DEFAULTS = {
    "prewarm": (True, False),            # 启动即热门搜索预热（会打本机 API/源站）
    "manga-stats-verify": (True, True),  # 纯本地目录核对，两平台都开
    "auto-follow": (True, False),        # 定期访问源站检查更新
    "task-watchdog": (True, True),       # 任务心跳看护
    "source-health": (True, False),      # 启动即全源健康检查（会打源站）
    # 搜索返回后的后台预热（0.68/0.69）：封面代理缓存 + 前几条详情。
    # 两平台都开（桌面开是为了真机一致），但**可被 WR_DISABLE_BACKGROUND 关掉** ——
    # 测试与"大流量计费/源站限流紧张"时不想有后台线程打源站（实测：测试里没关会让
    # 后台线程在临时数据目录留下 .txn.lock，干扰其它用例的"无残留文件"断言）。
    "search-warm": (True, True),
}


def _env_flag(env, key):
    v = env.get(key)
    if v is None:
        return None
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def worker_enabled(name, profile=None, env=None):
    """工作线程开关：显式 WR_BG_<NAME> > 全局 WR_DISABLE_BACKGROUND > profile 默认。

    独立开关的意义（设计 §5）：手机端需要"关掉源站密集访问、保留本地核对与看护"，
    而不是把所有后台能力一起砍掉。
    """
    env = os.environ if env is None else env
    # 优先级按文档：**显式开关 > 全局禁用 > profile 默认**。
    # 旧实现先判全局禁用再读显式开关，于是"全局禁用下想单独开回某一个 worker"
    # （测试里打开某个后台预热、或用户只想恢复某一个）根本做不到 —— 实测被
    # tests/test_manga_search_warm.py 与 test_manga_cover_proxy.py 抓到。
    explicit = _env_flag(env, "WR_BG_" + name.upper().replace("-", "_"))
    if explicit is not None:
        return explicit
    if _env_flag(env, "WR_DISABLE_BACKGROUND") is True:
        return False
    desktop, mobile = WORKER_DEFAULTS.get(name, (True, True))
    prof = (profile or PROFILE or "desktop").lower()
    return mobile if prof == "mobile" else desktop


def workers_plan(profile=None, env=None):
    return {n: worker_enabled(n, profile, env) for n in WORKER_DEFAULTS}


class RuntimeController:
    """显式、可重复、可观测的服务生命周期控制器。

    典型用法：
        rt = RuntimeController(name="webnovel", initialize_hooks=(...), workers={...})
        rt.initialize()          # 本地初始化：不联网、不起线程
        rt.start(wsgi_app=app)   # 起 HTTP 服务 + 受管工作线程（或只起工作线程）
        rt.status(); rt.stop()   # 幂等、有界
    """

    def __init__(self, name="webnovel", initialize_hooks=(), workers=None,
                 shutdown_hooks=(), 
                 logger=print):
        self.name = name
        self.instance_id = uuid.uuid4().hex[:12]
        self._lock = threading.RLock()
        self._log = logger
        self._state = STATE_STOPPED
        self._error = ""
        self._hooks = list(initialize_hooks)
        # 关闭钩子（P1-1）：停止前给宿主一次机会**记录现场**（例如把运行中的
        # 下载任务标成"服务被停止"）。必须极短且不得阻塞关闭。
        self._shutdown_hooks = list(shutdown_hooks or ())
        self._shutdown_ran = False
        self._workers = dict(workers or {})
        self._worker_threads = {}
        self._worker_stop = {}
        self._worker_round = {}          # name -> (monotonic 起始, 次数)
        self._initialized = False
        self._init_summary = {}
        self._started_at = 0.0
        self._server = None
        self._server_thread = None
        self._host = ""
        self._port = 0
        self._stop_reason = ""
        # http_mode: runtime=控制器自己起监听 | external=宿主托管（如 Flask app.run）
        self._http_mode = "none"

    # ── 注册 ─────────────────────────────────────────────
    def register_worker(self, name, factory):
        """factory(stop_event) -> None；stop_event.set() 后应尽快返回。"""
        with self._lock:
            self._workers[name] = factory

    def add_initialize_hook(self, fn, label=None):
        with self._lock:
            self._hooks.append((label or getattr(fn, "__name__", "hook"), fn))

    def add_shutdown_hook(self, fn, label=None):
        """关闭前回调（参数为 stop reason）。异常/超时都不得阻碍关闭。"""
        with self._lock:
            self._shutdown_hooks.append((label or getattr(fn, "__name__", "hook"), fn))

    def _run_shutdown_hooks(self, reason):
        """执行关闭钩子（只跑一次；逐条异常隔离，单条超时 3s 后放弃）"""
        with self._lock:
            if self._shutdown_ran:
                return []
            self._shutdown_ran = True
            hooks = list(self._shutdown_hooks)
        results = []
        for label, fn in hooks:
            box = {}

            def _call(_fn=fn, _box=box):
                try:
                    _box["ok"] = _fn(reason)
                except Exception as e:      # noqa: BLE001 - 钩子异常不得影响关闭
                    _box["err"] = f"{type(e).__name__}: {e}"

            th = threading.Thread(target=_call, name=f"shutdown-hook-{label}", daemon=True)
            th.start()
            th.join(timeout=3.0)
            if th.is_alive():
                self._log(f"[runtime] 关闭钩子 {label} 超时（不阻塞关闭）")
                results.append((label, "timeout"))
            elif "err" in box:
                self._log(f"[runtime] 关闭钩子 {label} 异常: {box['err']}")
                results.append((label, "error"))
            else:
                results.append((label, box.get("ok")))
        return results

    # ── 初始化（只做本地准备）─────────────────────────────
    def initialize(self, profile=None, force=False):
        with self._lock:
            if self._initialized and not force:
                return dict(self._init_summary, already=True)
            # 只把"已 READY"当成本轮已完成。
            # 2026-09-15 修复（设备实测）：start() 会**先**把状态置为 STARTING 再调用
            # initialize()，而旧条件把 STATE_STARTING 也当"已完成"直接返回 →
            # **初始化钩子在这条路径上从不执行**。手机端正是走 start() 这条路，
            # 于是 recover_tasks（装载磁盘上的漫画下载任务）从来没跑过：
            # 系统强杀/重启/升级后，磁盘上任务记录还在，但界面里任务不见了、
            # 也无法点"继续"（实测：_tasks.json 里 running 的记录装载后仍是 idle）。
            # 桌面 main() 显式先 initialize() 再 start()，所以一直没暴露。
            if self._state == STATE_READY:
                return dict(self._init_summary, already=True)
            self._state = STATE_STARTING
            self._error = ""
            hooks = list(self._hooks)
        summary = {"instance_id": self.instance_id, "profile": profile or PROFILE,
                   "mobile": IS_MOBILE if profile is None else profile == "mobile",
                   "hooks": {}, "workers_planned": workers_plan(profile)}
        try:
            for item in hooks:
                label, fn = item if isinstance(item, tuple) else (item.__name__, item)
                t0 = time.monotonic()
                try:
                    r = fn()
                    summary["hooks"][label] = {"ok": True,
                                               "ms": round((time.monotonic() - t0) * 1000, 1),
                                               "result": r if isinstance(r, (dict, list, str, int, float, bool, type(None))) else None}
                except Exception as e:              # 钩子失败要暴露，不静默吞
                    summary["hooks"][label] = {"ok": False,
                                               "error": f"{type(e).__name__}: {e}"}
                    self._log(f"[runtime] initialize 钩子 {label} 失败: "
                              f"{type(e).__name__}: {e}")
                    raise
            with self._lock:
                self._initialized = True
                self._init_summary = summary
            return summary
        except Exception as e:
            with self._lock:
                self._state = STATE_FAILED
                self._error = f"{type(e).__name__}: {e}"
            return dict(summary, failed=True, error=self._error)

    # ── 启动 HTTP 服务 ───────────────────────────────────
    def start(self, wsgi_app=None, host="127.0.0.1", port=0, server="auto",
              start_workers=True, blocking=False, profile=None, env=None):
        """创建唯一的 HTTP 服务（可选）与受管工作线程。

        server: auto | waitress | werkzeug  —— auto 时 mobile 用 waitress、桌面用 werkzeug
        wsgi_app 为 None 时只启动工作线程（桌面由 app.run 自己托管 HTTP）。
        """
        with self._lock:
            if self._state in (STATE_READY, STATE_STARTING):
                return self.status()
            self._state = STATE_STARTING
            self._error = ""
            self._stop_reason = ""
            self._shutdown_ran = False
        try:
            res = self.initialize(profile=profile) if not self._initialized else None
            if res and res.get("failed"):
                raise RuntimeError(f"initialize 失败: {self._error}")
            if wsgi_app is not None:
                kind = server
                if kind == "auto":
                    kind = "waitress" if IS_MOBILE else "werkzeug"
                self._serve(wsgi_app, host, port, kind, blocking)
                self._http_mode = "runtime"
            else:
                # 宿主自己托管 HTTP（桌面 main() 用 app.run）：状态机仍要如实置 READY，
                # 否则 /api/health 会一直显示 starting（实测发现的假状态）
                self._http_mode = "external"
                self._host, self._port = host, port
            if start_workers:
                self.start_workers(profile=profile, env=env)
            with self._lock:
                self._state = STATE_READY
                self._started_at = time.time()
            self._log(f"[runtime] ready: {self.name} instance={self.instance_id} "
                      f"http={self._host or '-'}:{self._port or '-'}"
                      f"({self._http_mode}) workers={list(self._worker_threads)}")
        except BaseException as e:
            # 用 BaseException：werkzeug 在端口被占用时是 `sys.exit(1)`（SystemExit），
            # 不是普通异常——若不接住，端口冲突会被当成"进程退出"，既不报错也不
            # 清理，正是设计 §5.3 要求明确处理的场景。
            # 启动失败必须**自己收拾干净**：关闭已建立的监听、停掉已启动的工作
            # 线程，避免"启动失败但留下半个服务/重复工作线程"（设计 §11 阶段 B
            # 退出条件：重复启动、启动失败后重试均不得出现双服务/双下载）
            self._cleanup_after_failure()
            with self._lock:
                self._state = STATE_FAILED
                self._error = f"{type(e).__name__}: {e}"
            self._log(f"[runtime] start 失败: {self._error}")
        return self.status()

    def _cleanup_after_failure(self):
        try:
            self._run_shutdown_hooks("start_failed")
            self._release(stop_reason="start_failed")
        except Exception as e:
            self._log(f"[runtime] 启动失败清理异常: {type(e).__name__}: {e}")

    def _release(self, stop_reason="", join_timeout=5.0):
        """释放监听与工作线程（不改变状态；stop() 与启动失败清理共用）"""
        with self._lock:
            events = list(self._worker_stop.items())
            threads = list(self._worker_threads.items())
            srv, sthr = self._server, self._server_thread
        for _n, ev in events:
            ev.set()
        try:
            if srv is not None:
                if hasattr(srv, "close"):
                    srv.close()
                elif hasattr(srv, "shutdown"):
                    srv.shutdown()
        except Exception as e:
            self._log(f"[runtime] 关闭监听异常: {type(e).__name__}: {e}")
        deadline = time.monotonic() + max(0.0, join_timeout)
        for name, t in threads:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
            if t.is_alive():
                self._log(f"[runtime] 工作线程 {name} 未在期限内退出（不阻塞关闭）")
        if sthr is not None:
            sthr.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            self._worker_threads.clear()
            self._worker_stop.clear()
            self._server = None
            self._server_thread = None
            self._http_mode = "none"

    def _serve(self, wsgi_app, host, port, kind, blocking):
        try:
            if kind == "waitress":
                from waitress.server import create_server as _create
                srv = _create(wsgi_app, host=host, port=port or 0, threads=16,
                              clear_untrusted_proxy_headers=True)
                self._host = host
                self._port = getattr(srv, "effective_port", None) or port
            else:
                from werkzeug.serving import make_server as _make
                srv = _make(host, port or 0, wsgi_app, threaded=True)
                self._host, self._port = host, srv.server_port
        except BaseException as e:
            raise RuntimeError(
                f"无法在 {host}:{port or '任意'} 建立监听"
                f"（端口被占用或不可用）: {type(e).__name__}") from e
        self._server = srv
        if blocking:
            self._log(f"[runtime] http 阻塞运行 http://{self._host}:{self._port}")
            srv.run() if kind == "waitress" else srv.serve_forever()
            return
        target = srv.run if kind == "waitress" else srv.serve_forever
        self._server_thread = threading.Thread(target=target, name="runtime-http",
                                               daemon=True)
        self._server_thread.start()

    # ── 受管工作线程 ─────────────────────────────────────
    def start_workers(self, profile=None, env=None):
        plan = workers_plan(profile, env)
        started = []
        for name, factory in list(self._workers.items()):
            if not plan.get(name, False):
                continue
            with self._lock:
                if name in self._worker_threads and self._worker_threads[name].is_alive():
                    continue
                ev = threading.Event()
                self._worker_stop[name] = ev
            t = threading.Thread(target=self._run_worker, args=(name, factory, ev),
                                 name=f"wr-{name}", daemon=True)
            with self._lock:
                self._worker_threads[name] = t
            t.start()
            started.append(name)
        if started:
            self._log(f"[runtime] 启动工作线程: {started}（plan={plan}）")
        return plan

    def _run_worker(self, name, factory, stop_event):
        try:
            factory(stop_event)
        except Exception as e:
            self._log(f"[runtime] 工作线程 {name} 异常退出: {type(e).__name__}: {e}")
        finally:
            with self._lock:
                self._worker_stop.pop(name, None)

    def worker_alive(self, name):
        t = self._worker_threads.get(name)
        return bool(t and t.is_alive())

    # ── 轮次记账：区分"系统暂停"与"业务卡死" ──────────────
    def begin_round(self, name, expected_interval, factor=2.0, tolerance=5.0):
        """开始一轮循环，返回是否**疑似被系统暂停**（挂起导致的一轮不判业务异常）。

        依据：挂起/冻结会同时拉长 monotonic 与 wall clock；用 monotonic 间隔与
        预期周期比较即可识别，无需额外权限。
        """
        now = time.monotonic()
        with self._lock:
            last, count = self._worker_round.get(name, (0.0, 0))
            self._worker_round[name] = (now, count + 1)
        suspended = bool(last) and (now - last) > (expected_interval * factor + tolerance)
        return suspended

    # ── 状态 / 停止 ──────────────────────────────────────
    def status(self):
        with self._lock:
            return {
                "name": self.name,
                "instance_id": self.instance_id,
                "state": self._state,
                "initialized": self._initialized,
                "profile": PROFILE,
                "mobile": IS_MOBILE,
                "host": self._host,
                "port": self._port,
                "http_mode": self._http_mode,
                "http_running": (self._http_mode == "external" and self._state == STATE_READY)
                                or bool(self._server_thread and self._server_thread.is_alive()),
                "workers": sorted(self._worker_threads),
                "workers_alive": [n for n in self._worker_threads if self.worker_alive(n)],
                "uptime_s": round(time.time() - self._started_at, 1) if self._started_at else 0.0,
                "error": self._error,
                "stop_reason": self._stop_reason,
            }

    def stop(self, reason="requested", join_timeout=5.0):
        """幂等且有界：先置停止事件唤醒各循环，再关闭监听，最后有界 join。"""
        with self._lock:
            if self._state == STATE_STOPPED:
                return self.status()
            self._state = STATE_DRAINING
            self._stop_reason = reason
        # P1-1: 先把运行中的任务标记成"为什么停"，再关监听/join 线程
        self._run_shutdown_hooks(reason)
        self._release(stop_reason=reason, join_timeout=join_timeout)
        with self._lock:
            self._state = STATE_STOPPED
            self._started_at = 0.0
        self._log(f"[runtime] stopped ({reason})")
        return self.status()


_RUNTIME = RuntimeController(name="process-default")


def get_runtime():
    """进程默认控制器（桌面 app.py 与移动端各自可建自己的实例）。"""
    return _RUNTIME
