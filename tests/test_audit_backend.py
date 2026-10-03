# -*- coding: utf-8 -*-
"""后端并发边界 / 契约一致性回归（离线，确定性）

覆盖本轮确认的收敛项：
1. cleaner._L5_replace 的 ##pat##repl## 与 rules.RuleEngine.get_string 保持
   同一 extract-first 契约（命中只保留匹配、无命中空串、普通 ##pat##repl 全文替换）；
2. manga 章节修复的单飞锁：lookup+acquire 与 release+pop 同 guard，互斥成立；
3. DownloadManager.save：深快照（锁内）→ 写盘（锁外）不被嵌套结构原地修改污染；
   过期序号快照不得覆盖新快照；
4. 一键检查 worker：executor 构建 / submit / shutdown / running 复位在最外层
   finally 内；路由 Thread.start 失败必须复位 running；
5. _mcache 以 (source, comic_id) 为键，写入与"补充下载"读取两侧一致；
6. 代理延迟读快照 + 同一代理 http/https 计数去重；
7. 收藏（update_json）并发读改写不丢更新、写失败脱敏；
8. cache_path 拒绝非法章节 id 与符号链接越界。

全部离线、无外网、不触碰真实用户数据（conftest 已隔离 DATA_DIR/SOURCES_DIR）。
"""
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


@pytest.fixture(autouse=True)
def _reset_shared_state():
    """隔离模块级共享状态，避免用例间相互污染。"""
    import server.state as st
    import server.novel_api as napi
    st._manga_check_state.update(running=False, total=0, started_at=0.0,
                                 finished_at=0.0)
    st._manga_check_state["results"] = []
    st._mcache.clear()
    napi._src_check_state.update(running=False, total=0, started_at=0.0,
                                 finished_at=0.0)
    napi._src_check_state["results"] = []
    napi._check_jobs.clear()
    yield
    st._manga_check_state.update(running=False, total=0)
    st._manga_check_state["results"] = []
    st._mcache.clear()
    napi._src_check_state.update(running=False, total=0)
    napi._src_check_state["results"] = []
    napi._check_jobs.clear()


# ══════════════════════════════════════════════════════════════
# 1. cleaner / rules 的 ## 清洗契约一致（extract-first）
# ══════════════════════════════════════════════════════════════

_3HASH_RULE = r"§##第(\d+)章##第$1章##"


def test_replace_first_hit_keeps_only_match():
    from engine.cleaner import _L5_replace
    # ##pat##repl##：只保留（加工后的）第一处匹配，丢弃其余全文
    assert _L5_replace("正文第123章内容", r"##第(\d+)章##第$1章##") == "第123章"


def test_replace_first_miss_returns_empty():
    from engine.cleaner import _L5_replace
    # 未命中 → 空串（不是原样返回整段正文）
    assert _L5_replace("完全无关的一段话", r"##第(\d+)章##第$1章##") == ""


def test_plain_global_replace_keeps_whole_text():
    from engine.cleaner import _L5_replace
    # 单个 ##（非 replaceFirst）：全文所有匹配都被替换，非只处理第一处
    assert _L5_replace("a1b2c3", r"##(\d)##<$1>") == "a<1>b<2>c<3>"
    # 单 ## 无替换串 = 删除全文所有匹配
    assert _L5_replace("abc123def456", r"##\d+") == "abcdef"


def test_cleaner_matches_rule_engine_get_string(monkeypatch):
    """cleaner 与 RuleEngine.get_string 对同一 ##pat##repl## 必须同解。"""
    import engine.rules as rules
    from engine.cleaner import _L5_replace

    # 让取值链原样返回 result，从而只校验 ## 清洗段的语义
    monkeypatch.setattr(rules, "execute_chain",
                        lambda cand, result, ctx: result)
    eng = rules.RuleEngine()

    text = "前缀第7章后缀第9章尾巴"
    rule = r"§##第(\d+)章##第$1章##"
    assert eng.get_string(rule, text) == _L5_replace(text, rule) == "第7章"

    # 无命中：两侧都为空
    assert eng.get_string(rule, "没有章节") == _L5_replace("没有章节", rule) == ""


# ══════════════════════════════════════════════════════════════
# 2. 章节修复单飞锁：互斥
# ══════════════════════════════════════════════════════════════

class _CountingAdapter:
    """images() 内计数并发进入次数——单飞锁有效时恒为 1。"""

    def __init__(self):
        self._lk = threading.Lock()
        self.cur = 0
        self.max = 0

    def images(self, comic_id, chapter_id):
        with self._lk:
            self.cur += 1
            self.max = max(self.max, self.cur)
        time.sleep(0.12)          # 拉宽临界区，逼出并发
        with self._lk:
            self.cur -= 1
        return []                 # 空清单 → 路由安全返回 500，不进入取图


