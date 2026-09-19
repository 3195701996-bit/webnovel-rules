# -*- coding: utf-8 -*-
"""0.61.0 回归：**import 不得有启动副作用**（技术指南 §8.1 / §19"运行时"检查表）。

技术指南要求：
  "import 阶段：只定义对象、蓝图、路由、配置读取函数；initialize 阶段才注入目录、
   创建目录、加载配置；start 阶段才启动 worker 与 WSGI。"

修复前实测问题：`server/state.py` 在**模块级**直接调用了
`_load_disk_tasks()` / `_toc_cache_load()` / `_search_cache_load()` / `_manga_stats_load()`。
其中 `_load_disk_tasks()` 会把磁盘上仍是 running 的任务改写成"中断"并**落盘**——
也就是说 `import server.state` 一次就改一次用户数据；在 Android 端，import 发生在
Chaquopy 初始化线程里，宿主既无法控制时机也无法控制耗时。

本文件用**独立子进程**验证（不用 monkeypatch 伪造，避免"测试环境里恰好没触发"）：
  1. 冷 import（只 `import server.state`）之后，数据目录**没有任何新文件/改动**；
  2. 显式调用 `load_persisted_state()` 之后，才发生装载（任务被标记中断、缓存进内存）；
  3. 该函数幂等：重复调用不会重复写盘、结果一致；
  4. `app` 导入同样不落盘（initialize 钩子才是写点）。
"""
import json
import os
import subprocess
import sys
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(code, data_dir):
    """在子进程里跑一段代码，返回 (stdout, returncode)；环境只给临时数据目录"""
    env = dict(os.environ)
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "WR_TEST": "1",
        "WR_DISABLE_BACKGROUND": "1",
        "WR_DATA_DIR": data_dir,
        "WR_SOURCES_DIR": os.path.join(data_dir, "sources"),
    })
    env.pop("WR_MOBILE_TOKEN", None)
    p = subprocess.run([sys.executable, "-c", textwrap.dedent(code)],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    return p.stdout.strip(), p.returncode, p.stderr.strip()


def _snapshot(root):
    out = {}
    for cur, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(cur, f)
            try:
                out[os.path.relpath(p, root)] = os.path.getmtime(p)
            except OSError:
                out[os.path.relpath(p, root)] = -1
    return out


def _seed_task(data_dir):
    tasks = os.path.join(data_dir, "tasks")
    os.makedirs(tasks, exist_ok=True)
    p = os.path.join(tasks, "timportcheck.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"id": "timportcheck", "source_uid": "s", "book_url": "https://e.com/b",
                   "book_key": "s_1", "status": "running", "created_at": "2026-01-01 00:00:00",
                   "finished_at": None, "progress": {"completed": 3, "total": 10},
                   "pause_requested": False, "log": []}, f, ensure_ascii=False)
    return p


def test_import_has_no_side_effects(tmp_path):
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    _seed_task(data_dir)

    before = _snapshot(data_dir)
    out, code, err = _run("import server.state", data_dir)
    assert code == 0, err
    after = _snapshot(data_dir)
    assert after == before, (
        "import server.state 改动了数据目录："
        f"{sorted(set(after.items()) ^ set(before.items()))}")

    # 任务文件内容也不能被动过（import 不得写盘）
    with open(os.path.join(data_dir, "tasks", "timportcheck.json"), encoding="utf-8") as f:
        assert json.load(f)["status"] == "running", "import 阶段不许改写任务状态"


def test_explicit_load_marks_and_is_idempotent(tmp_path):
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    task_p = _seed_task(data_dir)

    out, code, err = _run(
        """
        from server.state import load_persisted_state, _tasks
        print("tasks_before", len(_tasks))
        load_persisted_state()
        print("tasks_after", len(_tasks))
        print("status", _tasks["timportcheck"]["status"])
        print("kind", _tasks["timportcheck"].get("stop_kind"))
        import os, json
        m1 = os.path.getmtime(%r)
        load_persisted_state()          # 幂等：第二遍不该再写
        m2 = os.path.getmtime(%r)
        print("mtime_same", m1 == m2)
        """
        % (task_p, task_p), data_dir)
    assert code == 0, err
    assert "tasks_before 0" in out, out
    assert "tasks_after 1" in out, out
    assert "status stopped" in out, out
    assert "kind process_restart" in out, out
    assert "mtime_same True" in out, out


def test_app_import_does_not_touch_data(tmp_path):
    """导入 app（蓝图装配）也不许落盘：真正的写点只有 initialize 钩子"""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    _seed_task(data_dir)
    before = _snapshot(data_dir)
    out, code, err = _run("import app; print('ok')", data_dir)
    assert code == 0, err
    assert "ok" in out
    after = _snapshot(data_dir)
    assert after == before, "import app 改动了数据目录（应只在 initialize 钩子中装载）"


def test_load_hook_is_registered_on_runtime():
    """运行时必须真的注册了这个钩子，否则"显式装载"变成"永不装载"""
    import app as app_mod
    assert hasattr(app_mod, "_load_persisted_state")
    # app.py 装配的是它自己的 _RUNTIME（process-default 那个是给测试/嵌入方用的）
    hooks = [h[0] if isinstance(h, tuple) else getattr(h, "__name__", "?")
             for h in app_mod._RUNTIME._hooks]
    assert "load_persisted_state" in hooks, hooks
    assert "recover_tasks" in hooks, hooks
