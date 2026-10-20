# -*- coding: utf-8 -*-
"""详情"有没有内容"的判据必须同时看 chapters 与 volumes（0.74.11 实测缺陷）。

## 缺陷（三个症状、一个根因）

`api_manga_detail` 与 `_info.json` 缓存判定都只看 `chapters` 是否为空。
而**只有卷的漫画**（源站把章节标成 type=2 卷）返回的是 `chapters=[] volumes=[…]`
——这是**正常数据**，却被当成"风控脏数据"：

1. 详情接口又走一次 Playwright 兜底：实测《巨人》(jurenmeiman) **24.2 秒**
   （同一时刻話型漫画《魔王大人…》**1.0 秒**）；
2. 响应被标成 `fallback=True`，看起来像降级；
3. `_info.json` 缓存因 `chapters` 为空被判无效 → **每次打开都重新走一遍**。

修后：**2.8 秒**、`fallback=None`、第二次 **0 ms** 命中缓存。

## 本文件锁死

判据本身（chapters 或 volumes 任一非空 = 有内容）+ 端点行为（卷型漫画不被打上
fallback、缓存能复用）。
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.manga_api import _has_detail_content  # noqa: E402


# ── 1. 判据 ────────────────────────────────────────────────────────────
def test_has_content_with_chapters():
    assert _has_detail_content({"chapters": [{"id": "1"}]}) is True


def test_has_content_with_volumes_only():
    """**只有卷**是正常数据（实测《巨人》5 章全是卷）"""
    assert _has_detail_content({"chapters": [], "volumes": [{"id": "v1"}]}) is True


def test_no_content_when_both_empty():
    assert _has_detail_content({"chapters": [], "volumes": []}) is False


def test_has_content_tolerates_bad_input():
    assert _has_detail_content(None) is False
    assert _has_detail_content({}) is False
    assert _has_detail_content({"chapters": None, "volumes": None}) is False


# ── 2. 端点行为：卷型漫画不得被误判 ────────────────────────────────────
@pytest.fixture()
def volume_only_client(tmp_path, monkeypatch, request):
    """构造"只有卷"的适配器返回，走真实的详情端点"""
    import server.manga_api as ma
    import server.state as st

    data = tmp_path / "data"
    (data / "manga" / "_cache").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(st, "DATA_DIR", str(data), raising=False)
    monkeypatch.setattr(ma, "MANGA_DETAIL_CACHE_DIR", str(data / "manga" / "_cache"),
                        raising=False)

    from engine.manga.base import Chapter, ComicDetails

    class _Ad(object):
        name = "拷贝漫画"
        key = "copymanga"

        def __init__(self):
            self.calls = 0
            self.in_cooldown = lambda: False

        def comic_info(self, cid):
            self.calls += 1
            return ComicDetails(
                id=cid, title="巨人", cover="https://x/c.jpg", author="A",
                chapters=[Chapter(id="v1", name="第01卷", group="卷"),
                          Chapter(id="v2", name="第02卷", group="卷")])

    ad = _Ad()
    monkeypatch.setattr(ma, "_manga_read_adapter", lambda s: ad, raising=False)
    monkeypatch.setattr(ma, "_manga_adapter", lambda s: ad, raising=False)
    fb_calls = []
    monkeypatch.setattr(ma, "_api_detail_fallback",
                        lambda s, c, e: fb_calls.append(e) or None, raising=False)
    monkeypatch.setattr(ma, "_scan_downloaded_chapters", lambda s, c: [], raising=False)
    # 每条用例一个独立 comic_id：同进程内详情有内存级缓存，同 id 会互相影响
    cid = "jur_" + request.node.name[-12:]
    import app
    return app.app.test_client(), ad, fb_calls, data, cid


def test_volume_only_detail_is_not_treated_as_dirty(volume_only_client):
    c, ad, fb_calls, data, cid = volume_only_client
    t0 = time.time()
    r = c.get("/api/manga/copymanga/%s" % cid)
    d = r.get_json() or {}
    assert r.status_code == 200, d
    assert not fb_calls, "只有卷的漫画**不得**触发网页兜底（旧写法因此多花 24 秒）"
    assert d.get("fallback") is None, "不得被打上 fallback 标记"
    assert len(d.get("volumes") or []) == 2, d
    assert d.get("chapters") == []
    assert ad.calls == 1


def test_volume_only_detail_cache_is_reused(volume_only_client):
    """缓存判定同样要看 volumes：否则每次打开都重新抓一遍"""
    c, ad, fb_calls, data, cid = volume_only_client
    c.get("/api/manga/copymanga/%s" % cid)
    assert ad.calls == 1, "第一次应当真的去取数"
    t0 = time.time()
    c.get("/api/manga/copymanga/%s" % cid)
    assert ad.calls == 1, "第二次必须命中缓存，不得重新抓取"
    assert time.time() - t0 < 1.0


def test_mixed_detail_resume_index_uses_volume_first_order(
        volume_only_client, tmp_path, monkeypatch):
    """详情返回的 resume.index 必须与卷优先的 Web/APK 阅读目录一致。"""
    c, ad, _fallbacks, _data, cid = volume_only_client
    import server.manga_api as ma
    from engine.manga.base import Chapter, ComicDetails
    history = tmp_path / "_history.json"
    history.write_text(json.dumps({f"copymanga:{cid}": {
        "idx": 1, "chapter_id": "v1", "chapter_label": "第01卷",
        "pos": "第01卷 P3",
    }}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ma, "MANGA_HISTORY_FILE", str(history))
    ad.comic_info = lambda comic_id: ComicDetails(
        id=comic_id, title="混合目录", cover="", author="",
        chapters=[Chapter(id="c1", name="第01话"),
                  Chapter(id="v1", name="第01卷")])

    response = c.get(f"/api/manga/copymanga/{cid}")
    assert response.status_code == 200, response.get_data(as_text=True)
    detail = response.get_json()
    assert detail["volumes"][0]["id"] == "v1"
    assert detail["chapters"][0]["id"] == "c1"
    assert detail["resume"]["index"] == 0


def test_chinese_numeral_volumes_are_classified_and_ordered_before_episodes(
        volume_only_client, tmp_path, monkeypatch):
    """中文数字卷不能被误放入单话目录或排在阅读顺序后部。"""
    c, ad, _fallbacks, _data, cid = volume_only_client
    import server.manga_api as ma
    from engine.manga.base import Chapter, ComicDetails
    history = tmp_path / "_history_cn_volume.json"
    history.write_text(json.dumps({f"copymanga:{cid}": {
        "chapter_id": "v3", "chapter_label": "第三卷",
    }}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ma, "MANGA_HISTORY_FILE", str(history))
    ad.comic_info = lambda comic_id: ComicDetails(
        id=comic_id, title="中文卷号", cover="", author="",
        chapters=[Chapter(id="v12", name="第十二卷"),
                  Chapter(id="c2", name="第2話"),
                  Chapter(id="v3", name="第三卷"),
                  Chapter(id="c1", name="第1話")])

    response = c.get(f"/api/manga/copymanga/{cid}")
    assert response.status_code == 200, response.get_data(as_text=True)
    detail = response.get_json()
    assert [row["id"] for row in detail["volumes"]] == ["v3", "v12"]
    assert [row["id"] for row in detail["chapters"]] == ["c1", "c2"]
    assert detail["resume"]["index"] == 0