def test_repair_single_flight_mutual_exclusion(monkeypatch, client):
    import app
    import engine.manga.manager as mgr
    import server.manga_api as mapi

    fake = _CountingAdapter()
    monkeypatch.setattr(mgr, "get_adapter", lambda key, **kw: fake)
    monkeypatch.setattr(mapi._manga_dl, "status",
                        lambda key: {"status": "none"})

    n = 8
    barrier = threading.Barrier(n)
    codes = []
    codes_lock = threading.Lock()
    url = "/api/manga/copymanga/c1/chapter/ch1/repair"

    def hit():
        c = app.app.test_client()
        barrier.wait()
        r = c.post(url, json={"overwrite": True})
        with codes_lock:
            codes.append(r.status_code)

    ts = [threading.Thread(target=hit) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    # 任一时刻最多一个线程在修复临界区（旧实现回收竞态会让 max 达到 2）
    assert fake.max == 1
    # 未拿到锁的并发请求应被 409 拒绝，而不是排队进入
    assert codes.count(409) >= 1


# ══════════════════════════════════════════════════════════════
# 3. DownloadManager.save：深快照 + 过期序号丢弃 + 并发不损坏
# ══════════════════════════════════════════════════════════════

def test_save_deep_snapshot_isolates_nested_mutation(tmp_path, monkeypatch):
    import engine.manga.download_manager as dmmod

    dm = dmmod.DownloadManager(state_file=str(tmp_path / "_tasks.json"))
    dm._tasks = {"k": {"status": "running", "chapters": [{"id": "a"}]}}

    captured = {}
    monkeypatch.setattr(dmmod, "atomic_write",
                        lambda path, data: captured.update(data))
    dm.save()
    # 写盘成功后，worker 原地改嵌套结构，不得影响已落盘快照
    dm._tasks["k"]["chapters"].append({"id": "b"})
    assert captured["k"]["chapters"] == [{"id": "a"}]


def test_save_drops_stale_snapshot(tmp_path, monkeypatch):
    import engine.manga.download_manager as dmmod

    dm = dmmod.DownloadManager(state_file=str(tmp_path / "_tasks.json"))
    dm._tasks = {"k": {"status": "running"}}
    dm._save_written = 5        # 已写入更高序号
    dm._save_seq = 0            # 本次快照序号将成为 1 < 5 → 必须丢弃
    called = []
    monkeypatch.setattr(dmmod, "atomic_write",
                        lambda path, data: called.append(data))
    dm.save()
    assert called == []


def test_save_concurrent_threads_no_corruption(tmp_path, monkeypatch):
    import engine.manga.download_manager as dmmod

    p = tmp_path / "_tasks.json"
    dm = dmmod.DownloadManager(state_file=str(p))
    dm._tasks = {"k": {"status": "running"}}
    errors = []

    def worker(n):
        try:
            for i in range(40):
                with dm._lock:
                    dm._tasks["k"] = {
                        "status": "running", "n": n, "i": i,
                        "chapters": [{"id": f"{n}-{j}"} for j in range(i)],
                    }
                dm.save()
        except Exception as e:      # noqa: BLE001 - 汇总线程异常供断言
            errors.append(e)

    ts = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert errors == []
    # 终态一致：所有快照都被记账
    assert dm._save_written == dm._save_seq
    # 文件是完整可解析 JSON，且无 .tmp 残留
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["k"]["status"] == "running"
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_media_delete_reservation_blocks_download_start_and_resume():
    from engine.manga.download_manager import DownloadManager

    dm = DownloadManager()
    key = "copymanga_web:comic-reserved"
    assert dm.begin_media_delete(key)
    assert dm.start("copymanga_web", "comic-reserved", "作品") == (key, "deleting")
    assert dm.resume(key) is False
    assert dm.resume_result(key) == "deleting"
    assert dm.begin_media_delete(key) is False
    dm.end_media_delete(key)
    assert dm.begin_media_delete(key)
    dm.end_media_delete(key)


def test_resume_returns_false_for_missing_or_terminal_task(monkeypatch):
    from engine.manga.download_manager import DownloadManager

    dm = DownloadManager()
    monkeypatch.setattr(dm, "_kick_workers", lambda: None)
    assert dm.resume("missing") is False
    assert dm.resume_result("missing") == "no_task"
    dm._tasks["done"] = {"status": "done"}
    assert dm.resume("done") is False
    assert dm.resume_result("done") == "not_resumable"
    dm._tasks["paused"] = {"status": "paused"}
    assert dm.resume("paused") is True
    assert dm.resume_result("paused") == "resumed"
    dm._tasks["running"] = {"status": "running"}
    assert dm.resume_result("running") == "resumed"


# ══════════════════════════════════════════════════════════════
# 4. 一键检查 worker / 路由：异常路径必须复位 running
# ══════════════════════════════════════════════════════════════

def test_src_check_worker_resets_running_on_executor_failure(monkeypatch):
    import concurrent.futures as cf
    import server.novel_api as napi

    def boom(*a, **kw):
        raise RuntimeError("cannot create threads")

    monkeypatch.setattr(cf, "ThreadPoolExecutor", boom)
    napi._src_check_state["running"] = True
    # 构建异常会穿出 worker（线程目标即在此终止），但最外层 finally 必须先复位
    with pytest.raises(RuntimeError):
        napi._src_check_worker([{"uid": "u1", "bookSourceName": "s1"}])
    assert napi._src_check_state["running"] is False


def test_manga_check_worker_resets_running_on_executor_failure(monkeypatch):
    import concurrent.futures as cf
    import server.state as st

    def boom(*a, **kw):
        raise RuntimeError("cannot create threads")

    monkeypatch.setattr(cf, "ThreadPoolExecutor", boom)
    st._manga_check_state["running"] = True
    # 构建异常会穿出 worker，但最外层 finally 必须先复位 running
    with pytest.raises(RuntimeError):
        st._manga_check_worker([("copymanga", "c1", "t1")])
    assert st._manga_check_state["running"] is False


class _BoomThread:
    def __init__(self, *a, **kw):
        pass

    def start(self):
        raise RuntimeError("thread start failed")


def test_sources_check_all_resets_running_on_thread_start_failure(
        monkeypatch, client):
    import server.novel_api as napi
    monkeypatch.setattr(napi, "load_enabled",
                        lambda: [{"uid": "u1", "bookSourceName": "s1"}])
    monkeypatch.setattr(napi.threading, "Thread", _BoomThread)
    r = client.post("/api/sources/check-all")
    assert r.status_code == 500
    assert napi._src_check_state["running"] is False


def test_manga_check_updates_resets_running_on_thread_start_failure(
        monkeypatch, client, tmp_path):
    import server.manga_api as mapi
    import server.state as st

    lib = [{"source": "copymanga", "comic_id": "c9", "title": "t",
            "status": "done"}]
    lp = tmp_path / "_library.json"
    lp.write_text(json.dumps(lib), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_LIBRARY_FILE", str(lp))
    monkeypatch.setattr(mapi._manga_dl, "status",
                        lambda key: {"status": "none"})
    monkeypatch.setattr(mapi.threading, "Thread", _BoomThread)

    r = client.post("/api/manga/library/check-updates")
    assert r.status_code == 500
    assert st._manga_check_state["running"] is False


def test_manga_check_updates_resets_running_on_status_exception(
        monkeypatch, client, tmp_path):
    """items 准备阶段的 _manga_dl.status 抛错也必须复位 running。

    此前 items 准备在 try 之外 → 异常直接泄出、running 永久 True，
    一键检查从此恒定 409。
    """
    import server.manga_api as mapi
    import server.state as st

    lib = [{"source": "copymanga", "comic_id": "c9", "title": "t",
            "status": "done"}]
    lp = tmp_path / "_library.json"
    lp.write_text(json.dumps(lib), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_LIBRARY_FILE", str(lp))

    def boom(key):
        raise RuntimeError("/Users/secret/_manga_tasks.json: corrupt")

    monkeypatch.setattr(mapi._manga_dl, "status", boom)

    r = client.post("/api/manga/library/check-updates")
    assert r.status_code == 500
    assert st._manga_check_state["running"] is False
    # 脱敏：异常原文（本地路径）不得回显
    assert "secret" not in json.dumps(r.get_json())


class _CountingThread:
    """记录启动次数、不执行 target（使 job 停在 checking 便于观察单飞）。"""
    started = []

    def __init__(self, *a, **kw):
        pass

    def start(self):
        type(self).started.append(1)


def test_book_check_update_single_flight(monkeypatch):
    """判定与登记同锁：并发 N 个请求只有 1 个真正启动 worker。"""
    import app as app_mod
    import server.novel_api as napi

    # napi.threading 即全局 threading 模块，patch 其 Thread 会连带影响本测试
    # 自身；先留真实引用，仅让路由侧的 Thread 走计数假件。
    real_thread = threading.Thread
    monkeypatch.setattr(napi, "load_book_state",
                        lambda p: {"book": {"book_url": "http://x/1",
                                            "source_uid": "u"}})
    monkeypatch.setattr(napi.threading, "Thread", _CountingThread)
    _CountingThread.started = []

    n = 8
    barrier = threading.Barrier(n)
    out = []
    lock = threading.Lock()

    def fire():
        c = app_mod.app.test_client()
        barrier.wait()
        r = c.post("/api/books/b1/check-update")
        with lock:
            out.append((r.status_code, (r.get_json() or {}).get("message", "")))

    ts = [real_thread(target=fire) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert len(_CountingThread.started) == 1          # 仅一个 worker 启动
    assert {s for s, _ in out} == {202}
    assert sum(1 for _, m in out if m == "已有检查在进行中") == n - 1


def test_book_check_update_start_failure_sets_error_and_retryable(monkeypatch):
    """Thread.start 失败：job 从 checking 落 error（脱敏）且可重试。"""
    import app as app_mod
    import server.novel_api as napi

    monkeypatch.setattr(napi, "load_book_state",
                        lambda p: {"book": {"book_url": "http://x/1",
                                            "source_uid": "u"}})
    c = app_mod.app.test_client()

    # 第一次：线程启动失败
    monkeypatch.setattr(napi.threading, "Thread", _BoomThread)
    r = c.post("/api/books/b1/check-update")
    assert r.status_code == 500
    # 若不置 error，永久停在 checking → 后续请求恒 202 无法重试
    assert napi._check_jobs["b1"]["status"] == "error"
    assert "thread start failed" not in json.dumps(r.get_json())

    # 第二次：线程可正常启动 → 不再被「已有检查在进行中」挡住
    class _OkThread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass

    monkeypatch.setattr(napi.threading, "Thread", _OkThread)
    r2 = c.post("/api/books/b1/check-update")
    assert r2.status_code == 202
    assert r2.get_json().get("checking") is True
    assert napi._check_jobs["b1"]["status"] == "checking"


# ══════════════════════════════════════════════════════════════
# 5. _mcache 键 = (source, comic_id)
# ══════════════════════════════════════════════════════════════

def test_manga_check_worker_mcache_keyed_by_source_comic(monkeypatch):
    import server.state as st

    def fake_one(src, cid, force_refresh=False):
        return {"has_update": True, "missing_count": 1,
                "missing": [{"id": "m1"}], "latest": "L"}

    monkeypatch.setattr(st, "_manga_check_one", fake_one)
    st._mcache.clear()
    st._manga_check_state["results"] = []
    # 两个源共用同一 comic_id —— 单 comic_id 键会互相覆盖
    st._manga_check_worker([("srcA", "123", "tA"), ("srcB", "123", "tB")])

    assert set(st._mcache.keys()) == {("srcA", "123"), ("srcB", "123")}
    assert st._mcache[("srcA", "123")]["source"] == "srcA"
    assert st._mcache[("srcB", "123")]["source"] == "srcB"


def test_check_updates_download_uses_tuple_key(monkeypatch, client):
    import server.manga_api as mapi
    import server.state as st

    now = time.time()
    st._mcache.clear()
    st._mcache[("sA", "123")] = {"ts": now, "source": "sA", "title": "tA",
                                 "missing": ["m1"]}
    st._mcache[("sB", "123")] = {"ts": now, "source": "sB", "title": "tB",
                                 "missing": ["m2"]}
    calls = []
    monkeypatch.setattr(
        mapi._manga_dl, "start",
        lambda src, cid, title, chapters=None, **kw:
            (calls.append((src, cid, tuple(chapters or []))) or ("task", "queued")))

    r = client.post("/api/manga/check-updates-download")
    assert r.status_code == 200
    assert r.get_json().get("started") == 2
    assert {(s, c) for s, c, _ in calls} == {("sA", "123"), ("sB", "123")}


# ══════════════════════════════════════════════════════════════
# 6. 代理：延迟读快照 + 同一代理 http/https 去重
# ══════════════════════════════════════════════════════════════

def _real_fetcher_cls():
    """取回真实 Fetcher 类（与执行顺序无关）。

    test_api.py 的 _mock_fetcher 会把 engine.fetcher.Fetcher 换成 MockFetcher
    且不回滚；全量/乱序运行时直接 `from engine.fetcher import Fetcher` 会拿到
    MockFetcher（无 _proxy_latency/_direct_fail_hosts 等）。按既有约定
    tests/test_api.py / test_r35b_ssrf_depth.py 经 REAL_FETCHER_CLASS 拿真实类。
    """
    from engine import fetcher as fm
    return getattr(fm, "REAL_FETCHER_CLASS", fm.Fetcher)


def test_proxy_counters_dedupe_same_proxy(monkeypatch):
    Fetcher = _real_fetcher_cls()

    monkeypatch.setattr(Fetcher, "_proxy_latency", {})
    monkeypatch.setattr(Fetcher, "_proxy_fails", {})
    monkeypatch.setattr(Fetcher, "_proxy_good", [])

    p = {"http": "p1", "https": "p1"}   # 同址
    Fetcher._record_latency(p, 1.0)
    Fetcher._record_latency(p, 10.0)
    # 去重后：1.0 → 1.0*0.7 + 10*0.3 = 3.7（重复加权会得到 5.59）
    assert Fetcher._proxy_latency["p1"] == pytest.approx(3.7)

    Fetcher._proxy_failed(p)
    assert Fetcher._proxy_fails["p1"] == 1      # 非 2

    Fetcher._proxy_ok(p)
    assert Fetcher._proxy_fails["p1"] == 0
    assert Fetcher._proxy_good == ["p1"]        # 非 ["p1","p1"]


def test_pick_proxy_latency_snapshot_under_churn(monkeypatch):
    Fetcher = _real_fetcher_cls()

    monkeypatch.setattr(Fetcher, "_direct_fail_hosts",
                        {"example.com": time.time()})
    monkeypatch.setattr(Fetcher, "_proxy_pool", ["a", "b", "c", "d"])
    monkeypatch.setattr(Fetcher, "_proxy_latency", {})
    monkeypatch.setattr(Fetcher, "_proxy_good", [])
    monkeypatch.setattr(Fetcher, "_proxy_fails", {})
    monkeypatch.setattr(Fetcher, "_load_proxy_pool",
                        classmethod(lambda cls: ["a", "b", "c", "d"]))

    stop = threading.Event()

    def churn():
        i = 0
        while not stop.is_set():
            Fetcher._proxy_latency[f"p{i % 5}"] = (i % 7) * 0.3
            i += 1

    t = threading.Thread(target=churn)
    t.start()
    try:
        for _ in range(300):
            pick = Fetcher._pick_proxy({"bookSourceUrl": "http://example.com/"})
            # 命中代理池分支：返回 http/https 同址
            if pick is not None:
                assert set(pick.keys()) == {"http", "https"}
    finally:
        stop.set()
        t.join()


# ══════════════════════════════════════════════════════════════
# 7. 收藏：update_json 并发不丢更新 + 写失败脱敏
# ══════════════════════════════════════════════════════════════

def test_update_json_concurrent_no_lost_update(tmp_path):
    from engine.app_utils import update_json

    p = str(tmp_path / "fav.json")

    def add(k):
        def _m(d):
            d = d if isinstance(d, dict) else {}
            d[k] = {"ts": 1}
            return d
        update_json(p, _m, {})

    ts = [threading.Thread(target=add, args=(f"k{i}",)) for i in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    data = json.loads(open(p, encoding="utf-8").read())
    assert len(data) == 20     # 读改写同临界区 → 无覆盖丢失


def test_favorite_save_failure_sanitized(monkeypatch, client):
    import engine.app_utils as AU

    def boom(path, data):
        raise OSError("/Users/secret/dir/_favorites.json: read-only fs")

    monkeypatch.setattr(AU, "atomic_write", boom)
    r = client.post("/api/manga/favorites",
                    json={"source": "copymanga", "comic_id": "c1",
                          "title": "t"})
    assert r.status_code == 500
    body = r.get_json()
    # 统一错误响应体（_err_response）：固定脱敏文案，绝不回显异常原文
    assert body.get("error") == "收藏保存失败"
    # 服务端绝对路径不得泄漏给客户端
    assert "secret" not in json.dumps(body)
    assert "/Users/" not in json.dumps(body)


def test_favorites_update_status_and_completion(monkeypatch, client, tmp_path):
    import server.manga_api as mapi
    fav_file = tmp_path / "_favorites.json"
    fav_file.write_text(json.dumps({"source:c1": {"title": "作品", "ts": 1}}),
                        encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(fav_file))
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(tmp_path / "_history.json"))
    monkeypatch.setattr(mapi, "_manga_cached_chapters", lambda *a: [])
    monkeypatch.setattr(mapi, "_manga_check_one", lambda *a, **k: {
        "ok": True, "all_chapters": [{"id": "a"}, {"id": "b"}],
        "latest_chapter_id": "b", "latest": "第2话", "update_time": "2026-10-01",
    })
    r = client.post("/api/manga/favorites/check-updates")
    assert r.status_code == 200 and r.get_json()["started"] == 1
    deadline = time.time() + 2
    while time.time() < deadline:
        state = client.get("/api/manga/favorites/check-updates/status").get_json()
        if not state["running"]:
            break
        time.sleep(.01)
    assert state["checked"] == 1 and state["succeeded"] == 1 and state["failed"] == 0
    saved = json.loads(fav_file.read_text(encoding="utf-8"))["source:c1"]
    assert saved["unread_count"] == 2
    assert saved["latest_chapter_id"] == "b"
    assert saved["update_time"] == "2026-10-01"


def test_favorite_update_keeps_full_catalog_beyond_public_limit(
        monkeypatch, client, tmp_path):
    """The response cap must not truncate the persisted unread baseline."""
    import server.manga_api as mapi
    import server.state as state

    comic_id = "long-favorite-series"
    source = "fixture-source"
    favorite_file = tmp_path / "_favorites-long.json"
    history_file = tmp_path / "_history-long.json"
    cache_root = tmp_path / "_cache-long"
    favorite_file.write_text(json.dumps({f"{source}:{comic_id}": {
        "title": "超长篇作品", "ts": 1,
    }}), encoding="utf-8")
    full_catalog = [
        {"id": f"chapter-{i}", "name": f"第{i}话"}
        for i in range(1, 5003)
    ]
    history_file.write_text(json.dumps({f"{source}:{comic_id}": {
        "read_chapter_ids": [row["id"] for row in full_catalog[:5000]],
    }}), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(favorite_file))
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history_file))
    monkeypatch.setattr(mapi, "MANGA_CACHE_DIR", str(cache_root))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(cache_root))
    monkeypatch.setattr(state, "MANGA_DOWNLOADS_DIR", str(tmp_path / "downloads"))
    monkeypatch.setattr(mapi, "_manga_cached_chapters", lambda *_: [])

    class FakeAdapter:
        def comic_info(self, _comic_id):
            return SimpleNamespace(
                update_time="2026-10-03",
                chapters=[SimpleNamespace(id=row["id"], name=row["name"])
                          for row in full_catalog],
            )

    monkeypatch.setattr(state, "_manga_adapter", lambda _source: FakeAdapter())
    monkeypatch.setattr(state, "_downloaded_ids_for_chapters", lambda *_: set())
    monkeypatch.setattr(state, "_integrity_summary", lambda *_: None)
    monkeypatch.setattr(state._manga_dl, "status", lambda *_: {"status": "idle"})
    checked = state._manga_check_one(source, comic_id, force_refresh=True)
    assert len(checked["all_chapters"]) == 5000  # existing public bound
    assert len(checked["_catalog_chapters"]) == 5002
    monkeypatch.setattr(mapi, "_manga_check_one", lambda *a, **k: checked)

    response = client.post("/api/manga/favorites/check-updates")
    assert response.status_code == 200 and response.get_json()["started"] == 1
    deadline = time.time() + 5
    while time.time() < deadline:
        status = client.get("/api/manga/favorites/check-updates/status").get_json()
        if not status["running"]:
            break
        time.sleep(.01)
    assert status["succeeded"] == 1 and status["failed"] == 0

    saved_favorite = json.loads(favorite_file.read_text(encoding="utf-8"))[
        f"{source}:{comic_id}"]
    assert saved_favorite["unread_count"] == 2
    catalog_file = cache_root / source / comic_id / "_favorites_catalog.json"
    catalog = json.loads(catalog_file.read_text(encoding="utf-8"))["data"]
    assert len(catalog["chapters"]) == 5002
    assert catalog["chapters"][-1]["id"] == "chapter-5002"

    # The single-title endpoint remains bounded and does not leak the internal
    # full catalog field over HTTP.
    response = client.post(f"/api/manga/{source}/{comic_id}/check-update")
    body = response.get_json()
    assert response.status_code == 200
    assert len(body["all_chapters"]) == 5000
    assert "_catalog_chapters" not in body


def test_favorites_update_migrates_legacy_jm_identity(monkeypatch, client, tmp_path):
    """启动追更检查必须命中旧前缀键，并在成功后规范化为唯一收藏键。"""
    import server.manga_api as mapi
    fav_file = tmp_path / "_favorites-legacy.json"
    fav_file.write_text(json.dumps({"jm:JM559440": {
        "title": "旧版收藏", "ts": 1,
    }}), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(fav_file))
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(tmp_path / "_history.json"))
    monkeypatch.setattr(mapi, "MANGA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(mapi, "_manga_check_one", lambda *a, **k: {
        "ok": True, "all_chapters": [{"id": "ch1", "name": "第1话"}],
        "latest_chapter_id": "ch1", "latest": "第1话",
        "update_time": "2026-10-02",
    })

    response = client.post("/api/manga/favorites/check-updates")
    assert response.status_code == 200 and response.get_json()["started"] == 1
    deadline = time.time() + 2
    while time.time() < deadline:
        state = client.get("/api/manga/favorites/check-updates/status").get_json()
        if not state["running"]:
            break
        time.sleep(.01)
    assert state["checked"] == 1 and state["succeeded"] == 1
    saved = json.loads(fav_file.read_text(encoding="utf-8"))
    assert list(saved) == ["jm:559440"]
    assert saved["jm:559440"]["latest_chapter_id"] == "ch1"


