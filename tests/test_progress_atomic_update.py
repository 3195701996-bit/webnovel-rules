# -*- coding: utf-8 -*-
"""A05 回归：阅读进度保存的事务性

修复前缺陷（server/novel_api.py / server/manga_api.py）：
1. _read_json 与 _write_json 分别加锁，读-改-写不在同一临界区——
   并发保存不同书籍/漫画的进度时后写整体覆盖先写，进度丢失。
2. _write_json 吞掉写盘异常仍返回 ok:true——磁盘写失败前端以为已同步。

本文件验证：
- update_json 在同一临界区完成 读-改-原子替换，并发不互相覆盖；
- mutator 抛异常时不写盘、异常向上传播；
- 两个进度端点写盘失败时返回非成功响应（500 + ok:false）。
"""
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.app_utils as AU  # noqa: E402
from engine.app_utils import update_json, _read_json  # noqa: E402


# ── update_json 单元行为 ─────────────────────────────────────

def test_update_json_creates_and_returns(tmp_path):
    p = str(tmp_path / "prog.json")
    out = update_json(p, lambda d: {**(d or {}), "a": {"idx": 1}}, {})
    assert out == {"a": {"idx": 1}}
    assert _read_json(p, None) == {"a": {"idx": 1}}


def test_update_json_mutator_error_no_write(tmp_path):
    """mutator 抛异常：不落盘、异常原样传播"""
    p = tmp_path / "prog.json"
    p.write_text(json.dumps({"old": 1}), encoding="utf-8")

    def _boom(d):
        raise ValueError("bad mutator")

    with pytest.raises(ValueError):
        update_json(str(p), _boom, {})
    assert _read_json(str(p), None) == {"old": 1}   # 原内容未被破坏


def test_update_json_write_failure_propagates(tmp_path, monkeypatch):
    """写盘失败必须向调用者传播（与 _write_json 的吞异常语义相反）"""
    def _bad_write(path, data):
        raise OSError("disk full")
    monkeypatch.setattr(AU, "atomic_write", _bad_write)
    with pytest.raises(OSError):
        update_json(str(tmp_path / "p.json"), lambda d: {"x": 1}, {})


# ── 并发：不同键的进度必须都保留 ─────────────────────────────

def test_update_json_concurrent_distinct_keys_no_lost_update(tmp_path):
    """修复前：读、写分别加锁，两个线程各读旧值→各自合并→后写覆盖先写。
    8 线程 × 25 次各自写自己的键，最终 8 个键必须全部在。"""
    p = str(tmp_path / "prog.json")
    n_threads, n_iters = 8, 25
    barrier = threading.Barrier(n_threads)
    errors = []

    def worker(tid):
        key = f"book_{tid}"
        try:
            barrier.wait(timeout=10)
            for i in range(n_iters):
                def _merge(d, key=key, i=i):
                    d = d or {}
                    d[key] = {"idx": i}
                    return d
                update_json(p, _merge, {})
        except Exception as e:      # 线程内异常要带回主线程断言
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"线程内异常: {errors[:3]}"
    final = _read_json(p, None)
    assert final is not None
    for tid in range(n_threads):
        assert f"book_{tid}" in final, f"book_{tid} 被并发覆盖丢失"
        assert final[f"book_{tid}"]["idx"] == n_iters - 1


# ── 端点：写盘失败返回非成功 ─────────────────────────────────

@pytest.fixture(scope="module")
def client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


