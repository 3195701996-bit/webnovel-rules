# -*- coding: utf-8 -*-
"""0.61.0 回归（离线）：**"完全本地"短路也必须过处理版本判定**。

来源：《升级版 Venera：当前纠偏与开发指南》§7 发布门槛——
"章节 URL 的'完全本地'短路也纳入处理版本判定，旧缓存离线打开时要给出
'需要重新获取'而非被当作完整章节"。

为什么必须做：`GET /chapter/<id>/urls` 有一条"整章都在本地 → 直接返回本地路径、
不问源站"的短路。它此前**不看图片处理版本**，于是修复块还原算法之后，
旧算法写下的花图仍会被当成"完整章节"返回——用户看到花图，而正确的算法
连一次机会都没有（图片直出端点其实已经查版本，但那要等客户端逐张来取）。

本用例锁住三条契约：
1. 目录完整 + 处理版本陈旧 + **源仍可用** → **不短路**（继续走源站路径重新取图）；
2. 目录完整 + 处理版本陈旧 + **源已下线**（拿不到适配器）→ 仍返回本地（不锁死用户
   已下载内容），但必须带 `stale_processing=true` 与可读的 `stale_reason`；
3. 目录完整 + 处理版本一致（或该源根本没有图片后处理）→ 与原来一样短路，
   不给客户端添乱。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


class _FakeAdapter:
    """带图片后处理版本的假适配器（key/PROCESS_VERSION 就是短路口径需要的全部）"""
    key = "fakejm"
    PROCESS_VERSION = 2


@pytest.fixture()
def chapter_dir(monkeypatch, tmp_path):
    """造一个"整章都在本地"的章节目录（4 张合法大小的图片）"""
    import server.manga_api as mapi
    import server.state as st

    media = tmp_path / "media"
    d = media / "fakejm" / "100" / "200"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(4):
        (d / f"{i:04d}.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 2000)

    # 路径解析交给假实现：本用例考的是"处理版本判定"，不是目录解析规则。
    # （真实实现 _manga_media_root 会在 downloads/_cache 之间挑选，patch 常量
    #   并不能改它的判断，实测会 404。）
    monkeypatch.setattr(mapi, "_manga_media_root",
                        lambda s, c, ch=None: str(d), raising=False)
    monkeypatch.setattr(mapi, "MANGA_DOWNLOADS_DIR", str(media), raising=False)
    monkeypatch.setattr(mapi, "_known_chapter_pages", lambda s, c, ch: 4, raising=False)
    # 处理版本从**类**读（真实实现走 engine.manga.manager.adapter_meta，不实例化）；
    # 假适配器没进注册表，所以这里把类级元信息也给出
    import engine.manga.manager as mgr
    monkeypatch.setattr(mgr, "adapter_meta",
                        lambda k: {"registered": k == "fakejm",
                                   "scrambled": k == "fakejm",
                                   "process_version": 2 if k == "fakejm" else 0},
                        raising=False)
    monkeypatch.setattr(mapi, "_prefetch_next_chapter_images",
                        lambda *a, **k: None, raising=False)
    yield mapi, d


def _mark(d, version, key="fakejm"):
    with open(os.path.join(str(d), "_processed.json"), "w", encoding="utf-8") as f:
        json.dump({key: {"version": version}}, f)


def test_stale_local_chapter_is_not_short_circuited_when_source_alive(client, chapter_dir,
                                                                     monkeypatch):
    mapi, d = chapter_dir
    _mark(d, 1)                                  # 旧版本处理缓存
    monkeypatch.setattr(mapi, "_manga_read_adapter", lambda s: _FakeAdapter())
    # 源可用：短路必须让路（后面那段会去问源站；这里让它显式抛错以便断言"没短路"）
    def _boom(*a, **k):
        raise RuntimeError("已走到源站路径（说明没有短路）")

    monkeypatch.setattr(mapi, "_manga_read_images", _boom)
    r = client.get("/api/manga/fakejm/100/chapter/200/urls")
    assert r.status_code == 500 or r.status_code >= 400, \
        "旧版本缓存 + 源可用时必须继续走源站路径（不该直接返回 local_only）"
    body = r.get_data(as_text=True)
    assert "local_only" not in body, f"不得直接宣告完全本地：{body[:200]}"


def test_stale_local_chapter_keeps_local_but_says_refetch_when_source_gone(client,
                                                                         chapter_dir,
                                                                         monkeypatch):
    mapi, d = chapter_dir
    _mark(d, 1)
    monkeypatch.setattr(mapi, "_manga_read_adapter", lambda s: None)
    r = client.get("/api/manga/fakejm/100/chapter/200/urls")
    assert r.status_code == 200
    o = r.get_json()
    assert o["local_only"] is True, "源下线也要保留本地阅读能力（不锁死已下载内容）"
    assert o["count"] == 4 and all(i["local"] for i in o["images"])
    assert o.get("stale_processing") is True, "必须如实标注这是旧版图片缓存"
    reason = o.get("stale_reason") or ""
    assert "重新获取" in reason or "重新" in reason, reason


def test_current_version_chapter_short_circuits_as_before(client, chapter_dir, monkeypatch):
    mapi, d = chapter_dir
    _mark(d, 2)                                  # 与适配器版本一致
    monkeypatch.setattr(mapi, "_manga_read_adapter", lambda s: _FakeAdapter())
    r = client.get("/api/manga/fakejm/100/chapter/200/urls")
    assert r.status_code == 200
    o = r.get_json()
    assert o["local_only"] is True and o["count"] == 4
    assert not o.get("stale_processing"), "版本一致时不该标注旧缓存"


def test_source_without_processing_version_is_unaffected(client, chapter_dir, monkeypatch):
    """没有图片后处理的源不受版本约束（不能因为缺 marker 就把正常章节推去源站）"""
    mapi, d = chapter_dir

    class _Plain:
        key = "plain"
        # 故意不定义 PROCESS_VERSION

    import engine.manga.manager as mgr
    monkeypatch.setattr(mgr, "adapter_meta",
                        lambda k: {"registered": True, "scrambled": False,
                                   "process_version": 0}, raising=False)
    monkeypatch.setattr(mapi, "_manga_read_adapter", lambda s: _Plain())
    r = client.get("/api/manga/fakejm/100/chapter/200/urls")
    assert r.status_code == 200
    o = r.get_json()
    assert o["local_only"] is True and not o.get("stale_processing")


def test_missing_marker_counts_as_stale_for_processing_sources(client, chapter_dir,
                                                              monkeypatch):
    """处理源但目录没有 marker（旧数据）→ 同样按"需要重新获取"处理"""
    mapi, d = chapter_dir
    p = os.path.join(str(d), "_processed.json")
    if os.path.exists(p):
        os.remove(p)
    monkeypatch.setattr(mapi, "_manga_read_adapter", lambda s: None)
    r = client.get("/api/manga/fakejm/100/chapter/200/urls")
    o = r.get_json()
    assert o.get("stale_processing") is True and "重新" in (o.get("stale_reason") or "")

def test_chapter_meta_reports_stale_for_readers(client, chapter_dir, monkeypatch):
    """读者进入章节时就要知道"这是旧版缓存，需联网重新获取"（指南 §4 P0）。

    `/chapter/<id>` 是原生阅读器实际调用的端点；它必须在本地命中时也带上
    stale_processing/stale_reason，否则用户先看到花图/失败图才知道有问题。
    """
    mapi, d = chapter_dir
    _mark(d, 1)                                  # 旧版本
    monkeypatch.setattr(mapi, "_manga_read_adapter", lambda s: _FakeAdapter())
    monkeypatch.setattr(mapi, "_local_chapter_images",
                        lambda s, c, ch: [f"/api/manga/{s}/{c}/chapter/{ch}/img/{i}"
                                          for i in range(4)], raising=False)
    r = client.get("/api/manga/fakejm/100/chapter/200")
    assert r.status_code == 200, r.get_data(as_text=True)
    o = r.get_json()
    assert o["local"] is True and o["count"] == 4
    assert o.get("stale_processing") is True, o
    assert "重新获取" in (o.get("stale_reason") or ""), o


def test_chapter_meta_clean_cache_has_no_stale_flag(client, chapter_dir, monkeypatch):
    mapi, d = chapter_dir
    _mark(d, 2)                                  # 当前版本
    monkeypatch.setattr(mapi, "_manga_read_adapter", lambda s: _FakeAdapter())
    monkeypatch.setattr(mapi, "_local_chapter_images",
                        lambda s, c, ch: ["/x"] * 2, raising=False)
    o = client.get("/api/manga/fakejm/100/chapter/200").get_json()
    assert o["local"] is True and not o.get("stale_processing"), o


def test_img_keeps_local_when_source_gone_but_meta_says_stale(client, chapter_dir,
                                                              monkeypatch):
    """源已下线：**图片照旧直出**（不锁死用户已下载内容），
    但章节元信息必须说明这是旧版缓存——两种责任分开，指南 §2.3 与 §4 P0 都满足。"""
    mapi, d = chapter_dir
    _mark(d, 1)
    monkeypatch.setattr(mapi, "_manga_read_adapter", lambda s: None)
    r = client.get("/api/manga/fakejm/100/chapter/200/img/0")
    assert r.status_code == 200, r.status_code          # 本地仍可读
    assert r.data[:3] == b"\xff\xd8\xff", "返回的应是本地图片字节"


def test_img_reports_stale_code_when_refetch_fails(client, chapter_dir, monkeypatch):
    """源可用 + 本地旧缓存 + 回源失败（典型：无网络）→ 409 STALE_IMAGE_CACHE，
    文案指明"需要联网重新获取"（不是"图片读取失败"那种像解码坏了的说法）。"""
    mapi, d = chapter_dir
    _mark(d, 1)
    monkeypatch.setattr(mapi, "_manga_read_adapter", lambda s: _FakeAdapter())

    def _net_down(*a, **k):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(mapi, "_serve_remote_image", _net_down)
    monkeypatch.setattr(mapi, "_manga_read_images", _net_down)
    r = client.get("/api/manga/fakejm/100/chapter/200/img/0")
    assert r.status_code == 409, (r.status_code, r.data[:120])
    b = r.get_json()
    assert b.get("code") == "STALE_IMAGE_CACHE"
    assert "重新获取" in (b.get("error") or "")


# ── 0.63.1 补测：把"旧缓存提示"与"解码失败"两条原因**分开**，并锁住配对关系 ──
#
# 背景（0.63.0 实测踩到的坑）：设备上 Pillow 缺 WebP，**每一张图都解码失败**，
# 但错误被报成"旧版处理缓存，需重新获取"——用户按提示重建也修不好，排障被误导。
# 所以：① 不是旧缓存时绝不能报旧缓存；② 是旧缓存且回源失败时，必须把**本次**
# 失败原因附在后面；③ 逐页 `/img` 报 409 时，章节级 `/chapter` 必须同时给出提示条，
# 否则用户只看到"第 N 页加载失败"（指南 §4 P0："不显示为新的图片解码错误"）。


def test_current_version_failure_is_not_disguised_as_stale(client, chapter_dir, monkeypatch):
    """处理版本一致 + 取图失败 ≠ "旧版缓存需要重新获取"。

    这类失败（解码/网络/源站）必须如实报错；报成"旧缓存"会让用户白折腾重建。
    """
    mapi, d = chapter_dir
    _mark(d, 2)                                  # 与适配器版本一致 → 不是旧缓存
    monkeypatch.setattr(mapi, "_manga_read_adapter", lambda s: _FakeAdapter())

    def _net_down(*a, **k):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(mapi, "_serve_local_image", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(mapi, "_serve_remote_image", _net_down)
    r = client.get("/api/manga/fakejm/100/chapter/210/img/0")
    assert r.status_code != 409, (r.status_code, r.data[:200])
    body = r.get_data(as_text=True)
    assert "STALE_IMAGE_CACHE" not in body, body[:200]
    assert "旧版处理缓存" not in body, "版本一致时不得声称是旧版缓存"
    assert "旧版" not in body and "重新获取" not in body, body[:200]


def test_stale_409_keeps_underlying_cause(client, chapter_dir, monkeypatch):
    """是旧缓存 **且** 回源失败 → 409 里必须同时有"重新获取"和本次失败原因，
    否则用户/排障无法区分"该重建"与"设备解码坏了"。"""
    mapi, d = chapter_dir
    _mark(d, 1)
    monkeypatch.setattr(mapi, "_manga_read_adapter", lambda s: _FakeAdapter())

    def _decode_boom(*a, **k):
        raise TypeError("cannot decode webp without libwebp")

    monkeypatch.setattr(mapi, "_serve_remote_image", _decode_boom)
    r = client.get("/api/manga/fakejm/100/chapter/211/img/0")
    assert r.status_code == 409, (r.status_code, r.data[:160])
    err = (r.get_json() or {}).get("error") or ""
    assert "重新获取" in err, err
    assert "本次回源失败" in err, f"必须带上本次失败原因（否则两类原因混为一谈）：{err}"
    assert "TypeError" in err and "libwebp" in err, err


def test_banner_and_page_409_come_from_the_same_condition(client, chapter_dir, monkeypatch):
    """配对不变式（指南 §4 P0）：同状态下
       `/chapter`（章节级）必须给提示条，`/img`（逐页）才可能 409。

    客户端只在 `/chapter` 的 `stale_processing` 上挂提示条与「重新获取」按钮，
    逐页失败 UI 只有"第 N 页加载失败"。若只有 `/img` 报 409 而章节不带标记，
    用户就会把"旧缓存"看成"解码坏了"——正是指南禁止的那种显示。
    """
    mapi, d = chapter_dir
    _mark(d, 1)
    monkeypatch.setattr(mapi, "_manga_read_adapter", lambda s: _FakeAdapter())
    monkeypatch.setattr(mapi, "_local_chapter_images",
                        lambda s, c, ch: ["/x"] * 4, raising=False)

    def _net_down(*a, **k):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(mapi, "_serve_remote_image", _net_down)
    meta = client.get("/api/manga/fakejm/100/chapter/212").get_json()
    img = client.get("/api/manga/fakejm/100/chapter/212/img/0")
    assert meta.get("stale_processing") is True, meta
    assert img.status_code == 409, img.status_code
    # 两侧的说明文字必须同源（同一个判定、同一句话），不能各说各话
    reason = meta.get("stale_reason") or ""
    assert reason and reason in ((img.get_json() or {}).get("error") or ""), \
        (reason, img.get_json())
