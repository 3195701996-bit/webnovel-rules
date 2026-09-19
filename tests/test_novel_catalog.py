# -*- coding: utf-8 -*-
"""0.59.0 回归（离线）：小说源移动可用性台账（把路线 P0-3 的纪律用到小说侧）。

背景：P0-3 给漫画源建了"注册/依赖/实测/分类"的单一事实来源，小说侧一直没有——
用户只看到 34 个源和一句"部分源可用"，**没人知道哪几个可用、卡在哪一步**。

本文件锁住的契约：
1. 分类口径：**未验证 ≠ 可用**——没有实测记录的源永远是「待验证」，
   不允许因为"启用着"或"看起来正常"就标成可用；
2. 停用 ≠ 不能用：用户停用的源单独一类，理由是"已停用（未纳入实测）"；
3. 部分可用必须写出**卡在哪一步**（按 搜索 → 详情 → 目录 → 正文 的固定顺序取最早失败）；
4. 实测记录过期（≥7 天）不是分类变化，而是加一条"结论已过期（N 天前），建议重测"；
5. 汇总口径分子分母一致：启用口径通过率 = 启用且已验证 / 启用；
6. 台账与 `/api/sources` 合并字段**同源**（同一个 build()），不会两处不一致；
7. Markdown 导出带证据数字（搜索条数/目录章数/正文字数）与"不推测"的免责说明。
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import novel_catalog as nc  # noqa: E402


def _src(uid, enabled=True, name=None, url="https://x.example.com"):
    return {"uid": uid, "bookSourceName": name or uid, "bookSourceUrl": url,
            "enabled": enabled}


def _rec(uid, status, stages=None, tested_at=None, reason="", keyword="剑来"):
    return {"uid": uid, "name": uid, "status": status, "reason": reason,
            "tested_at": tested_at or time.strftime("%Y-%m-%d %H:%M:%S"),
            "keyword": keyword, "stages": stages or {}}


_OK_STAGES = {"search": {"ok": True, "count": 12},
              "toc": {"ok": True, "count": 1278},
              "content": {"ok": True, "chars": 642}}


# ── 1) 分类：待验证 ≠ 可用 ─────────────────────────────────────────
def test_untested_source_is_never_marked_usable():
    item = nc.classify(_src("s1"), None)
    assert item["category"] == "untested" and item["category_label"] == "待验证"
    assert "尚无实测记录" in item["reason"]
    assert item["tested_at"] == "" and item["failed_stage"] == ""


def test_verified_requires_all_three_stages():
    item = nc.classify(_src("s1"), _rec("s1", "verified", _OK_STAGES))
    assert item["category"] == "android_verified" and item["category_label"] == "已验证"
    assert item["counts"] == {"search": 12, "toc": 1278, "chars": 642}
    assert item["failed_stage"] == "" and item["reason"] == ""


@pytest.mark.parametrize("status,expected", [
    ("partial", "android_partial"),
    ("failed", "android_failed"),
    ("unsupported", "android_unsupported"),
])
def test_status_mapping(status, expected):
    item = nc.classify(_src("s1"), _rec("s1", status, {"search": {"ok": False}}))
    assert item["category"] == expected
    assert item["category_label"] == nc.CATEGORY_LABELS[expected]


def test_disabled_is_its_own_category_even_with_record():
    """停用是用户的选择，不是"这个源不能用"——两类必须分开"""
    item = nc.classify(_src("s1", enabled=False), _rec("s1", "failed",
                                                       {"search": {"ok": False}}))
    assert item["category"] == "disabled" and item["category_label"] == "已停用"
    assert "已停用" in item["reason"]
    assert item["tested_at"], "停用也要把上次实测时间带出来（信息不藏）"
    assert item["record_status"] == "failed" and "上次实测" in item["reason"]
    assert item["failed_stage"] == "search", "停用源也保留上次卡在哪一步"


def test_unknown_status_does_not_become_usable():
    item = nc.classify(_src("s1"), _rec("s1", "weird_status"))
    assert item["category"] == "untested"
    assert "未知实测状态" in item["reason"]


# ── 2) 失败阶段：按固定顺序取最早 ──────────────────────────────────
def test_earliest_failed_stage_order():
    stages = {"search": {"ok": True}, "book": {"ok": False}, "toc": {"ok": False},
              "content": {"ok": False}}
    assert nc.earliest_failed_stage(stages) == "book"
    assert nc.earliest_failed_stage({"search": {"ok": True}, "toc": {"ok": False},
                                     "content": {"ok": False}}) == "toc"
    assert nc.earliest_failed_stage(_OK_STAGES) == ""
    assert nc.earliest_failed_stage({}) == ""


def test_partial_reports_stage_label():
    item = nc.classify(_src("s1"), _rec(
        "s1", "partial", {"search": {"ok": True, "count": 5}, "toc": {"ok": False}},
        reason=""))
    assert item["failed_stage"] == "toc" and item["failed_stage_label"] == "目录"
    assert item["reason"] == "目录阶段失败", "没有服务端原因时也要给出可读原因"


# ── 3) 过期标记 ────────────────────────────────────────────────────
def test_stale_record_is_flagged_not_reclassified():
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 10 * 86400))
    item = nc.classify(_src("s1"), _rec("s1", "verified", _OK_STAGES, tested_at=old))
    assert item["category"] == "android_verified", "过期不等于不能用"
    assert item["stale"] is True and item["stale_days"] >= 10
    assert "过期" in item["reason"] and "建议重测" in item["reason"]


def test_fresh_record_is_not_stale():
    item = nc.classify(_src("s1"), _rec("s1", "verified", _OK_STAGES))
    assert item["stale"] is False and item["stale_days"] == 0


# ── 4) build：汇总口径、排序、同 uid 取最新 ────────────────────────
def test_build_summary_and_ordering(monkeypatch):
    srcs = [_src("ok_src"), _src("bad_src"), _src("half_src"), _src("off_src", enabled=False),
            _src("new_src")]
    recs = [_rec("ok_src", "verified", _OK_STAGES),
            _rec("bad_src", "failed", {"search": {"ok": False}}),
            _rec("half_src", "partial", {"search": {"ok": True}, "toc": {"ok": False}},
                 reason="目录获取失败"),
            _rec("off_src", "verified", _OK_STAGES)]
    monkeypatch.setattr(nc, "load_all", lambda: srcs)
    monkeypatch.setattr(nc.source_verify, "results_payload",
                        lambda: {"items": recs})
    data = nc.build()
    s = data["summary"]
    assert s["total"] == 5 and s["enabled"] == 4 and s["disabled"] == 1
    # "当前可用"只看启用源；"曾经测通"把已停用的也算上——两件事必须分开
    assert s["verified"] == 1 and s["verified_enabled"] == 1
    assert s["record_verified"] == 2 and s["disabled_verified"] == 1
    assert s["enabled_pass_rate"] == 25, "1/4 启用源通过"
    assert s["partial"] == 1 and s["failed"] == 1 and s["untested"] == 1
    # 排序：问题在前，已验证靠后，停用最后
    order = [r["uid"] for r in data["sources"]]
    assert order.index("bad_src") < order.index("half_src") < order.index("new_src")
    assert order.index("new_src") < order.index("ok_src") < order.index("off_src")


def test_build_keeps_newest_record_per_uid(monkeypatch):
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3 * 86400))
    new = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 60))
    monkeypatch.setattr(nc, "load_all", lambda: [_src("dup_src")])
    monkeypatch.setattr(nc.source_verify, "results_payload", lambda: {"items": [
        _rec("dup_src", "failed", {"search": {"ok": False}}, tested_at=old),
        _rec("dup_src", "verified", _OK_STAGES, tested_at=new),
    ]})
    row = nc.build()["sources"][0]
    assert row["category"] == "android_verified" and row["tested_at"] == new


def test_build_survives_broken_verify_store(monkeypatch):
    """实测记录读不出来时，台账必须仍然可用（全部列待验证），而不是整页报错"""
    monkeypatch.setattr(nc, "load_all", lambda: [_src("s1")])

    def _boom():
        raise RuntimeError("记录文件损坏")

    monkeypatch.setattr(nc.source_verify, "results_payload", _boom)
    data = nc.build()
    assert data["summary"]["untested"] == 1 and data["sources"][0]["category"] == "untested"


# ── 5) Markdown 导出 ──────────────────────────────────────────────
def test_markdown_contains_evidence_and_disclaimer(monkeypatch):
    monkeypatch.setattr(nc, "load_all", lambda: [_src("ok_src"), _src("new_src")])
    monkeypatch.setattr(nc.source_verify, "results_payload",
                        lambda: {"items": [_rec("ok_src", "verified", _OK_STAGES)]})
    md = nc.render_markdown(nc.build())
    assert "# 小说源移动可用性台账" in md
    assert "搜索 12 条" in md and "目录 1278 章" in md and "正文 642 字" in md
    assert "待验证" in md and "未验证 ≠ 可用" in md
    assert "| 源 |" in md and md.count("\n|") >= 3


# ── 6) HTTP 契约 + 与 /api/sources 同源 ───────────────────────────
@pytest.fixture(scope="module")
def client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


def test_catalog_api_contract(client):
    r = client.get("/api/novel/catalog")
    assert r.status_code == 200
    d = r.get_json()
    assert d["sources"] and d["summary"]["total"] == len(d["sources"])
    for row in d["sources"]:
        for k in ("uid", "name", "enabled", "category", "category_label", "reason",
                  "failed_stage", "tested_at", "stale", "counts"):
            assert k in row, f"台账条目缺少字段 {k}"
        # 诚实性不变式：标成"已验证"的必须有实测时间
        if row["category"] == "android_verified":
            assert row["tested_at"], f"{row['uid']} 标已验证却没有实测时间"


def test_catalog_markdown_endpoint(client):
    r = client.get("/api/novel/catalog?format=md")
    assert r.status_code == 200
    assert "小说源移动可用性台账" in r.get_data(as_text=True)


def test_sources_endpoint_merges_catalog_fields(client):
    """书源页一次请求就能拿到分类（与台账同一个 build()，不会两处不一致）"""
    d = client.get("/api/novel/catalog").get_json()
    cat = {r["uid"]: r for r in d["sources"]}
    srcs = client.get("/api/sources").get_json()["sources"]
    assert srcs
    checked = 0
    for s in srcs:
        uid = s.get("uid") or ""
        row = cat.get(uid)
        if not row:
            continue
        checked += 1
        assert s.get("category") == row["category"]
        assert s.get("category_label") == row["category_label"]
        assert s.get("category_reason") == row["reason"]
        assert s.get("failed_stage") == row["failed_stage"]
        assert s.get("verify_stale") == row["stale"]
        assert s.get("verify_counts") == row["counts"]
    assert checked >= 1, "至少应有一个源能在两处对上"

# ── 7) "本轮跳过"不许覆盖已有结论；同 uid 多文件要合并成一条 ──────────
def test_skip_record_does_not_clobber_verified(tmp_path, monkeypatch):
    """分批跑第二轮时，skipped 记录不能把"已验证"顶掉（否则台账越跑越差）"""
    import engine.source_verify as sv
    sf = tmp_path / "verified.json"
    monkeypatch.setattr(sv, "VERIFIED_FILE", str(sf), raising=False)
    monkeypatch.setattr(sv, "_results", None, raising=False)
    sv._write_merged({"items": [_rec("s1", "verified", _OK_STAGES, tested_at="2026-09-16 10:00:00")]})
    sv._save({"items": [
        {"uid": "s1", "status": "skipped", "tested_at": "2026-09-16 12:00:00",
         "reason": "最近 7 天内已验证通过，本轮不重复验证", "stages": {}},
        {"uid": "s2", "status": "verified", "tested_at": "2026-09-16 12:00:00",
         "reason": "", "stages": _OK_STAGES},
    ]})
    items = {it["uid"]: it for it in sv.load_results()["items"]}
    assert items["s1"]["status"] == "verified", "跳过不得覆盖已验证结论"
    assert items["s1"]["tested_at"] == "2026-09-16 10:00:00"
    assert items["s1"]["skipped_at"] == "2026-09-16 12:00:00", "但要留下『本轮跳过过』的痕迹"
    assert items["s2"]["status"] == "verified"


def test_lone_skip_is_untested_with_reason():
    """没有可沿用结论的 skipped（例如首轮就跳过）→ 待验证 + 跳过原因（不是『未知状态』）"""
    item = nc.classify(_src("s1"), _rec("s1", "skipped", {},
                                        reason="书源已停用，本轮未纳入"))
    assert item["category"] == "untested"
    assert item["reason"] == "本轮跳过：书源已停用，本轮未纳入"
    assert item["record_status"] == "skipped"


def test_build_merges_duplicate_uid_files(monkeypatch):
    """同 uid 两个文件 → 台账一条（否则用户看到两行一样的源，以为界面坏了）"""
    dup = [_src("same_uid", name="重复源A"), _src("same_uid", name="重复源A")]
    monkeypatch.setattr(nc, "load_all", lambda: dup)
    monkeypatch.setattr(nc.source_verify, "results_payload",
                        lambda: {"items": [_rec("same_uid", "verified", _OK_STAGES)]})
    data = nc.build()
    assert len(data["sources"]) == 1, "同 uid 必须合并成一条"
    row = data["sources"][0]
    assert row["files"] == 2
    assert "重复" in row["reason"] and "重复源清理" in row["reason"]
    assert data["summary"]["duplicate_uids"] == 1
    assert data["summary"]["total"] == 1, "汇总按 uid 计数，不按文件数"

def test_duplicate_uid_uses_any_enabled_and_flags_mismatch(monkeypatch):
    """同 uid 多文件：只要有一份启用就算启用；启用状态不一致必须写出来"""
    dup = [_src("same_uid", enabled=True), _src("same_uid", enabled=False)]
    monkeypatch.setattr(nc, "load_all", lambda: dup)
    monkeypatch.setattr(nc.source_verify, "results_payload",
                        lambda: {"items": [_rec("same_uid", "verified", _OK_STAGES)]})
    row = nc.build()["sources"][0]
    assert row["enabled"] is True, "有一份启用就算启用（用户可以只启用其中一份）"
    assert row["category"] == "android_verified"
    assert "1/2" in row["reason"] and "不一致" in row["reason"]

    both_off = [_src("same_uid", enabled=False), _src("same_uid", enabled=False)]
    monkeypatch.setattr(nc, "load_all", lambda: both_off)
    row2 = nc.build()["sources"][0]
    assert row2["enabled"] is False and row2["category"] == "disabled"
