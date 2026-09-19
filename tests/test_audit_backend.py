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
            calls.append((src, cid, tuple(chapters or []))))

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
