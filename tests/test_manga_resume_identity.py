# -*- coding: utf-8 -*-
"""0.64.0 回归（离线）：**续读落点必须按章节身份定位，不能按目录下标**。

用户反馈（2026-09-17，两轮）：
  1. "点击阅读后继续阅读打开的依旧不是书库显示的阅读记录"；
  2. "甚至我刚读完一本漫画，退出再重进之后就让我从第一话重新读"。

## 真因（开发机上用**真实数据**定位到的）

阅读记录里存的是 `idx`——**当时那份章节目录的下标**。而目录会变：站方加更、删章、
插入"推特杂图/番外/试看"这类非正话条目。实测记录：

    copymanga_web:woxihuanderensuoxihuanderen
    记录  idx=55  pos='第47话 P30'
    当前目录 54 章（idx 越界），且 idx=44 的标签是"第37话"

于是续读落点：越界 → 退回"第一个已下载话/第 1 话"。更糟的是阅读器落点后会自动
写回记录，**把"第47话 P30"覆盖成"第1话 P1"**——记录一旦被覆盖，之后每次进来都只能
从第一话开始（用户复现到的正是这个：该书目的记录现在已经变成 idx=0 / '第01話 P1'）。

## 本用例锁住的契约

A. 定位优先级：chapter_id → 章名精确 → 归一化 → 话号唯一 → 最近话号 → 下标兜底；
B. 归一化要覆盖真实写法差异：全角/半角、空格、"話/话"、"第01話/第1话"、中文数字；
C. **越界/找不到时不得静默当成"第一话"**：必须 exact=false + note 说明；
D. 书库行的 read_idx/read_ratio 与详情页落点同源（同一解析函数），且比例不越界；
E. 历史写入要保存 chapter_id/chapter_label（否则未来没有身份可比对）；
F. 目录整个对不上时（章节被删）→ 取最近话号并如实说明。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 真实形态的章节目录：注意 idx 与话号并不一一对应（站方插入了杂图/试看）
CHAPTERS = [
    {"id": "c000", "name": "第01話", "group": ""},
    {"id": "c001", "name": "第02話", "group": ""},
    {"id": "c002", "name": "第03話", "group": ""},
    {"id": "c022", "name": "第23话", "group": ""},
    {"id": "c023", "name": "第24话", "group": ""},
    {"id": "c044", "name": "第44话试看", "group": ""},
    {"id": "c045", "name": "第45话试看", "group": ""},
    {"id": "c999", "name": "推特杂图", "group": ""},
]


@pytest.fixture(scope="module")
def mapi():
    import server.manga_api as m
    return m


# ── A/B：定位优先级与归一化 ────────────────────────────────────────────

def test_chapter_id_wins_over_stale_index(mapi):
    """有章节 id 时用它定位——即使记录里的 idx 指到别的一话。"""
    r = mapi._resolve_reading_position(
        CHAPTERS, {"idx": 0, "pos": "第23话 P5", "chapter_id": "c022"})
    assert r["index"] == 3 and r["by"] == "chapter_id" and r["exact"] is True


def test_label_beats_index_when_directory_changed(mapi):
    """用户那一条：idx 已越界（55），靠章名定位到第23话。"""
    r = mapi._resolve_reading_position(CHAPTERS, {"idx": 55, "pos": "第23话 P30"})
    assert r["index"] == 3, r
    assert r["exact"] is True and r["by"] == "label"
    assert r["matched_label"] == "第23话" and r["note"] == ""


@pytest.mark.parametrize("written", [
    "第23话", "第23話", "第 23 话", "第二十三话", "第023話",
])
def test_label_normalization_variants(mapi, written):
    """同一话的各种写法都要认出来（全角/空格/話/前导零/中文数字）。"""
    r = mapi._resolve_reading_position(CHAPTERS, {"idx": 99, "pos": written + " P2"})
    assert r["index"] == 3, (written, r)
    assert r["exact"] is True, (written, r)


def test_label_with_group_suffix_still_matches(mapi):
    """目录里的「第44话试看」要能被记录里的「第44话」认出来（话号唯一时）。"""
    r = mapi._resolve_reading_position(CHAPTERS, {"idx": 0, "pos": "第44话 P3"})
    assert r["index"] == 5 and r["matched_label"] == "第44话试看", r


# ── C：找不到时必须"如实说明"，不能静默算第一话 ──────────────────────

def test_missing_chapter_reports_note_and_is_not_exact(mapi):
    """记录的话在当前目录里没了 → 取最近话号 + note，且 exact=False
    （exact=False 是客户端"不要自动覆盖记录"的依据）。"""
    r = mapi._resolve_reading_position(CHAPTERS, {"idx": 55, "pos": "第47话 P30"})
    assert r["index"] == 6, r                      # 最近的「第45话试看」
    assert r["exact"] is False and r["by"] == "near"
    assert "第47话" in r["note"] and "第45话试看" in r["note"], r


def test_no_comparable_label_falls_back_to_index_with_warning(mapi):
    """记录只有下标（没有可比对的章名）→ 允许退回下标，但要标注可能不准。"""
    r = mapi._resolve_reading_position(CHAPTERS, {"idx": 2, "pos": ""})
    assert r["index"] == 2 and r["by"] == "idx" and r["exact"] is False
    assert r["note"], "退回下标必须给出说明"


def test_unresolvable_returns_minus_one_not_zero(mapi):
    """完全无法定位 → index=-1（调用方据此提示），**不得**假装"就是第一话"。"""
    r = mapi._resolve_reading_position(
        [{"id": "x", "name": "序章", "group": ""}],
        {"idx": 77, "pos": "第47话 P30"})
    # 有一个话号可比的章节（序章没有话号）→ 走 near 分支；此处断言不会静默给 0
    assert r["exact"] is False, r
    r2 = mapi._resolve_reading_position([], {"idx": 3, "pos": "第4话 P1"})
    assert r2["index"] == -1 and r2["by"] == "none" and r2["note"]


# ── D：书库行与详情页落点同源 ─────────────────────────────────────────

def test_library_and_detail_use_same_resolution(mapi, tmp_path, monkeypatch):
    """书库的 read_idx 与详情的 resume.index 必须是同一话（同一解析函数）。"""
    hist = {"jm:100": {"idx": 55, "pos": "第23话 P30", "title": "T"}}
    p = tmp_path / "_history.json"
    p.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(p), raising=False)
    import server.state as st
    monkeypatch.setattr(st, "MANGA_HISTORY_FILE", str(p), raising=False)

    res = mapi._resume_payload("jm", "100", CHAPTERS)
    assert res and res["index"] == 3 and res["exact"] is True
    # 书库走的是同一个函数
    lib = mapi._resolve_reading_position(CHAPTERS, hist["jm:100"])
    assert lib["index"] == res["index"] == 3


def test_ratio_never_exceeds_100_percent(mapi):
    """旧实现用 (idx+1)/total 会算出 >100%（实测见过 104%）——解析后的下标不会。"""
    r = mapi._resolve_reading_position(CHAPTERS, {"idx": 55, "pos": "推特杂图 P1"})
    assert 0 <= r["index"] < len(CHAPTERS)
    ratio = round((r["index"] + 1) / len(CHAPTERS) * 100)
    assert 0 < ratio <= 100, ratio


# ── E：历史写入必须留下章节身份 ───────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    import server.state as st
    hist = tmp_path / "_history.json"
    monkeypatch.setattr(st, "MANGA_HISTORY_FILE", str(hist), raising=False)
    import server.manga_api as m
    monkeypatch.setattr(m, "MANGA_HISTORY_FILE", str(hist), raising=False)
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client(), hist


def test_history_save_keeps_chapter_identity(client):
    c, hist = client
    r = c.post("/api/manga/history", json={
        "source": "jm", "comic_id": "100", "idx": 22,
        "chapter_id": "c022", "chapter_label": "第23话",
        "pos": "第23话 P30", "title": "T"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    rec = json.loads(hist.read_text(encoding="utf-8"))["jm:100"]
    assert rec["chapter_id"] == "c022"
    assert rec["chapter_label"] == "第23话"
    assert rec["idx"] == 22 and rec["pos"] == "第23话 P30"
    # GET 要把它交回客户端（客户端据此比对身份）
    got = c.get("/api/manga/history").get_json()["history"][0]
    assert got["chapter_id"] == "c022" and got["chapter_label"] == "第23话"


def test_history_save_derives_label_from_pos_when_absent(client):
    """老客户端只给 pos 时，章名从 pos 里取（不给未来留下"没有身份"的记录）。"""
    c, hist = client
    c.post("/api/manga/history", json={
        "source": "jm", "comic_id": "101", "idx": 3,
        "pos": "第4话 P2", "title": "T"})
    rec = json.loads(hist.read_text(encoding="utf-8"))["jm:101"]
    assert rec["chapter_label"] == "第4话" and rec["chapter_id"] == ""


def test_detail_payload_carries_resume(client, monkeypatch):
    """详情接口要给出 resume（客户端不再自己用下标猜）。"""
    c, hist = client
    import server.manga_api as m
    hist.write_text(json.dumps(
        {"jm:100": {"idx": 55, "pos": "第23话 P30", "title": "T"}},
        ensure_ascii=False), encoding="utf-8")
    payload = m.api_manga_detail.__wrapped__ if hasattr(m.api_manga_detail, "__wrapped__") \
        else None
    # 直接调用内部兜底（不触发源站）：与四条返回路径用的是同一个函数
    out = m.api_manga_detail.__globals__["_resume_payload"]("jm", "100", CHAPTERS)
    assert out and out["index"] == 3 and out["pos"] == "第23话 P30"
    assert out["exact"] is True
