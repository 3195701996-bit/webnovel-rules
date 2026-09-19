# -*- coding: utf-8 -*-
"""书库封面离线可看（回归：未联网时书库不显示封面）

根因：书库记录的 cover 只有**源站 URL**，本地从不保存封面文件；断网后浏览器
取不到图，卡片封面空白。修复契约：

  1. 新增 GET /api/manga/<source>/<comic_id>/cover：本地 cover.<ext> 存在则
     静态直出（immutable + ETag/304），**无需任何网络**；
  2. 本地没有但书库记录里有源站封面地址 → 回源一次并落盘（之后离线可看）；
     回源失败（断网）→ 明确 5xx，绝不 hang、也不写坏文件；
  3. 书库响应新增 cover_view（展示地址，走服务器接口），原 cover（源站 URL）
     保持不变——"重新下载/补下载"接口仍需要源站地址；
  4. 打开书库时后台把缺失封面补齐（低并发+单飞+失败冷却）；
  5. copymanga / copymanga_web 互为备选源查找封面。

本文件全程离线（本机无外网），正好覆盖"断网"这一场景。
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CID = "1472083"
SRC = "jm"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 2048
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import server.state as st
    import server.manga_api as ma

    cache = tmp_path / "_cache"
    dl = tmp_path / "downloads"
    (cache / SRC / CID).mkdir(parents=True, exist_ok=True)
    (dl / SRC / CID).mkdir(parents=True, exist_ok=True)
    lib = tmp_path / "_library.json"
    lib.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(ma, "MANGA_CACHE_DIR", str(cache), raising=False)
    monkeypatch.setattr(ma, "MANGA_DOWNLOADS_DIR", str(dl), raising=False)
    monkeypatch.setattr(ma, "MANGA_LIBRARY_FILE", str(lib), raising=False)
    monkeypatch.setattr(st, "MANGA_CACHE_DIR", str(cache))
    monkeypatch.setattr(st, "MANGA_DOWNLOADS_DIR", str(dl))
    monkeypatch.setattr(st, "MANGA_LIBRARY_FILE", str(lib), raising=False)

    import app
    app.app.config["TESTING"] = True
    c = app.app.test_client()

    def set_lib(records):
        lib.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

    with ma._cover_warm_lock:
        ma._cover_warm_inflight.clear()
        ma._cover_warm_fail.clear()
    return {"c": c, "cache": cache, "dl": dl, "lib": lib, "set_lib": set_lib,
            "ma": ma, "st": st, "tmp": tmp_path}


def _cover_url(source=SRC, cid=CID):
    return f"/api/manga/{source}/{cid}/cover"


# ── 1. 本地已有封面：断网可看 ────────────────────────────────
def test_local_cover_served_offline(env):
    p = os.path.join(str(env["cache"]), SRC, CID, "cover.jpg")
    with open(p, "wb") as f:
        f.write(JPEG)
    r = env["c"].get(_cover_url())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert r.data == JPEG
    cc = r.headers.get("Cache-Control", "")
    assert "immutable" in cc, cc
    etag = r.headers.get("ETag")
    assert etag
    r2 = env["c"].get(_cover_url(), headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.data == b""


def test_local_cover_in_downloads_dir_preferred(env):
    """已下载漫画的封面放在 downloads 目录，离线同样直出"""
    p = os.path.join(str(env["dl"]), SRC, CID, "cover.webp")
    with open(p, "wb") as f:
        f.write(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 2048)
    r = env["c"].get(_cover_url())
    assert r.status_code == 200
    assert r.data.startswith(b"RIFF")


def test_local_cover_path_ignores_tiny_file(env):
    """> 1000B 才算有效封面（与图片缓存同一标准），坏文件不得顶替"""
    p = os.path.join(str(env["cache"]), SRC, CID, "cover.jpg")
    with open(p, "wb") as f:
        f.write(b"\xff\xd8" + b"\x00" * 100)      # 过小
    assert env["st"]._local_cover_path(SRC, CID) is None
    r = env["c"].get(_cover_url())
    assert r.status_code == 404


# ── 2. 本地没有封面时的行为 ──────────────────────────────────
def test_no_local_no_remote_returns_404(env):
    r = env["c"].get(_cover_url())
    assert r.status_code == 404
    assert r.get_json().get("error")


def test_remote_fetch_failure_is_explicit_and_no_bad_file(env):
    """断网 + 本地无封面 → 明确 5xx；不得留下坏封面文件"""
    env["set_lib"]([{"source": SRC, "comic_id": CID, "title": "t",
                     "cover": "https://cdn.invalid.example/cover.jpg"}])
    t0 = time.time()
    r = env["c"].get(_cover_url())
    assert r.status_code >= 500, r.status_code
    assert r.get_json().get("error")
    assert time.time() - t0 < 60, "断网时必须快速失败，不能挂住"
    assert env["st"]._local_cover_path(SRC, CID) is None
    assert not os.path.exists(os.path.join(str(env["cache"]), SRC, CID, "cover.jpg"))


# ── 3. 回源成功即落盘（之后离线可看） ────────────────────────
def test_fetch_success_saves_local_then_offline_ok(env, monkeypatch):
    env["set_lib"]([{"source": SRC, "comic_id": CID, "title": "t",
                     "cover": "https://cdn.invalid.example/cover.jpg"}])

    class _Resp:
        status_code = 200
        content = JPEG
        headers = {"Content-Type": "image/jpeg"}

    calls = {"n": 0}
    seen_headers = {}

    def _fake_fetch(url, headers, timeout=20, **kw):
        calls["n"] += 1
        assert url.endswith("/cover.jpg")
        seen_headers.update(headers or {})
        return _Resp()

    import engine.manga.downloader as dl_mod
    monkeypatch.setattr(dl_mod, "fetch_image_checked", _fake_fetch)

    r = env["c"].get(_cover_url())
    assert r.status_code == 200 and r.data == JPEG
    assert calls["n"] == 1
    # 落在哪个根取决于该漫画是否有 downloads 目录（downloads 优先），
    # 只需保证"本地有了封面文件"
    _lp = env["st"]._local_cover_path(SRC, CID)
    assert _lp and os.path.exists(_lp), "回源成功后必须落盘（离线可看的前提）"
    # 二次请求（模拟之后断网）→ 直接本地直出，不再回源
    r2 = env["c"].get(_cover_url())
    assert r2.status_code == 200 and r2.data == JPEG
    assert calls["n"] == 1, "本地已有封面不得再回源"
    # 回源必须带 User-Agent（部分源 CDN 校验；见 _fetch_cover_to_local）
    assert seen_headers.get("User-Agent"), f"封面回源缺少 User-Agent: {seen_headers}"


def test_save_cover_bytes_uses_magic_extension(env):
    st = env["st"]
    p = st._save_cover_bytes(SRC, CID, PNG)
    assert p and p.endswith("cover.png"), p
    assert st._local_cover_path(SRC, CID) == p
    os.remove(p)
    p2 = st._save_cover_bytes(SRC, CID, JPEG)
    assert p2 and p2.endswith("cover.jpg"), p2


# ── 4. 书库响应：cover_view + 原 cover 保留 ──────────────────
def test_library_exposes_cover_view_keeps_remote_cover(env):
    env["set_lib"]([
        {"source": SRC, "comic_id": CID, "title": "有源站封面",
         "cover": "https://cdn.invalid.example/cover.jpg", "images": 10},
        {"source": SRC, "comic_id": "999", "title": "无封面", "cover": "",
         "images": 5},
    ])
    d = env["c"].get("/api/manga/library").get_json()
    by = {str(x["comic_id"]): x for x in d["comics"]}
    assert by[CID]["cover_view"] == f"/api/manga/{SRC}/{CID}/cover"
    assert by[CID]["cover"] == "https://cdn.invalid.example/cover.jpg", \
        "源站 cover 必须保留（下载/补下载接口要用）"
    assert by["999"]["cover_view"] == "", "没有封面地址时不应给出无效地址"


def test_library_cover_view_present_when_local_cover_exists(env):
    p = os.path.join(str(env["cache"]), SRC, CID, "cover.webp")
    with open(p, "wb") as f:
        f.write(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 2048)
    env["set_lib"]([{"source": SRC, "comic_id": CID, "title": "本地封面",
                     "cover": "", "images": 3}])
    d = env["c"].get("/api/manga/library").get_json()
    assert d["comics"][0]["cover_view"] == f"/api/manga/{SRC}/{CID}/cover"


# ── 5. 打开书库 → 后台补齐封面 ───────────────────────────────
def test_library_view_warms_missing_covers(env, monkeypatch):
    env["set_lib"]([{"source": SRC, "comic_id": CID, "title": "待补",
                     "cover": "https://cdn.invalid.example/c.jpg", "images": 1}])
    ma = env["ma"]
    done = {"n": 0}

    def _fake_local(source, comic_id, url, timeout=20):
        r = env["st"]._save_cover_bytes(source, comic_id, JPEG)
        done["n"] += 1          # 计数在落盘之后：避免测试读到"还没写完"的中间态
        return r

    monkeypatch.setattr(ma, "_fetch_cover_to_local", _fake_local)
    env["c"].get("/api/manga/library")
    t0 = time.time()
    while time.time() - t0 < 5 and not env["st"]._local_cover_path(SRC, CID):
        time.sleep(0.02)
    assert done["n"] == 1, "打开书库应后台补齐缺失封面"
    assert env["st"]._local_cover_path(SRC, CID)


def test_cover_warm_respects_failure_cooldown(env):
    """同一封面补失败后进入冷却：不会每次打开书库都打一次源站"""
    ma = env["ma"]
    with ma._cover_warm_lock:
        ma._cover_warm_fail[(SRC, CID)] = time.time()
    ma._warm_missing_covers([{"source": SRC, "comic_id": CID,
                              "_cover_url": "https://x/y.jpg"}])
    assert (SRC, CID) not in ma._cover_warm_inflight, "冷却期内不得再排队"


# ── 6. copymanga 双源互查 ────────────────────────────────────
def test_cover_url_lookup_across_copymanga_sources(env):
    env["set_lib"]([{"source": "copymanga", "comic_id": "abc",
                     "title": "t", "cover": "https://cdn.invalid.example/a.jpg"}])
    ma = env["ma"]
    assert ma._library_cover_url("copymanga_web", "abc") == \
        "https://cdn.invalid.example/a.jpg"
    assert ma._library_cover_url("copymanga", "abc") == \
        "https://cdn.invalid.example/a.jpg"
    assert ma._library_cover_url("jm", "abc") == ""