def test_favorite_removed_during_update_check_is_not_resurrected(
        monkeypatch, client, tmp_path):
    """A background result may update a favorite, never undo the user's delete."""
    import server.manga_api as mapi

    key = "copymanga_web:remove-during-check"
    fav_file = tmp_path / "_favorites.json"
    fav_file.write_text(json.dumps({key: {"title": "稍后取消收藏", "ts": 1}}),
                        encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(fav_file))
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(tmp_path / "_history.json"))
    entered_check = threading.Event()
    release_check = threading.Event()

    def slow_check(*args, **kwargs):
        entered_check.set()
        assert release_check.wait(3), "test did not release favorite update check"
        return {
            "ok": True, "all_chapters": [{"id": "ch1"}],
            "latest_chapter_id": "ch1", "latest": "第1话",
            "update_time": "2026-10-02",
        }

    monkeypatch.setattr(mapi, "_manga_check_one", slow_check)
    response = client.post("/api/manga/favorites/check-updates")
    assert response.status_code == 200
    try:
        assert entered_check.wait(2), "background favorite check did not start"
        deleted = client.delete(
            "/api/manga/favorites/copymanga_web/remove-during-check")
        assert deleted.status_code == 200
    finally:
        release_check.set()

    deadline = time.time() + 2
    while time.time() < deadline:
        state = client.get("/api/manga/favorites/check-updates/status").get_json()
        if not state["running"]:
            break
        time.sleep(.01)
    assert not state["running"]
    assert key not in json.loads(fav_file.read_text(encoding="utf-8"))


