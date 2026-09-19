# -*- coding: utf-8 -*-
"""漫画「排行/分类浏览」契约回归（离线：不触网、不读用户数据）

背景：漫画侧没有 Legado 的 exploreUrl，探索页只列**适配器自己声明了
categories()/browse()** 的源。禁漫天堂的 /categories 与 /categories/filter
是 APP API 实测可用端点，本文件把它的口径钉住：

1. 只暴露顶层分类（子分类 slug 跨父类重名，当筛选条件会串味）
2. 排序参数 o 与搜索一致；未知 key 一律回落 mr，不瞎编
3. 分类列表拿不到时**只丢分类**，排行入口仍在（不整页报错）
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.manga.jm import Jm  # noqa: E402

CATS_FIXTURE = {
    "categories": [
        {"id": 0, "name": "最新A漫", "slug": "", "total_albums": 0},
        {"id": "1", "name": "同人", "slug": "doujin", "total_albums": "348446",
         "sub_categories": [{"CID": "1", "name": "漢化", "slug": "chinese"}]},
        {"id": "2", "name": "單本", "slug": "single", "total_albums": "42838",
         "sub_categories": [{"CID": "5", "name": "漢化", "slug": "chinese"}]},
        {"id": "6", "name": "English Manga", "slug": "meiman", "total_albums": "397296"},
    ],
    "blocks": [],
}

# /categories/filter 的 content[] 项（搜索结果同款字段）
ITEM = {
    "id": "1472788", "author": "黃偉達", "name": "[黃偉達] 雲海警殤 後半部分",
    "image": "", "category": {"id": "4", "title": "其他類"},
    "category_sub": {"id": None, "title": None},
}

def _patch_adapter(monkeypatch, adapter):
    """把路由**实际使用**的解析函数换成假适配器。

    必须 patch `server.manga_api` 上的名字：该模块在导入时 `from server.state import
    ... _manga_read_adapter ...`，因此 patch `state` 上的同名函数不会影响路由的引用
    （实测：patch state 无效、仍打真实源站 2.1s）。
    同时清掉实例缓存，保证与用例顺序无关。
    """
    import server.manga_api as mapi
    import server.state as st
    st._manga_read_instances.clear()
    st._manga_adapter_instances.clear()
    monkeypatch.setattr(mapi, "_manga_read_adapter", lambda k: adapter, raising=False)
    monkeypatch.setattr(mapi, "_manga_adapter", lambda k: adapter, raising=False)



@pytest.fixture(autouse=True)
def _clear_adapter_instance_caches():
    """每个用例前清掉适配器**实例缓存**。

    `_manga_read_adapter` / `_manga_adapter` 都带实例缓存（`_manga_read_instances` /
    `_manga_adapter_instances`）。本文件多处用 `monkeypatch.setattr(mgr, "get_adapter", ...)`
    来替换适配器，但只要同进程更早的用例实例化过真实适配器，patch 就不再生效 →
    "单独跑绿、全量跑红"的顺序相关 flaky（实测踩到）。
    """
    import server.state as _st
    _st._manga_read_instances.clear()
    _st._manga_adapter_instances.clear()
    yield
    _st._manga_read_instances.clear()
    _st._manga_adapter_instances.clear()


class _Recorder:
    """记录 _get 被请求的 URL；按端点分流返回预设响应（/categories 与
    /categories/filter 前缀相同，因此不做字符串包含匹配，直接分流）。"""

    def __init__(self, cats=None, content=None, raise_cats=False, raise_list=False):
        self.urls = []
        self._cats = cats if cats is not None else CATS_FIXTURE
        self._content = content if content is not None else [ITEM]
        self._raise_cats = raise_cats
        self._raise_list = raise_list

    def __call__(self, url, timeout=15):
        self.urls.append(url)
        if "/categories/filter" in url or "/search?" in url:
            if self._raise_list:
                raise RuntimeError("boom:filter")
            return json.dumps({"content": self._content, "total": 10000},
                              ensure_ascii=False)
        if self._raise_cats:
            raise RuntimeError("boom:categories")
        return json.dumps(self._cats, ensure_ascii=False)

    @property
    def last(self):
        return self.urls[-1]


@pytest.fixture(autouse=True)
def _clear_cats_cache():
    """分类清单有进程内 TTL 缓存（避免探索页每次都被一次网络往返拖住），
    测试之间必须清掉，否则后一个用例会读到前一个的假数据。"""
    Jm._CATS_CACHE.update(at=0.0, data=[])
    yield
    Jm._CATS_CACHE.update(at=0.0, data=[])


@pytest.fixture
def jm(tmp_path, monkeypatch):
    ad = Jm(state_dir=str(tmp_path))
    # 不触网：图片域刷新与域名拉取都跳过（状态目录里已有回落服务器）
    monkeypatch.setattr(ad, "refresh_img_domain", lambda *a, **k: False)
    monkeypatch.setattr(ad, "_img_refreshed", True)
    return ad


def _with(ad, monkeypatch, rec):
    monkeypatch.setattr(ad, "_get", rec)
    return rec


# ── categories() ──

def test_categories_orders_then_top_categories(jm, monkeypatch):
    rec = _Recorder()
    _with(jm, monkeypatch, rec)
    cats = jm.categories()
    keys = [c["key"] for c in cats]
    # 6 个排序入口 + 3 个非空 slug 的顶层分类
    assert keys[:6] == ["o:mr", "o:mv", "o:mp", "o:tf", "o:tr", "o:md"]
    assert keys[6:] == ["c:doujin", "c:single", "c:meiman"]
    assert len(keys) == len(set(keys))                     # 不重复
    assert all(c["group"] == jm.ORDER_GROUP for c in cats[:6])
    assert all(c["group"] == jm.CATEGORY_GROUP for c in cats[6:])
    assert [c["name"] for c in cats[6:]] == ["同人", "單本", "English Manga"]


def test_categories_skip_empty_slug_and_dedupe(jm, monkeypatch):
    """slug 为空的那条就是"最新A漫"（等价 o:mr），不能重复列；重名 slug 只留一条。"""
    payload = {"categories": [
        {"id": 0, "name": "最新A漫", "slug": ""},
        {"id": "1", "name": "同人", "slug": "doujin"},
        {"id": "9", "name": "同人（重复）", "slug": "doujin"},
    ]}
    _with(jm, monkeypatch, _Recorder(cats=payload))
    keys = [c["key"] for c in jm.categories()]
    assert keys.count("c:doujin") == 1
    assert "c:" not in keys


def test_categories_survive_category_api_failure(jm, monkeypatch):
    """分类接口挂了不整页报错：排序入口仍在（宁可少列，不让探索页因它变空）。"""
    _with(jm, monkeypatch, _Recorder(raise_cats=True))
    cats = jm.categories()
    assert [c["key"] for c in cats] == ["o:mr", "o:mv", "o:mp", "o:tf", "o:tr", "o:md"]


# ── browse() ──

def test_browse_order_url(jm, monkeypatch):
    rec = _with(jm, monkeypatch, _Recorder())
    jm.browse("o:mv", 1)
    assert rec.last.endswith("/categories/filter?page=1&o=mv")


def test_browse_category_url_keeps_order_param(jm, monkeypatch):
    rec = _with(jm, monkeypatch, _Recorder())
    jm.browse("c:doujin", 2)
    url = rec.last
    assert "/categories/filter?page=2" in url
    assert "c=doujin" in url and "o=mr" in url


def test_browse_unknown_key_falls_back_to_latest(jm, monkeypatch):
    """未知 key 不许拼进 URL 乱试（会拿到默认列表却标成用户点的分类）。"""
    rec = _with(jm, monkeypatch, _Recorder())
    jm.browse("o:zzz", 1)
    assert rec.last.endswith("/categories/filter?page=1&o=mr")
    jm.browse("garbage", 1)
    assert rec.last.endswith("/categories/filter?page=1&o=mr")


def test_browse_bad_page_is_clamped(jm, monkeypatch):
    rec = _with(jm, monkeypatch, _Recorder())
    jm.browse("o:mr", "0")
    assert "page=1" in rec.last
    jm.browse("o:mr", None)
    assert "page=1" in rec.last


def test_browse_maps_items_like_search(jm, monkeypatch):
    _with(jm, monkeypatch, _Recorder())
    out = jm.browse("o:mr", 1)
    assert len(out) == 1
    c = out[0]
    assert c.id == "1472788"
    assert c.author == "黃偉達"
    assert c.tags == ["其他類"]                      # category_sub 为空则不塞空串
    assert "/media/albums/1472788_3x4.jpg" in c.cover
    assert c.source_key == "jm"


def test_search_ignores_items_without_id(jm, monkeypatch):
    """共用映射：无 id 的脏数据不能变成空 id 的 Comic（会污染去重与详情跳转）。"""
    payload = {"content": [{"name": "无 id"}, ITEM]}
    _with(jm, monkeypatch, _Recorder(content=payload["content"]))
    out = jm.search("測試")
    assert [c.id for c in out] == ["1472788"]


# ── 探索能力清单（server/novel_api._manga_explore_info）──

def _explore_info(monkeypatch, adapters):
    """adapters: {key: adapter 或 None}"""
    import engine.manga.manager as mgr
    infos = [{"key": k, "name": k, "version": "1"} for k in adapters]
    monkeypatch.setattr(mgr, "list_adapters", lambda: infos)
    monkeypatch.setattr(mgr, "get_adapter",
                        lambda k, state_dir=None: adapters.get(k))
    import server.novel_api as na
    return na._manga_explore_info()


class _NoBrowse:
    pass


class _WithBrowse:
    def categories(self):
        return [{"key": "o:mv", "name": "最多观看", "group": "排行（全站）"},
                {"key": "c:doujin", "name": "同人", "group": "分类"}]


def test_explore_info_lists_group_and_skips_others(monkeypatch):
    info = _explore_info(monkeypatch, {"jm": _WithBrowse(), "other": _NoBrowse()})
    assert info["supported"] is True
    assert [s["key"] for s in info["sources"]] == ["jm"]
    cats = info["sources"][0]["categories"]
    assert cats[0] == {"key": "o:mv", "name": "最多观看", "group": "排行（全站）"}
    assert cats[1]["group"] == "分类"


def test_explore_info_false_when_none_declares(monkeypatch):
    info = _explore_info(monkeypatch, {"other": _NoBrowse()})
    assert info["supported"] is False and info["sources"] == []
    assert "未提供排行/分类接口" in info["reason"]


def test_explore_info_ignores_broken_categories(monkeypatch):
    class _Boom:
        def categories(self):
            raise RuntimeError("统计炸了")

    info = _explore_info(monkeypatch, {"jm": _Boom()})
    assert info["supported"] is False and info["sources"] == []


# ── HTTP 端点口径 ──

@pytest.fixture
def client():
    import app
    return app.app.test_client()


def test_endpoint_unknown_category_404(client, monkeypatch):
    import engine.manga.manager as mgr
    _patch_adapter(monkeypatch, _WithBrowse())
    r = client.get("/api/manga/browse?source=jm&category=c:nope")
    assert r.status_code == 404
    assert "分类" in r.get_json()["error"]


def test_endpoint_source_without_categories_404(client, monkeypatch):
    # 直接 patch **解析函数**（而不是 manager.get_adapter）：
    # `_manga_read_adapter` 有实例缓存 `_manga_read_instances`，只要同进程里更早的
    # 用例实例化过真实 jm 读适配器，patch get_adapter 就不再生效 → 本用例会拿到
    # 真实适配器并返回 200（实测：单独跑绿、全量跑红，属顺序相关的隐藏 flaky）。
    _patch_adapter(monkeypatch, _NoBrowse())
    r = client.get("/api/manga/browse?source=jm")
    assert r.status_code == 404
    assert "未提供排行/分类" in r.get_json()["error"]


def test_categories_cached_within_ttl(jm, monkeypatch):
    """清单接口每次都要问一遍分类：TTL 内只允许一次真实请求。"""
    rec = _with(jm, monkeypatch, _Recorder())
    first = jm.categories()
    second = jm.categories()
    assert first == second
    assert sum(1 for u in rec.urls if u.endswith("/categories")) == 1
    cats_calls = [u for u in rec.urls if "/categories/filter" not in u]
    assert len(cats_calls) == 1


def test_categories_cache_expires(jm, monkeypatch):
    """TTL 过期后必须重新取（不能把陈旧分类永远钉住）。"""
    import engine.manga.jm as jm_mod
    rec = _with(jm, monkeypatch, _Recorder())
    jm.categories()
    Jm._CATS_CACHE["at"] -= (jm_mod.Jm._CATS_TTL + 1)
    jm.categories()
    assert len([u for u in rec.urls if "/categories/filter" not in u]) == 2


def test_categories_failure_does_not_poison_cache(jm, monkeypatch):
    """取失败不能把空清单缓存起来：源站恢复后必须能拿到分类。"""
    _with(jm, monkeypatch, _Recorder(raise_cats=True))
    assert [c["key"] for c in jm.categories()] == ["o:mr", "o:mv", "o:mp", "o:tf", "o:tr", "o:md"]
    _with(jm, monkeypatch, _Recorder())
    keys = [c["key"] for c in jm.categories()]
    assert "c:doujin" in keys
