# -*- coding: utf-8 -*-
"""P1-1/P1-2/P2-2/P2-8 回归：DownloadManager._active 槽位计数与单 worker 保证

全程离线：假适配器预注入 _adapter_pool，不触发任何网络访问。
覆盖五条 worker 退出路径的 _active 递减（每条必须精确一次）：
正常完成 / cancel / pause / gone / 异常。
"""
import json
import os
import sys
import threading
import time

HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HUB)

from engine.manga import download_manager as dm  # noqa: E402


# ── 假适配器（离线）─────────────────────────────────────────
class _Ch:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name
        self.group = "default"


class _Detail:
    def __init__(self, chapters, cover="http://x/c.jpg"):
        self.chapters = chapters
        self.cover = cover


class _BlockingAd:
    """comic_info 阻塞至 release，用于在 worker 存活期间制造 cancel/delete 竞态"""
    name = "fake"

    def __init__(self, chapters):
        self.entered = threading.Event()
        self.release = threading.Event()
        self._chapters = chapters

    def comic_info(self, comic_id):
        self.entered.set()
        assert self.release.wait(10), "测试未及时放行"
        return _Detail(self._chapters)

    def images(self, comic_id, ch_id):
        return []  # 0 张图 → 不触发任何下载


class _ImagesAd:
    """1 章 2 图 → 离线走完正常 done 路径（图片用假下载器落盘，不触网）

    注：0 图不再是"正常完成"。图片列表为空在真实链路里按失败处理
    （见 test_empty_images_marks_chapter_failed）：0 图曾被当作 done，
    用户以为下载成功、实际整章没有图。
    """
    name = "fake"

    def __init__(self, imgs=None):
        self._imgs = list(imgs if imgs is not None else
                          ["http://cdn/fake/1.jpg", "http://cdn/fake/2.jpg"])

    def comic_info(self, comic_id):
        return _Detail([_Ch("c1", "第1话")])

    def images(self, comic_id, ch_id):
        return list(self._imgs)


class _NoImagesAd(_ImagesAd):
    """源站返回空图片列表（风控/瞬态）"""

    def __init__(self):
        super().__init__(imgs=[])


def _install_fake_downloader(monkeypatch):
    """替换 ImageDownloader：写真实文件返回，不触网"""
    from engine.manga import downloader as _dl_mod

    class _FakeDL:
        def __init__(self, ad, cache_root, **kw):
            self.cache_root = cache_root

        def get(self, url, comic_id, chapter_id, idx, timeout=20):
            d = os.path.join(self.cache_root, str(chapter_id))
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, f"{idx:04d}.jpg")
            with open(p, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 2048)
            with open(p, "rb") as f:
                return f.read(), p

    monkeypatch.setattr(_dl_mod, "ImageDownloader", _FakeDL)


class _BoomAd:
    """comic_info 抛带敏感字样的异常 → 验证 error 字段脱敏"""
    name = "fake"

    def comic_info(self, comic_id):
        raise RuntimeError("secret-token-xyz http://internal/")

    def images(self, comic_id, ch_id):
        raise AssertionError("不应被调用")


# ── 辅助 ───────────────────────────────────────────────────
def _make_mgr(tmp_path, ad=None):
    m = dm.DownloadManager(state_file=str(tmp_path / "_tasks.json"))
    m.configure(state_dir=str(tmp_path / "st"),
                downloads_root=str(tmp_path / "dl"),
                library_file=str(tmp_path / "lib.json"))
    # 预注入假适配器，跳过 get_adapter（避免联网初始化）
    m._adapter_pool = {"fake": ad} if ad else {}
    return m