def test_concurrent_favorite_update_starts_attach_to_one_worker(
        monkeypatch, client, tmp_path):
    """Web/APK startup races must not launch duplicate source scans."""
    import server.manga_api as mapi
    import server.state as state

    key = "copymanga_web:single-flight-check"
    favorite_file = tmp_path / "_favorites-single-flight.json"
    history_file = tmp_path / "_history-single-flight.json"
    cache_root = tmp_path / "_cache-single-flight"
    favorite_file.write_text(json.dumps({key: {"title": "单飞作品", "ts": 1}}),
                             encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(favorite_file))
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history_file))
    monkeypatch.setattr(mapi, "MANGA_CACHE_DIR", str(cache_root))
    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(cache_root))
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow_check(*_args, **_kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(3), "test did not release the source check"
        return {"ok": True, "all_chapters": [{"id": "ch1", "name": "第1话"}],
                "latest_chapter_id": "ch1", "latest": "第1话"}

    monkeypatch.setattr(mapi, "_manga_check_one", slow_check)
    first = client.post("/api/manga/favorites/check-updates")
    assert first.status_code == 200
    assert first.get_json()["started"] == 1
    try:
        assert entered.wait(2), "first favorite check did not enter the worker"
        second = client.post("/api/manga/favorites/check-updates")
        assert second.status_code == 200
        assert second.get_json() == {
            "ok": True, "started": 0, "already_running": True,
        }
        assert len(calls) == 1, "a concurrent startup must not create another source scan"
    finally:
        release.set()

    deadline = time.time() + 3
    while time.time() < deadline:
        status = client.get("/api/manga/favorites/check-updates/status").get_json()
        if not status["running"]:
            break
        time.sleep(.01)
    assert not status["running"]
    assert status["checked"] == 1 and status["succeeded"] == 1


def test_read_progress_for_favorite_does_not_require_downloaded_files(
        monkeypatch, client, tmp_path):
    """在线阅读收藏作品时，进度/未读必须独立于本地下载目录持久化。"""
    import server.manga_api as mapi

    source, comic_id = "copymanga_web", "online-only-favorite"
    key = f"{source}:{comic_id}"
    manga_root = tmp_path / "manga"
    favorites_file = manga_root / "_favorites.json"
    history_file = manga_root / "_history.json"
    favorites_file.parent.mkdir(parents=True)
    favorites_file.write_text(json.dumps({key: {
        "title": "仅在线收藏作品", "cover": "cover", "ts": 1,
    }}), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(favorites_file))
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history_file))
    monkeypatch.setattr(mapi, "MANGA_DIR", str(manga_root))
    monkeypatch.setattr(mapi, "MANGA_DOWNLOADS_DIR", str(manga_root / "downloads"))
    monkeypatch.setattr(mapi, "MANGA_CACHE_DIR", str(manga_root / "_cache"))
    monkeypatch.setattr(mapi, "_manga_cached_chapters", lambda *_: [
        {"id": "ch1", "name": "第1话"}, {"id": "ch2", "name": "第2话"},
    ])

    saved = client.post("/api/manga/history", json={
        "source": source, "comic_id": comic_id, "idx": 0,
        "chapter_id": "ch1", "chapter_label": "第1话", "pos": "第1话 P3",
    })
    assert saved.status_code == 200 and saved.get_json()["ok"] is True
    history = json.loads(history_file.read_text(encoding="utf-8"))["copymanga:" + comic_id]
    assert history["chapter_id"] == "ch1"
    assert history["read_chapter_ids"] == ["ch1"]
    assert history["pos"] == "第1话 P3"
    assert not (manga_root / "downloads").exists()

    favorite = client.get("/api/manga/favorites").get_json()["favorites"][0]
    assert favorite["comic_id"] == comic_id
    assert favorite["unread_count"] == 1
    assert favorite["latest_chapter_id"] == "ch2"


