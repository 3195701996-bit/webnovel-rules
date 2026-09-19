# -*- coding: utf-8 -*-
"""逐源功能级验证（engine/source_verify.py）单元测试。

覆盖诊断 §6 的要求：逐源验证搜索/详情/目录/正文；未验证项不得标成通过；
规则需要 JS 或缺少对应依赖时如实标 unsupported 并给出原因。
"""
import json
import os

import pytest

from engine import source_verify as sv


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "VERIFIED_FILE", os.path.join(str(tmp_path), "source_verified.json"))
    sv.reset_for_tests()
    yield
    sv.reset_for_tests()


def _src(uid="s1", name="测试源", **kw):
    d = {"uid": uid, "bookSourceName": name, "bookSourceUrl": "https://example.com",
         "enabled": True, "searchUrl": "/search?q={{key}}",
         "ruleSearch": {"bookList": ".list@li", "name": ".name@text",
                        "bookUrl": "a@href"}}
    d.update(kw)
    return d


# ── JS 与依赖判定 ────────────────────────────────────────────

def test_needs_js_detects_markers():
    ok, reason = sv.needs_js(_src(ruleContent={"content": "@js:java.ajax(url)"}))
    assert ok is True and "JS" in reason


def test_needs_js_false_for_plain_rules():
    ok, _ = sv.needs_js(_src())
    assert ok is False


def test_required_deps_maps_rule_types():
    assert "cssselect" in sv.required_deps(_src())
    assert "lxml" in sv.required_deps(_src(ruleToc={"chapterList": "//div[@id='list']/a"}))
    assert "jsonpath_ng" in sv.required_deps(_src(ruleSearch={"bookList": "$.data[*]"}))
    assert sv.required_deps({"uid": "x", "bookSourceUrl": "https://a"}) == set()


def test_unsupported_when_dep_missing(monkeypatch):
    monkeypatch.setattr(sv, "_missing_deps", lambda deps: ["jsonpath_ng"])
    r = sv.verify_one(_src(ruleSearch={"bookList": "$.data[*]"}))
    assert r["status"] == "unsupported"
    assert "jsonpath_ng" in r["reason"]
    assert r["stages"] == {}


def test_unsupported_when_js_required():
    r = sv.verify_one(_src(ruleContent={"content": "<js>return 1</js>"}))
    assert r["status"] == "unsupported"
    assert "不执行 JS" in r["reason"]


# ── 阶段归类（不把部分通过当成通过）────────────────────────────

def test_classify_verified_requires_all_three_stages():
    st = {"search": {"ok": True}, "toc": {"ok": True},
          "content": {"ok": True, "chars": 2500}}
    assert sv.classify(st)[0] == "verified"
    for missing in ("toc", "content"):
        bad = dict(st)
        bad[missing] = {"ok": False, "detail": "炸了"}
        status, reason = sv.classify(bad)
        assert status == "partial" and reason
    assert sv.classify({"search": {"ok": False, "detail": "搜索失败"}})[0] == "failed"


def test_classify_never_verified_without_content():
    """只有搜索成功（旧版校验的全部内容）绝不算 verified：归为 partial，并说明缺哪一阶段。"""
    st = {"search": {"ok": True, "count": 10}}
    status, reason = sv.classify(st)
    assert status == "partial"
    assert "目录" in reason


# ── 全流程（用假爬虫，不联网）────────────────────────────────

class _FakeCrawler:
    def __init__(self, search, chapters, text, raise_at=""):
        self._search, self._chapters, self._text, self._raise_at = search, chapters, text, raise_at

    def search(self, keyword, page=1):
        if self._raise_at == "search":
            raise RuntimeError("search boom")
        return self._search

    def get_book(self, url, fast=False, deadline=None):
        return {"name": "书", "book_url": url}

    def get_toc(self, book, max_pages=50, timeout=15, deadline=None):
        if self._raise_at == "toc":
            raise RuntimeError("toc boom")
        return self._chapters

    def get_content(self, url, max_pages=30, timeout=25, deadline=None):
        if self._raise_at == "content":
            raise RuntimeError("content boom")
        return self._text


@pytest.fixture
def fake_crawler(monkeypatch):
    def _install(**kw):
        import engine.crawler as crawler_mod
        monkeypatch.setattr(crawler_mod, "SourceCrawler",
                            lambda src, progress_callback=None: _FakeCrawler(**kw))
    return _install


