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

import pytest

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
    m, path = _manager(tmp_path, _running_task())
    m.load()
    t = m.all_tasks()["copymanga:demo"]
    assert t["status"] == "stopped", "重启后不允许还显示 running（线程已经没了）"
    reason = t.get("stop_reason") or ""
    assert "中断" in reason and "继续" in reason, reason
    assert t.get("current") == "", "停在半截的'当前章节'要清掉，否则界面像还在跑"
    persisted = json.loads(path.read_text(encoding="utf-8"))["copymanga:demo"]
    assert persisted["status"] == "stopped"
    assert persisted["stop_kind"] == "process_restart"
    assert persisted["stop_reason"] == reason


def test_queued_task_is_converged_to_resumable_stopped_after_restart(tmp_path):
    """内存队列不会跨进程保存；不能让孤立 queued 永久伪装成进行中。"""
    queued = _running_task()
    queued["copymanga:demo"].update({
        "status": "queued", "done": 0, "images_done": 0,
        "current": "等待同源任务完成",
    })
    manager, path = _manager(tmp_path, queued)

    manager.load()

    task = manager.status("copymanga:demo")
    assert task["status"] == "stopped"
    assert task["stop_kind"] == "process_restart"
    assert "仍在等待队列" in task["stop_reason"]
    assert "尚未开始执行" in task["stop_reason"]
    assert task["current"] == ""
    assert manager.paused_keys() == ["copymanga:demo"]
    saved = json.loads(path.read_text(encoding="utf-8"))["copymanga:demo"]
    assert saved["status"] == "stopped"
    assert saved["chapters"] == ["v1", "v2"]


def test_legacy_queued_user_pause_remains_paused_after_restart(tmp_path):
    """旧快照若以 queued+paused 表示用户暂停，恢复时也必须尊重暂停意图。"""
    queued = _running_task()
    queued["copymanga:demo"].update({
        "status": "queued", "paused": True,
        "stop_kind": "user_pause", "stop_reason": "用户已暂停排队任务",
    })
    manager, _path = _manager(tmp_path, queued)

    manager.load()

    task = manager.status("copymanga:demo")
    assert task["status"] == "paused"
    assert task["paused"] is True
    assert task["stop_kind"] == "user_pause"
    assert task["stop_reason"] == "用户已暂停排队任务"
    assert manager.paused_keys() == ["copymanga:demo"]


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


def test_resume_clears_terminal_diagnostics_from_previous_worker_run(tmp_path):
    """Retry summaries are per run; stale bad-page/removal counts must not accumulate."""
    task = _running_task()["copymanga:demo"]
    task.update({
        "status": "error", "failed_chapters": 2,
        "failed_ids": ["chapter-a", "chapter-b"],
        "bad_page_chapters": 3, "bad_page_ids": ["a", "b", "c"],
        "bad_page_count": 8, "removed_chapters": 1,
        "removed_ids": ["chapter-gone"],
    })
    manager, _path = _manager(tmp_path, {"copymanga:demo": task})
    manager.load()
    manager._kick_workers = lambda: None

    assert manager.resume_result("copymanga:demo") == "resumed"
    resumed = manager.status("copymanga:demo")
    assert resumed["status"] == "queued"
    assert resumed["failed_chapters"] == 0 and resumed["failed_ids"] == []
    assert resumed["bad_page_chapters"] == 0
    assert resumed["bad_page_ids"] == [] and resumed["bad_page_count"] == 0
    assert resumed["removed_chapters"] == 0 and resumed["removed_ids"] == []


def test_queued_task_merges_mixed_chapter_ids_without_duplicates(tmp_path):
    task = _running_task()["copymanga:demo"]
    task.update({"status": "paused", "chapters": ["v1", {"id": "v2"}],
                 "paused": True})
    manager, _path = _manager(tmp_path, {"copymanga:demo": task})
    manager.load()
    manager._kick_workers = lambda: None

    key, status = manager.start("copymanga", "demo", "演示", chapters=[
        "v1", {"id": "v2", "name": "卷二"}, "v3", "v3", {"name": "缺少 ID"},
    ])

    assert key == "copymanga:demo" and status == "queued"
    assert manager.status(key)["chapters"] == ["v1", {"id": "v2"}, "v3"]