def _wait_status(m, key, statuses, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = m.status(key)
        if st.get("status") in statuses:
            return st
        time.sleep(0.02)
    raise AssertionError(f"等待状态 {statuses} 超时，当前: {m.status(key)}")


def _wait_slot_released(m, key, timeout=10):
    """终态落库先于 finally 槽位释放，需单独等待归零"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if m._active == 0 and key not in m._worker_alive:
            return
        time.sleep(0.02)
    raise AssertionError(f"槽位未释放: _active={m._active}, alive={m._worker_alive}")


# ── P1-1：五条退出路径 _active 精确递减一次 ─────────────────
def test_gone_exit_releases_slot(tmp_path):
    """gone：worker 起步前任务已被 delete → 首个读取点直接 return"""
    m = _make_mgr(tmp_path)
    key = "fake:b1"
    with m._lock:
        m._tasks[key] = {"status": "running", "source": "fake",
                         "comic_id": "b1", "title": "t", "chapters": []}
        m._active = 1
        m._worker_alive.add(key)
        m._tasks.pop(key)  # 模拟 delete 抢先 pop
    m._worker(key)  # 同步执行，不经线程
    assert m._active == 0
    assert key not in m._worker_alive


def test_normal_done_releases_slot(tmp_path, monkeypatch):
    _install_fake_downloader(monkeypatch)
    m = _make_mgr(tmp_path, _ImagesAd())
    key, st = m.start("fake", "b1", "T")
    assert st == "queued"
    r = _wait_status(m, key, ("done",))
    assert r["status"] == "done"
    _wait_slot_released(m, key)
    # P2-2: 持久化文件应为完整可解析 JSON（原子写）
    data = json.load(open(m._state_file, encoding="utf-8"))
    assert data[key]["status"] == "done"


def test_empty_images_marks_chapter_failed(tmp_path, monkeypatch):
    """回归：源站返回空图片列表 → 该章标记失败（可续传），绝不静默 done

    旧行为把 0 图当"下载成功"：用户看到任务完成，进阅读器却没有一张图，
    与 jm 风控导致的"整章不加载"是同一类问题（假成功/假失败不可区分）。
    """
    _install_fake_downloader(monkeypatch)
    m = _make_mgr(tmp_path, _NoImagesAd())
    # comic_id 与正常完成用例错开：图片 URL 列表按 (源,漫画,章节) 落磁盘缓存，
    # 同 id 会被上一个用例写下的列表命中（那是缓存设计使然，不是本用例要测的）
    key, _ = m.start("fake", "b9", "T")
    r = _wait_status(m, key, ("done", "error"))
    assert r["status"] == "error", f"空图片列表不得静默完成: {r}"
    assert int(r.get("failed_chapters") or 0) >= 1
    _wait_slot_released(m, key)


def test_cancel_path_releases_slot(tmp_path):
    ad = _BlockingAd([_Ch("c1", "第1话")])
    m = _make_mgr(tmp_path, ad)
    key, _ = m.start("fake", "b1", "T")
    assert ad.entered.wait(10)
    th = m._threads[key]
    m.cancel(key)          # running → cancel
    ad.release.set()
    r = _wait_status(m, key, ("stopped",))
    assert r["status"] == "stopped"
    th.join(10)
    _wait_slot_released(m, key)


def test_pause_path_releases_slot(tmp_path):
    ad = _BlockingAd([_Ch("c1", "第1话")])
    m = _make_mgr(tmp_path, ad)
    key, _ = m.start("fake", "b1", "T")
    assert ad.entered.wait(10)
    th = m._threads[key]
    assert m.pause(key)    # running → paused 标记
    ad.release.set()
    r = _wait_status(m, key, ("paused",))
    assert r["status"] == "paused"
    th.join(10)
    _wait_slot_released(m, key)


def test_error_path_releases_slot_and_sanitized(tmp_path):
    """P1-1 异常路径 + P2-8 脱敏：error 不含异常原文，且是稳定文案"""
    m = _make_mgr(tmp_path, _BoomAd())
    key, _ = m.start("fake", "b1", "T")
    r = _wait_status(m, key, ("error",))
    assert isinstance(r["error"], str)
    assert "secret-token-xyz" not in r["error"]
    assert "internal" not in r["error"]
    assert r["error"] == "下载过程异常中断，可重新启动续传"
    _wait_slot_released(m, key)


def test_adapter_missing_releases_slot(tmp_path):
    """适配器不存在分支：_mark_done(error) 后 return，finally 递减"""
    m = _make_mgr(tmp_path)  # _adapter_pool 空 → get_adapter("fake") 返回 None
    key, _ = m.start("fake", "b1", "T")
    r = _wait_status(m, key, ("error",))
    assert r["error"] == "适配器不存在"
    _wait_slot_released(m, key)


# ── P1-2：cancel→delete→立即 restart 不产生双 worker ────────
def test_cancel_delete_restart_no_double_worker(tmp_path):
    ad = _BlockingAd([])   # 放行后 0 章节 → error 退出（离线）
    m = _make_mgr(tmp_path, ad)
    key, _ = m.start("fake", "b1", "T")
    assert ad.entered.wait(10)
    th = m._threads[key]
    assert m._active == 1

    m.cancel(key)          # running → cancel
    m.delete(key)          # 非 running → 直接 pop
    assert m.status(key)["status"] == "idle"

    # 旧 worker 仍存活 → start 必须拒绝重建
    key2, st2 = m.start("fake", "b1", "T")
    assert key2 == key
    assert st2 == "stopping"
    assert m._threads[key] is th, "不得启动第二个 worker"
    assert m._active == 1, "不得重复分配槽位"
    assert list(m._worker_alive) == [key]

    # 放行 → 旧 worker gone 退出 → finally 释放槽位
    ad.release.set()
    th.join(10)
    assert not th.is_alive()
    assert m._active == 0
    assert key not in m._worker_alive

    # 旧 worker 退出后 restart 放行，且新 worker 离线跑完
    key3, st3 = m.start("fake", "b1", "T")
    assert st3 == "queued"
    assert key in m._worker_alive
    r = _wait_status(m, key, ("error",))
    assert r["error"].startswith("没有可下载的章节")
    _wait_slot_released(m, key)
    # 全程最多一个 worker 线程
    assert m._active == 0


def test_cancel_without_delete_restart_rejected(tmp_path):
    """cancel 后（任务 dict 仍在、状态 cancel）立即 start 同样被拒"""
    ad = _BlockingAd([])
    m = _make_mgr(tmp_path, ad)
    key, _ = m.start("fake", "b1", "T")
    assert ad.entered.wait(10)
    th = m._threads[key]
    m.cancel(key)
    _, st = m.start("fake", "b1", "T")
    assert st == "stopping"
    ad.release.set()
    th.join(10)
    _wait_slot_released(m, key)
    # worker 退出后任务为 stopped，可正常重启
    _, st2 = m.start("fake", "b1", "T")
    assert st2 == "queued"
    _wait_status(m, key, ("error",))
    _wait_slot_released(m, key)
