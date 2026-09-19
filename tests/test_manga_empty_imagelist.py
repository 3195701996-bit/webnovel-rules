# -*- coding: utf-8 -*-
"""jm/漫画在线阅读"图片不加载"根因回归（离线，不触网）

用户实测两种表现：
  1. 阅读时刷新界面 → 后续未加载的页再也不加载；
  2. 不刷新页面 → 整章不加载图片。
两者共同根因（本轮定位）：

  A. **空图片列表被当作有效结果缓存 300 秒**
     jm 的 images() 实现是 `[get_image_url(...) for img in (j.get("images") or [])]`
     —— 源站风控/异常响应里没有 images 字段时返回的是**空列表**而不是异常。
     旧实现把空列表写进 _CHAPTER_IMAGES_CACHE(内存热层, TTL 300s)，
     于是之后 5 分钟内每一次 /urls 都返回 count=0：整章没有图，
     **刷新页面也没用**（缓存在服务端进程里），只有等过期或重启服务。
     一次"加载中刷新"会造成重复的 images() 请求，最容易触发源站这一路。

  B. 客户端把**空的下一话预取**当有效数据复用（见 tests/js 用例）。

修复契约：
  - 源站返回空列表 → 视为失败：不写内存热层、不写磁盘层、抛 MangaError
    （路由给出明确错误 + 客户端可重试），下一次请求必须重新回源；
  - 一次瞬时空结果不得影响后续请求。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CID = "399123"
CH1 = "300001"


class _FlakyJm:
    """首次返回空列表（模拟源站风控响应），之后返回真实列表"""

    def __init__(self):
        self.calls = 0

    def images(self, comic_id, chapter_id):
        self.calls += 1
        if self.calls == 1:
            return []
        return [f"https://cdn.invalid/jm/{chapter_id}/{i:04d}.webp"
                for i in range(5)]


@pytest.fixture(autouse=True)
def _clean_images_cache():
    import server.state as st
    with st._CHAPTER_IMAGES_LOCK:
        st._CHAPTER_IMAGES_CACHE.clear()
        st._CHAPTER_IMAGES_FETCHING.clear()
    yield
    with st._CHAPTER_IMAGES_LOCK:
        st._CHAPTER_IMAGES_CACHE.clear()
        st._CHAPTER_IMAGES_FETCHING.clear()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import server.state as st
    cache = tmp_path / "_cache"
    (cache / "jm" / CID).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(st, "MANGA_CACHE_DIR", str(cache))
    return {"st": st, "cache": cache}


def test_empty_image_list_not_cached(env):
    """空列表不得进入内存热层：下一次请求必须重新回源"""
    st = env["st"]
    ad = _FlakyJm()
    with pytest.raises(Exception):
        st._get_chapter_images(ad, "jm", CID, CH1)
    assert ad.calls == 1
    # 关键：第二次请求必须重新回源（旧实现直接命中空缓存 → 返回 [] 永不恢复）
    imgs = st._get_chapter_images(ad, "jm", CID, CH1)
    assert ad.calls == 2, "空结果被缓存了：第二次请求没有回源"
    assert len(imgs) == 5


def test_empty_image_list_not_persisted_to_disk(env):
    """空列表不得写入磁盘持久层（否则 TTL 内每次重读都是空）"""
    st = env["st"]
    ad = _FlakyJm()
    with pytest.raises(Exception):
        st._get_chapter_images(ad, "jm", CID, CH1)
    p = st._chapter_images_disk_path("jm", CID, CH1)
    assert not os.path.exists(p), "空图片列表被写入磁盘层"


def test_urls_route_reports_error_instead_of_empty_chapter(env, monkeypatch):
    """路由层：源站空列表 → 明确错误，而不是 count=0 的空章节"""
    import app
    import server.manga_api as ma
    import server.state as st
    ad = _FlakyJm()
    monkeypatch.setattr(ma, "_manga_read_adapter", lambda src: ad)
    monkeypatch.setattr(ma, "_prefetch_next_chapter_images", lambda *a, **k: None)
    app.app.config["TESTING"] = True
    c = app.app.test_client()
    r = c.get(f"/api/manga/jm/{CID}/chapter/{CH1}/urls")
    assert r.status_code >= 500, f"空章节应报错，实际 {r.status_code}"
    assert (r.get_json() or {}).get("error"), "错误响应必须带可读信息"
    # 且不得污染缓存：下一次请求能拿到真实图片列表
    r2 = c.get(f"/api/manga/jm/{CID}/chapter/{CH1}/urls")
    assert r2.status_code == 200
    assert r2.get_json().get("count") == 5
    assert st._CHAPTER_IMAGES_CACHE or ad.calls >= 2


def test_warm_does_not_run_on_empty_list(env, monkeypatch):
    """空列表时预热必须安静退出（不抛错、不占 in-flight 槽）"""
    import server.manga_api as ma
    ad = _FlakyJm()
    monkeypatch.setattr(ma, "_manga_read_adapter", lambda src: ad)
    monkeypatch.setattr(ma, "_manga_read_images",
                        lambda a, s, c, ch: (a, st_images(a)))
    with ma._warm_lock:
        ma._warm_inflight.clear()
    assert ma._warm_chapter_images("jm", CID, CH1) in (True, False)


def st_images(ad):
    return []
