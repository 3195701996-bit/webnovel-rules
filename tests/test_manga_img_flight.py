# -*- coding: utf-8 -*-
"""漫画图片下载器的**同图单飞**回归（离线，不触网）

背景：jm 等源每 IP 带宽/风控受限。阅读路径上有两个生产者会要同一张图——
服务器端预热线程（dl.get 直调）与用户滚到该页时的 /img 请求。没有单飞时
同一张图会被抓两遍：用户可见页被自己的预热拖慢，风控计数还翻倍。

语义（与 /img 的 P1-4 单飞一致）：
  - 同 cache_path 只允许一个回源者（leader）；
  - 跟随者等 leader 结果：落盘即直出，未落盘即明确失败（不自行回源）；
  - 不同图互不影响；
  - flight 表在结束时清理，不泄漏。
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# JPEG 魔数 + 填充到 >4096B（下载器缓存有效性阈值）
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 6000


class _FakeAdapter:
    concurrent = 2
    name = "fake"

    def image_headers(self, u):
        return {}

    def image_url(self, u):
        return u

    def on_image_failed(self, u):
        return None


class _Resp:
    def __init__(self, content=JPEG, status=200):
        self.status_code = status
        self.headers = {"Content-Type": "image/jpeg"}
        self.content = content


@pytest.fixture()
def dl(tmp_path, monkeypatch):
    import engine.manga.downloader as dl_mod

    calls = []
    lock = threading.Lock()

    class _Session:
        def get(self, url, **kw):
            with lock:
                calls.append(url)
            time.sleep(0.15)          # 放大窗口：无单飞时两线程必然并发抓同一张
            return _Resp()

    monkeypatch.setattr(dl_mod, "pin_curl_session", lambda *a, **k: False)
    monkeypatch.setattr(dl_mod, "_session_pool", [_Session()])
    monkeypatch.setattr(dl_mod, "_session_pool_locks", [threading.Lock()])
    monkeypatch.setattr(dl_mod, "_SESSION_POOL_SIZE", 1)
    d = dl_mod.ImageDownloader(_FakeAdapter(), str(tmp_path), min_interval=0.0)
    return {"dl": d, "calls": calls, "mod": dl_mod, "tmp": tmp_path}


def _run_pair(fn):
    out = {}

    def _w(tag):
        try:
            out[tag] = fn()
        except Exception as e:                      # noqa: BLE001 - 测试需保留异常
            out[tag] = e

    t1 = threading.Thread(target=_w, args=("a",))
    t2 = threading.Thread(target=_w, args=("b",))
    t1.start()
    time.sleep(0.05)                                # 让 a 先成为 leader
    t2.start()
    t1.join(30)
    t2.join(30)
    return out


def test_same_image_fetched_once(dl):
    d = dl["dl"]
    out = _run_pair(lambda: d.get("https://cdn.example/a.jpg", "c1", "ch1", 0))
    assert isinstance(out["a"], tuple) and isinstance(out["b"], tuple), out
    assert out["a"][0] == JPEG and out["b"][0] == JPEG
    assert out["a"][1] == out["b"][1]               # 同一落盘路径
    assert len(dl["calls"]) == 1, f"同图应只回源一次，实际 {dl['calls']}"
    assert d._flight == {}, "flight 表应清理干净"


def test_different_images_not_merged(dl):
    d = dl["dl"]
    out = _run_pair(lambda: d.get("https://cdn.example/a.jpg", "c1", "ch1", 0))
    # 换一张（同章不同序号）：必须各自回源
    r3 = d.get("https://cdn.example/b.jpg", "c1", "ch1", 1)
    assert r3[0] == JPEG
    assert len(dl["calls"]) == 2
    assert isinstance(out["a"], tuple)


def test_follower_does_not_refetch_after_leader_failure(dl, monkeypatch):
    """leader 失败 → 跟随者明确失败，绝不自行回源（不做第二个生产者）"""
    d = dl["dl"]
    n = {"dl": 0}

    def _boom(*a, **k):
        n["dl"] += 1
        time.sleep(0.1)
        raise dl["mod"].MangaError("模拟回源失败")

    monkeypatch.setattr(d, "_download", _boom)
    out = _run_pair(lambda: d.get("https://cdn.example/a.jpg", "c1", "ch1", 0))
    assert isinstance(out["a"], Exception)
    assert isinstance(out["b"], Exception)
    assert n["dl"] == 1, "跟随者不得重复回源"
    assert d._flight == {}, "失败路径也必须清理 flight 表"


def test_cached_page_short_circuits_without_flight(dl, tmp_path):
    """已落盘页直接直出：不进单飞、不回源"""
    d = dl["dl"]
    cp = d.cache_path("c1", "ch1", 0, ".jpg")
    with open(cp, "wb") as f:
        f.write(JPEG)
    data, path = d.get("https://cdn.example/a.jpg", "c1", "ch1", 0)
    assert data == JPEG and path == cp
    assert dl["calls"] == []
    assert d._flight == {}


def test_stale_flight_entry_taken_over(dl, monkeypatch):
    """leader 挂死（事件永不 set）→ 跟随者超上限后接管回源，不永久挂住"""
    d = dl["dl"]
    monkeypatch.setattr(dl["mod"], "_FLIGHT_WAIT_MIN", 0.2)
    monkeypatch.setattr(dl["mod"], "_FLIGHT_WAIT_FACTOR", 0.0)
    cp = d.cache_path("c1", "ch1", 0, ".jpg")
    monkeypatch.setattr(d, "_download", lambda *a, **k: (JPEG, cp))
    d._flight[cp] = threading.Event()          # 挂死 leader：永不 set
    t0 = time.time()
    data, path = d.get("https://cdn.example/a.jpg", "c1", "ch1", 0, timeout=0.1)
    assert data == JPEG and path == cp
    assert time.time() - t0 < 20, "不得无限等待挂死 leader"
    assert d._flight == {}, "接管者结束时应摘除自己登记的事件"


def test_takeover_registers_fresh_waiter_event(dl, monkeypatch):
    """接管必须登记**新**事件：后续跟随者不能继续等那个永不 set 的旧事件"""
    d = dl["dl"]
    monkeypatch.setattr(dl["mod"], "_FLIGHT_WAIT_MIN", 0.2)
    monkeypatch.setattr(dl["mod"], "_FLIGHT_WAIT_FACTOR", 0.0)
    cp = d.cache_path("c1", "ch1", 0, ".jpg")
    dead = threading.Event()
    d._flight[cp] = dead
    seen = {}

    def _slow_download(*a, **k):
        seen["waiter"] = d._flight.get(cp)
        time.sleep(0.3)
        return JPEG, cp

    monkeypatch.setattr(d, "_download", _slow_download)
    out = _run_pair(lambda: d.get("https://cdn.example/a.jpg", "c1", "ch1", 0,
                                  timeout=0.1))
    assert isinstance(out["a"], tuple) and isinstance(out["b"], tuple), out
    assert seen["waiter"] is not None and seen["waiter"] is not dead, \
        "接管者登记的事件必须与挂死事件不同"


def test_follower_returns_small_image_written_by_leader(dl, monkeypatch, tmp_path):
    """窄条图（<4096B 缓存阈值）也必须在 leader 落盘后直出，不得对用户报错"""
    d = dl["dl"]
    small = b"\xff\xd8\xff\xe0" + b"\x00" * 600      # ~604B：真实存在但小于阈值
    cp = d.cache_path("c1", "ch1", 0, ".jpg")

    def _slow(*a, **k):
        with open(cp, "wb") as f:
            f.write(small)
        time.sleep(0.3)
        return small, cp

    monkeypatch.setattr(d, "_download", _slow)
    out = _run_pair(lambda: d.get("https://cdn.example/a.jpg", "c1", "ch1", 0))
    assert isinstance(out["a"], tuple) and isinstance(out["b"], tuple), out
    assert out["b"][0] == small, "跟随者应直出 leader 写入的小图，而不是报错"
