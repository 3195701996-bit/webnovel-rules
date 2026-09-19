# -*- coding: utf-8 -*-
"""jm/漫画在线阅读预热流水线回归（离线，不触网、不启 Chrome）

覆盖 2026-09-10 的阅读提速改造：

1. /urls 返回 lazy（混淆源）时自动启动服务器端预热；
2. 预热按整话顺序抓取→落阅读缓存，跳过已缓存页；
3. 同话单飞（并发重复请求只跑一个预热线程）；
4. **代际守卫**：用户切话后旧话预热立即放弃，不继续耗受限带宽；
5. 主预热完成后接力预热下一话前几页；下一话已有缓存则跳过；
6. 次预热（下一话）不篡改代际（否则会把当前话预热顶掉）；
7. WR_DISABLE_MANGA_WARM=1 可整体关闭预热；
8. 缓存命中走磁盘静态直出（immutable 强缓存 + 条件请求 304）。
"""
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CID = "399123"
CH1 = "300001"
CH2 = "300002"



@pytest.fixture(autouse=True)
def _enable_prewarm(monkeypatch):
    """测试环境默认 `WR_DISABLE_BACKGROUND=1`（不起后台线程），而本文件测的正是
    **预热行为本身**，所以显式打开 prewarm 开关（0.71.0 起预热也尊重总开关）。"""
    monkeypatch.setenv("WR_BG_PREWARM", "1")


def _wait(pred, timeout=8.0, interval=0.01):
    _end = time.time() + timeout
    while time.time() < _end:
        if pred():
            return True
        time.sleep(interval)
    return False


class _FakeDL:
    """假下载器：记录调用并**真实写入阅读缓存**（_page_cached 判定 >1000B）"""

    def __init__(self, cache_root, delay=0.01):
        self.cache_root = cache_root
        self.delay = delay
        self.calls = []
        self._lock = threading.Lock()

    def pages(self, chapter_id):
        with self._lock:
            return [i for (c, i) in self.calls if c == chapter_id]

    def get(self, url, comic_id, chapter_id, idx):
        with self._lock:
            self.calls.append((chapter_id, idx))
        if self.delay:
            time.sleep(self.delay)
        _d = os.path.join(self.cache_root, "jm", comic_id, chapter_id)
        os.makedirs(_d, exist_ok=True)
        _p = os.path.join(_d, f"{idx:04d}.webp")
        with open(_p, "wb") as f:
            f.write(b"W" * 2048)
        with open(_p, "rb") as f:
            return f.read(), _p


class _FakeAdapter:
    """混淆源适配器：有 unscramble_image 属性 → /urls 走 lazy 服务器通道"""

    def unscramble_image(self, data, url=None):
        return data