def test_favorites_sort_by_source_update_time(monkeypatch, client, tmp_path):
    import server.manga_api as mapi

    favorite_file = tmp_path / "_favorites.json"
    favorite_file.write_text(json.dumps({
        "mangadex:older": {"title": "较早更新", "update_time": "2026-09-29", "ts": 99},
        "copymanga_web:newer": {"title": "较新更新", "update_time": "2026-10-02", "ts": 1},
    }), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(favorite_file))
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(tmp_path / "_history.json"))
    monkeypatch.setattr(mapi, "_manga_cached_chapters", lambda *_: [])

    favorites = client.get("/api/manga/favorites").get_json()["favorites"]
    assert [item["title"] for item in favorites] == ["较新更新", "较早更新"]


def test_favorites_update_thread_start_failure_resets_state(monkeypatch, client, tmp_path):
    import server.manga_api as mapi
    fav_file = tmp_path / "_favorites.json"
    fav_file.write_text(json.dumps({"source:c1": {"title": "作品", "ts": 1}}),
                        encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(fav_file))
    monkeypatch.setattr(mapi.threading, "Thread", _BoomThread)
    response = client.post("/api/manga/favorites/check-updates")
    assert response.status_code == 500
    state = client.get("/api/manga/favorites/check-updates/status").get_json()
    assert not state["running"] and state["failed"] == 1