def test_verify_one_verified_with_evidence(fake_crawler):
    fake_crawler(search=[{"name": "剑来", "book_url": "https://e.com/b/1"}],
                 chapters=[{"name": "第1章", "url": "https://e.com/c/1"},
                           {"name": "第2章", "url": "https://e.com/c/2"}],
                 text="正文内容" * 60)          # 240 字 > MIN_CONTENT_CHARS(200)
    r = sv.verify_one(_src(), keyword="剑来")
    assert r["status"] == "verified"
    assert r["stages"]["search"]["count"] == 1
    assert r["stages"]["toc"]["count"] == 2
    assert r["stages"]["content"]["chars"] == 240
    assert r["stages"]["search"]["sample"]["book_url"] == "https://e.com/b/1"


def test_verify_one_partial_when_content_fails(fake_crawler):
    fake_crawler(search=[{"name": "书", "book_url": "https://e.com/b/1"}],
                 chapters=[{"name": "第1章", "url": "https://e.com/c/1"}],
                 text="   ")
    r = sv.verify_one(_src(), keyword="剑来")
    assert r["status"] == "partial"
    assert "正文" in r["reason"]
    assert r["stages"]["toc"]["ok"] is True


def test_pass_rate_scopes_use_matching_numerators(monkeypatch):
    """跑"含停用源"的批次时，启用口径不能拿全量通过数当分子（曾出现 21/6=350%）。"""
    sv._results = {"tested_at": "t", "items": [
        {"uid": "a", "status": "verified"},          # 启用
        {"uid": "z", "status": "verified"},          # 停用（不在启用列表里）
    ]}
    monkeypatch.setattr(sv, "_source_uids", lambda: (["a", "b"], ["a", "b", "z"]))
    sm = sv.results_payload()["summary"]
    assert sm["verified"] == 2 and sm["verified_enabled"] == 1
    assert sm["pass_rate"] == 50          # 1/2 启用源
    assert sm["pass_rate_all"] == 67      # 2/3 全部源
    assert sm["pass_rate"] <= 100 and sm["pass_rate_all"] <= 100


def test_short_content_is_not_verified(fake_crawler):
    """取到 300 字也算"能读"就会高估通过率（实测踩到过），故低于下限判 partial。"""
    fake_crawler(search=[{"name": "书", "book_url": "https://e.com/b"}],
                 chapters=[{"name": "第1章", "url": "https://e.com/c"}],
                 text="正文" * 50)               # 100 字，低于 200 字下限
    r = sv.verify_one(_src(), keyword="剑来")
    assert r["status"] == "partial"
    assert "正文偏短" in r["reason"]
    assert r["stages"]["content"]["ok"] is True   # 阶段本身成功，但不足以判通过


def test_short_but_acceptable_content_is_verified_with_note(fake_crawler):
    """200~1000 字：算通过，但必须带"建议抽查"的提示（真实批次里出现过 318 字）。"""
    fake_crawler(search=[{"name": "书", "book_url": "https://e.com/b"}],
                 chapters=[{"name": "第1章", "url": "https://e.com/c"}],
                 text="正文" * 159)              # 318 字
    r = sv.verify_one(_src(), keyword="剑来")
    assert r["status"] == "verified"
    assert "建议抽查" in (r.get("note") or "")


def test_results_are_saved_after_each_source(monkeypatch, fake_crawler):
    """长批次被系统杀掉时，已验完的结论必须留下（实测：28 个源的批次被杀后整轮白跑）。"""
    fake_crawler(search=[{"name": "书", "book_url": "https://e.com/b"}],
                 chapters=[{"name": "第1章", "url": "https://e.com/c"}], text="正文" * 150)
    srcs = [_src(f"s{i}", f"源{i}") for i in range(3)]
    monkeypatch.setattr("engine.source_mgr.load_all", lambda: srcs)
    seen = []
    real_save = sv._save

    def _spy(data):
        seen.append(len(data.get("items") or []))
        return real_save(data)

    monkeypatch.setattr(sv, "_save", _spy)
    sv.start(keyword="剑来", limit=3, skip_verified_days=0)
    for _ in range(200):
        if sv.status()["status"] != "running":
            break
        import time as _t
        _t.sleep(0.05)
    # 每验完一个源就写一次（至少 3 次），而不是只在最后写一次
    assert seen[:3] == [1, 2, 3] or seen == [1, 2, 3], seen


