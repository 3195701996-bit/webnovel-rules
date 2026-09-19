# -*- coding: utf-8 -*-
"""重复源文件的检测与**安全清理**契约（离线，不触网）

背景：内置源目录里实测有 4 组"双份文件"（同一 uid + 同一 bookSourceUrl，仅文件名不同）。
同一 uid 两个文件会让"启用/删除"只作用于其中一个；此前 App 只能提示
"建议在网页端或文件系统里清理"——那是要求用户去用电脑，与方向基线 §8.A 冲突。

本用例锁住：
  1. 检测口径：按 (uid, bookSourceUrl) 分组；除 enabled/校验结果外内容一致 → safe；
     规则内容不同 → needs_review（**不**提议清理）；
  2. 建议保留哪一份：优先"已启用的那份"，都不启用时保留名字更短的那份（确定性）；
  3. 清理动作：只移动不删除（移到 sources_removed/<时间戳>/），并返回备份路径；
  4. 拒绝规则：不在安全组 remove 列表里的文件名一律拒绝（不允许删任意文件）。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def sandbox(monkeypatch, tmp_path):
    """把书源目录换到临时目录（不动真实 sources/）"""
    import engine.source_mgr as sm
    src = tmp_path / "sources"
    src.mkdir()
    monkeypatch.setattr(sm, "SOURCES_DIR", str(src))
    monkeypatch.setattr(sm, "_SOURCES_CACHE", None, raising=False)
    monkeypatch.setattr(sm, "_SOURCES_FINGERPRINT", None, raising=False)
    return src


def _write(dirpath, name, uid, url="/s?q={{key}}", enabled=True, extra=None):
    d = {"uid": uid, "bookSourceName": uid, "bookSourceUrl": url,
         "searchUrl": "/search?q={{key}}", "enabled": enabled}
    if extra:
        d.update(extra)
    (dirpath / name).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def test_detects_safe_duplicates_and_picks_enabled_one(sandbox):
    from engine.source_mgr import find_duplicate_sources
    _write(sandbox, "a.com.json", "a.com", enabled=True)
    _write(sandbox, "爱看书_a.com.json", "a.com", enabled=False)
    _write(sandbox, "solo.com.json", "solo.com")
    r = find_duplicate_sources()
    assert r["safe_groups"] == 1 and r["review_groups"] == 0
    assert r["removable"] == 1
    g = r["groups"][0]
    assert g["safe"] is True
    assert g["keep"] == "a.com.json", "应保留已启用的那一份"
    assert g["remove"] == ["爱看书_a.com.json"]


def test_picks_shorter_name_when_both_disabled(sandbox):
    from engine.source_mgr import find_duplicate_sources
    _write(sandbox, "b.com.json", "b.com", enabled=False)
    _write(sandbox, "b.com_副本.json", "b.com", enabled=False)
    g = find_duplicate_sources()["groups"][0]
    assert g["keep"] == "b.com.json" and g["remove"] == ["b.com_副本.json"]


def test_different_rules_are_never_proposed(sandbox):
    from engine.source_mgr import find_duplicate_sources
    _write(sandbox, "c.com.json", "c.com")
    _write(sandbox, "c.com_改过.json", "c.com", extra={"searchUrl": "/other?q={{key}}"})
    r = find_duplicate_sources()
    assert r["safe_groups"] == 0 and r["review_groups"] == 1
    assert r["removable"] == 0, "规则不同的文件不得提议清理"
    assert r["groups"][0]["keep"] == ""
    assert "人工确认" in r["groups"][0]["reason"]


def test_enabled_difference_is_not_a_rule_difference(sandbox):
    """只在启用状态上不同（用户点过开关）也算重复：这正是实测那 4 组的情形"""
    from engine.source_mgr import find_duplicate_sources
    _write(sandbox, "d.com.json", "d.com", enabled=True)
    _write(sandbox, "d.com_x.json", "d.com", enabled=False)
    r = find_duplicate_sources()
    assert r["safe_groups"] == 1 and r["removable"] == 1


def test_cleanup_moves_to_backup_and_refuses_others(sandbox, monkeypatch, tmp_path):
    import app as _app
    import server.novel_api as api
    import engine.source_mgr as sm
    monkeypatch.setattr(api, "SOURCES_DIR", str(sandbox), raising=False)
    _write(sandbox, "e.com.json", "e.com", enabled=True)
    _write(sandbox, "e.com_copy.json", "e.com", enabled=False)
    _write(sandbox, "keep.com.json", "keep.com")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr("engine.config.DATA_DIR", str(data_dir), raising=False)

    from engine import config as cfg
    real = cfg.DATA_DIR
    monkeypatch.setattr(cfg, "DATA_DIR", str(data_dir), raising=False)
    try:
        client = _app.app.test_client()
        # 1) 清理安全组里那一个
        r = client.post("/api/sources/duplicates/cleanup",
                        json={"files": ["e.com_copy.json"]})
        assert r.status_code == 200, r.get_data(as_text=True)
        body = r.get_json()
        assert body["moved"] == ["e.com_copy.json"]
        assert not (sandbox / "e.com_copy.json").exists(), "文件应被移走"
        backup = body["backup_dir"]
        assert os.path.isdir(backup)
        assert os.path.isfile(os.path.join(backup, "e.com_copy.json")), "备份里必须有它"
        assert (sandbox / "e.com.json").is_file(), "保留的那份不能动"
        # 2) 拒绝任意文件（不在安全组 remove 列表里）
        r2 = client.post("/api/sources/duplicates/cleanup",
                         json={"files": ["keep.com.json"]})
        assert r2.status_code == 409, "不在可清理列表里的文件必须被拒绝"
        assert (sandbox / "keep.com.json").is_file()
    finally:
        monkeypatch.setattr(cfg, "DATA_DIR", real, raising=False)


# ── 删除墓碑：用户删掉的源不许被随包内置源在下次启动时又加回来 ──

def test_delete_records_tombstone_and_import_clears_it(sandbox):
    """实测缺陷：在 App 里清掉 4 个重复文件后，下次启动又被解出 4 个（"删了又回来"）"""
    import engine.source_mgr as sm
    _write(sandbox, "f.com.json", "f.com")
    assert sm.removed_names() == set()
    assert sm.delete_source("f.com") is True
    assert not (sandbox / "f.com.json").exists()
    assert "f.com.json" in sm.removed_names(), "删除必须留墓碑（种子解压要跳过它）"
    # 用户重新导入同名源 → 解碑
    sm.import_sources([{"bookSourceName": "F", "bookSourceUrl": "https://f.com",
                        "uid": "f.com", "searchUrl": "/s?q={{key}}"}], validate=False)
    assert "f.com.json" not in sm.removed_names(), "重新导入后必须解碑"


def test_cleanup_records_tombstone(sandbox, monkeypatch, tmp_path):
    import app as _app
    import server.novel_api as api
    import engine.source_mgr as sm
    monkeypatch.setattr(api, "SOURCES_DIR", str(sandbox), raising=False)
    _write(sandbox, "g.com.json", "g.com", enabled=True)
    _write(sandbox, "g.com_copy.json", "g.com", enabled=False)
    client = _app.app.test_client()
    r = client.post("/api/sources/duplicates/cleanup", json={"files": ["g.com_copy.json"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert "g.com_copy.json" in sm.removed_names()
    client.post("/api/sources/duplicates/cleanup", json={"files": ["g.com_copy.json"]})
    assert "g.com_copy.json" in sm.removed_names(), "重复清理也要留墓碑"


def test_tombstone_is_not_shown_as_a_source(sandbox):
    """墓碑文件（sources/.seed-removed.json）不能被当成书源显示出来

    整包设备运行时被它绊到过：用例把点文件当成"现有源"，复制出一个没有 uid/url 的
    "重复源"，界面自然不出现重复卡片。服务端这一侧同样要保证它不出现在源列表里。
    """
    import engine.source_mgr as sm
    _write(sandbox, "h.com.json", "h.com")
    sm.note_removed(["h.com_copy.json"])
    assert (sandbox / ".seed-removed.json").is_file()
    uids = [d.get("uid") for d in sm.load_all()]
    assert "h.com" in uids
    assert not any(str(u).startswith(".") for u in uids), uids
    names = [n for n, _ in sm.load_all_with_files()]
    assert ".seed-removed.json" not in names, names
    # 按 uid 找文件、按 uid 删除也不该碰到墓碑
    assert sm._source_files_with_uid(".seed-removed") == []


# ── 恢复：清理只移动不删除，但用户点不到备份空间 → 必须能在 App 里恢复 ──

def test_removed_files_can_be_restored(sandbox, monkeypatch, tmp_path):
    import app as _app
    import server.novel_api as api
    import engine.source_mgr as sm
    from engine import config as cfg
    monkeypatch.setattr(api, "SOURCES_DIR", str(sandbox), raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", str(data_dir), raising=False)

    _write(sandbox, "r.com.json", "r.com", enabled=True)
    _write(sandbox, "r.com_copy.json", "r.com", enabled=False)
    client = _app.app.test_client()
    assert client.post("/api/sources/duplicates/cleanup",
                       json={"files": ["r.com_copy.json"]}).status_code == 200
    assert not (sandbox / "r.com_copy.json").exists()
    assert "r.com_copy.json" in sm.removed_names()

    # 1) 能列出可恢复的批次
    listed = client.get("/api/sources/removed").get_json()
    assert listed["total"] == 1
    assert listed["batches"][0]["files"][0]["name"] == "r.com_copy.json"

    # 2) 恢复：文件回来 + 墓碑解除
    r = client.post("/api/sources/removed/restore", json={"latest": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["restored"] == ["r.com_copy.json"], body
    assert (sandbox / "r.com_copy.json").is_file(), "文件必须真的回来"
    assert "r.com_copy.json" not in sm.removed_names(), "恢复后必须解除墓碑"
    assert body["remaining"] == 0

    # 3) 已有同名文件时不得覆盖（如实跳过）
    _write(sandbox, "s.com.json", "s.com")
    sm.note_removed(["s.com.json"])
    b = sm._removed_root()
    os.makedirs(b + "/batch-x", exist_ok=True)
    (sandbox / "s.com.json").write_text('{"uid":"USER-EDITED"}', encoding="utf-8")
    with open(os.path.join(b, "batch-x", "s.com.json"), "w", encoding="utf-8") as f:
        f.write('{"uid":"OLD"}')
    r2 = sm.restore_removed(batch="batch-x")
    assert r2["restored"] == []
    assert r2["skipped"] and "已有同名文件" in r2["skipped"][0]["why"]
    assert "USER-EDITED" in (sandbox / "s.com.json").read_text(encoding="utf-8"), \
        "绝不能覆盖用户当前的文件"
