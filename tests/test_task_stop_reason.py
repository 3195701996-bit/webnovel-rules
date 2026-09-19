# -*- coding: utf-8 -*-
"""0.56.0 回归（离线）：任务"为什么停了"必须是结构化事实（路线 P1-1、§8 后台行）。

路线原文（§7 P1-1）："下载任务检查点：前台服务被停止、用户手动停止、进程重启后
**可解释地**恢复或等待用户恢复。"
§8 最低测试矩阵（后台行）："前台、锁屏、系统终止、服务超时 → 任务状态/恢复原因可见"。

此前的缺口：任务停了只有日志里一句中文，接口只给 status；用户看到"已停止"却不知道
是自己停的、系统杀的，还是下载出错——也就不知道该不该点"继续"。

本用例锁住的契约：
1. 四条停止路径各自写入稳定的 `stop_kind` + 用户可读 `stop_reason`：
   user_pause / user_stop / service_stopped / process_restart / error / done；
2. 用户可读 ≠ 内部串：`mobile:fgs_timeout` 这类原因码必须翻译成人话，
   并且**系统限制要说成系统限制**（不能让用户以为下载坏了）；
3. 断点信息（checkpoint：已下载/总数/失败数）随停止一起落盘，
   重新启动后 `resumable` 复位、旧原因作废（界面不许拿上一轮原因误导）；
4. 恢复时不被"停止"路径覆盖：用户在停止请求后线程收尾，不得把原因改成别的；
5. 关闭钩子（服务停止）必须**只跑一次**、异常隔离、且不阻塞关闭。
"""
import json
import os
import shutil
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOOK_URL = "http://stopreason.example.com/book/1"
SRC = "stopreason_src"


@pytest.fixture(scope="module")
def client():
    """模块级 client：**必须在任何 BOOKS_DIR/TASKS_DIR 补丁之前**导入 app。

    踩过的坑：函数内 `import app` 时，若别的用例把 `app` 从 sys.modules 里删过
    （本仓库确有重载 app 的用例），这里就会在补丁生效期间真·重新导入一次，
    app.py 按值绑定的 BOOKS_DIR/TASKS_DIR 被永久钉在 tmp 目录上——
    后续用例（如 test_api 的书库扫描）看到的就是"书不见了"。"""
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


@pytest.fixture()
def st(monkeypatch, tmp_path):
    """隔离的 tasks/books 目录（不碰真实数据）"""
    import server.state as _st
    tasks = tmp_path / "tasks"
    books = tmp_path / "books"
    tasks.mkdir(parents=True, exist_ok=True)
    books.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_st, "TASKS_DIR", str(tasks), raising=False)
    monkeypatch.setattr(_st, "BOOKS_DIR", str(books), raising=False)
    # 必须**原地清空**而不是替换 dict：server/novel_api.py 是 `from server.state
    # import _tasks/_crawlers` 的按值绑定，换掉对象会让接口读到另一个 dict
    # （整包跑时表现为 pause/stop 返回 400「任务不在运行中」，单跑却通过）。
    saved = (dict(_st._tasks), dict(_st._crawlers), dict(_st._PROGRESS_FLUSH))
    _st._tasks.clear()
    _st._crawlers.clear()
    _st._PROGRESS_FLUSH.clear()
    yield _st
    _st._tasks.clear(); _st._tasks.update(saved[0])
    _st._crawlers.clear(); _st._crawlers.update(saved[1])
    _st._PROGRESS_FLUSH.clear(); _st._PROGRESS_FLUSH.update(saved[2])
    shutil.rmtree(tasks, ignore_errors=True)


def _mk_task(st, task_id="tstop0001", status=None, progress=None):
    from server.state import ST_RUNNING, _save_task
    t = {
        "id": task_id, "source_uid": SRC, "book_url": BOOK_URL,
        "book_key": f"{SRC}_abcdef1234",
        "status": status or ST_RUNNING,
        "created_at": "2026-09-16T03:00:00", "finished_at": None,
        "progress": progress or {"status": "running", "completed": 7, "total": 100,
                                 "failed_chapters": 2, "current": "第8章"},
        "pause_requested": False, "log": [],
    }
    st._tasks[task_id] = t
    _save_task(t)
    return t


