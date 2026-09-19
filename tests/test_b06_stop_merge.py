# -*- coding: utf-8 -*-
"""B06 回归：小说爬取的停止响应与断点检查低效

修复前缺陷：
1. engine/crawler.py 断点检查用 completed 列表做 membership（O(n) 逐章扫）。
2. 下载循环用 as_completed 阻塞等待，期间停止请求完全不可见；
   停止时也不区分"停止中"（在飞请求收尾）与"已停止"（终态）。
3. server/state.py 在 crawl 返回后无条件 merge_txt——暂停/停止也做
   全量合并，重复暂停重复合并。

本文件验证：
- 停止请求后不补发新章（慢假章节模拟），已完成缓存仍可续爬；
- 进度状态区分 stopping（停止中）与 stopped（已停止）；
- 暂停/停止路径不触发 merge_txt（计数断言），完成路径才合并，
  重复暂停不重复合并；
- merge_txt 内容 revision 守卫：内容未变跳过全量合并，新章写入后
  重新合并，章节顺序与导出内容不变；
- completed 持久化格式保持 list 不变。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.crawler import CrawlTask  # noqa: E402
from engine.config import ST_PAUSED, ST_STOPPED, ST_DONE, ST_RUNNING  # noqa: E402

_SRC = {
    "bookSourceName": "B06假源",
    "bookSourceUrl": "https://b06.example.com",
    "uid": "b06_fake",
    "concurrentRate": "2",
}

_BOOK_URL = "https://b06.example.com/book/1"


def _write_state(book_dir, n=6):
    os.makedirs(book_dir, exist_ok=True)
    chapters = [{"name": f"第{i}章", "url": f"{_BOOK_URL}/c{i}.html"}
                for i in range(1, n + 1)]
    state = {
        "book": {"book_url": _BOOK_URL, "name": "B06测试书"},
        "chapters": chapters,
        "completed": [],
        "failed": {},
        "updated_at": "2026-01-01 00:00:00",
    }
    with open(os.path.join(book_dir, "_state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f)
    return chapters


class _SlowCrawler:
    """慢假章节：每章 delay 秒，记录所有被请求过的 url"""
    def __init__(self, submitted, delay=0.25):
        self.submitted = submitted
        self.delay = delay

    def get_content(self, url, **kw):
        self.submitted.append(url)
        time.sleep(self.delay)
        return f"第{url}正文，足够长的内容以区分指纹。" * 20


# ── 停止响应：不补发新章 + stopping/stopped 状态区分 + 断点续爬 ──

def test_stop_no_new_chapters_and_resume(tmp_path):
    book_dir = str(tmp_path / "book1")
    chapters = _write_state(book_dir, n=6)

    submitted1 = []
    payloads = []
    task = CrawlTask(_SRC, _BOOK_URL, book_dir, resume=True,
                     progress_callback=payloads.append,
                     stop_callback=lambda: any(
                         (p.get("completed") or 0) >= 1 for p in payloads))
    task.crawler = _SlowCrawler(submitted1, delay=0.25)
    t0 = time.time()
    task.crawl()
    elapsed = time.time() - t0

    # 停止请求后未补发新章：6 章只提交了最初在飞的 2 章
    assert len(submitted1) == 2, f"停止后补发了新章: {submitted1}"
    # 停止响应及时：2 章 × 0.25s 即应退出（不等待整书）
    assert elapsed < 2.0, f"停止响应过慢: {elapsed:.2f}s"
    # 状态区分：停止中（在飞收尾）→ 已停止（终态）
    statuses = [p.get("status") for p in payloads]
    assert "stopping" in statuses, f"缺少'停止中'状态: {statuses}"
    assert statuses[-1] == "stopped", f"终态不是'已停止': {statuses[-1]}"
    assert statuses.index("stopping") < len(statuses) - 1

    # 断点持久化：已完成章进 _state.json，且 completed 仍是 list（格式不变）。
    # 停止时收下已运行且成功返回的请求，避免续爬浪费同一份网络工作。
    with open(os.path.join(book_dir, "_state.json"), encoding="utf-8") as f:
        st = json.load(f)
    assert isinstance(st["completed"], list)
    assert set(st["completed"]) == set(submitted1)
    # 运行时 set 镜像与 list 一致
    assert task._completed_set == set(task.completed)

    # 续爬：已落盘/已缓存章节不重下，剩余章节补齐，终态 done
    submitted2 = []
    payloads2 = []
    task2 = CrawlTask(_SRC, _BOOK_URL, book_dir, resume=True,
                      progress_callback=payloads2.append,
                      stop_callback=lambda: False)
    task2.crawler = _SlowCrawler(submitted2, delay=0.05)
    task2.crawl()
    assert not (set(submitted2) & set(st["completed"])), \
        f"已缓存章节被重下: {set(submitted2) & set(st['completed'])}"
    assert set(submitted2) | set(submitted1) == {c["url"] for c in chapters}
    assert payloads2[-1].get("status") == "done"
    assert len(task2.completed) == 6


# ── merge_txt 内容 revision 延迟合并 ──

def test_merge_txt_revision_guard_and_order(tmp_path):
    book_dir = str(tmp_path / "book2")
    chapters = _write_state(book_dir, n=3)
    task = CrawlTask(_SRC, _BOOK_URL, book_dir, resume=True)
    assert task._load_state()   # 只载入目录/断点，不走网络爬取

    # 写入前两章缓存（模拟爬取写入，内容 revision 递增）
    task._save_cache(chapters[0]["url"],
                     f"{chapters[0]['name']}\n\n第一章正文内容，足够长以区分。" * 10)
    task._save_cache(chapters[1]["url"],
                     f"{chapters[1]['name']}\n\n第二章正文内容，与第一章不同。" * 10)

    out1 = task.merge_txt()
    assert out1 and os.path.exists(out1)
    with open(out1, encoding="utf-8") as f:
        txt1 = f.read()
    assert txt1.index("第一章正文") < txt1.index("第二章正文")  # 章节顺序不变
    mtime1 = os.path.getmtime(out1)

    # 内容未变 → 重复合并被 revision 守卫跳过（不重新写盘）
    out2 = task.merge_txt()
    assert out2 == out1
    assert os.path.getmtime(out1) == mtime1

    # 新章写入 → revision 变化 → 重新合并且包含新章
    task._save_cache(chapters[2]["url"],
                     f"{chapters[2]['name']}\n\n第三章新写入的正文内容。" * 10)
    out3 = task.merge_txt()
    with open(out3, encoding="utf-8") as f:
        txt3 = f.read()
    assert "第三章新写入的正文" in txt3
    assert txt3.index("第二章正文") < txt3.index("第三章新写入")


# ── state._run_task：仅完成路径触发 merge_txt ──

def _run_fake_task(monkeypatch, tmp_path, tid, *, pause=False, stop=False,
                   merge_counter):
    import server.state as state

    class _FakeTask:
        def __init__(self, source, book_url, book_dir, resume=True,
                     progress_callback=None, stop_callback=None):
            self._progress = progress_callback or (lambda p: None)

        def crawl(self, preset_chapters=None):
            if stop:
                self._progress({"status": "stopped", "completed": 0})
            else:
                self._progress({"status": "running", "completed": 1})

        def merge_txt(self):
            merge_counter.append(tid)

    monkeypatch.setattr(state, "CrawlTask", _FakeTask)
    monkeypatch.setattr(state, "get_by_uid", lambda uid: dict(_SRC))

    book_key = f"b06_{tid}"
    book_dir = os.path.join(state.BOOKS_DIR, book_key)
    os.makedirs(book_dir, exist_ok=True)
    with state._lock:
        state._tasks[tid] = {
            "id": tid, "source_uid": _SRC["uid"], "book_url": _BOOK_URL,
            "book_key": book_key, "status": ST_RUNNING,
            "created_at": "2026-01-01T00:00:00", "finished_at": None,
            "progress": None, "pause_requested": pause, "log": [],
        }
    try:
        state._run_task(tid)
        with state._lock:
            return dict(state._tasks[tid])
    finally:
        with state._lock:
            state._tasks.pop(tid, None)
            state._crawlers.pop(tid, None)


def test_run_task_merge_only_on_done(monkeypatch, tmp_path):
    merge_counter = []

    # 暂停路径：不触发 merge_txt
    t = _run_fake_task(monkeypatch, tmp_path, "b06_pause1", pause=True,
                       merge_counter=merge_counter)
    assert t["status"] == ST_PAUSED
    assert merge_counter == [], f"暂停路径触发了 merge_txt: {merge_counter}"

    # 重复暂停：仍不合并
    t = _run_fake_task(monkeypatch, tmp_path, "b06_pause2", pause=True,
                       merge_counter=merge_counter)
    assert t["status"] == ST_PAUSED
    assert merge_counter == [], f"重复暂停触发了 merge_txt: {merge_counter}"

    # 停止路径：不触发 merge_txt
    t = _run_fake_task(monkeypatch, tmp_path, "b06_stop1", stop=True,
                       merge_counter=merge_counter)
    assert t["status"] == ST_STOPPED
    assert merge_counter == [], f"停止路径触发了 merge_txt: {merge_counter}"

    # 完成路径：恰好合并一次
    t = _run_fake_task(monkeypatch, tmp_path, "b06_done1",
                       merge_counter=merge_counter)
    assert t["status"] == ST_DONE
    assert merge_counter == ["b06_done1"], \
        f"完成路径应恰好合并一次: {merge_counter}"


def test_stop_drains_successful_inflight_results(tmp_path):
    import threading

    book_dir = str(tmp_path / "drain")
    chapters = _write_state(book_dir, n=4)
    started = threading.Event()
    release = threading.Event()
    barrier = threading.Barrier(2)
    submitted = []
    payloads = []

    class GatedCrawler:
        def get_content(self, url):
            submitted.append(url)
            barrier.wait(timeout=3)
            started.set()
            assert release.wait(3)
            return "completed body: " + url

    def progress(payload):
        payloads.append(payload)
        if payload.get("status") == "stopping":
            release.set()

    task = CrawlTask(_SRC, _BOOK_URL, book_dir, resume=True,
                     progress_callback=progress,
                     stop_callback=started.is_set)
    task.crawler = GatedCrawler()
    try:
        task.crawl()
    finally:
        release.set()
    assert set(submitted) == {c["url"] for c in chapters[:2]}
    assert set(task.completed) == set(submitted)
    assert all(task._has_cache(url) for url in submitted)
    statuses = [p["status"] for p in payloads]
    stop_at = statuses.index("stopping")
    assert "running" not in statuses[stop_at:]
    assert statuses[-1] == "stopped"


def test_already_stopped_does_not_submit(tmp_path):
    book_dir = str(tmp_path / "already_stopped")
    _write_state(book_dir, n=4)
    submitted = []
    task = CrawlTask(_SRC, _BOOK_URL, book_dir, resume=True,
                     stop_callback=lambda: True)
    task.crawler = _SlowCrawler(submitted, delay=0)
    task.crawl()
    assert submitted == []


def test_retry_without_progress_does_not_repeat_checkpoint(tmp_path, monkeypatch):
    import engine.crawler as crawler

    book_dir = str(tmp_path / "retry_checkpoints")
    _write_state(book_dir, n=1)
    calls = []
    saves = []
    task = CrawlTask(_SRC, _BOOK_URL, book_dir, resume=True)

    def fetch(url):
        calls.append(url)
        if len(calls) < 3:
            raise ConnectionError("temporary")
        return "body after retry"

    save = task._save_state

    def save_counted():
        saves.append(len(task.completed))
        save()

    monkeypatch.setattr(crawler, "CRAWL_RETRY_DELAY", 0)
    monkeypatch.setattr(task.crawler, "get_content", fetch)
    monkeypatch.setattr(task, "_save_state", save_counted)
    task.crawl()
    assert len(calls) == 3
    assert saves == [1]


def test_checkpoint_crossing_batch_boundary(tmp_path, monkeypatch):
    import concurrent.futures as cf

    book_dir = str(tmp_path / "batch_checkpoints")
    _write_state(book_dir, n=24)
    task = CrawlTask(dict(_SRC, concurrentRate="4"), _BOOK_URL,
                     book_dir, resume=True)
    task.crawler = _SlowCrawler([], delay=0)
    saves = []
    save = task._save_state

    def save_counted():
        saves.append(len(task.completed))
        save()

    def wait_batch(futures, **kwargs):
        batch = futures[:3]
        for future in batch:
            future.result(timeout=3)
        return set(batch), set(futures[3:])

    monkeypatch.setattr(cf, "wait", wait_batch)
    monkeypatch.setattr(task, "_save_state", save_counted)
    task.crawl()
    assert saves == [21, 24]
