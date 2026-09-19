# -*- coding: utf-8 -*-
"""导出版本机制回归（离线、确定性）

覆盖本轮结构性优化：
1. content_revision 由章节 .cache 指纹派生——内容/顺序/数量变化都改变版本，
   无缓存返回空串；
2. merge_txt 写出 _export.json（rev + txt 指纹），版本一致时跳过全量合并
   （不重写 book.txt）；
3. export_is_fresh 双重校验：版本不符 / txt 被外部改写 → 判陈旧；
4. /api/books/<key>/txt：版本陈旧触发重建（单飞，只合并一次）；
   重建失败且无文件 → 500 显式错误；重建失败但有旧文件 → 200 + 陈旧标记。

不触网、不写真实用户数据（conftest 已隔离 DATA_DIR）。
"""
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import crawler as CR  # noqa: E402

_REAL_CRAWL_TASK = CR.CrawlTask      # 打桩前捕获：_merge 走真实合并逻辑


def _mk_book(book_dir, chapters, texts):
    """构造书籍目录：_state.json + 章节 .cache"""
    os.makedirs(book_dir, exist_ok=True)
    chs = []
    for i, (name, url) in enumerate(chapters, 1):
        chs.append({"name": name, "url": url})
        body = texts.get(url)
        if body is not None:
            p = os.path.join(book_dir, f"{CR._cache_key_of(url)}.cache")
            with open(p, "w", encoding="utf-8") as f:
                f.write(f"{name}\n\n{body}")
    state = {"book": {"name": "测试书", "author": "作者",
                      "source_uid": "testsrc", "book_url": "https://x.test/book/1"},
             "chapters": chs, "completed": [c["url"] for c in chs],
             "failed": {}, "updated_at": "2026-09-10 10:00:00"}
    with open(os.path.join(book_dir, "_state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    return state


CHS = [("第1章", "https://x.test/1"), ("第2章", "https://x.test/2"),
       ("第3章", "https://x.test/3")]
TXT = {"https://x.test/1": "正文一" * 30,
       "https://x.test/2": "正文二" * 30,
       "https://x.test/3": "正文三" * 30}


def _named(chs):
    """(name, url) 列表 → 带 name 的章节 dict（版本含章节名，须与合并一致）"""
    return [{"url": u, "name": n} for n, u in chs]


def _write_state(book_dir, chapters):
    """仅更新 _state.json 的 chapters（模拟任务把新目录保存到磁盘权威）"""
    p = os.path.join(book_dir, "_state.json")
    with open(p, encoding="utf-8") as f:
        state = json.load(f)
    state["chapters"] = [{"name": n, "url": u} for n, u in chapters]
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════
# 1. content_revision
# ══════════════════════════════════════════════════════════════

def test_revision_empty_without_cache(tmp_path):
    d = str(tmp_path / "book")
    os.makedirs(d, exist_ok=True)
    assert CR.content_revision(d, [{"url": "https://x.test/1"}]) == ""


def test_revision_changes_with_content_and_order(tmp_path):
    d = str(tmp_path / "book")
    _mk_book(d, CHS, TXT)
    rev0 = CR.content_revision(d, [{"url": u} for _, u in CHS])
    assert rev0

    # 内容被重写（大小变化）→ 版本变化
    p = os.path.join(d, f"{CR._cache_key_of('https://x.test/2')}.cache")
    with open(p, "w", encoding="utf-8") as f:
        f.write("第2章\n\n" + "改写正文" * 80)
    rev1 = CR.content_revision(d, [{"url": u} for _, u in CHS])
    assert rev1 != rev0

    # 目录顺序变化 → 版本变化（序位参与哈希）
    rev2 = CR.content_revision(
        d, [{"url": u} for _, u in reversed(CHS)])
    assert rev2 not in (rev0, rev1)

    # 同内容重写：size 相同但 mtime_ns 变 → 版本仍变化（指纹语义，等价 ETag；
    # 重爬同内容只多触发一次幂等合并，不会给出陈旧导出）
    with open(p, "w", encoding="utf-8") as f:
        f.write("第2章\n\n" + TXT["https://x.test/2"])
    rev3 = CR.content_revision(d, [{"url": u} for _, u in CHS])
    assert rev3 != rev1
    # 不读缓存、无缓存目录时版本稳定（同一磁盘状态多次计算一致）
    assert CR.content_revision(d, [{"url": u} for _, u in CHS]) == rev3


def test_revision_ignores_missing_and_empty_cache(tmp_path):
    d = str(tmp_path / "book")
    _mk_book(d, CHS[:2], TXT)
    # 空缓存文件不计入（避免把"空文件"当成可导出内容）
    empty = os.path.join(d, f"{CR._cache_key_of('https://x.test/3')}.cache")
    open(empty, "w").close()
    rev_a = CR.content_revision(d, [{"url": u} for _, u in CHS])
    rev_b = CR.content_revision(d, [{"url": u} for _, u in CHS[:2]])
    assert rev_a == rev_b and rev_a


def test_revision_uses_url_identity_on_swap(tmp_path):
    """两章同名、缓存 size 相同、mtime 相同（同批写入常见）：若只用
    (idx,name,mtime,size)，交换二者位置得到完全相同的序列 → 版本不变、
    交换后误判新鲜。必须纳入 url 身份才能识别交换。"""
    d = str(tmp_path / "book")
    os.makedirs(d, exist_ok=True)
    u1, u2 = "https://x.test/a", "https://x.test/b"
    tns = 1_700_000_000_000_000_000
    for u in (u1, u2):
        p = os.path.join(d, f"{CR._cache_key_of(u)}.cache")
        with open(p, "w", encoding="utf-8") as f:
            f.write("同名\n\n等长正文")        # 两文件字节数相同
        os.utime(p, ns=(tns, tns))             # 强制同 size、同 mtime_ns
    chs = [{"url": u1, "name": "同名"}, {"url": u2, "name": "同名"}]
    rev = CR.content_revision(d, chs)
    assert rev
    assert rev != CR.content_revision(d, list(reversed(chs)))


def test_revision_serialization_consistent_and_unambiguous(tmp_path):
    """一致序列化：多次计算稳定；章名含分隔符（: ;）也不制造歧义。"""
    d = str(tmp_path / "book")
    os.makedirs(d, exist_ok=True)
    u1, u2 = "https://x.test/s1", "https://x.test/s2"
    tns = 1_700_000_000_000_000_100
    for u in (u1, u2):
        p = os.path.join(d, f"{CR._cache_key_of(u)}.cache")
        with open(p, "w", encoding="utf-8") as f:
            f.write("章\n\n等长")
        os.utime(p, ns=(tns, tns))
    L = [{"url": u1, "name": "a:b;c"}, {"url": u2, "name": "d"}]
    r1 = CR.content_revision(d, L)
    assert r1
    assert CR.content_revision(d, [dict(x) for x in L]) == r1   # 稳定
    L2 = [{"url": u1, "name": "a:b;c!"}, {"url": u2, "name": "d"}]
    assert CR.content_revision(d, L2) != r1                     # 含分隔符仍可区分


# ══════════════════════════════════════════════════════════════
# 2/3. merge_txt 写元数据 + export_is_fresh 双重校验
# ══════════════════════════════════════════════════════════════

class _FakeBook:
    def __init__(self, chapters):
        self.chapters = chapters


def _merge(book_dir, chapters):
    t = _REAL_CRAWL_TASK.__new__(_REAL_CRAWL_TASK)   # 只测 merge_txt 逻辑
    t.book_dir = book_dir
    t.book = _FakeBook([{"url": u, "name": n} for n, u in chapters])
    t._merged_rev = None
    return t.merge_txt()


def test_merge_writes_meta_and_skips_when_fresh(tmp_path):
    d = str(tmp_path / "book")
    _mk_book(d, CHS, TXT)
    out = _merge(d, CHS)
    assert out and os.path.exists(out)
    meta = CR.read_export_meta(d)
    assert meta.get("rev") == CR.content_revision(d, _named(CHS))
    assert meta.get("txt_size") == os.path.getsize(out)
    assert CR.export_is_fresh(d, _named(CHS), out) is True

    mtime_ns = os.stat(out).st_mtime_ns
    # 版本一致 → 第二次合并直接复用，不重写文件
    assert _merge(d, CHS) == out
    assert os.stat(out).st_mtime_ns == mtime_ns

    # 内容变化 → 判陈旧并触发重写
    p = os.path.join(d, f"{CR._cache_key_of('https://x.test/1')}.cache")
    with open(p, "w", encoding="utf-8") as f:
        f.write("第1章\n\n" + "新正文" * 50)
    assert CR.export_is_fresh(d, _named(CHS), out) is False
    _merge(d, CHS)
    assert os.stat(out).st_mtime_ns != mtime_ns
    assert CR.export_is_fresh(d, _named(CHS), out) is True


def test_export_fresh_detects_txt_tampering(tmp_path):
    d = str(tmp_path / "book")
    _mk_book(d, CHS, TXT)
    out = _merge(d, CHS)
    chapters = _named(CHS)
    assert CR.export_is_fresh(d, chapters, out) is True
    with open(out, "w", encoding="utf-8") as f:
        f.write("被外部截断")
    assert CR.export_is_fresh(d, chapters, out) is False


def test_export_fresh_without_meta_is_stale(tmp_path):
    d = str(tmp_path / "book")
    _mk_book(d, CHS, TXT)
    out = _merge(d, CHS)
    os.remove(os.path.join(d, CR.EXPORT_META_NAME))
    assert CR.export_is_fresh(d, [{"url": u} for _, u in CHS], out) is False


def test_export_fresh_conservative_on_partial_write(tmp_path):
    """txt 与 meta 是两次独立原子写（非单一事务）：崩溃发生在两次之间、
    或外部改写使二者不一致时，检测必须保守判陈旧（触发重建），绝不把
    错配内容当新鲜下发。"""
    d = str(tmp_path / "book")
    _mk_book(d, CHS, TXT)
    out = _merge(d, CHS)
    chapters = _named(CHS)
    assert CR.export_is_fresh(d, chapters, out) is True
    meta_path = os.path.join(d, CR.EXPORT_META_NAME)
    with open(meta_path, encoding="utf-8") as f:
        meta_backup = f.read()
    # 场景A：崩溃在写 meta 之前 → meta 缺失
    os.remove(meta_path)
    assert CR.export_is_fresh(d, chapters, out) is False
    # 场景B：meta 存在但 txt 已被改动（meta 与 txt 不一致）
    with open(meta_path, "w", encoding="utf-8") as f:
        f.write(meta_backup)
    with open(out, "a", encoding="utf-8") as f:
        f.write("尾部残留")
    assert CR.export_is_fresh(d, chapters, out) is False


# ══════════════════════════════════════════════════════════════
# 4. 导出端点：单飞重建 + 显式失败处理
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _book_in_books_dir(key, chapters, texts):
    from engine.config import BOOKS_DIR
    d = os.path.join(BOOKS_DIR, key)
    _mk_book(d, chapters, texts)
    return d


class _FakeTask:
    """替换 CrawlTask：记录调用次数，可配置成功/失败"""
    calls = []
    fail = False
    no_state = False

    def __init__(self, *a, **kw):
        self.book_dir = a[2] if len(a) > 2 else kw.get("book_dir")
        self._rev = None

    def _load_state(self):
        if type(self).no_state:
            return False
        self.book = _FakeBook([{"url": u, "name": n} for n, u in CHS_FAKE])
        type(self).calls.append(1)
        return True

    def merge_txt(self, force=False):
        if type(self).fail:
            raise RuntimeError("merge boom")
        return _merge(self.book_dir, CHS_FAKE)


CHS_FAKE = CHS


def _install_fake_task(monkeypatch, fake=None, src_ok=True):
    import server.novel_api as napi
    monkeypatch.setattr(
        napi, "get_by_uid",
        (lambda uid: {"bookSourceUrl": "https://x.test",
                      "bookSourceName": "测试源"}) if src_ok else (lambda uid: None))
    fake = fake or _FakeTask
    fake.calls = []
    fake.fail = False
    fake.no_state = False
    monkeypatch.setattr(CR, "CrawlTask", fake, raising=True)
    return fake


def test_export_rebuilds_when_stale_and_single_flight(client, monkeypatch):
    _install_fake_task(monkeypatch)
    d = _book_in_books_dir("testsrc_book_txt1", CHS, TXT)
    p = os.path.join(d, "book.txt")
    # 首次导出：无 book.txt → 触发一次重建
    r = client.get("/api/books/testsrc_book_txt1/txt")
    assert r.status_code == 200
    assert os.path.exists(p)
    assert len(_FakeTask.calls) == 1
    # 已新鲜 → 不再重建
    r2 = client.get("/api/books/testsrc_book_txt1/txt")
    assert r2.status_code == 200
    assert len(_FakeTask.calls) == 1
    # 章节缓存变化 → 陈旧 → 再重建一次
    c1 = os.path.join(d, f"{CR._cache_key_of('https://x.test/3')}.cache")
    with open(c1, "w", encoding="utf-8") as f:
        f.write("第3章\n\n" + "补章正文" * 40)
    r3 = client.get("/api/books/testsrc_book_txt1/txt")
    assert r3.status_code == 200
    assert len(_FakeTask.calls) == 2


def test_export_rebuild_failure_without_file_is_500(client, monkeypatch):
    fake = _install_fake_task(monkeypatch)
    fake.fail = True
    _book_in_books_dir("testsrc_book_txt2", CHS, TXT)
    r = client.get("/api/books/testsrc_book_txt2/txt")
    assert r.status_code == 500
    body = r.get_json() or {}
    assert "重建失败" in (body.get("error") or "")


def test_export_rebuild_failure_with_stale_file_marks_stale(client, monkeypatch):
    fake = _install_fake_task(monkeypatch)
    d = _book_in_books_dir("testsrc_book_txt3", CHS, TXT)
    p = os.path.join(d, "book.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("旧全文")
    fake.fail = True
    r = client.get("/api/books/testsrc_book_txt3/txt")
    assert r.status_code == 200
    assert r.headers.get("X-Export-Stale") == "1"
    assert r.headers.get("X-Export-Error") == "1"
    # 重建失败必须保留旧全文：响应体与磁盘旧文件都不被破坏
    assert r.get_data(as_text=True) == "旧全文"
    with open(p, encoding="utf-8") as f:
        assert f.read() == "旧全文"


def test_export_missing_source_uses_local_cache(client, monkeypatch):
    """旧书书源已被删除：内容已在本地 .cache → 无需书源，离线合并导出 200"""
    _install_fake_task(monkeypatch, src_ok=False)
    d = _book_in_books_dir("nosuchsrc_book_txt4", CHS, TXT)
    p = os.path.join(d, "book.txt")
    assert not os.path.exists(p)
    r = client.get("/api/books/nosuchsrc_book_txt4/txt")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "正文一" in body and "正文三" in body
    assert body.index("第1章") < body.index("第2章") < body.index("第3章")
    assert r.headers.get("X-Export-Stale") is None, "本地合并后应为新鲜版本"
    assert CR.export_is_fresh(d, _named(CHS), p) is True


def test_export_missing_source_without_cache_is_500(client, monkeypatch):
    """书源不存在且本地无任何可合并缓存 → 显式 500，而非静默 404/假成功"""
    _install_fake_task(monkeypatch, src_ok=False)
    _book_in_books_dir("nosuchsrc_book_txt4b", CHS, {})   # 不写任何 .cache
    r = client.get("/api/books/nosuchsrc_book_txt4b/txt")
    assert r.status_code == 500
    assert "无可合并" in ((r.get_json() or {}).get("error") or "")


def test_export_concurrent_requests_share_one_rebuild(client, monkeypatch):
    """按书单飞：并发导出同一本只重建一次（第二个请求等锁后命中新鲜版本）"""
    fake = _install_fake_task(monkeypatch)
    _book_in_books_dir("testsrc_book_txt5", CHS, TXT)
    results = []

    def _get():
        results.append(client.get("/api/books/testsrc_book_txt5/txt").status_code)

    ths = [threading.Thread(target=_get) for _ in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert results == [200] * 4
    assert len(fake.calls) <= 2      # 单飞：最多一次重建（+可能的竞态冗余一次）


# ══════════════════════════════════════════════════════════════
# 5. 章节名（元数据）变化 → 版本变化，旧标题不得误判新鲜
# ══════════════════════════════════════════════════════════════

def test_revision_changes_when_chapter_renamed(tmp_path):
    d = str(tmp_path / "book")
    _mk_book(d, CHS, TXT)
    out = _merge(d, CHS)
    assert CR.export_is_fresh(d, _named(CHS), out) is True

    # 仅重命名（缓存文件 size/mtime 不变）→ 导出标题会变 → 必须判陈旧
    renamed = [("第一章", CHS[0][1]), (CHS[1][0], CHS[1][1]),
               (CHS[2][0], CHS[2][1])]
    assert (CR.content_revision(d, _named(renamed))
            != CR.content_revision(d, _named(CHS)))
    assert CR.export_is_fresh(d, _named(renamed), out) is False

    # 任务会把重命名后的新目录写回 _state.json（合并以磁盘权威目录为准）
    _write_state(d, renamed)
    _merge(d, renamed)
    with open(out, encoding="utf-8") as f:
        assert "第一章" in f.read()


# ══════════════════════════════════════════════════════════════
# 6. 确定性交错：合并读缓存期间缓存被改写 → 保守判陈旧（不误报新鲜）
# ══════════════════════════════════════════════════════════════

def test_merge_cache_changed_during_read_is_not_fresh(tmp_path, monkeypatch):
    """rev 计算后、读缓存前另一路重爬改写某章缓存。

    合并读到的正文是新内容，但记录的 rev 是旧状态 → 当前版本与 meta.rev
    不符 → export_is_fresh 必须返回 False（保守），绝不把新内容配旧 rev
    误判为新鲜（否则会永久下发错配全文）。"""
    d = str(tmp_path / "book")
    _mk_book(d, CHS, TXT)
    c1 = os.path.join(d, f"{CR._cache_key_of('https://x.test/1')}.cache")
    real = CR.content_revision
    state = {"n": 0}

    def racing(book_dir, chapters):
        rev = real(book_dir, chapters)
        if state["n"] == 0:      # 第一次（合并读前）计算完后立即改写缓存
            with open(c1, "w", encoding="utf-8") as f:
                f.write("第1章\n\n" + "交错改写正文" * 60)
        state["n"] += 1
        return rev

    monkeypatch.setattr(CR, "content_revision", racing)
    out = _merge(d, CHS)
    assert out
    with open(out, encoding="utf-8") as f:
        assert "交错改写正文" in f.read()       # txt 已含新内容

    # 关键：meta.rev 停留在旧状态，当前版本 ≠ meta.rev → 不新鲜
    assert CR.read_export_meta(d).get("rev") != \
        CR.content_revision(d, _named(CHS))
    assert CR.export_is_fresh(d, _named(CHS), out) is False


# ══════════════════════════════════════════════════════════════
# 7. 旧书无源：引擎级本地合并无需书源（不触网）
# ══════════════════════════════════════════════════════════════

def test_merge_book_txt_local_without_source(tmp_path):
    d = str(tmp_path / "book")
    _mk_book(d, CHS, TXT)
    out = CR.merge_book_txt(d, _named(CHS))
    assert out and os.path.exists(out)
    assert CR.export_is_fresh(d, _named(CHS), out) is True
    with open(out, encoding="utf-8") as f:
        body = f.read()
    assert body.index("第1章") < body.index("第2章") < body.index("第3章")


# ══════════════════════════════════════════════════════════════
# 8. 固定条带锁：同一 key 永久同一锁对象，不增长/不淘汰
#    （无"持有者/等待者跨容量被换锁"的竞态，不依赖 not locked() 租约）
# ══════════════════════════════════════════════════════════════

def test_export_lock_is_stable_stripe():
    """同一 book_key 永久映射同一锁对象；海量其它 key 不改变映射、不增长"""
    import server.novel_api as napi
    lk = napi._export_lock("same_book")
    assert napi._export_lock("same_book") is lk
    n_stripes = len(napi._export_lock_stripes)
    assert n_stripes == napi._EXPORT_LOCK_STRIPES
    # 远超条带数的不同 key：表大小仍恒为条带数（无增长），same_book 不变
    for i in range(n_stripes * 5):
        napi._export_lock(f"other_{i}")
    assert len(napi._export_lock_stripes) == n_stripes
    assert napi._export_lock("same_book") is lk


def test_export_lock_holder_and_waiter_survive_many_keys():
    """确定性持有/等待者跨「容量」测试：持有中、且有等待者时，创建海量
    其它 key 也不得让该书换锁；持有者释放后等待者必在同一锁对象上获得。"""
    import server.novel_api as napi
    lk = napi._export_lock("held_book")
    lk.acquire()
    waiter_got = {"obj": None}
    acquired = threading.Event()
    same_obj_ok = {"ok": False}

    def waiter():
        got = napi._export_lock("held_book")
        waiter_got["obj"] = got
        got.acquire()                 # 阻塞在持有者释放前
        same_obj_ok["ok"] = got is lk  # 等待者与持有者必为同一对象
        acquired.set()
        got.release()

    th = threading.Thread(target=waiter)
    th.start()
    try:
        # 跨容量压力：键数远超条带数；若实现会淘汰/换锁，held_book 必被换。
        for i in range(napi._EXPORT_LOCK_STRIPES * 6):
            napi._export_lock(f"pressure_{i}")
        assert napi._export_lock("held_book") is lk      # 不因容量压力换对象
        assert not acquired.wait(0.2)                    # 持有者持锁，等待者必阻塞
    finally:
        lk.release()
    assert acquired.wait(2.0)                            # 释放后等待者获同一锁
    th.join()
    assert waiter_got["obj"] is lk and same_obj_ok["ok"]


def test_merge_lock_is_stable_stripe():
    """合并锁同为固定条带：同一 book_dir 永久同一对象，海量其它目录不改变"""
    lk = CR._merge_lock("/tmp/stripe_book_a")
    assert CR._merge_lock("/tmp/stripe_book_a") is lk
    n_stripes = len(CR._merge_lock_stripes)
    assert n_stripes == CR._MERGE_LOCK_STRIPES
    for i in range(n_stripes * 5):
        CR._merge_lock(f"/tmp/stripe_book_other_{i}")
    assert len(CR._merge_lock_stripes) == n_stripes
    assert CR._merge_lock("/tmp/stripe_book_a") is lk


def test_merge_lock_holder_and_waiter_survive_many_dirs():
    """合并锁的确定性持有/等待者跨容量：海量其它目录不得让同一目录换锁"""
    lk = CR._merge_lock("/tmp/mheld")
    lk.acquire()
    acquired = threading.Event()
    same_obj_ok = {"ok": False}

    def waiter():
        got = CR._merge_lock("/tmp/mheld")
        got.acquire()
        same_obj_ok["ok"] = got is lk
        acquired.set()
        got.release()

    th = threading.Thread(target=waiter)
    th.start()
    try:
        for i in range(CR._MERGE_LOCK_STRIPES * 6):
            CR._merge_lock(f"/tmp/mpressure_{i}")
        assert CR._merge_lock("/tmp/mheld") is lk
        assert not acquired.wait(0.2)
    finally:
        lk.release()
    assert acquired.wait(2.0)
    th.join()
    assert same_obj_ok["ok"]


# ══════════════════════════════════════════════════════════════
# 9. 并发合并：book.txt 与 _export.json 始终成对，版本不指向别的内容
# ══════════════════════════════════════════════════════════════

def test_concurrent_merges_disk_state_pairs_txt_and_meta(tmp_path):
    """并发合并（含旧任务携带的短列表）：以磁盘 _state.json 为权威，
    book.txt 与 _export.json 始终成对，且不被短列表回退。"""
    d = str(tmp_path / "book")
    _mk_book(d, CHS, TXT)                     # _state.json = 完整权威目录
    a = _named(CHS)
    b = _named(CHS[:2])                       # 旧短列表（应被磁盘权威覆盖）
    rev_full = CR.content_revision(d, a)

    def worker(chapters):
        for _ in range(20):
            CR.merge_book_txt(d, chapters, force=True)

    ths = [threading.Thread(target=worker, args=(c,)) for c in (a, a, b, b)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    out = os.path.join(d, "book.txt")
    meta = CR.read_export_meta(d)
    with open(out, encoding="utf-8") as f:
        body = f.read()
    # 磁盘权威 → 版本恒为完整目录的 rev；内容与版本严格成对
    assert meta.get("rev") == rev_full
    assert "第3章" in body
    assert meta.get("txt_size") == os.path.getsize(out)
    assert CR.export_is_fresh(d, a, out) is True


# ══════════════════════════════════════════════════════════════
# 10. 权威目录：旧任务短列表不得回退覆盖完整全文（磁盘 _state.json 为准）
# ══════════════════════════════════════════════════════════════

def test_merge_disk_state_prevents_partial_rollback(tmp_path):
    d = str(tmp_path / "book")
    _mk_book(d, CHS, TXT)                     # _state.json 记录完整目录（权威）
    full = _named(CHS)
    partial = _named(CHS[:2])
    out = os.path.join(d, "book.txt")
    CR.merge_book_txt(d, full, force=True)    # 新版本（完整目录）
    assert CR.export_is_fresh(d, full, out) is True
    # 旧任务携带短列表后到：merge 取磁盘权威目录 → 不得回退
    CR.merge_book_txt(d, partial, force=True)
    with open(out, encoding="utf-8") as f:
        assert "第3章" in f.read()
    assert CR.export_is_fresh(d, full, out) is True


def test_merge_without_state_uses_passed_list(tmp_path):
    """_state.json 不存在 → 按传入 chapters 合并"""
    d = str(tmp_path / "book")
    _mk_book(d, CHS, TXT)
    os.remove(os.path.join(d, "_state.json"))
    out = CR.merge_book_txt(d, _named(CHS[:2]))
    assert out
    with open(out, encoding="utf-8") as f:
        assert "第3章" not in f.read()


def test_merge_invalid_state_falls_back_to_passed(tmp_path):
    """_state.json 损坏/非法（非 JSON、chapters 非法）→ 回退传入 chapters"""
    d = str(tmp_path / "book")
    _mk_book(d, CHS, TXT)
    with open(os.path.join(d, "_state.json"), "w", encoding="utf-8") as f:
        f.write("{ 这不是 JSON")
    out = CR.merge_book_txt(d, _named(CHS[:2]))
    assert out
    with open(out, encoding="utf-8") as f:
        assert "第3章" not in f.read()
    # chapters 为空列表等非法情形同样回退
    with open(os.path.join(d, "_state.json"), "w", encoding="utf-8") as f:
        json.dump({"chapters": []}, f)
    out2 = CR.merge_book_txt(d, _named(CHS[:2]), force=True)
    with open(out2, encoding="utf-8") as f:
        assert "第3章" not in f.read()


# ══════════════════════════════════════════════════════════════
# 11. 未完整读取：读失败/空内容不得出部分 txt，保旧全文且不误判新鲜
# ══════════════════════════════════════════════════════════════

def test_merge_read_failure_preserves_old_txt(tmp_path):
    """某章缓存存在且非空但读取失败（非法 UTF-8）→ 合并失败、旧 txt 保留、
    且绝不被标为新鲜（若静默跳过会写出缺章全文并误判新鲜）。"""
    d = str(tmp_path / "book")
    _mk_book(d, CHS, TXT)
    out = CR.merge_book_txt(d, _named(CHS), force=True)
    with open(out, encoding="utf-8") as f:
        old = f.read()
    # 第二章缓存写入非法 UTF-8 字节（size > 0）→ 读取必失败
    p = os.path.join(d, f"{CR._cache_key_of('https://x.test/2')}.cache")
    with open(p, "wb") as f:
        f.write(b"\xff\xfe\x00 bad utf8 \xff")
    assert CR.merge_book_txt(d, _named(CHS), force=True) is None
    with open(out, encoding="utf-8") as f:
        assert f.read() == old                     # 旧全文未被破坏
    assert CR.export_is_fresh(d, _named(CHS), out) is False   # 不误判新鲜


def test_export_rebuild_read_failure_keeps_old_txt(client, monkeypatch):
    """导出侧：本地重建因读失败 → 200 + 陈旧/错误标记，旧 txt 内容不变"""
    _install_fake_task(monkeypatch, src_ok=False)
    d = _book_in_books_dir("nosuchsrc_readfail", CHS, TXT)
    p = os.path.join(d, "book.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("旧全文")
    bad = os.path.join(d, f"{CR._cache_key_of('https://x.test/1')}.cache")
    with open(bad, "wb") as f:
        f.write(b"\xff\xfe not utf8 \xff")
    r = client.get("/api/books/nosuchsrc_readfail/txt")
    assert r.status_code == 200
    assert r.headers.get("X-Export-Stale") == "1"
    assert r.get_data(as_text=True) == "旧全文"
    with open(p, encoding="utf-8") as f:
        assert f.read() == "旧全文"