# ── 1) 进程重启：磁盘上还是 running → 必须带原因收敛 ────────────────
def test_process_restart_marks_reason_and_checkpoint(st):
    _mk_task(st)
    st._tasks.clear()
    st._load_disk_tasks()
    t = st._tasks["tstop0001"]
    assert t["status"] == "stopped"
    assert t["stop_kind"] == "process_restart"
    assert "系统" in t["stop_reason"] and "继续" in t["stop_reason"]
    assert t["resumable"] is True
    assert t["checkpoint"] == {"done": 7, "total": 100, "failed": 2, "current": "第8章"}
    # 落盘（下次启动还能读到原因）
    with open(os.path.join(st.TASKS_DIR, "tstop0001.json"), encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["stop_kind"] == "process_restart"
    assert on_disk["stop_reason"] == t["stop_reason"]


def test_service_stop_reason_translation(st):
    from server.state import service_stop_reason
    # 系统限制必须说清楚是系统行为，且给出"继续"的出路
    fgs = service_stop_reason("mobile:fgs_timeout")
    assert "系统" in fgs and "不是下载失败" in fgs and "继续" in fgs
    assert "mobile:" not in fgs and "fgs_timeout" not in fgs, "内部原因码不得漏给用户"
    assert "通知" in service_stop_reason("user_notification")
    assert "回收" in service_stop_reason("service_destroy")
    # 未知原因照实带出来，不编造
    unknown = service_stop_reason("weird_thing")
    assert "weird_thing" in unknown
    assert "未记录" in service_stop_reason("")


# ── 2) 服务/引擎停止：运行中的任务被标记（钩子调用）────────────────
def test_interrupt_running_tasks_marks_and_flags(st):
    t = _mk_task(st)
    stop_flag = {"v": False}
    st._crawlers[t["id"]] = {"task": None, "stop": stop_flag}
    n = st.interrupt_running_tasks(st.service_stop_reason("mobile:fgs_timeout"),
                                   kind=st.STOP_KIND_SERVICE)
    assert n == 1
    assert t["status"] == "stopped"
    assert stop_flag["v"] is True, "必须同时置停止标志，抓取线程才会退出"
    assert t["stop_kind"] == "service_stopped"
    assert t["resumable"] is True
    # 幂等：再调一次不再重复计数
    assert st.interrupt_running_tasks("again", kind=st.STOP_KIND_SERVICE) == 0


def test_interrupt_does_not_touch_finished_or_paused(st):
    a = _mk_task(st, "tstopA", status="done")
    b = _mk_task(st, "tstopB", status="paused")
    n = st.interrupt_running_tasks("服务停止", kind=st.STOP_KIND_SERVICE)
    assert n == 0
    assert a.get("stop_kind") is None and b.get("stop_kind") is None


# ── 3) 恢复：旧原因作废、resumable 复位 ────────────────────────────
def test_relaunch_clears_previous_reason(st, monkeypatch):
    t = _mk_task(st)
    st.interrupt_running_tasks("服务被停止", kind=st.STOP_KIND_SERVICE)
    launched = {}

    class _NoThread:
        def __init__(self, *a, **k):
            launched["args"] = a

        def start(self):
            pass

    monkeypatch.setattr(st.threading, "Thread", _NoThread)
    tid, err = st._launch_task(t["id"])
    assert err is None and tid == t["id"]
    assert t["status"] == "running"
    assert t["stop_kind"] == "" and t["stop_reason"] == ""
    assert t["resumable"] is False, "正在跑的任务不该显示成可继续的停止态"


# ── 4) 快照把结构化字段交给客户端（默认值也要有，老任务记录不能报错）──
def test_snapshot_exposes_fields_for_legacy_task(st):
    t = _mk_task(st, status="stopped")
    t.pop("stop_kind", None)
    t.pop("stop_reason", None)
    snap = st._task_snapshot(t["id"])
    assert snap["stop_kind"] == "" and snap["stop_reason"] == ""
    assert snap["resumable"] is True           # 停止态默认可继续
    assert snap["checkpoint"]["done"] == 7
    snap_done = st._task_snapshot(_mk_task(st, "tstopC", status="done")["id"])
    assert snap_done["resumable"] is False


# ── 5) 用户暂停/停止的请求当下就写原因（线程收尾前用户已可能离开）──
def test_pause_and_stop_endpoints_write_reason(client, st):
    from server import novel_api as napi
    t = _mk_task(st)
    st._crawlers[t["id"]] = {"task": None, "stop": {"v": False}}

    r = client.post(f"/api/tasks/{t['id']}/pause")
    assert r.status_code == 200
    assert t["stop_kind"] == "user_pause" and "暂停" in t["stop_reason"]
    assert t["resumable"] is True

    t["stop_kind"], t["stop_reason"], t["pause_requested"] = "", "", False
    r = client.post(f"/api/tasks/{t['id']}/stop")
    assert r.status_code == 200
    assert t["stop_kind"] == "user_stop" and "停止" in t["stop_reason"]

    assert napi._task_snapshot(t["id"])["stop_reason"] == t["stop_reason"]


def test_thread_exit_keeps_service_reason(st, monkeypatch):
    """服务停止原因比线程收尾的笼统文案更具体 → 不许被覆盖"""
    t = _mk_task(st)
    t["progress"] = {"status": "stopped", "completed": 3, "total": 100}
    st.interrupt_running_tasks("系统停止了前台服务", kind=st.STOP_KIND_SERVICE)
    t["status"] = "running"      # 模拟线程还没退出
    # 复刻 _run_task 收尾分支的判定
    t["status"] = "stopped"
    if t.get("stop_kind") not in (st.STOP_KIND_SERVICE, st.STOP_KIND_RESTART):
        st._set_stop(t, st.STOP_KIND_USER_STOP, "你在 App 里停止了任务")
    assert t["stop_kind"] == "service_stopped"
    assert "系统停止了前台服务" in t["stop_reason"]


# ── 6) 关闭钩子：只跑一次、异常隔离、不阻塞 ───────────────────────
def test_shutdown_hook_runs_once_and_isolates_errors():
    from server import runtime as rt
    calls = []

    def _boom(reason):
        calls.append(("boom", reason))
        raise RuntimeError("钩子自己炸了")

    def _ok(reason):
        calls.append(("ok", reason))
        return 2

    c = rt.RuntimeController(name="t", workers={}, logger=lambda *a: None,
                             shutdown_hooks=(("boom", _boom), ("ok", _ok)))
    res = c._run_shutdown_hooks("mobile:fgs_timeout")
    assert dict(res)["ok"] == 2
    assert dict(res)["boom"] == "error"
    assert calls == [("boom", "mobile:fgs_timeout"), ("ok", "mobile:fgs_timeout")]
    # 第二次不再执行（避免重复标记/重复写盘）
    assert c._run_shutdown_hooks("again") == []
    assert len(calls) == 2


def test_shutdown_hook_timeout_does_not_block():
    from server import runtime as rt
    released = threading.Event()

    def _slow(reason):
        released.wait(30)      # 模拟钩子卡住
        return "late"

    c = rt.RuntimeController(name="t", workers={}, logger=lambda *a: None,
                             shutdown_hooks=(("slow", _slow),))
    t0 = time.monotonic()
    res = c._run_shutdown_hooks("x")
    assert (time.monotonic() - t0) < 5.0, "钩子超时必须放弃等待，不能拖住关闭"
    assert dict(res)["slow"] == "timeout"
    released.set()


def test_stop_invokes_shutdown_hooks():
    from server import runtime as rt
    seen = []

    def _hook(reason):
        seen.append(reason)
        return 1

    c = rt.RuntimeController(name="t", workers={}, logger=lambda *a: None,
                             shutdown_hooks=(("mark", _hook),))
    c.start(start_workers=False, profile="desktop", env={})
    c.stop("mobile:fgs_timeout", join_timeout=0.2)
    assert seen == ["mobile:fgs_timeout"], "停止必须先记录现场（再释放监听）"


# ── 7) 漫画下载：同一套原因字段 ────────────────────────────────────
def test_manga_download_reasons(monkeypatch, tmp_path):
    from engine.manga.download_manager import DownloadManager
    mgr = DownloadManager(state_file=str(tmp_path / "manga_tasks.json"))
    mgr._tasks["jm:1"] = {"status": "running", "title": "T", "done": 2, "total": 10,
                          "paused": False, "current": "第3话"}
    mgr._tasks["jm:2"] = {"status": "queued", "title": "Q", "done": 0, "total": 4,
                          "paused": False, "current": ""}
    n = mgr.interrupt_running("系统停止了前台服务")
    assert n == 2
    assert mgr._tasks["jm:1"]["status"] == "cancel", "运行中任务要让 worker 感知退出"
    assert mgr._tasks["jm:2"]["status"] == "stopped"
    assert "系统停止了前台服务" == mgr._tasks["jm:1"]["stop_reason"]

    mgr._tasks["jm:3"] = {"status": "running", "title": "R", "paused": False}
    assert mgr.cancel("jm:3") is True
    assert "取消" in mgr._tasks["jm:3"]["stop_reason"]

    # 只验证状态/原因迁移，不起真实 worker（真实 worker 需要 source 字段与适配器）
    monkeypatch.setattr(mgr, "_kick_workers", lambda: None)
    assert mgr.resume("jm:3") is True
    assert mgr._tasks["jm:3"]["status"] == "queued", "cancel 态也必须能继续"
    assert mgr._tasks["jm:3"]["stop_reason"] == "", "重新开始跑就不该再显示旧原因"


def test_manga_load_records_restart_reason(tmp_path):
    from engine.manga.download_manager import DownloadManager
    sf = str(tmp_path / "manga_tasks.json")
    with open(sf, "w", encoding="utf-8") as f:
        json.dump({"jm:9": {"status": "running", "title": "X", "done": 1, "total": 5}}, f)
    mgr = DownloadManager(state_file=sf)
    mgr.load()
    job = mgr.all_tasks()["jm:9"]
    assert job["status"] == "stopped"
    assert "停止" in job["stop_reason"] and "继续" in job["stop_reason"]


# ── 8) 漫画任务的 stop_kind 与小说任务**逐字同值** ──────────────────
# 背景（本轮排查）：漫画任务此前只写 stop_reason 不写 stop_kind，
# 而两条后端都经同一个 /api/tasks 与同一套客户端渲染。当前界面只读
# stop_reason（不读 stopKind），所以当时没有可见缺陷；但字段缺失意味着
# 任何"按 kind 判别是否可继续/怎么提示"的新逻辑都会在漫画一侧静默失效。
# 这里把两边的取值钉死，防止再次漂移。
def test_manga_stop_kind_matches_novel_contract():
    from engine.manga import download_manager as dl
    from server import state as st
    pairs = [
        (dl.STOP_KIND_USER_PAUSE, st.STOP_KIND_USER_PAUSE),
        (dl.STOP_KIND_USER_STOP, st.STOP_KIND_USER_STOP),
        (dl.STOP_KIND_SERVICE, st.STOP_KIND_SERVICE),
        (dl.STOP_KIND_RESTART, st.STOP_KIND_RESTART),
        (dl.STOP_KIND_ERROR, st.STOP_KIND_ERROR),
        (dl.STOP_KIND_DONE, st.STOP_KIND_DONE),
    ]
    for mine, theirs in pairs:
        assert mine == theirs, f"漫画/小说停止原因取值必须一致: {mine!r} != {theirs!r}"


def test_manga_stop_kind_written_on_every_stop_path(monkeypatch, tmp_path):
    from engine.manga.download_manager import (DownloadManager, STOP_KIND_DONE,
                                              STOP_KIND_ERROR, STOP_KIND_RESTART,
                                              STOP_KIND_SERVICE, STOP_KIND_USER_PAUSE,
                                              STOP_KIND_USER_STOP)
    mgr = DownloadManager(state_file=str(tmp_path / "manga_tasks.json"))
    monkeypatch.setattr(mgr, "_kick_workers", lambda: None)
    monkeypatch.setattr(mgr, "save", lambda *a, **k: None)

    # 进程重启：running → stopped
    mgr._tasks["jm:r"] = {"status": "running", "title": "R", "done": 1, "total": 5}
    with open(str(tmp_path / "manga_tasks.json"), "w", encoding="utf-8") as f:
        json.dump(mgr._tasks, f)
    mgr.load()
    assert mgr._tasks["jm:r"]["stop_kind"] == STOP_KIND_RESTART

    # 用户暂停（运行中 / 排队中两条分支）
    mgr._tasks["jm:p"] = {"status": "running", "title": "P", "paused": False}
    assert mgr.pause("jm:p") is True
    assert mgr._tasks["jm:p"]["stop_kind"] == STOP_KIND_USER_PAUSE
    mgr._tasks["jm:q"] = {"status": "queued", "title": "Q", "paused": False}
    assert mgr.pause("jm:q") is True
    assert mgr._tasks["jm:q"]["stop_kind"] == STOP_KIND_USER_PAUSE

    # 用户取消 / 服务中断
    mgr._tasks["jm:c"] = {"status": "running", "title": "C", "paused": False}
    assert mgr.cancel("jm:c") is True
    assert mgr._tasks["jm:c"]["stop_kind"] == STOP_KIND_USER_STOP
    # 服务中断：判定规则与小说侧一致（只认 status == running；pause 请求后
    # status 仍是 running，所以它也会被登记——不能跳过，否则那些 worker 在
    # 关闭流程里继续跑）。因此这里被登记的是 jm:p + jm:s 两条。
    mgr._tasks["jm:s"] = {"status": "running", "title": "S", "paused": False}
    assert mgr.interrupt_running("系统停止了前台服务") == 2
    assert mgr._tasks["jm:s"]["stop_kind"] == STOP_KIND_SERVICE
    assert mgr._tasks["jm:p"]["stop_kind"] == STOP_KIND_SERVICE

    # 继续下载必须清掉旧原因（否则界面在已恢复的任务上继续报"上次为什么停"）
    assert mgr.resume("jm:c") is True
    assert mgr._tasks["jm:c"]["stop_kind"] == "" and mgr._tasks["jm:c"]["stop_reason"] == ""

    # worker 终态：error / done
    mgr._tasks["jm:e"] = {"status": "running", "title": "E"}
    mgr._mark_done("jm:e", "error", error="源站 403")
    assert mgr._tasks["jm:e"]["stop_kind"] == STOP_KIND_ERROR
    mgr._tasks["jm:d"] = {"status": "running", "title": "D"}
    mgr._mark_done("jm:d", "done")
    assert mgr._tasks["jm:d"]["stop_kind"] == STOP_KIND_DONE

    # 暂停/取消的终态落库不得覆盖用户原因
    mgr._tasks["jm:k"] = {"status": "cancel", "title": "K"}
    mgr.cancel("jm:k")
    mgr._mark_done("jm:k", "stopped", error="")
    assert mgr._tasks["jm:k"]["stop_kind"] == STOP_KIND_USER_STOP, \
        "worker 收尾落 stopped 时不能把'你取消了'覆盖成别的原因"