@pytest.mark.parametrize("route,body", [
    ("download", {"chapters": ["ch9"]}),
    ("download-new", {"new_chapters": ["ch9"]}),
])
def test_download_start_auto_favorites_and_preserves_tracking(
        monkeypatch, client, tmp_path, route, body):
    import server.manga_api as mapi
    fav_file = tmp_path / "_favorites.json"
    fav_file.write_text(json.dumps({"copymanga_web:comic1": {
        "title": "旧标题", "cover": "old-cover", "ts": 10,
        "unread_count": 6, "latest_chapter_id": "ch8",
        "update_time": "2026-09-30", "read_chapter_ids": ["ch1", "ch2"],
    }}), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(fav_file))
    monkeypatch.setattr(mapi._manga_dl, "start",
                        lambda *a, **k: ("task-key", "queued"))

    response = client.post(f"/api/manga/copymanga_web/comic1/{route}", json={
        "title": "新标题", "cover": "new-cover", **body})
    assert response.status_code == 200
    assert response.get_json()["favorited"] is True
    saved = json.loads(fav_file.read_text(encoding="utf-8"))["copymanga:comic1"]
    assert saved["title"] == "新标题" and saved["cover"] == "new-cover"
    assert saved["unread_count"] == 6 and saved["latest_chapter_id"] == "ch8"
    assert saved["update_time"] == "2026-09-30"
    assert saved["read_chapter_ids"] == ["ch1", "ch2"]


def test_batch_update_download_also_favorites(monkeypatch, client, tmp_path):
    import server.manga_api as mapi
    fav_file = tmp_path / "_favorites.json"
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(fav_file))
    monkeypatch.setattr(mapi, "_mcache", {("copymanga_web", "comic2"): {
        "ts": time.time(), "missing": ["ch10"], "source": "copymanga_web",
        "title": "批量更新作品", "cover": "cover-url",
    }})
    monkeypatch.setattr(mapi._manga_dl, "start",
                        lambda *a, **k: ("task-key", "queued"))
    response = client.post("/api/manga/check-updates-download")
    assert response.status_code == 200
    assert response.get_json()["started"] == 1
    assert json.loads(fav_file.read_text(encoding="utf-8"))[
        "copymanga:comic2"]["cover"] == "cover-url"


def test_favorite_unread_count_uses_read_chapter_set_not_last_index(
        monkeypatch, client, tmp_path):
    import server.manga_api as mapi
    fav_file, history_file = tmp_path / "_favorites.json", tmp_path / "_history.json"
    key = "copymanga_web:comic3"
    read_ids = [f"ch{i}" for i in range(1, 14)] + [f"ch{i}" for i in range(19, 31)]
    fav_file.write_text(json.dumps({key: {"title": "跳读作品", "ts": 1}}),
                        encoding="utf-8")
    history_file.write_text(json.dumps({key: {
        "source": "copymanga_web", "comic_id": "comic3", "idx": 11,
        "read_chapter_ids": read_ids,
    }}), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(fav_file))
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history_file))
    monkeypatch.setattr(mapi, "_manga_check_one", lambda *a, **k: {
        "ok": True,
        "all_chapters": [{"id": f"ch{i}"} for i in range(1, 32)],
        "latest_chapter_id": "ch31", "latest": "第31话",
        "update_time": "2026-10-02",
    })

    def check_and_wait():
        result = client.post("/api/manga/favorites/check-updates")
        assert result.status_code == 200
        deadline = time.time() + 2
        while time.time() < deadline:
            state = client.get("/api/manga/favorites/check-updates/status").get_json()
            if not state["running"]:
                return
            time.sleep(.01)
        pytest.fail("收藏更新检查未在时限内完成")

    check_and_wait()
    saved = json.loads(fav_file.read_text(encoding="utf-8"))["copymanga:comic3"]
    assert saved["unread_count"] == 6  # 跳过 14–18 + 新增 31

    # 重读第 12 话只更新最近位置，不会抹掉其它已读章节或填平未读缺口。
    response = client.post("/api/manga/history", json={
        "source": "copymanga_web", "comic_id": "comic3", "idx": 11,
        "chapter_id": "ch12", "chapter_label": "第12话", "pos": "第12话 P2",
    })
    assert response.status_code == 200
    saved_history = json.loads(history_file.read_text(encoding="utf-8"))["copymanga:comic3"]
    assert set(saved_history["read_chapter_ids"]) == set(read_ids)
    check_and_wait()
    saved = json.loads(fav_file.read_text(encoding="utf-8"))["copymanga:comic3"]
    assert saved["unread_count"] == 6