@pytest.fixture(autouse=True)
def _clean_warm_state():
    import server.manga_api as ma
    with ma._warm_lock:
        ma._warm_inflight.clear()
        ma._warm_gen.clear()
    yield
    with ma._warm_lock:
        ma._warm_inflight.clear()
        ma._warm_gen.clear()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离缓存/下载目录 + 假适配器与假下载器（都在 tmp 内，绝不触网）"""
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

    dl = _FakeDL(str(cache))
    ad = _FakeAdapter()
    monkeypatch.setattr(ma, "_manga_read_adapter", lambda src: ad)
    monkeypatch.setattr(ma, "_read_downloader",
                        lambda a, s, c, *rest: dl)
    monkeypatch.setattr(ma, "_prefetch_next_chapter_images",
                        lambda *a, **k: None)
    calls = {"images": 0}

    def _fake_images(a, src, cid, chid):
        calls["images"] += 1
        return a, [f"https://cdn.invalid/jm/{cid}/{chid}/{i}.webp"
                   for i in range(20)]

    monkeypatch.setattr(ma, "_manga_read_images", _fake_images)
    return {"cache": cache, "downloads": dl_dir, "dl": dl, "ad": ad,
            "calls": calls, "ma": ma, "tmp": tmp_path}


def _cached_pages(env, chapter_id, n=20):
    return [i for i in range(n)
            if os.path.exists(os.path.join(str(env["cache"]), "jm", CID,
                                           chapter_id, f"{i:04d}.webp"))]


def _run_to_idle(env, keys, timeout=8.0):
    """等待指定预热键全部退出 in-flight"""
    ma = env["ma"]
    return _wait(lambda: all(
        k not in ma._warm_inflight for k in keys), timeout=timeout)


# ── 1. 整话预热 + 单飞 ───────────────────────────────────────
def test_warm_whole_chapter_single_flight(env):
    ma = env["ma"]
    ma._warm_chapter_images("jm", CID, CH1, concurrency=2)
    # 立即再叫一次：必须被单飞挡住（返回 True 但不重复回源）
    assert ma._warm_chapter_images("jm", CID, CH1, concurrency=2) is True
    # 默认预热并发：2026-09-16 实测后由 2 调到 4 ——
    # 禁漫 CDN 对并行是真并行（裸 requests 3 线程取 3 张总耗时 1.88s ≈ 单张耗时；
    # 串行取 2 张要 3.41s）。2 并发时一话 40 页要 ~80s 才填满，用户滚到哪都要等，
    # 表现为"加载过慢"。4 仍低于阅读通道按需取图的 8 并发，不抢当前可见页。
    assert ma._WARM_CONCURRENCY == 8
    assert _run_to_idle(env, [("jm", CID, CH1)])
    assert _cached_pages(env, CH1) == list(range(20))
    assert env["calls"]["images"] == 1        # 单飞：图列表只取了一次
    assert env["dl"].pages(CH1) == list(range(20))


def test_warm_skips_already_cached_pages(env):
    ma = env["ma"]
    _d = os.path.join(str(env["cache"]), "jm", CID, CH1)
    os.makedirs(_d, exist_ok=True)
    for i in (0, 1, 2):
        with open(os.path.join(_d, f"{i:04d}.webp"), "wb") as f:
            f.write(b"W" * 2048)
    ma._warm_chapter_images("jm", CID, CH1, concurrency=2)
    assert _run_to_idle(env, [("jm", CID, CH1)])
    assert env["dl"].pages(CH1) == list(range(3, 20))   # 已缓存页不再回源


# ── 2. 代际守卫：切话即放弃 ──────────────────────────────────
def test_warm_aborts_when_user_switches_chapter(env):
    ma = env["ma"]
    env["dl"].delay = 0.03
    ma._warm_chapter_images("jm", CID, CH1, concurrency=1)
    assert _wait(lambda: len(env["dl"].pages(CH1)) >= 4, timeout=6)
    # 用户切到下一话 → 旧话预热必须放弃剩余页
    ma._warm_chapter_images("jm", CID, CH2, concurrency=1)
    assert ma._warm_gen[("jm", CID)] == CH2
    assert _run_to_idle(env, [("jm", CID, CH1), ("jm", CID, CH2)])
    assert len(env["dl"].pages(CH1)) < 20
    assert len(env["dl"].pages(CH2)) == 20


def test_guard_warm_does_not_override_generation(env):
    """次预热（下一话）不得篡改代际，否则会把当前话的预热顶掉"""
    ma = env["ma"]
    ma._warm_chapter_images("jm", CID, CH1, concurrency=1)
    ma._warm_chapter_images("jm", CID, CH2, limit=3, guard_chapter=CH1)
    assert ma._warm_gen[("jm", CID)] == CH1
    assert _run_to_idle(env, [("jm", CID, CH1), ("jm", CID, CH2)])


def test_guard_warm_gives_up_when_user_left(env):
    """发起次预热时用户还在 CH1，随后切到 CH2 → 该次预热必须放弃"""
    ma = env["ma"]
    with ma._warm_lock:
        ma._warm_gen[("jm", CID)] = CH2      # 用户已切到 CH2
    ma._warm_chapter_images("jm", CID, CH1, limit=5, guard_chapter=CH1)
    assert _run_to_idle(env, [("jm", CID, CH1)])
    assert env["dl"].pages(CH1) == []


# ── 3. 主预热接力下一话 ──────────────────────────────────────
def _write_info(env, ids):
    _p = os.path.join(str(env["cache"]), "jm", CID, "_info_full.json")
    with open(_p, "w", encoding="utf-8") as f:
        json.dump({"data": {"chapters": [{"id": i} for i in ids]}}, f)
    return _p


def test_primary_warm_chains_next_chapter(env):
    ma = env["ma"]
    _write_info(env, [CH1, CH2])
    ma._warm_chapter_images("jm", CID, CH1, concurrency=2)
    assert _wait(lambda: len(_cached_pages(env, CH2)) == ma._NEXT_WARM_PAGES,
                 timeout=8)
    assert _run_to_idle(env, [("jm", CID, CH1), ("jm", CID, CH2)])
    # 下一话只预热首屏所需页数（不整话抢带宽）
    assert env["dl"].pages(CH2) == list(range(ma._NEXT_WARM_PAGES))
    assert len(_cached_pages(env, CH1)) == 20        # 当前话仍预热整话
    assert ma._warm_gen[("jm", CID)] == CH1          # 接力不篡改代际


def test_fully_cached_chapter_still_chains_next(env):
    """回归：本话已全缓存（重读旧章）时仍要接力预热下一话。

    旧实现 _todo 为空即 return，导致"读完这章再翻下一章"永远没有预热，
    下一话首图退回一次完整 CDN 往返（实测 0.17s）。
    """
    ma = env["ma"]
    _write_info(env, [CH1, CH2])
    _d = os.path.join(str(env["cache"]), "jm", CID, CH1)
    os.makedirs(_d, exist_ok=True)
    for i in range(20):
        with open(os.path.join(_d, f"{i:04d}.webp"), "wb") as f:
            f.write(b"W" * 2048)
    ma._warm_chapter_images("jm", CID, CH1, concurrency=2)
    assert _wait(lambda: len(_cached_pages(env, CH2)) == ma._NEXT_WARM_PAGES,
                 timeout=8)
    assert env["dl"].pages(CH1) == []                 # 本话零回源
    assert env["dl"].pages(CH2) == list(range(ma._NEXT_WARM_PAGES))


def test_state_prefetch_warms_images_when_urls_cached(env, monkeypatch):
    """回归：下一话 URL 列表已有缓存 ≠ 图在本地——仍要预热首屏图片。

    旧实现命中 URL 缓存就直接 return，连图片预热一起跳过，
    /urls 的"下一话首屏已就绪"承诺在最常见的重读场景下失效。
    """
    import server.state as st
    import server.manga_api as ma

    _write_info(env, [CH1, CH2])
    seen = []

    def _rec(src, cid, chid, limit=None, guard_chapter=None):
        seen.append((src, cid, chid, limit, guard_chapter))
        return True

    monkeypatch.setattr(ma, "_warm_chapter_images", _rec)
    monkeypatch.setattr(st, "_manga_read_adapter", lambda src: env["ad"])
    _imgs_called = {"n": 0}

    def _imgs(a, src, cid, chid):
        _imgs_called["n"] += 1
        return a, []

    monkeypatch.setattr(st, "_manga_read_images", _imgs)
    st._CHAPTER_IMAGES_CACHE[("jm", CID, CH2)] = (time.time(), ["u"])  # 列表热
    st._prefetch_next_chapter_images("jm", CID, CH1)
    assert _wait(lambda: seen, timeout=5)
    assert seen == [("jm", CID, CH2, st._NEXT_CHAPTER_WARM_PAGES, CH1)]
    assert _imgs_called["n"] == 0                     # 列表已缓存 → 不再回源
    assert _wait(lambda: not st._prefetch_inflight, timeout=5)


def test_next_chapter_warm_skipped_when_already_local(env):
    ma = env["ma"]
    _write_info(env, [CH1, CH2])
    _d = os.path.join(str(env["downloads"]), "jm", CID, CH2)
    os.makedirs(_d, exist_ok=True)
    with open(os.path.join(_d, "0000.webp"), "wb") as f:
        f.write(b"W" * 2048)
    ma._warm_chapter_images("jm", CID, CH1, concurrency=2)
    assert _run_to_idle(env, [("jm", CID, CH1)])
    time.sleep(0.2)
    assert env["dl"].pages(CH2) == []                # 已下载 → 不预热


# ── 4. 开关 ──────────────────────────────────────────────────
def test_warm_disabled_by_env(env, monkeypatch):
    monkeypatch.setenv("WR_DISABLE_MANGA_WARM", "1")
    assert env["ma"]._warm_chapter_images("jm", CID, CH1) is False
    time.sleep(0.2)
    assert env["dl"].calls == []
    assert _cached_pages(env, CH1) == []


# ── 5. /urls 触发预热（路由接线） ────────────────────────────
def test_urls_triggers_warm_for_lazy_source(env):
    import app
    app.app.config["TESTING"] = True
    c = app.app.test_client()
    r = c.get(f"/api/manga/jm/{CID}/chapter/{CH1}/urls")
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["count"] == 20
    assert all(e.get("lazy") for e in data["images"])   # 混淆源 → 服务器通道
    assert _wait(lambda: len(_cached_pages(env, CH1)) >= 5, timeout=8)


# ── 6. 缓存命中：磁盘静态直出 + 强缓存/304 ───────────────────
def test_cached_image_served_immutable_and_304(env):
    import app
    app.app.config["TESTING"] = True
    c = app.app.test_client()
    _d = os.path.join(str(env["cache"]), "jm", CID, CH1)
    os.makedirs(_d, exist_ok=True)
    with open(os.path.join(_d, "0000.webp"), "wb") as f:
        f.write(b"W" * 2048)
    url = f"/api/manga/jm/{CID}/chapter/{CH1}/img/0"
    r = c.get(url)
    assert r.status_code == 200
    assert r.data == b"W" * 2048
    cc = r.headers.get("Cache-Control", "")
    assert "immutable" in cc and "max-age=604800" in cc
    etag = r.headers.get("ETag")
    assert etag
    # 再次阅读 → 条件请求 304（零字节传输；本地直出不等源站）
    r2 = c.get(url, headers={"If-None-Match": etag})
    assert r2.status_code == 304
    # 预热/按需抓到的图同样零回源（下载器不会被调用）
    assert env["dl"].calls == []


def test_remote_serve_uses_static_disk_path(env):
    """_serve_remote_image 命中磁盘缓存 → send_from_directory 静态直出"""
    import app
    ma = env["ma"]
    _d = os.path.join(str(env["cache"]), "jm", CID, CH1)
    os.makedirs(_d, exist_ok=True)
    _p = os.path.join(_d, "0000.webp")

    def _get(url, comic_id, chapter_id, idx):
        with open(_p, "wb") as f:
            f.write(b"W" * 2048)
        return open(_p, "rb").read(), _p

    env["dl"].get = _get
    with app.app.test_request_context():
        resp = ma._serve_remote_image(env["ad"], "jm", CID, CH1, 0)
    assert resp.status_code == 200
    assert "immutable" in resp.headers.get("Cache-Control", "")
    assert resp.headers.get("ETag")
