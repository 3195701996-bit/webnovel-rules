# -*- coding: utf-8 -*-
"""R78 回归：一键恢复只该恢复**最近一批**（此前把所有历史批次都恢复了）。

真缺陷（用户可感知，方向与承诺相反）：
App 的确认弹窗写着「将把**最近一次**清理/删除的源文件恢复到书源目录」，
客户端也照发 `{"latest": true}` —— 而服务端**从不读这个字段**，`batch=None`
直接落到"遍历所有批次"分支：于是用户点"恢复最近一次"，实际发生的是
"恢复全部历史批次"，连他当初**故意删掉**的源也一并带回来，
并解除删除墓碑（内置源升级从此不再把它当"用户删过"）。

本文件钉死三条口径：
  1. `latest=True` 只动最近一批（且最近一批 = **仍有文件**的那些批次里的最新一个）；
  2. 不传 `latest` 时旧行为（全批次）**保持不变**——桌面/网页端还在用；
  3. 界面上"点一次会恢复几个"的数字 = 最近一批的数量（`latest_count`），
     否则按钮写 9、点完回来 3，用户会以为又坏了一次。
"""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import source_mgr as sm  # noqa: E402


def _batch(name, files):
    d = os.path.join(sm._removed_root(), name)
    os.makedirs(d, exist_ok=True)
    for fn in files:
        with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
            json.dump({"bookSourceName": fn, "bookSourceUrl": "https://x.test"},
                      f, ensure_ascii=False)
    return d


def _in_sources(fn):
    return os.path.exists(os.path.join(sm.SOURCES_DIR, fn))


@pytest.fixture
def two_batches():
    """旧批次 2 个文件 + 新批次 2 个文件（批次名可排序，新的更大）"""
    root = sm._removed_root()
    shutil.rmtree(root, ignore_errors=True)
    _batch("20260918-100000", ["old_a.json", "old_b.json"])
    _batch("20260918-120000", ["new_a.json", "new_b.json"])
    for fn in ("old_a.json", "old_b.json", "new_a.json", "new_b.json"):
        p = os.path.join(sm.SOURCES_DIR, fn)
        if os.path.exists(p):
            os.remove(p)
    sm.invalidate_sources_cache()
    yield
    shutil.rmtree(root, ignore_errors=True)
    for fn in ("old_a.json", "old_b.json", "new_a.json", "new_b.json"):
        p = os.path.join(sm.SOURCES_DIR, fn)
        if os.path.exists(p):
            os.remove(p)


def test_latest_restores_only_newest_batch(two_batches):
    r = sm.restore_removed(latest=True)
    assert r["restored"] == ["new_a.json", "new_b.json"], r
    assert r["batches"] == ["20260918-120000"], r
    assert _in_sources("new_a.json") and _in_sources("new_b.json")
    # **关键**：用户当初删掉的旧批次不能被这次点击带回来
    assert not _in_sources("old_a.json") and not _in_sources("old_b.json")
    assert os.path.exists(os.path.join(sm._removed_root(), "20260918-100000",
                                       "old_a.json"))


def test_latest_click_twice_drains_batches_newest_first(two_batches):
    sm.restore_removed(latest=True)
    assert sm.list_removed_batches()["latest_dir"] == "20260918-100000"
    r2 = sm.restore_removed(latest=True)
    assert r2["restored"] == ["old_a.json", "old_b.json"], r2
    assert sm.list_removed_batches()["total"] == 0


def test_latest_count_is_what_one_click_restores(two_batches):
    info = sm.list_removed_batches()
    assert info["total"] == 4
    assert info["latest_count"] == 2          # 按钮上的数字
    assert info["latest_dir"] == "20260918-120000"
    r = sm.restore_removed(latest=True)
    assert len(r["restored"]) == info["latest_count"]


def test_empty_batch_dir_is_not_chosen_as_latest(two_batches):
    """只有目录、没有 .json 的批次不算"最近一批"（否则会"恢复 0 个"卡住）"""
    os.makedirs(os.path.join(sm._removed_root(), "20260918-235959"), exist_ok=True)
    assert sm.list_removed_batches()["latest_dir"] == "20260918-120000"
    assert sm.restore_removed(latest=True)["restored"] == ["new_a.json", "new_b.json"]


def test_latest_with_names_filters_within_newest_batch(two_batches):
    r = sm.restore_removed(names=["new_a.json"], latest=True)
    assert r["restored"] == ["new_a.json"], r
    assert not _in_sources("new_b.json")      # 同一批里没点名的保持不动
    assert not _in_sources("old_a.json")      # 更早批次完全不碰


def test_explicit_batch_wins_over_latest(two_batches):
    r = sm.restore_removed(batch="20260918-100000", latest=True)
    assert r["restored"] == ["old_a.json", "old_b.json"], r
    assert not _in_sources("new_a.json")


def test_latest_with_nothing_removed_is_empty_not_error():
    shutil.rmtree(sm._removed_root(), ignore_errors=True)
    r = sm.restore_removed(latest=True)
    assert r["restored"] == [] and r["batches"] == [], r


def test_all_batches_behavior_unchanged_without_latest(two_batches):
    """**不回退**：不传 latest 时仍是"恢复所有批次"（桌面/网页端口径）"""
    r = sm.restore_removed()
    assert sorted(r["restored"]) == ["new_a.json", "new_b.json",
                                    "old_a.json", "old_b.json"], r


def test_existing_source_file_is_never_overwritten(two_batches):
    p = os.path.join(sm.SOURCES_DIR, "new_a.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"bookSourceName":"用户当前在用的那份"}')
    r = sm.restore_removed(latest=True)
    assert r["restored"] == ["new_b.json"], r
    assert any(s["name"] == "new_a.json" for s in r["skipped"]), r
    with open(p, encoding="utf-8") as f:
        assert "用户当前在用的那份" in f.read()


def test_api_reads_latest_and_reports_remaining(two_batches):
    """接口层：`latest` 必须真的被读（这正是本缺陷的根因）"""
    from server import novel_api as na
    assert na is not None
    import app as appmod
    c = appmod.app.test_client()
    info = c.get("/api/sources/removed").get_json()
    assert info["latest_count"] == 2 and info["total"] == 4, info
    r = c.post("/api/sources/removed/restore", json={"latest": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert sorted(d["restored"]) == ["new_a.json", "new_b.json"], d
    assert d["remaining"] == 2, d            # 还有旧批次 2 个
    assert d["latest_count"] == 2, d         # 下一批的数量（供按钮如实显示）
    # 旧批次仍在备份里，没被这次点击碰到
    assert not os.path.exists(os.path.join(sm.SOURCES_DIR, "old_a.json"))