@pytest.mark.parametrize("status", ["queued", "running"])
def test_user_pause_is_persisted_and_survives_restart(tmp_path, status):
    """暂停操作先落盘；进程重启不得把用户暂停误成意外中断/待运行任务。"""
    m, path = _manager(tmp_path, {})
    key = "copymanga:held"
    job = _running_task()["copymanga:demo"]
    job["status"] = status
    job["paused"] = False
    with m._lock:
        m._tasks[key] = job

    assert m.pause(key)
    saved = json.loads(path.read_text(encoding="utf-8"))[key]
    assert saved["stop_kind"] == "user_pause"
    assert saved["paused"] is True
    if status == "queued":
        assert saved["status"] == "paused"

    recovered = DownloadManager(state_file=str(path))
    recovered.load()
    restored = recovered.status(key)
    assert restored["status"] == "paused"
    assert restored["paused"] is True
    assert restored["stop_kind"] == "user_pause"
    assert "继续" in restored["stop_reason"]


def test_load_is_idempotent(tmp_path):
    """重复装载不得重复任务、也不得把已收敛的 stopped 再改一次"""
    m, _p = _manager(tmp_path, _running_task())
    m.load()
    m.load()
    assert len(m.all_tasks()) == 1


def test_missing_or_broken_state_file_is_survivable(tmp_path):
    """无备份的损坏状态仍可启动，且原始字节不能被后续保存抹掉。"""
    m = DownloadManager(state_file=str(tmp_path / "nope.json"))
    m.load()
    assert m.all_tasks() == {}
    bad = tmp_path / "bad.json"
    corrupt_bytes = b"{not json\xff"
    bad.write_bytes(corrupt_bytes)
    m2 = DownloadManager(state_file=str(bad))
    m2.load()
    assert m2.all_tasks() == {}
    recovery_files = list(tmp_path.glob("bad.json.corrupt*"))
    assert len(recovery_files) == 1
    assert recovery_files[0].read_bytes() == corrupt_bytes
    m2.save()
    assert recovery_files[0].read_bytes() == corrupt_bytes, \
        "空内存态后续落盘也不得销毁唯一的损坏原件"


def test_corrupt_primary_recovers_previous_valid_snapshot(tmp_path):
    """主快照损坏时回退上一代有效任务，并保留损坏主文件原字节。"""
    path = tmp_path / "tasks.json"
    manager = DownloadManager(state_file=str(path))
    key = "copymanga:recover"
    task = _running_task()["copymanga:demo"]
    task.update({"status": "paused", "images_done": 5, "paused": True})
    manager._tasks[key] = task
    manager.save()  # 第一代主快照
    manager._tasks[key]["images_done"] = 9
    manager.save()  # 第二代主快照，第一代进入 .bak
    assert json.loads((tmp_path / "tasks.json.bak").read_text(encoding="utf-8"))[
        key]["images_done"] == 5

    corrupt_bytes = b"truncated task snapshot"
    path.write_bytes(corrupt_bytes)
    recovered = DownloadManager(state_file=str(path))
    recovered.load()
    restored = recovered.status(key)
    assert restored["images_done"] == 5
    assert restored["status"] == "paused"
    assert (tmp_path / "tasks.json.corrupt").read_bytes() == corrupt_bytes

    # 下一次正常保存可以修复主路径，但原始损坏文件仍保留供诊断/人工恢复。
    recovered.save()
    assert json.loads(path.read_text(encoding="utf-8"))[key]["images_done"] == 5
    assert (tmp_path / "tasks.json.corrupt").read_bytes() == corrupt_bytes


def test_deleted_task_is_not_resurrected_from_previous_snapshot(tmp_path):
    """A later corrupt primary must not undo a user's explicit task deletion."""
    path = tmp_path / "tasks.json"
    manager = DownloadManager(state_file=str(path))
    key = "mangadex:deleted-task"
    task = _running_task()["copymanga:demo"]
    task.update({"status": "paused", "paused": True})
    manager._tasks[key] = task
    manager.save()
    assert key in json.loads(path.read_text(encoding="utf-8"))

    manager.delete(key)
    assert json.loads(path.read_text(encoding="utf-8")) == {}
    backup = json.loads((tmp_path / "tasks.json.bak").read_text(encoding="utf-8"))
    assert backup == {}, \
        "the recovery generation must not retain explicitly deleted task identities"

    corrupt_bytes = b"truncated after delete"
    path.write_bytes(corrupt_bytes)
    recovered = DownloadManager(state_file=str(path))
    recovered.load()
    assert recovered.all_tasks() == {}
    assert (tmp_path / "tasks.json.corrupt").read_bytes() == corrupt_bytes
