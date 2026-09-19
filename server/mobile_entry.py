# -*- coding: utf-8 -*-
"""手机独立服务器 APK 的 Python 入口（阶段 C：接入真实业务）

阶段 A 只验证底层可行性（内嵌 Python + 原生依赖 + 本机 WSGI + 私有文件）；
阶段 C 起这里改为**直接承载真实系统**：装载仓库唯一的业务代码（app.py +
server/ + engine/ + templates/ + static/ + sources/），在手机上提供与桌面一致的
网页界面与 API，不维护第二套实现。

设计对齐（《手机独立服务器 APK 设计方案》）：
- §2.5 路径注入必须在导入 config/state/app **之前**：本模块先把 WR_DATA_DIR /
  WR_CACHE_DIR / WR_DEFER_INIT / WR_PROFILE 写进 os.environ，再导入 app。
- §5 显式生命周期：导入不产生副作用（WR_DEFER_INIT=1），初始化/启动/停止都经
  server.runtime.RuntimeController；本模块只做"宿主"该做的事。
- §5.3 本机会话：所有路径（页面/API/图片/导出）都要带会话凭据（Cookie 或
  X-Mobile-Token）；凭据只在本应用内部传递，不进 URL/日志/localStorage。
- §3 只绑回环：固定 127.0.0.1，不接受远端指定监听地址。
- §6.2：不做开机自启、不后台自行拉起。

阶段 A 的自检端点保留在 /__mobile/*（同样要求会话凭据），用于真机排障。
"""
import json
from hmac import compare_digest
import os
import sys
import threading
import time
import uuid

STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_DRAINING = "draining"
STATE_FAILED = "failed"


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class MobileRuntime:
    """手机宿主侧运行时门面：环境注入 → 真实 app 初始化 → WSGI 服务 → 停止。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.state = STATE_STOPPED
        self.instance_id = ""
        self.port = 0
        self.host = "127.0.0.1"
        self.token = ""
        self.data_dir = ""
        self.cache_dir = ""
        self.started_at = 0.0
        self.last_error = ""
        self._server = None
        self._thread = None
        self._app = None
        self._runtime = None          # server.runtime.RuntimeController（真实控制器）
        self.wsgi = ""                # 实际使用的 WSGI 服务器（waitress|werkzeug）

    # ── 环境注入（必须在导入业务模块之前）──
    def _inject_env(self, data_dir, cache_dir, token, port):
        os.environ["WR_DATA_DIR"] = data_dir
        os.environ["WR_CACHE_DIR"] = cache_dir
        os.environ["WR_DEFER_INIT"] = "1"      # 导入不做事，由本模块显式初始化
        os.environ["WR_PROFILE"] = "mobile"    # 资源预算按设计 §9
        os.environ["WR_MOBILE_TOKEN"] = token
        os.environ["WR_MOBILE_PORT"] = str(port or 0)

    def initialize(self, data_dir=None, cache_dir=None, token=None, port=None):
        data_dir = data_dir or os.environ.get("WR_DATA_DIR") or ""
        if not data_dir:
            raise RuntimeError("缺少 WR_DATA_DIR：Android 侧必须注入应用私有目录")
        self.data_dir = os.path.abspath(data_dir)
        self.cache_dir = os.path.abspath(cache_dir or os.path.join(self.data_dir, "cache"))
        self.token = token or os.environ.get("WR_MOBILE_TOKEN") or uuid.uuid4().hex
        self.port = int(port or os.environ.get("WR_MOBILE_PORT") or 0)
        self.instance_id = uuid.uuid4().hex[:12]
        self._inject_env(self.data_dir, self.cache_dir, self.token, self.port)
        # 私有目录（设计 §7）：业务数据 / 可编辑书源 / 运行配置 / 可重建缓存分开
        self.paths = {
            "data": os.path.join(self.data_dir, "data"),
            "sources": os.path.join(self.data_dir, "sources"),
            "config": os.path.join(self.data_dir, "runtime_config"),
        }
        for p in list(self.paths.values()) + [self.cache_dir]:
            os.makedirs(p, exist_ok=True)
        probe = os.path.join(self.paths["config"], "write_probe.txt")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok %s\n" % _now())
        os.environ["WR_SOURCES_DIR"] = self.paths["sources"]
        # 出站代理（0.73.0）：设置页"网络 → 代理"写这个文件，engine/netproxy 读它。
        # 放在应用私有目录里（不是 assets）：卸载即清、无需运行时权限，
        # 且改完立刻生效（netproxy 按 mtime 缓存，不需要重启引擎）。
        os.environ["WR_PROXY_FILE"] = os.path.join(self.paths["config"], "proxy.txt")
        self._bootstrap_sources()
        with open(os.path.join(self.paths["config"], "runtime.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"schema_version": 1, "instance_id": self.instance_id,
                       "port": self.port, "mode": "phase-c",
                       "python": sys.version.split()[0],
                       "started_at": time.time()}, f, ensure_ascii=False)
        return {"instance_id": self.instance_id, "port": self.port}

    def _bootstrap_sources(self):
        """把随 APK 打包的只读书源写入可写目录；**已存在不覆盖**（保留用户修改）。

        三条路径，按**主次**顺序（顺序很关键，见下）：
          1. 原生侧（Kotlin LocalServerService）启动前已把 `assets/sources/*.json`
             解到目标目录 —— 这是**主路径**。Chaquopy 的 Python 代码在 assets zip 内，
             构建期新生成的 .py 无法稳定编译进包（实测只会以未编译 .py 入包、
             设备上 import 必失败），因此书源改走 assets 由原生侧解出。
          2. 目录方式：开发/测试环境用 WR_BUNDLED_SOURCES_DIR 指向仓库 sources/。
          3. 历史形态的 `bundled_sources` 模块（旧包可能带）。
        只有三条都拿不到书源时才报警——主路径已就位时不该报"模块不可用"，
        那是误报（会让排查的人以为书源丢了）。
        """
        dst = self.paths["sources"]
        try:
            _existing = [f for f in os.listdir(dst) if f.endswith(".json")]
        except OSError:
            _existing = []
        if _existing:
            print("[mobile] 内置书源已由原生侧解出 %d 个，无需 bootstrap"
                  % len(_existing), flush=True)
            return
        pairs = {}
        src = os.environ.get("WR_BUNDLED_SOURCES_DIR") or ""
        if src and os.path.isdir(src):
            for fn in os.listdir(src):             # 开发/测试形态
                if fn.endswith(".json"):
                    try:
                        with open(os.path.join(src, fn), encoding="utf-8") as f:
                            pairs[fn] = f.read()
                    except OSError:
                        pass
        if not pairs:
            try:
                import bundled_sources as _bs       # 历史形态
                pairs = dict(getattr(_bs, "SOURCES", {}) or {})
            except Exception:
                pairs = {}
        n = 0
        for fn, content in pairs.items():
            d = os.path.join(dst, fn)
            if os.path.exists(d):
                continue
            try:
                tmp = d + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp, d)
                n += 1
            except OSError:
                pass
        if n:
            print("[mobile] 内置书源初始化 %d 个" % n, flush=True)
        else:
            print("[mobile] ⚠ 未找到任何内置书源：搜索/抓取将不可用"
                  "（检查 APK 的 assets/sources 是否解出）", flush=True)

    def pick_port(self, preferred=None):
        import socket
        for cand in (preferred, self.port):
            if cand:
                s = socket.socket()
                try:
                    s.bind(("127.0.0.1", int(cand)))
                    return int(cand)
                except OSError:
                    continue
                finally:
                    s.close()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    # ── 启动：真实 app + 受管工作线程 + Waitress ──
    def start(self):
        with self._lock:
            if self.state in (STATE_READY, STATE_STARTING):
                return self.status()
            self.state = STATE_STARTING
            self.last_error = ""
        try:
            import app as APP                     # 真实业务入口（已 defer init）
            from server import runtime as RT
            self._runtime = APP._RUNTIME
            self.port = self.pick_port(self.port)
            st = self._runtime.start(wsgi_app=None, host=self.host, port=self.port,
                                     start_workers=True, profile="mobile")
            if st.get("state") == RT.STATE_FAILED:
                raise RuntimeError("业务初始化失败: %s" % st.get("error"))
            self._app = self._auth_middleware(APP.app)
            self._serve(self._app, self.port)
            self.started_at = time.time()
            self.state = STATE_READY
            with open(os.path.join(self.paths["config"], "session.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"instance_id": self.instance_id, "port": self.port,
                           "token": self.token, "started_at": self.started_at}, f)
            print("[mobile] 真实服务就绪 http://%s:%d workers=%s profile=mobile"
                  % (self.host, self.port, st.get("workers")), flush=True)
        except Exception as e:
            self.state = STATE_FAILED
            self.last_error = "%s: %s" % (type(e).__name__, e)
            print("[mobile] 启动失败: %s" % self.last_error, flush=True)
        return self.status()

    def _serve(self, wsgi_app, port):
        """发行目标用 Waitress（设计 §4/§5.2：不依赖 Flask 开发启动器）。

        开发/测试环境可能没装 waitress，此时回落到 werkzeug 的线程化服务器，
        并在 status 里**如实标注**用的是哪一个（不假装是 Waitress）。"""
        try:
            from waitress.server import create_server
            srv = create_server(wsgi_app, host=self.host, port=port, threads=8,
                                clear_untrusted_proxy_headers=True)
            self.wsgi = "waitress"
        except ImportError:
            from werkzeug.serving import make_server
            srv = make_server(self.host, port, wsgi_app, threaded=True)
            self.wsgi = "werkzeug"
            print("[mobile] 未安装 waitress，回落到 werkzeug 开发服务器"
                  "（发行版必须用 waitress）", flush=True)
        self._server = srv
        self._thread = threading.Thread(target=self._run_server, name="mobile-wsgi",
                                        daemon=True)
        self._thread.start()

    def _run_server(self):
        try:
            # waitress: run()；werkzeug: serve_forever()——两者都必须支持
            fn = getattr(self._server, "run", None) or self._server.serve_forever
            fn()
        except Exception as e:                    # 关闭时的正常噪声不当作故障
            if self.state not in (STATE_DRAINING, STATE_STOPPED):
                print("[mobile] WSGI 退出: %s: %s" % (type(e).__name__, e), flush=True)

    # ── 会话凭据（设计 §5.3）：页面/API/图片/导出全部校验 ──
    def _auth_middleware(self, wsgi_app):
        token = self.token

        def _json(start_response, payload, status="200 OK"):
            body = json.dumps(payload).encode()
            start_response(status, [("Content-Type", "application/json"),
                                    ("Content-Length", str(len(body)))])
            return [body]

        def _mw(environ, start_response):
            path = environ.get("PATH_INFO", "")
            # 宿主就绪探测：唯一的免凭据端点，且只回答"活着没"（设计 §5.3：
            # 就绪信息不暴露书库路径/账号/书源秘密）
            if path == "/__mobile/health":
                return _json(start_response,
                             {"ok": self.state == STATE_READY,
                              "state": self.state})
            cookie = environ.get("HTTP_COOKIE", "") or ""
            hdr = environ.get("HTTP_X_MOBILE_TOKEN", "") or ""
            got = hdr
            if not got:
                for part in cookie.split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "mobile_session":
                        got = v
                        break
            # 恒定时间比较（技术指南 §9.2）：避免按字符短路带来的时序侧信道；
            # 失败响应统一 401 且不回显任何 token 片段。
            if not got or not compare_digest(got, token):
                return _json(start_response, {"ok": False, "error": "unauthorized"},
                             status="401 Unauthorized")
            # 控制通道（排障/宿主展示用）：运行状态 + 能力台账，仍不含凭据
            if path == "/__mobile/status":
                from server import capabilities as _cap
                return _json(start_response,
                             {"ok": True, "runtime": self.status(),
                              "capabilities": _cap.summary()})
            if path == "/__mobile/stop":
                threading.Thread(
                    target=lambda: (time.sleep(0.3), self.stop("api")),
                    daemon=True).start()
                return _json(start_response, {"ok": True, "stopping": True})
            return wsgi_app(environ, start_response)

        return _mw

    def status(self):
        try:
            rt = self._runtime.status() if self._runtime is not None else {}
        except Exception:
            rt = {}
        return {
            "state": self.state,
            "instance_id": self.instance_id,
            "host": self.host,
            "port": self.port,
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "wsgi": self.wsgi,
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "data_dir": self.data_dir,
            "cache_dir": self.cache_dir,
            "error": self.last_error,
            "business": {"state": rt.get("state"), "workers": rt.get("workers", []),
                         "initialized": rt.get("initialized")},
        }

    def stop(self, reason="requested"):
        with self._lock:
            if self.state == STATE_STOPPED:
                return self.status()
            self.state = STATE_DRAINING
        try:
            if self._runtime is not None:
                self._runtime.stop("mobile:%s" % reason)   # 先停业务（含工作线程）
            if self._server is not None:
                # waitress 用 close()；werkzeug 用 shutdown()——两者都要支持
                if hasattr(self._server, "close"):
                    self._server.close()
                elif hasattr(self._server, "shutdown"):
                    self._server.shutdown()
            if self._thread is not None:
                self._thread.join(timeout=5)
        except Exception as e:
            print("[mobile] 停止异常: %s: %s" % (type(e).__name__, e), flush=True)
        finally:
            self._server = None
            self._thread = None
            self.started_at = 0.0
            self.state = STATE_STOPPED
            try:
                p = os.path.join(self.paths["config"], "session.json")
                if os.path.exists(p):
                    os.remove(p)                      # 凭据不驻留
            except OSError:
                pass
        print("[mobile] 已停止 (%s)" % reason, flush=True)
        return self.status()


_RUNTIME = MobileRuntime()


def get_runtime():
    return _RUNTIME


def main():                                            # pragma: no cover - Chaquopy 入口
    info = _RUNTIME.initialize()
    print("[mobile] initialize ok: %s" % info, flush=True)
    st = _RUNTIME.start()
    print("[mobile] start -> %s port=%s" % (st["state"], st["port"]), flush=True)
    return st["port"]