def test_read_chapter_identity_is_not_dropped_after_five_thousand_reads(
        client, monkeypatch, tmp_path):
    """Long-running series must not forget old read chapters at an arbitrary cap."""
    import server.manga_api as mapi
    history_file = tmp_path / "_history.json"
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history_file))
    key = "jm:long-series"
    read_ids = [f"ch{index}" for index in range(5000)]
    history_file.write_text(json.dumps({key: {
        "idx": 4999, "pos": "第4999话 P1", "read_chapter_ids": read_ids,
        "read_chapters": [{"id": chapter_id, "label": f"第{i}话"}
                           for i, chapter_id in enumerate(read_ids)],
    }}), encoding="utf-8")
    payload = {
        "source": "jm", "comic_id": "long-series", "idx": 5000,
        "chapter_id": "ch5000", "chapter_label": "第5000话", "pos": "第5000话 P1",
    }
    assert client.post("/api/manga/history", json=payload).status_code == 200
    saved = json.loads(history_file.read_text(encoding="utf-8"))["jm:long-series"]
    assert len(saved["read_chapter_ids"]) == 5001
    assert "ch0" in saved["read_chapter_ids"]


def test_favorite_get_uses_new_checked_catalog_over_stale_detail_snapshot(
        monkeypatch, client, tmp_path):
    """A shelf refresh after checking must not replace fresh counts with stale TOC."""
    import server.manga_api as mapi
    import server.state as st

    source, comic_id = "copymanga_web", "favorite-stale-toc"
    key = f"{source}:{comic_id}"
    fav_file = tmp_path / "manga" / "_favorites.json"
    history_file = tmp_path / "manga" / "_history.json"
    cache_dir = tmp_path / "manga" / "_cache"
    detail_dir = cache_dir / source / comic_id
    detail_dir.mkdir(parents=True)
    fav_file.parent.mkdir(parents=True, exist_ok=True)
    fav_file.write_text(json.dumps({key: {"title": "收藏更新回归", "ts": 1}}),
                        encoding="utf-8")
    history_file.write_text(json.dumps({key: {
        "ts": 1, "read_chapter_ids": ["ch1", "ch2"],
    }}), encoding="utf-8")
    (detail_dir / "_info_full.json").write_text(json.dumps({"ts": 1, "data": {
        "volumes": [],
        "chapters": [{"id": "ch1", "name": "第1话"},
                     {"id": "ch2", "name": "第2话"}],
    }}), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(fav_file))
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history_file))
    monkeypatch.setattr(mapi, "MANGA_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(st, "MANGA_CACHE_DIR", str(cache_dir))
    st._manga_chapters_cache.pop((source, comic_id), None)
    monkeypatch.setattr(mapi, "_manga_check_one", lambda *a, **k: {
        "ok": True,
        "all_chapters": [{"id": f"ch{i}", "name": f"第{i}话"}
                          for i in range(1, 5)],
        "latest_chapter_id": "ch4", "latest": "第4话",
        "update_time": "2026-10-02",
    })

    response = client.post("/api/manga/favorites/check-updates")
    assert response.status_code == 200
    deadline = time.time() + 2
    while time.time() < deadline:
        state = client.get("/api/manga/favorites/check-updates/status").get_json()
        if not state["running"]:
            break
        time.sleep(.01)
    assert state["succeeded"] == 1

    # Simulate a cold process cache: the persisted checked catalog must outrank
    # the older full-detail snapshot after restart as well.
    st._manga_chapters_cache.pop((source, comic_id), None)
    favorite = client.get("/api/manga/favorites").get_json()["favorites"][0]
    assert favorite["unread_count"] == 2
    assert favorite["latest_chapter_id"] == "ch4"
    assert favorite["latest_chapter_label"] == "第4话"

    # A read after the check is immediately reflected against that same catalog.
    response = client.post("/api/manga/history", json={
        "source": source, "comic_id": comic_id, "idx": 3,
        "chapter_id": "ch4", "chapter_label": "第4话", "pos": "第4话 P1",
    })
    assert response.status_code == 200
    favorite = client.get("/api/manga/favorites").get_json()["favorites"][0]
    assert favorite["unread_count"] == 1


def test_favorite_check_catalog_write_failure_does_not_publish_or_update_unread(
        monkeypatch, client, tmp_path):
    import server.manga_api as mapi
    import server.state as st

    source, comic_id = "copymanga", "catalog-write-failure"
    key = f"{source}:{comic_id}"
    favorites = tmp_path / "_favorites.json"
    history = tmp_path / "_history.json"
    cache = tmp_path / "cache"
    detail_dir = cache / source / comic_id
    detail_dir.mkdir(parents=True)
    favorites.write_text(json.dumps({key: {
        "title": "旧目录", "unread_count": 9,
    }}), encoding="utf-8")
    history.write_text(json.dumps({}), encoding="utf-8")
    (detail_dir / "_info_full.json").write_text(json.dumps({"ts": 1, "data": {
        "volumes": [], "chapters": [{"id": "old", "name": "旧话"}],
    }}), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(favorites))
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history))
    monkeypatch.setattr(mapi, "MANGA_CACHE_DIR", str(cache))
    monkeypatch.setattr(st, "MANGA_CACHE_DIR", str(cache))
    st._manga_catalog_invalidate(source, comic_id)
    assert [row["id"] for row in st._manga_cached_chapters(source, comic_id)] == ["old"]
    original_atomic_write = mapi.atomic_write

    def fail_catalog_write(path, payload, *args, **kwargs):
        if str(path).endswith("_favorites_catalog.json"):
            raise OSError("injected catalog disk failure")
        return original_atomic_write(path, payload, *args, **kwargs)

    monkeypatch.setattr(mapi, "atomic_write", fail_catalog_write)
    monkeypatch.setattr(mapi, "_manga_check_one", lambda *a, **k: {
        "ok": True,
        "all_chapters": [
            {"id": "new-1", "name": "新话1"},
            {"id": "new-2", "name": "新话2"},
        ],
        "latest_chapter_id": "new-2", "latest": "新话2",
        "update_time": "2026-10-03",
    })

    response = client.post("/api/manga/favorites/check-updates")
    assert response.status_code == 200
    deadline = time.time() + 2
    while time.time() < deadline:
        status = client.get("/api/manga/favorites/check-updates/status").get_json()
        if not status["running"]:
            break
        time.sleep(.01)
    assert status["failed"] == 1 and status["succeeded"] == 0
    assert json.loads(favorites.read_text(encoding="utf-8"))[key][
        "unread_count"] == 9
    assert [row["id"] for row in st._manga_cached_chapters(source, comic_id)] == ["old"]


