# -*- coding: utf-8 -*-
"""回归：/urls 不得把"部分缓存"截断成"整章就这么多页"（离线）

用户报告："在线阅读时若在加载时刷新界面会导致后续未加载漫画不再加载"。

根因（本轮定位，实测复现）：/urls 开头的"完全本地"短路用
    _n0 = max(本地序号) + 1;  if len(本地序号) == _n0: 宣布整章就 _n0 页
只验证了"本地文件连续（0..max 无缺页）"，**没有**验证"是否整章"。阅读缓存里
只落了前 6~8 页时（加载中刷新、预热跑一半、网络慢）就被宣布成"整章 6~8 页"，
客户端根本不知道还有第 9 页 → 后续页面永远不再加载，且不可自愈。

修复契约：
  1. 阅读缓存目录（_cache/）里"连续但有权威页数且不足"→ 不得短路；
     必须返回完整页数（已缓存页 local + 其余 lazy），并触发预热；
  2. 混淆源（jm）缺页必须是 lazy 服务器通道，绝不能给浏览器直连 CDN
     的乱序图；
  3. 已下载目录（downloads/）无权威页数时仍保持原短路（整章下载语义，
     零源站请求）；
  4. 两层 URL 缓存都没有权威页数时，宁可做一次有界回源，也不截断。
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 混淆源的真实处理版本（jm）：**跟着真实实现走**。服务端对真实源按 adapter_meta
# 的版本判定缓存新旧，用例里写死版本会在实现提升后立刻失效（0.65.0 就踩到）。
from engine.manga.jm import PROCESS_VERSION as JM_PV  # noqa: E402

CID = "399123"
CH1 = "300001"
N_PAGES = 20
PARTIAL = 6


class _ScrambledAd:
    """混淆源：images() 给 CDN 直链（浏览器直连会拿到乱序图）。

    0.61.0 起混淆源带**图片处理版本**（真实 jm 现为 PROCESS_VERSION=3，0.65.0 提升）：
    本地短路必须过版本判定，所以本文件凡是要验证"短路口径"的用例都需要
    先用 `_mark_current()` 把目录标成当前版本——真实链路由下载器写入该标记。
    """
    key = "jm"
    PROCESS_VERSION = JM_PV

    def unscramble_image(self, data, ep_id=None, url=None):
        return data

    def images(self, comic_id, chapter_id):
        return [f"https://cdn.invalid/jm/{chapter_id}/{i:05d}.jpg"
                for i in range(N_PAGES)]


class _PlainAd:
    def images(self, comic_id, chapter_id):
        return [f"https://cdn.invalid/plain/{chapter_id}/{i:05d}.jpg"
                for i in range(N_PAGES)]


def _touch_pages(d, n, ext=".jpg"):
    os.makedirs(d, exist_ok=True)
    for i in range(n):
        with open(os.path.join(d, f"{i:04d}{ext}"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 2048)
    return d


def _mark_current(d):
    """把章节目录标记为**当前处理版本**（0.61.0 起本地短路必须过版本判定）。

    真实链路上这一步由下载器/读图通道完成（`_processed.json`）；用例里手工造的
    目录没有标记，会被新规则判成"旧版缓存"而不再短路——所以凡是要验证
    "本地短路"的用例，都必须先把目录标成当前版本。
    """
    ver = JM_PV   # 与真实 jm 当前处理版本一致（服务端对真实源按 adapter_meta 判定）
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "_processed.json"), "w", encoding="utf-8") as f:
        json.dump({"jm": {"version": ver}}, f)
    return d


def _write_url_list(cache, ch, n=N_PAGES):
    p = os.path.join(str(cache), "jm", CID, f"{ch}_imgs.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"v": 2, "ts": time.time(),
                   "urls": [f"https://cdn.invalid/jm/{ch}/{i:05d}.jpg"
                            for i in range(n)]}, f)
    return p


@pytest.fixture(autouse=True)
def _clean_state():
    import server.state as st
    import server.manga_api as ma
    with st._CHAPTER_IMAGES_LOCK:
        st._CHAPTER_IMAGES_CACHE.clear()
        st._CHAPTER_IMAGES_FETCHING.clear()
    # 预热器是同一轮改动引入的：缺失时不应让用例在 setup 阶段就 ERROR，
    # 而要让它跑到行为断言上（旧实现应因"整章被截断"而断言失败）
    if hasattr(ma, "_warm_lock"):
        with ma._warm_lock:
            ma._warm_inflight.clear()
            ma._warm_gen.clear()
    yield
    with st._CHAPTER_IMAGES_LOCK:
        st._CHAPTER_IMAGES_CACHE.clear()
        st._CHAPTER_IMAGES_FETCHING.clear()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import server.state as st
    import server.manga_api as ma

    cache = tmp_path / "_cache"
    dl_dir = tmp_path / "downloads"
    (cache / "jm" / CID).mkdir(parents=True, exist_ok=True)
    (dl_dir / "jm" / CID).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ma, "MANGA_CACHE_DIR", str(cache), raising=False)
    monkeypatch.setattr(ma, "MANGA_DOWNLOADS_DIR", str(dl_dir), raising=False)
    monkeypatch.setattr(st, "MANGA_CACHE_DIR", str(cache))
    monkeypatch.setattr(st, "MANGA_DOWNLOADS_DIR", str(dl_dir))
    monkeypatch.setattr(ma, "_prefetch_next_chapter_images", lambda *a, **k: None)
    warmed = []
    if hasattr(ma, "_warm_chapter_images"):
        monkeypatch.setattr(ma, "_warm_chapter_images",
                            lambda *a, **k: warmed.append(a) or True)
    ad = _ScrambledAd()
    monkeypatch.setattr(ma, "_manga_read_adapter", lambda src: ad)
    monkeypatch.setattr(ma, "_manga_read_images",
                        lambda a, s, c, ch: (a, a.images(c, ch)))
    import app
    app.app.config["TESTING"] = True
    return {"c": app.app.test_client(), "cache": cache, "dl": dl_dir,
            "ma": ma, "st": st, "warmed": warmed, "ad": ad,
            "monkeypatch": monkeypatch}


def _urls(env):
    r = env["c"].get(f"/api/manga/jm/{CID}/chapter/{CH1}/urls")
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def test_partial_reading_cache_not_truncated(env):
    """部分缓存 + 权威页数 20 → 必须返回 20 页（旧实现只返回 6 页）"""
    _mark_current(_touch_pages(os.path.join(str(env["cache"]), "jm", CID, CH1), PARTIAL))
    _write_url_list(env["cache"], CH1)
    d = _urls(env)
    assert d["count"] == N_PAGES, f"整章被截断成 {d['count']} 页"
    # 已缓存页本地直出
    assert all(d["images"][i]["local"] for i in range(PARTIAL))
    # 缺页：混淆源必须走 lazy 服务器通道（直连 CDN 会拿到乱序图）
    for i in range(PARTIAL, N_PAGES):
        e = d["images"][i]
        assert e.get("lazy") and not e.get("local"), e
        assert e["url"].endswith(f"/img/{i}")
    assert env["warmed"], "缺页章节必须启动预热"


def test_partial_without_known_count_still_not_truncated(env):
    """两层 URL 缓存都没有权威页数 → 宁可有界回源，也不截断"""
    _touch_pages(os.path.join(str(env["cache"]), "jm", CID, CH1), PARTIAL)
    d = _urls(env)                       # 只有本地文件，无 _imgs.json
    assert d["count"] == N_PAGES, f"无权威页数时被截断成 {d['count']} 页"


def test_complete_reading_cache_still_short_circuits(env):
    """整章都在本地且页数吻合 → 保持短路（不再回源、不触发预热）"""
    _mark_current(_touch_pages(os.path.join(str(env["cache"]), "jm", CID, CH1), N_PAGES))
    _write_url_list(env["cache"], CH1)
    d = _urls(env)
    assert d["count"] == N_PAGES and d.get("local_only") is True
    assert env["warmed"] == []


def test_downloaded_chapter_short_circuits_without_url_cache(env):
    """已下载目录（无 URL 缓存）仍短路：零源站请求，页数=本地文件数"""
    _mark_current(_touch_pages(os.path.join(str(env["dl"]), "jm", CID, CH1), 12))
    d = _urls(env)
    assert d["count"] == 12 and d.get("local_only") is True
    assert all(e["local"] for e in d["images"])


def test_plain_source_partial_keeps_cdn_links(env):
    """非混淆源缺页仍可给 CDN 直链（浏览器直连更快），不必强制走服务器"""
    ad = _PlainAd()
    env["monkeypatch"].setattr(env["ma"], "_manga_read_adapter", lambda src: ad)
    env["monkeypatch"].setattr(env["ma"], "_manga_read_images",
                               lambda a, s, c, ch: (a, a.images(c, ch)))
    _touch_pages(os.path.join(str(env["cache"]), "jm", CID, CH1), PARTIAL)
    _write_url_list(env["cache"], CH1)
    d = _urls(env)
    assert d["count"] == N_PAGES
    assert d["images"][PARTIAL]["url"].startswith("https://cdn.invalid/"), \
        "非混淆源缺页应保留 CDN 直链"

def test_stale_processed_chapter_not_short_circuited(env):
    """0.61.0（升级版 Venera 指南 §7）：处理版本陈旧的本地章节**不得短路**——
    否则旧算法写下的花图会被当作"完整章节"直出，修复后的算法没有机会跑。

    判据用"有没有回源"而不是"每页是否 local"：回源之后已缓存页仍可以给本地
    `/img/` 路径（那条路径逐张做版本判定、必要时重建），关键是**不再由
    /urls 宣布 local_only 把整章锁死**。
    """
    d = _touch_pages(os.path.join(str(env["cache"]), "jm", CID, CH1), N_PAGES)
    _write_url_list(env["cache"], CH1)
    with open(os.path.join(d, "_processed.json"), "w", encoding="utf-8") as f:
        json.dump({"jm": {"version": 1}}, f)          # 旧版本

    got = _urls(env)
    # 关键：不再由 /urls 宣布"完全本地"——客户端会逐张打 /img，
    # 而 /img 会对每张图做处理版本判定（旧版本不直出，回源重建整章）。
    assert got.get("local_only") is not True, "旧版本缓存不得宣告完全本地"
    assert got["count"] == N_PAGES
    for e in got["images"]:
        assert "/img/" in e["url"], \
            f"旧版本缓存的每一页都必须走服务器取图通道（逐张过版本判定）：{e}"
