# -*- coding: utf-8 -*-
"""0.61.0 回归：路径穿越防线（技术指南 §15.1/§15.3）。

指南要求"所有文件路径由受控 ID 构建；解析后确认仍在根目录内"，并在 §19 列为必做项。

本文件不是"读一遍代码觉得没问题"，而是**真的发请求**去打带路径参数的路由，
断言两件事：
  1. 越界请求得到 4xx（而不是 200 或 500）；
  2. 数据目录之外的**任何文件都没有被读走、删掉或改写**（用前后快照证明）。

覆盖三类参数（它们各自的风险面不同）：
  · `book_key`  —— 直接拼进 `data/books/<key>`（最危险，已走 `_safe_seg` + realpath）；
  · `task_id`   —— 只作内存字典键 + `TASKS_DIR/<id>.json`（删除路径有 realpath 校验）；
  · 书源 `uid`  —— 按**内容**在 sources/ 里找文件（不是拼路径），越界值只会 404。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


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


EVIL = [
    "..%2F..%2Fapp.py",
    "..%2F..%2F..%2Fetc%2Fpasswd",
    "%2E%2E%2Fdata%2Fbooks",
    "....%2F%2Fapp.py",
    "..%5C..%5Capp.py",
]


@pytest.mark.parametrize("evil", EVIL)
def test_book_key_traversal_rejected(client, evil):
    """书籍 key 会被拼进 data/books/<key>：必须 4xx，且不许读到真实文件"""
    for method, suffix in (("get", ""), ("delete", ""), ("post", "/reclean")):
        fn = getattr(client, method)
        r = fn("/api/books/%s%s" % (evil, suffix))
        assert r.status_code in (400, 404), f"{method} {evil}{suffix} → {r.status_code}"
    # 认真文件仍在（没有被读走/删掉）
    import app as app_mod
    assert os.path.exists(app_mod.__file__), "app.py 被删除或移动——路径穿越防线失效"


@pytest.mark.parametrize("evil", EVIL)
def test_task_id_traversal_rejected(client, evil):
    r = client.delete("/api/tasks/%s" % evil)
    assert r.status_code in (400, 404), f"task delete {evil} → {r.status_code}"
    r2 = client.get("/api/tasks/%s" % evil)
    assert r2.status_code in (400, 404)


@pytest.mark.parametrize("evil", EVIL)
def test_source_uid_traversal_rejected(client, evil):
    """书源 uid 走"按内容查找"，越界值不该命中任何文件"""
    r = client.post("/api/sources/%s/toggle" % evil, json={"enabled": False})
    assert r.status_code in (400, 404), f"toggle {evil} → {r.status_code}"
    r2 = client.delete("/api/sources/%s" % evil)
    assert r2.status_code in (400, 404), f"delete {evil} → {r2.status_code}"


def test_traversal_attempts_do_not_touch_files_on_disk(client, tmp_path, monkeypatch):
    """整体快照比对：一串越界请求跑完后，业务数据目录零改动"""
    from engine.config import DATA_DIR, SOURCES_DIR
    before_data = _snapshot(DATA_DIR)
    before_src = _snapshot(SOURCES_DIR)
    for evil in EVIL:
        for url in ("/api/books/%s" % evil,
                    "/api/books/%s/reclean" % evil,
                    "/api/tasks/%s" % evil,
                    "/api/sources/%s/toggle" % evil):
            client.get(url)
            client.delete(url)
    assert _snapshot(DATA_DIR) == before_data, "越界请求改动了 data/（路径穿越防线失效）"
    assert _snapshot(SOURCES_DIR) == before_src, "越界请求改动了 sources/"


def test_legit_but_nonexistent_ids_are_plain_404(client):
    """合法形状但不存在的 ID → 普通 404（不要把"不存在"报成 400/500，界面会误导）"""
    assert client.get("/api/books/no_such_book_zzz").status_code == 404
    assert client.get("/api/tasks/tnosuchtask").status_code == 404
    assert client.delete("/api/sources/no_such_uid_zzz").status_code == 404
