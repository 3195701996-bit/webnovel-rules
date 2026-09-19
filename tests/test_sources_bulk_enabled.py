# -*- coding: utf-8 -*-
"""书源批量启用/停用 + 「文件名 ≠ uid」定位修复的契约回归（离线）

背景（真实问题，不是想象）：
1. 内置 34 个源的启用状态是长期一个个点出来的，手机端搜索只覆盖启用中的源。
   要"只启用验证通过的 / 停用失效的"若不能批量做，等于没有这个能力。
2. **更严重的既有 bug**：set_enabled / delete_source 过去直接拼 `sources/<uid>.json`，
   而手工放进 sources/ 的文件常按域名命名（实测 4 个：ixdzs8.com.json 的 uid 是
   「爱下书_ixdzs8__ixdzs8.com」等）。对这些源，界面显示"已停用"但**真正在用的
   那个文件根本没变**——用户看到的状态与实际生效的不一致。
   本文件把"按内容定位文件"这条口径钉死。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import source_mgr as sm  # noqa: E402


def _write(fn, uid, enabled=True, name="测试源"):
    p = os.path.join(sm.SOURCES_DIR, fn)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"uid": uid, "bookSourceName": name,
                   "bookSourceUrl": "https://%s.example.com" % uid,
                   "enabled": enabled}, f, ensure_ascii=False)
    sm.invalidate_sources_cache()
    return p


def _read(fn):
    with open(os.path.join(sm.SOURCES_DIR, fn), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def clean_sources():
    """清空隔离目录里的书源，用例自己造（conftest 复制进来的真实源不参与断言）"""
    d = sm.SOURCES_DIR
    saved = {}
    for fn in os.listdir(d):
        if fn.endswith(".json"):
            p = os.path.join(d, fn)
            saved[fn] = open(p, encoding="utf-8").read()
            os.remove(p)
    sm.invalidate_sources_cache()
    yield d
    for fn in list(os.listdir(d)):
        if fn.endswith(".json"):
            os.remove(os.path.join(d, fn))
    for fn, content in saved.items():
        with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
            f.write(content)
    sm.invalidate_sources_cache()


@pytest.fixture
def client():
    import app
    return app.app.test_client()


def _fake_verify(monkeypatch, pairs):
    """pairs: [(uid, status)]"""
    import engine.source_verify as sv
    monkeypatch.setattr(sv, "results_payload", lambda: {
        "items": [{"uid": u, "status": s} for u, s in pairs],
        "counts": {}, "total": len(pairs), "tested_at": "", "keyword": "",
        "summary": {},
    })


# ── 文件名 ≠ uid 的定位（bug 回归）──

def test_set_enabled_uses_file_that_holds_uid(clean_sources):
    """uid 与文件名不一致时，必须改**真正含该 uid 的文件**，且不得另建文件。"""
    _write("ixdzs8.com.json", "爱下书_ixdzs8__ixdzs8.com", enabled=True)
    before = set(os.listdir(sm.SOURCES_DIR))
    assert sm.set_enabled("爱下书_ixdzs8__ixdzs8.com", False) is True
    assert _read("ixdzs8.com.json")["enabled"] is False       # 原件被改
    assert set(os.listdir(sm.SOURCES_DIR)) == before           # 没有多出 <uid>.json


def test_delete_source_removes_file_that_holds_uid(clean_sources):
    _write("m.jhsssd.com.json", "精华书阁_m.jhsssd.com_")
    assert sm.delete_source("精华书阁_m.jhsssd.com_") is True
    assert not os.path.exists(os.path.join(sm.SOURCES_DIR, "m.jhsssd.com.json"))


def test_duplicate_uid_updates_all_files(clean_sources):
    """同一 uid 的两个文件都要改——否则列表里一个显示已停用、另一个还在被搜索用"""
    _write("a.json", "dup_uid", enabled=True)
    _write("b.json", "dup_uid", enabled=True)
    assert sm.set_enabled("dup_uid", False) is True
    assert _read("a.json")["enabled"] is False
    assert _read("b.json")["enabled"] is False


def test_unknown_uid_is_false_not_silent_success(clean_sources):
    assert sm.set_enabled("并不存在的源", True) is False
    assert sm.delete_source("并不存在的源") is False


# ── 批量端点 ──

def test_bulk_requires_enabled_field(client):
    r = client.post("/api/sources/bulk-enabled", json={"filter": "all"})
    assert r.status_code == 400


def test_bulk_unknown_filter_400(client, clean_sources):
    _write("a.json", "a")
    r = client.post("/api/sources/bulk-enabled", json={"enabled": True, "filter": "zzz"})
    assert r.status_code == 400


def test_bulk_requires_filter_or_uids(client, clean_sources):
    _write("a.json", "a")
    r = client.post("/api/sources/bulk-enabled", json={"enabled": True})
    assert r.status_code == 400


def test_bulk_verified_enables_only_verified(client, clean_sources, monkeypatch):
    _write("v.json", "src_v", enabled=False)
    _write("f.json", "src_f", enabled=False)
    _write("n.json", "src_n", enabled=False)
    _fake_verify(monkeypatch, [("src_v", "verified"), ("src_f", "failed")])
    r = client.post("/api/sources/bulk-enabled",
                    json={"enabled": True, "filter": "verified"})
    d = r.get_json()
    assert r.status_code == 200 and d["ok"]
    assert d["matched"] == 1 and d["changed"] == 1
    assert _read("v.json")["enabled"] is True
    assert _read("f.json")["enabled"] is False     # 失败的没被带上
    assert _read("n.json")["enabled"] is False     # 没测过的也没被带上（没测过≠通过）
    assert d["enabled_now"] == 1


def test_bulk_failed_disables_only_failed(client, clean_sources, monkeypatch):
    _write("v.json", "src_v", enabled=True)
    _write("f.json", "src_f", enabled=True)
    _fake_verify(monkeypatch, [("src_v", "verified"), ("src_f", "failed")])
    d = client.post("/api/sources/bulk-enabled",
                    json={"enabled": False, "filter": "failed"}).get_json()
    assert d["matched"] == 1 and d["changed"] == 1
    assert _read("f.json")["enabled"] is False
    assert _read("v.json")["enabled"] is True


def test_bulk_unverified_excludes_tested(client, clean_sources, monkeypatch):
    _write("v.json", "src_v", enabled=False)
    _write("n.json", "src_n", enabled=False)
    _fake_verify(monkeypatch, [("src_v", "verified")])
    d = client.post("/api/sources/bulk-enabled",
                    json={"enabled": True, "filter": "unverified"}).get_json()
    assert d["matched"] == 1 and d["changed"] == 1
    assert _read("n.json")["enabled"] is True
    assert _read("v.json")["enabled"] is False


def test_bulk_all_and_enabled_filters(client, clean_sources):
    _write("a.json", "a", enabled=True)
    _write("b.json", "b", enabled=False)
    d = client.post("/api/sources/bulk-enabled",
                    json={"enabled": True, "filter": "all"}).get_json()
    assert d["matched"] == 2 and d["changed"] == 1     # a 已是启用 → 不算改动
    assert d["enabled_now"] == 2
    d2 = client.post("/api/sources/bulk-enabled",
                     json={"enabled": False, "filter": "enabled"}).get_json()
    assert d2["matched"] == 2 and d2["changed"] == 2


def test_bulk_no_change_reports_honestly(client, clean_sources):
    _write("a.json", "a", enabled=True)
    d = client.post("/api/sources/bulk-enabled",
                    json={"enabled": True, "filter": "all"}).get_json()
    assert d["changed"] == 0
    assert "已经处于目标状态" in d["note"]


def test_bulk_no_match_explains_why(client, clean_sources, monkeypatch):
    """一条验证记录都没有时：如实说"没有匹配"，并提示先跑验证，而不是静默成功"""
    _write("a.json", "a", enabled=False)
    _fake_verify(monkeypatch, [])
    d = client.post("/api/sources/bulk-enabled",
                    json={"enabled": True, "filter": "verified"}).get_json()
    assert d["matched"] == 0 and d["changed"] == 0
    assert "没有匹配" in d["note"] and "功能验证" in d["note"]
    assert _read("a.json")["enabled"] is False


def test_bulk_explicit_uids_and_missing(client, clean_sources):
    _write("a.json", "a", enabled=False)
    _write("b.json", "b", enabled=False)
    d = client.post("/api/sources/bulk-enabled",
                    json={"enabled": True, "uids": ["a", "并不存在"]}).get_json()
    assert d["matched"] == 1 and d["changed"] == 1
    assert d["missing"] == ["并不存在"]
    assert _read("a.json")["enabled"] is True
    assert _read("b.json")["enabled"] is False


def test_bulk_uid_with_mismatched_filename(client, clean_sources):
    """端点也要走"按内容定位"：不能只对 uid==文件名 的源生效"""
    _write("ixdzs8.com.json", "爱下书_ixdzs8__ixdzs8.com", enabled=True)
    d = client.post("/api/sources/bulk-enabled",
                    json={"enabled": False, "uids": ["爱下书_ixdzs8__ixdzs8.com"]}).get_json()
    assert d["changed"] == 1
    assert _read("ixdzs8.com.json")["enabled"] is False


def test_toggle_endpoint_also_mismatched_filename(client, clean_sources):
    """单源开关走的是同一个定位逻辑（否则界面显示停用、实际没停）"""
    _write("m.jhsssd.com.json", "精华书阁_m.jhsssd.com_", enabled=True)
    r = client.post("/api/sources/%s/toggle" % "精华书阁_m.jhsssd.com_",
                    json={"enabled": False})
    assert r.status_code == 200
    assert _read("m.jhsssd.com.json")["enabled"] is False


def test_bulk_reports_uid_and_file_counts_separately(client, clean_sources, monkeypatch):
    """有重复 uid 时"唯一源数"与"文件数"必须分别给出。

    实测踩到：设备上 34 个文件 = 30 个唯一 uid，接口只回 uid 口径而界面按文件
    口径显示，数字差了 4，看起来像"少改了几个"。
    """
    _write("a.json", "dup", enabled=False)
    _write("b.json", "dup", enabled=False)          # 同一 uid 的重复文件
    _write("c.json", "solo", enabled=False)
    d = client.post("/api/sources/bulk-enabled",
                    json={"enabled": True, "filter": "all"}).get_json()
    assert d["total"] == 2 and d["total_entries"] == 3      # 2 个唯一源 / 3 个文件
    assert d["matched"] == 2
    assert d["changed"] == 3                                 # 3 个文件都被写
    assert d["enabled_now"] == 2                             # 唯一源口径
    assert d["enabled_entries"] == 3                         # 文件口径


def test_bulk_noop_filter_is_zero_change(client, clean_sources):
    """filter=enabled + enabled=true 是恒等操作：必须 matched>0 但 changed=0。"""
    _write("a.json", "a", enabled=True)
    _write("b.json", "b", enabled=False)
    d = client.post("/api/sources/bulk-enabled",
                    json={"enabled": True, "filter": "enabled"}).get_json()
    assert d["matched"] == 1 and d["changed"] == 0
    assert d["enabled_now"] == 1 and d["enabled_entries"] == 1
    assert "已经处于目标状态" in d["note"]


def test_bulk_enabled_filter_is_file_level_not_uid_level(client, clean_sources):
    """filter=enabled 必须只动**启用中的那些文件**。

    实测踩到：按 uid 级操作时，恒等调用（filter=enabled + enabled=true）会顺手把
    同 uid 的重复文件也启用（changed=4），等于悄悄把某些源的抓取次数翻倍。
    """
    _write("a.json", "dup", enabled=True)
    _write("b.json", "dup", enabled=False)          # 同一 uid 的重复文件（停用）
    d = client.post("/api/sources/bulk-enabled",
                    json={"enabled": True, "filter": "enabled"}).get_json()
    assert d["changed"] == 0                        # 恒等调用零改动
    assert d["matched"] == 1 and d["matched_entries"] == 1
    assert _read("b.json")["enabled"] is False      # 重复文件没被动
    assert d["enabled_entries"] == 1


def test_bulk_verified_is_uid_level_all_files(client, clean_sources, monkeypatch):
    """验证结论是按源的：启用"验证通过"的源时，它名下的文件都要启用（含重复文件）"""
    _write("a.json", "dup", enabled=False)
    _write("b.json", "dup", enabled=False)
    _fake_verify(monkeypatch, [("dup", "verified")])
    d = client.post("/api/sources/bulk-enabled",
                    json={"enabled": True, "filter": "verified"}).get_json()
    assert d["matched"] == 1 and d["matched_entries"] == 2
    assert d["changed"] == 2
    assert _read("a.json")["enabled"] is True and _read("b.json")["enabled"] is True