def test_skip_tested_days_resumes_after_interruption(monkeypatch, fake_crawler):
    """skip_tested_days>0：最近验过的源（哪怕结果是失败）也跳过，用于中断后续跑。"""
    import time as _t
    fake_crawler(search=[{"name": "书", "book_url": "https://e.com/b"}],
                 chapters=[{"name": "第1章", "url": "https://e.com/c"}], text="正文" * 150)
    srcs = [_src("done", "验过的源"), _src("todo", "没验过的源")]
    monkeypatch.setattr("engine.source_mgr.load_all", lambda: srcs)
    sv._save({"tested_at": "now", "items": [
        {"uid": "done", "status": "failed", "reason": "搜索无结果", "stages": {},
         "tested_at": _t.strftime("%Y-%m-%d %H:%M:%S")},
    ]})
    sv.start(keyword="剑来", skip_verified_days=0, skip_tested_days=1)
    for _ in range(200):
        if sv.status()["status"] != "running":
            break
        _t.sleep(0.05)
    assert sv.load_results()["last_batch"] == ["todo"], sv.load_results().get("last_batch")


def test_skip_verified_days_zero_means_no_skip():
    """显式传 0 时不应按缺省 7 天跳过（接口层曾用 `or 7` 把 0 吃掉）。"""
    import time as _t
    known = {"u": {"status": "verified", "tested_at": _t.strftime("%Y-%m-%d %H:%M:%S")}}
    assert sv._verified_recently("u", 7, known) is True        # 刚通过 → 7 天内跳过
    assert sv._verified_recently("u", 0, known) is False       # 0 天 = 不跳过
    assert sv._verified_recently("v", 7, known) is False       # 没有记录 → 不跳过
    assert sv._verified_recently("u", 7, {}) is False


def test_verify_one_partial_when_toc_fails(fake_crawler):
    fake_crawler(search=[{"name": "书", "book_url": "https://e.com/b/1"}],
                 chapters=[], text="x")
    r = sv.verify_one(_src(), keyword="剑来")
    assert r["status"] == "partial" and "目录" in r["reason"]


def test_verify_one_failed_when_search_empty(fake_crawler):
    fake_crawler(search=[], chapters=[], text="")
    r = sv.verify_one(_src(), keyword="不存在")
    assert r["status"] == "failed"
    assert r["stages"]["search"]["ok"] is False


def test_verify_one_survives_crawler_exception(fake_crawler):
    fake_crawler(search=[], chapters=[], text="", raise_at="search")
    r = sv.verify_one(_src())
    assert r["status"] == "failed" and "boom" in r["stages"]["search"]["detail"]


# ── 运行编排与持久化 ────────────────────────────────────────