def test_novel_progress_save_ok(client, tmp_path, monkeypatch):
    import server.novel_api as NA
    monkeypatch.setattr(NA, "BOOK_PROGRESS_FILE", str(tmp_path / "bp.json"))
    r = client.post("/api/books/src_abc123/progress",
                    json={"idx": 3, "pct": 42, "name": "第三章"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    saved = _read_json(str(tmp_path / "bp.json"), {})
    assert saved["src_abc123"]["idx"] == 3
    assert saved["src_abc123"]["pct"] == 42


def test_novel_progress_write_failure_returns_500(client, tmp_path, monkeypatch):
    """磁盘写失败：不得返回 ok:true（修复前端面以为已同步的静默丢失）"""
    import server.novel_api as NA
    prog = tmp_path / "bp.json"
    prog.write_text(json.dumps({"src_abc123": {"idx": 9}}), encoding="utf-8")
    monkeypatch.setattr(NA, "BOOK_PROGRESS_FILE", str(prog))

    def _bad_write(path, data):
        # 异常消息携带服务器绝对路径（真实 OSError 的典型形态）
        raise OSError(28, "No space left on device", str(tmp_path / "bp.json"))
    monkeypatch.setattr(AU, "atomic_write", _bad_write)

    r = client.post("/api/books/src_abc123/progress",
                    json={"idx": 10, "pct": 1})
    assert r.status_code == 500
    assert r.get_json()["ok"] is False
    # 原文件未被破坏
    assert _read_json(str(prog), {})["src_abc123"]["idx"] == 9


def test_novel_progress_write_failure_sanitized(client, tmp_path, monkeypatch):
    """A05/P2-8 脱敏：错误响应只含通用文案，不泄露原始异常与服务器路径"""
    import server.novel_api as NA
    monkeypatch.setattr(NA, "BOOK_PROGRESS_FILE", str(tmp_path / "bp.json"))

    def _bad_write(path, data):
        raise OSError(28, "No space left on device", str(tmp_path / "bp.json"))
    monkeypatch.setattr(AU, "atomic_write", _bad_write)

    r = client.post("/api/books/src_abc123/progress",
                    json={"idx": 10, "pct": 1})
    assert r.status_code == 500
    body = r.get_json()
    assert body["ok"] is False
    assert body["error"] == "进度保存失败，请稍后重试"   # 稳定通用文案
    leaked = r.get_data(as_text=True)
    assert str(tmp_path) not in leaked, "错误响应泄露服务器绝对路径"
    assert "No space left on device" not in leaked, "错误响应泄露原始异常消息"
    assert "OSError" not in leaked, "错误响应泄露异常类型名"


def test_manga_history_write_failure_returns_500(client, tmp_path, monkeypatch):
    import server.manga_api as MA
    hist = tmp_path / "hist.json"
    hist.write_text(json.dumps({"jm:1": {"idx": 5}}), encoding="utf-8")
    monkeypatch.setattr(MA, "MANGA_HISTORY_FILE", str(hist))

    def _bad_write(path, data):
        raise OSError(28, "No space left on device", str(tmp_path / "hist.json"))
    monkeypatch.setattr(AU, "atomic_write", _bad_write)

    r = client.post("/api/manga/history",
                    json={"source": "jm", "comic_id": "1", "idx": 6})
    assert r.status_code == 500
    body = r.get_json()
    assert body["ok"] is False
    # A05/P2-8 脱敏：通用文案，不泄露原始异常与服务器路径
    assert body["error"] == "进度保存失败，请稍后重试"
    leaked = r.get_data(as_text=True)
    assert str(tmp_path) not in leaked, "错误响应泄露服务器绝对路径"
    assert "No space left on device" not in leaked, "错误响应泄露原始异常消息"
    assert _read_json(str(hist), {})["jm:1"]["idx"] == 5


def test_manga_history_concurrent_saves_both_kept(client, tmp_path, monkeypatch):
    """端点级并发：两部漫画同时存进度，两条都必须保留"""
    import server.manga_api as MA
    monkeypatch.setattr(MA, "MANGA_HISTORY_FILE", str(tmp_path / "hist.json"))
    barrier = threading.Barrier(2)
    results = []

    def save(cid):
        barrier.wait(timeout=10)
        r = client.post("/api/manga/history",
                        json={"source": "jm", "comic_id": cid, "idx": 1})
        results.append(r.status_code)

    ts = [threading.Thread(target=save, args=(c,)) for c in ("100", "200")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert results == [200, 200]
    hist = _read_json(str(tmp_path / "hist.json"), {})
    assert "jm:100" in hist and "jm:200" in hist
