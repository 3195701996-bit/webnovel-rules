# -*- coding: utf-8 -*-
"""漫画下载任务：**被杀之后必须能被找回**（0.74.13 人工验证后补的断言）。

## 为什么要锁这条

手机上的下载随时可能被系统杀掉（后台限制、内存回收）。重启后用户必须能看到
那条任务、知道为什么停了、并能接着下——否则已下载的图片变成孤儿，
用户只能重新点一次下载。

实测（2026-09-18，桌面 + 手机等价依赖集，强杀进程模拟应用被杀）：

    下载中强杀 → 重启 → /api/tasks 列出：
      id=manga_copymanga:jurenmeiman · status=stopped ·
      progress={images_done:4, images_total:24} · resumable=true ·
      stop_reason=「应用进程或前台服务已停止，下载随之中断；已下载的图片保留，可点「继续」接着下。」
    续传 → done 2/2 · 文件 6 → 50

这条链路由 `DownloadManager.load()` + RuntimeController 的启动钩子
（`app._load_persisted_state` / `app._recover_tasks`）完成。
本文件锁住**装载与收敛语义**（钩子由 `tests/test_runtime_hooks*.py` 那类用例管）。

> 注意：直接在 `test_client()` 里 import app 是**不会**跑启动钩子的——
> 我第一版验证就是这么误判成"任务丢了"的。用例显式调用 `load()`，
> 与真实启动路径一致。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.manga.download_manager import DownloadManager  # noqa: E402


def _manager(tmp_path, tasks):
    p = tmp_path / "tasks.json"
    p.write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8")
    m = DownloadManager(state_file=str(p))
    return m, p


def _running_task(key="copymanga:demo"):
    return {key: {
        "status": "running", "total": 2, "done": 0, "current": "第01卷",
        "error": "", "title": "演示", "source": "copymanga", "comic_id": "demo",
        "cover": "", "images_done": 4, "images_total": 24, "failed_chapters": 0,
        "started_at": 1.0, "type": "manga", "chapters": ["v1", "v2"],
        "paused": False}}


def test_running_task_is_converged_to_stopped_with_reason(tmp_path):
    m, _p = _manager(tmp_path, _running_task())
    m.load()
    t = m.all_tasks()["copymanga:demo"]
    assert t["status"] == "stopped", "重启后不允许还显示 running（线程已经没了）"
    reason = t.get("stop_reason") or ""
    assert "中断" in reason and "继续" in reason, reason
    assert t.get("current") == "", "停在半截的'当前章节'要清掉，否则界面像还在跑"


def test_progress_and_chapter_list_survive_the_restart(tmp_path):
    """**进度与章节选择必须留着**：否则用户无法判断接着下什么、下到哪了"""
    m, _p = _manager(tmp_path, _running_task())
    m.load()
    t = m.all_tasks()["copymanga:demo"]
    assert t["images_done"] == 4 and t["images_total"] == 24
    assert t["chapters"] == ["v1", "v2"]
    assert t["total"] == 2


def test_done_and_paused_tasks_are_not_downgraded(tmp_path):
    """已完成的保持 done；用户主动暂停的保持 paused（别把用户的选择改成'中断'）"""
    tasks = _running_task()
    tasks["copymanga:done"] = dict(tasks["copymanga:demo"], status="done")
    tasks["copymanga:held"] = dict(tasks["copymanga:demo"], status="paused")
    m, _p = _manager(tmp_path, tasks)
    m.load()
    allt = m.all_tasks()
    assert allt["copymanga:done"]["status"] == "done"
    assert allt["copymanga:held"]["status"] == "paused"


def test_load_is_idempotent(tmp_path):
    """重复装载不得重复任务、也不得把已收敛的 stopped 再改一次"""
    m, _p = _manager(tmp_path, _running_task())
    m.load()
    m.load()
    assert len(m.all_tasks()) == 1


def test_missing_or_broken_state_file_is_survivable(tmp_path):
    """状态文件缺失/损坏只影响装载，不得让管理器起不来"""
    m = DownloadManager(state_file=str(tmp_path / "nope.json"))
    m.load()
    assert m.all_tasks() == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    m2 = DownloadManager(state_file=str(bad))
    m2.load()
    assert m2.all_tasks() == {}