def test_start_skips_disabled_by_default(monkeypatch, fake_crawler, tmp_path):
    fake_crawler(search=[{"name": "书", "book_url": "https://e.com/b"}],
                 chapters=[{"name": "第1章", "url": "https://e.com/c"}], text="正文" * 150)
    srcs = [_src("on1", "启用源"), _src("off1", "停用源", enabled=False)]
    monkeypatch.setattr("engine.source_mgr.load_all", lambda: srcs)

    started = sv.start(keyword="剑来")
    assert started["started"] is True
    for _ in range(100):
        if sv.status()["status"] != "running":
            break
        import time as _t
        _t.sleep(0.05)
    payload = sv.results_payload()
    uids = [i["uid"] for i in payload["items"]]
    assert uids == ["on1"], uids
    assert payload["counts"].get("verified") == 1
    # 结果必须落盘且可读回
    with open(sv.VERIFIED_FILE, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["items"][0]["status"] == "verified"


def test_start_honours_limit(monkeypatch, fake_crawler):
    fake_crawler(search=[{"name": "书", "book_url": "https://e.com/b"}],
                 chapters=[{"name": "第1章", "url": "https://e.com/c"}], text="正文" * 150)
    srcs = [_src(f"s{i}", f"源{i}") for i in range(5)]
    monkeypatch.setattr("engine.source_mgr.load_all", lambda: srcs)
    sv.start(keyword="剑来", limit=2)
    for _ in range(100):
        if sv.status()["status"] != "running":
            break
        import time as _t
        _t.sleep(0.05)
    assert len(sv.results_payload()["items"]) == 2


def test_start_refuses_concurrent_run(monkeypatch, fake_crawler):
    import threading
    gate = threading.Event()

    class _Slow(_FakeCrawler):
        def search(self, keyword, page=1):
            gate.wait(5)
            return [{"name": "书", "book_url": "https://e.com/b"}]

    import engine.crawler as crawler_mod
    monkeypatch.setattr(crawler_mod, "SourceCrawler",
                        lambda src, progress_callback=None: _Slow(
                            search=[], chapters=[{"name": "第1章", "url": "https://e.com/c"}],
                            text="正文" * 150))
    monkeypatch.setattr("engine.source_mgr.load_all", lambda: [_src("a", "源A"), _src("b", "源B")])

    first = sv.start(keyword="剑来")
    assert first["started"] is True
    second = sv.start(keyword="剑来")
    assert second["started"] is False and second.get("already_running") is True
    gate.set()
    for _ in range(100):
        if sv.status()["status"] != "running":
            break
        import time as _t
        _t.sleep(0.05)


def test_results_payload_counts_are_honest():
    sv._results = {"tested_at": "t", "keyword": "k", "items": [
        {"uid": "a", "status": "verified"}, {"uid": "b", "status": "partial"},
        {"uid": "c", "status": "failed"}, {"uid": "d", "status": "unsupported"},
    ]}
    p = sv.results_payload()
    assert p["counts"] == {"verified": 1, "partial": 1, "failed": 1, "unsupported": 1}
    assert p["total"] == 4
    assert "pending" not in p["counts"]      # 没验证过的不出现在结果里，而不是算作通过


def test_load_results_missing_file_is_none():
    assert sv.load_results() is None


# ── 分批、合并与通过率（本轮新增）─────────────────────────────

def test_save_merges_results_across_batches(monkeypatch):
    """第二批结果不得冲掉第一批的结论（此前每轮覆盖，导致无法统计通过率）。"""
    sv._save({"tested_at": "t1", "items": [
        {"uid": "a", "status": "verified", "tested_at": "t1", "stages": {}},
        {"uid": "b", "status": "failed", "tested_at": "t1", "stages": {}},
    ]})
    sv._save({"tested_at": "t2", "items": [
        {"uid": "b", "status": "verified", "tested_at": "t2", "stages": {}},
        {"uid": "c", "status": "partial", "tested_at": "t2", "stages": {}},
    ]})
    got = {i["uid"]: i["status"] for i in sv.results_payload()["items"]}
    assert got == {"a": "verified", "b": "verified", "c": "partial"}   # b 被新结论覆盖
    assert sv.load_results()["last_batch"] == ["b", "c"]


def test_start_offset_selects_later_sources(monkeypatch, fake_crawler):
    """没有 offset 时"再跑一批"永远从第一个源开始，后面的源永远轮不到（实测缺陷）。"""
    fake_crawler(search=[{"name": "书", "book_url": "https://e.com/b"}],
                 chapters=[{"name": "第1章", "url": "https://e.com/c"}], text="正文" * 150)
    srcs = [_src(f"s{i}", f"源{i}") for i in range(5)]
    monkeypatch.setattr("engine.source_mgr.load_all", lambda: srcs)
    sv.start(keyword="剑来", limit=2, offset=2)
    for _ in range(100):
        if sv.status()["status"] != "running":
            break
        import time as _t
        _t.sleep(0.05)
    uids = [i["uid"] for i in sv.results_payload()["items"]]
    assert uids == ["s2", "s3"], uids


def test_never_attempted_sources_come_first(monkeypatch, fake_crawler):
    """失败过的源不能永远排在队首，否则后续未验证的源永远轮不到。"""
    fake_crawler(search=[{"name": "书", "book_url": "https://e.com/b"}],
                 chapters=[{"name": "第1章", "url": "https://e.com/c"}], text="正文" * 150)
    srcs = [_src("a", "失败过的源"), _src("b", "从未验证的源")]
    monkeypatch.setattr("engine.source_mgr.load_all", lambda: srcs)
    sv._save({"tested_at": "old", "items": [
        {"uid": "a", "status": "failed", "reason": "搜索失败",
         "tested_at": "2020-01-01 00:00:00", "stages": {}},
    ]})
    sv.start(keyword="剑来", limit=1)
    for _ in range(100):
        if sv.status()["status"] != "running":
            break
        import time as _t
        _t.sleep(0.05)
    last_batch = sv.load_results()["last_batch"]
    assert last_batch == ["b"], last_batch


def test_start_skips_recently_verified(monkeypatch, fake_crawler):
    fake_crawler(search=[{"name": "书", "book_url": "https://e.com/b"}],
                 chapters=[{"name": "第1章", "url": "https://e.com/c"}], text="正文" * 150)
    srcs = [_src("a", "已通过源"), _src("b", "待验证源")]
    monkeypatch.setattr("engine.source_mgr.load_all", lambda: srcs)
    import time as _t
    sv._save({"tested_at": "now", "items": [
        {"uid": "a", "status": "verified", "stages": {},
         "tested_at": _t.strftime("%Y-%m-%d %H:%M:%S")},
    ]})
    sv.start(keyword="剑来", limit=5)
    for _ in range(100):
        if sv.status()["status"] != "running":
            break
        _t.sleep(0.05)
    items = {i["uid"]: i["status"] for i in sv.results_payload()["items"]}
    assert items["b"] == "verified"
    # 0.59.0 语义修正：近期已通过 → 本轮**跳过重测**，但跳过**不得覆盖已有结论**
    # （此前 skipped 会把 verified 顶掉，分批跑第二轮后台账就把"已验证"退回"待验证"，
    #   用户看到的是"越跑越差"；实测在小说源台账上撞到 4 个源被这样盖掉）。
    assert items["a"] == "verified", "跳过不得覆盖已验证结论"
    row_a = next(i for i in sv.results_payload()["items"] if i["uid"] == "a")
    assert "不重复验证" in row_a["skipped_reason"], "但必须留下『本轮跳过』的痕迹与原因"
    assert row_a["skipped_at"], "跳过时间也要记下来"


def test_summary_pass_rate_counts_never_attempted(monkeypatch):
    sv._results = {"tested_at": "t", "items": [
        {"uid": "a", "status": "verified"}, {"uid": "b", "status": "failed"},
    ]}
    # a 启用且通过、b 启用但失败；c/d 启用未验；e 是停用源
    monkeypatch.setattr(sv, "_source_uids", lambda: (["a", "b", "c", "d"], ["a", "b", "c", "d", "e"]))
    sm = sv.results_payload()["summary"]
    assert sm["verified"] == 1 and sm["failed"] == 1
    assert sm["never_attempted"] == 2          # c、d 从未验证，必须显式暴露
    assert sm["pass_rate"] == 25               # 1/4（启用口径：分子只数启用源中的通过）
    assert sm["verified_enabled"] == 1
    assert sm["enabled_total"] == 4
    assert sm["all_total"] == 5 and sm["pass_rate_all"] == 20   # 全量口径 1/5
    assert sm["never_attempted_all"] == 3


def test_summary_empty_is_zero_not_hundred(monkeypatch):
    monkeypatch.setattr(sv, "_source_uids", lambda: ([], []))
    sm = sv.summary_of([])
    assert sm["pass_rate"] == 0 and sm["enabled_total"] == 0 and sm["never_attempted"] == 0


def test_toc_is_retried_once_on_flaky_failure(monkeypatch):
    """取目录失败时重试一次：移动网络下常常只是一次抖动（同 URL 用 curl 实测 200/115KB）。
    但只重试一次，且失败时如实记录尝试次数——不能把真失败重试成"看起来通过"。"""
    import engine.crawler as crawler_mod

    class _Flaky:
        def __init__(self):
            self.calls = 0

        def search(self, keyword, page=1):
            return [{"name": "书", "book_url": "https://e.com/b/1"}]

        def get_book(self, url, fast=False, deadline=None):
            return {"name": "书"}

        def get_toc(self, book, max_pages=50, timeout=15, deadline=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("quanben.io 目录页获取失败")   # 第一次抖动
            return [{"name": "第1章", "url": "https://e.com/c/1"}]

        def get_content(self, url, max_pages=30, timeout=25, deadline=None):
            return "正文" * 150

    flaky = _Flaky()
    monkeypatch.setattr(crawler_mod, "SourceCrawler", lambda src, progress_callback=None: flaky)
    r = sv.verify_one(_src(), keyword="剑来")
    assert r["status"] == "verified"          # 抖动被一次重试救回
    assert r["stages"]["toc"]["attempts"] == 2
    assert flaky.calls == 2


def test_toc_failure_records_attempts(monkeypatch):
    """两次都失败：如实记 attempts=2 并保持 partial，不因为"重试过"就改判通过。"""
    import engine.crawler as crawler_mod

    class _Always:
        def search(self, keyword, page=1):
            return [{"name": "书", "book_url": "https://e.com/b/1"}]

        def get_book(self, url, fast=False, deadline=None):
            return {"name": "书"}

        def get_toc(self, book, max_pages=50, timeout=15, deadline=None):
            raise RuntimeError("目录页获取失败")

        def get_content(self, url, max_pages=30, timeout=25, deadline=None):
            return ""

    monkeypatch.setattr(crawler_mod, "SourceCrawler", lambda src, progress_callback=None: _Always())
    r = sv.verify_one(_src(), keyword="剑来")
    assert r["status"] == "partial"
    assert r["stages"]["toc"]["attempts"] == 2
    assert "目录页获取失败" in r["reason"]