def test_read_chapter_identity_migrates_changed_ids_only_for_unique_labels(
        monkeypatch, client, tmp_path):
    import server.manga_api as mapi
    fav_file, history_file = tmp_path / "_favorites.json", tmp_path / "_history.json"
    key = "copymanga_web:comic-identity-migration"
    fav_file.write_text(json.dumps({key: {"title": "ID迁移作品", "ts": 1}}),
                        encoding="utf-8")
    history_file.write_text(json.dumps({key: {
        "read_chapter_ids": ["old-1", "old-2"],
        "read_chapters": [
            {"id": "old-1", "label": "第01话"},
            {"id": "old-2", "label": "第02话"},
        ],
    }}), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(fav_file))
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history_file))
    monkeypatch.setattr(mapi, "_manga_check_one", lambda *a, **k: {
        "ok": True,
        "all_chapters": [
            {"id": "new-1", "name": "第1話"},
            {"id": "new-2", "name": "第2話"},
            {"id": "new-3", "name": "第3話"},
        ],
        "latest_chapter_id": "new-3", "latest": "第3話",
        "update_time": "2026-10-02",
    })

    response = client.post("/api/manga/favorites/check-updates")
    assert response.status_code == 200
    deadline = time.time() + 2
    while time.time() < deadline:
        state = client.get("/api/manga/favorites/check-updates/status").get_json()
        if not state["running"]:
            break
        time.sleep(.01)
    saved = json.loads(fav_file.read_text(encoding="utf-8"))[
        "copymanga:comic-identity-migration"]
    assert saved["unread_count"] == 1

    # 同名章节无法无歧义迁移时不猜测，宁可显示未读也不误标为已读。
    monkeypatch.setattr(mapi, "_manga_check_one", lambda *a, **k: {
        "ok": True,
        "all_chapters": [
            {"id": "new-a", "name": "第1话"},
            {"id": "new-b", "name": "第1話"},
            {"id": "new-3", "name": "第3话"},
        ],
        "latest_chapter_id": "new-3", "latest": "第3话",
        "update_time": "2026-10-02",
    })
    response = client.post("/api/manga/favorites/check-updates")
    assert response.status_code == 200
    deadline = time.time() + 2
    while time.time() < deadline:
        state = client.get("/api/manga/favorites/check-updates/status").get_json()
        if not state["running"]:
            break
        time.sleep(.01)
    saved = json.loads(fav_file.read_text(encoding="utf-8"))[
        "copymanga:comic-identity-migration"]
    assert saved["unread_count"] == 3


def test_delete_chapter_removes_alias_and_cache_by_saved_title(
        client, monkeypatch, tmp_path):
    import server.manga_api as mapi
    downloads = tmp_path / "downloads"
    cache = tmp_path / "cache"
    # requested detail uses new ID; old CopyManga alias and cache use the saved old ID.
    for root, source, chapter in ((downloads, "copymanga", "old-app-id"),
                                  (cache, "copymanga_web", "old-web-id")):
        base = root / source / "comic"
        (base / chapter).mkdir(parents=True)
        (base / chapter / "0000.jpg").write_bytes(b"image")
        (base / "_info.json").write_text(json.dumps({"chapters": [
            {"id": chapter, "name": "第1話"}]}), encoding="utf-8")
    current = cache / "copymanga_web" / "comic"
    (current / "_info_full.json").write_text(json.dumps({"data": {
        "chapters": [{"id": "new-detail-id", "name": "第1話"}],
    }}), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(mapi, "MANGA_CACHE_DIR", str(cache))
    monkeypatch.setattr(mapi._manga_dl, "status", lambda _key: {"status": "none"})
    rescans = []
    monkeypatch.setattr(mapi, "_manga_stats_request", lambda source, cid: rescans.append((source, cid)))
    response = client.post("/api/manga/copymanga_web/comic/chapters/delete",
                           json={"chapter_ids": ["new-detail-id"]})
    assert response.status_code == 200, response.get_data(as_text=True)
    result = response.get_json()
    assert result["deleted"] == 2 and result["missing"] == 0
    assert not (downloads / "copymanga" / "comic" / "old-app-id").exists()
    assert not (cache / "copymanga_web" / "comic" / "old-web-id").exists()
    assert set(rescans) == {("copymanga", "comic"), ("copymanga_web", "comic")}


# ══════════════════════════════════════════════════════════════
# 8. cache_path：非法章节 id / 符号链接越界
# ══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad", ["", ".", "..", "../x", "a/b", "a\\b",
                                 "x\x00y", "e..vil"])
def test_safe_chapter_dir_rejects_illegal(bad):
    from engine.manga.downloader import _safe_chapter_dir
    from engine.manga.base import MangaError
    with pytest.raises(MangaError):
        _safe_chapter_dir(bad)


def test_cache_path_rejects_illegal_chapter(tmp_path):
    from engine.manga.downloader import ImageDownloader
    from engine.manga.base import MangaError

    d = ImageDownloader(adapter=None, cache_root=str(tmp_path))
    with pytest.raises(MangaError):
        d.cache_path("c1", "../escape", 0)


def test_cache_path_rejects_symlink_escape(tmp_path):
    from engine.manga.downloader import ImageDownloader
    from engine.manga.base import MangaError

    root = tmp_path / "root"
    out = tmp_path / "outside"
    root.mkdir()
    out.mkdir()
    os.symlink(str(out), str(root / "evil"))    # root/evil → 越界目录

    d = ImageDownloader(adapter=None, cache_root=str(root))
    with pytest.raises(MangaError):
        d.cache_path("c1", "evil", 0)
