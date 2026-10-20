# -*- coding: utf-8 -*-
"""禁漫码归一化回归（离线）

规则：JM1234567 与 1234567 数字部分相同即同一部漫画。
不归一化会导致同一部漫画出现两套缓存目录、两条书库/收藏/历史记录。
"""
import os
import sys
import json

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.manga.jm import Jm  # noqa: E402


@pytest.mark.parametrize("raw,expect", [
    ("JM1234567", "1234567"),
    ("jm1234567", "1234567"),
    ("Jm1234567", "1234567"),
    ("1234567", "1234567"),
    ("  JM1234567  ", "1234567"),
    ("jm 1234567", "1234567"),
])
def test_normalize_equivalent_forms(raw, expect):
    assert Jm.normalize_id(raw) == expect


def test_prefixed_and_bare_are_identical():
    assert Jm.normalize_id("JM559440") == Jm.normalize_id("559440")


@pytest.mark.parametrize("raw", ["abc", "JMabc", "jm12ab", "", None])
def test_non_code_passthrough(raw):
    """非禁漫码原样返回，不得误剥前缀"""
    out = Jm.normalize_id(raw)
    assert out == (str(raw).strip() if raw else "")


def test_uppercase_prefix_was_broken_before():
    """回归点：旧实现 startswith('jm') 大小写敏感，
    JM 前缀漏剥会拼出 album?id=JM123 导致请求失败"""
    assert Jm.normalize_id("JM123") == "123"


# ── 服务端键一致性 ────────────────────────────────────────────

@pytest.fixture(scope="module")
def app_mod():
    import app
    return app


def test_download_key_identical(app_mod):
    assert app_mod._manga_dl_key("jm", "JM123") == app_mod._manga_dl_key("jm", "123")


def test_other_source_not_normalized(app_mod):
    """归一化只对 jm 生效，不得影响 copymanga 等源的 ID"""
    assert app_mod._manga_dl_key("copymanga", "jm123") == "copymanga:jm123"
    assert app_mod._norm_comic_id("copymanga", "JM123") == "JM123"


def test_favorite_add_then_delete_with_other_form(app_mod, tmp_path, monkeypatch):
    """用 JM 前缀收藏，用纯数字删除 —— 必须能删掉"""
    import server.manga_api as MA  # R47: 路由实现已拆入 manga_api，打桩随实现走
    fav_file = tmp_path / "fav.json"
    monkeypatch.setattr(MA, "MANGA_FAV_FILE", str(fav_file))
    app_mod.app.config["TESTING"] = True
    c = app_mod.app.test_client()

    c.post("/api/manga/favorites",
           json={"source": "jm", "comic_id": "JM559440", "title": "t"})
    saved = app_mod._read_json(str(fav_file), {})
    assert list(saved) == ["jm:559440"]

    c.delete("/api/manga/favorites/jm/559440")
    assert app_mod._read_json(str(fav_file), {}) == {}


def test_history_two_forms_share_one_entry(app_mod, tmp_path, monkeypatch):
    """两种写法保存进度应合并为一条，而非各存一条"""
    hist_file = tmp_path / "hist.json"
    import server.manga_api as MA  # R47: 同上，打桩随实现走
    monkeypatch.setattr(MA, "MANGA_HISTORY_FILE", str(hist_file))
    app_mod.app.config["TESTING"] = True
    c = app_mod.app.test_client()

    c.post("/api/manga/history",
           json={"source": "jm", "comic_id": "JM559440", "idx": 1})
    c.post("/api/manga/history",
           json={"source": "jm", "comic_id": "559440", "idx": 7})

    hist = app_mod._read_json(str(hist_file), {})
    assert list(hist) == ["jm:559440"]
    assert hist["jm:559440"]["idx"] == 7      # 后写覆盖，续读位置不丢


def test_legacy_prefixed_history_resumes_and_migrates_without_losing_reads(
        app_mod, tmp_path, monkeypatch):
    import server.manga_api as MA
    hist_file = tmp_path / "hist-legacy.json"
    hist_file.write_text(json.dumps({"jm:JM559440": {
        "idx": 0, "pos": "第7话 P12", "title": "旧书名", "ts": 1,
        "chapter_id": "ch7", "chapter_label": "第7话",
        "read_chapter_ids": ["ch1", "ch7"],
        "read_chapters": [{"id": "ch1", "label": "第1话"}],
    }}), encoding="utf-8")
    monkeypatch.setattr(MA, "MANGA_HISTORY_FILE", str(hist_file))

    resume = MA._resume_payload("jm", "559440", [
        {"id": "ch1", "name": "第1话"}, {"id": "ch7", "name": "第7话"},
    ])
    assert resume["index"] == 1
    assert resume["page"] == 12

    app_mod.app.config["TESTING"] = True
    response = app_mod.app.test_client().post("/api/manga/history", json={
        "source": "jm", "comic_id": "559440", "idx": 1,
        "pos": "第7话 P13", "chapter_id": "ch7", "chapter_label": "第7话",
    })
    assert response.get_json()["ok"] is True
    saved = json.loads(hist_file.read_text(encoding="utf-8"))
    assert list(saved) == ["jm:559440"]
    assert saved["jm:559440"]["read_chapter_ids"] == ["ch1", "ch7"]
    assert saved["jm:559440"]["chapter_id"] == "ch7"


def test_legacy_prefixed_favorites_are_deduplicated_and_deletable(
        app_mod, tmp_path, monkeypatch):
    import server.manga_api as MA
    fav_file = tmp_path / "fav-legacy.json"
    fav_file.write_text(json.dumps({
        "jm:JM559440": {"title": "舊記錄", "unread_count": 4, "ts": 1},
        "jm:559440": {"title": "新記錄", "unread_count": 2, "ts": 2},
    }), encoding="utf-8")
    monkeypatch.setattr(MA, "MANGA_FAV_FILE", str(fav_file))
    monkeypatch.setattr(MA, "_manga_cached_chapters", lambda *_: [])
    app_mod.app.config["TESTING"] = True
    client = app_mod.app.test_client()

    rows = client.get("/api/manga/favorites").get_json()["favorites"]
    assert len(rows) == 1
    assert rows[0]["comic_id"] == "559440"
    assert rows[0]["unread_count"] == 2

    deleted = client.delete("/api/manga/favorites/jm/559440")
    assert deleted.get_json()["ok"] is True
    assert json.loads(fav_file.read_text(encoding="utf-8")) == {}
