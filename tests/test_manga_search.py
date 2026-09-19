# -*- coding: utf-8 -*-
"""漫画搜索分页契约回归（离线：不触网、不读用户数据）

覆盖第二轮评审 P1-2/P1-3/P1-4：
total_hits 取 max 与前端 page*30 反推在多源/去重/末页场景下的失准。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config import MANGA_PAGE_SIZE  # noqa: E402
from engine.manga.base import Comic  # noqa: E402

PS = MANGA_PAGE_SIZE


class _FakeAdapter:
    """最小适配器桩：受控返回条数与源站 total"""

    def __init__(self, key, count, total=0, title_mode="unique"):
        self.key = key
        self.name = key
        self._count = count
        self._total = total
        self._title_mode = title_mode

    def search(self, keyword, page=1):
        out = []
        for i in range(self._count):
            if self._title_mode == "same":
                title = "SAME"
            elif self._title_mode == "dup_id":
                title = f"T-{self.key}-{i // 2}"   # 标题不同但 id 重复
            else:
                title = f"T-{self.key}-{i}"
            _id = (f"{self.key}-{page}-{i // 2}"
                   if self._title_mode == "dup_id" else f"{self.key}-{page}-{i}")
            out.append(Comic(id=_id, title=title,
                             author="", cover="", tags=[],
                             source_key=self.key, total=self._total))
        return out


class _OrderOnly(_FakeAdapter):
    """仅此类支持 order —— 用于校验签名探测而非 except TypeError"""

    def search(self, keyword, page=1, order=None):
        self.seen_order = order
        return super().search(keyword, page)


class _RaisesTypeError(_FakeAdapter):
    """内部抛 TypeError：旧实现会误判签名并重复搜索一次"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = 0

    def search(self, keyword, page=1):
        self.calls += 1
        raise TypeError("bad payload from source")


@pytest.fixture
def do_search(monkeypatch):
    """注入桩适配器后调用真实聚合逻辑"""
    import server.manga_api as A  # R47: 搜索实现已拆入 manga_api

    import engine.manga.manager as M

    def _run(adapters, page=1, order="", source=""):
        reg = {a.key: a for a in adapters}
        # _do_manga_search 内部 from ... import list_adapters，
        # 并经 _manga_adapter/_manga_read_adapter 取实例——打桩这两处真实来源
        monkeypatch.setattr(M, "list_adapters",
                            lambda: [{"key": k} for k in reg])
        monkeypatch.setattr(A, "_load_manga_adapters", lambda: None)
        monkeypatch.setattr(A, "_manga_adapter", lambda k: reg.get(k))
        monkeypatch.setattr(A, "_manga_read_adapter", lambda k: reg.get(k))
        return A._do_manga_search("kw", source, page, order)

    return _run


def test_full_page_reports_more(do_search):
    r = do_search([_FakeAdapter("a", PS, total=376)])
    assert r["has_more"] is True
    assert r["page_size"] == PS
    assert r["raw_count"] == PS


def test_last_page_stops(do_search):
    """末页 16 条且已覆盖 total → 不应再报有下一页"""
    r = do_search([_FakeAdapter("a", 16, total=376)], page=13)
    assert r["has_more"] is False


def test_out_of_range_page_is_empty_and_stops(do_search):
    r = do_search([_FakeAdapter("a", 0, total=376)], page=99)
    assert r["results"] == []
    assert r["has_more"] is False
    assert r["raw_count"] == 0


def test_same_title_never_deduped(do_search):
    """R65: 搜索结果不去重——同标题不同 (source,id) 是不同作品，
    全部独立保留（旧实现按标题合并，同源同名会丢条导致搜不到站内同名作）。"""
    r = do_search([_FakeAdapter("a", PS, title_mode="same"),
                   _FakeAdapter("b", PS, title_mode="same")])
    assert len(r["results"]) == PS * 2
    assert r["raw_count"] == PS * 2
    assert r["has_more"] is True


def test_duplicate_same_source_id_collapsed(do_search):
    """真正同 (source, id) 的重复才去重（源站异常返回重复条时）"""
    r = do_search([_FakeAdapter("a", PS * 2, title_mode="dup_id")])
    assert len(r["results"]) == PS
    assert r["raw_count"] == PS * 2


def test_source_without_total_uses_page_fill(do_search):
    """jm 类不返回 total 的源：靠本页是否拉满判断"""
    r = do_search([_FakeAdapter("a", PS, total=0)])
    assert r["has_more"] is True
    r2 = do_search([_FakeAdapter("a", 5, total=0)])
    assert r2["has_more"] is False


def test_mixed_sources_one_exhausted(do_search):
    """a 已到底、b 仍有 → 整体仍有下一页，且每源数据独立可见"""
    r = do_search([_FakeAdapter("a", 5, total=5),
                   _FakeAdapter("b", PS, total=900)])
    assert r["has_more"] is True
    assert r["per_source_total"]["a"]["count"] == 5
    assert r["per_source_total"]["b"]["total"] == 900


def test_order_passed_only_to_supporting_adapter(do_search):
    ad = _OrderOnly("jm", 3)
    do_search([ad], order="mp")
    assert ad.seen_order == "mp"


def test_internal_typeerror_does_not_retry(do_search):
    """适配器内部 TypeError 不得被当作签名不匹配而重发搜索"""
    ad = _RaisesTypeError("a", 3)
    r = do_search([ad], order="mp")
    assert ad.calls == 1
    assert r["results"] == []


def test_failure_recorded_in_errors_not_silent_empty(do_search):
    """R66: 源失败/超时进入 errors——与"真的没结果"区分,
    前端据此提示重试而非误导"源站没有该作品"。
    失败源同时成功源正常返回时: 结果保留 + errors 记录失败源"""
    bad = _RaisesTypeError("bad", 3)
    ok = _FakeAdapter("ok", 5)
    r = do_search([bad, ok])
    assert len(r["results"]) == 5          # 成功源结果不丢
    assert "bad" in r["errors"]            # 失败源进入 errors
    assert r["errors"]["bad"]
    assert "ok" not in r["errors"]


def test_all_failed_has_empty_results_plus_errors(do_search):
    bad = _RaisesTypeError("bad", 3)
    r = do_search([bad])
    assert r["results"] == []
    assert "bad" in r["errors"]
