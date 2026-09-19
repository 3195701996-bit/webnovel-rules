# -*- coding: utf-8 -*-
"""R78 回归：同一 uid 的重复书源文件，在"会发请求"的路径上只算一份。

实测缺陷（2026-09-18，手机口径）：
仓库里实测 **34 个源文件 / 30 个唯一 uid**（4 组重复，内容逐字相同）。
`load_enabled()` 把这些**文件**全部返回，而搜索/检验/逐源验证都是
"对列表里每个源发一次请求"：
  · 实测流式搜索 `total=18` 而唯一 uid 只有 **15** → 同一站点被搜了两遍；
  · 用户界面显示"已返回 N/18 个源"，与他实际拥有的 15 个源对不上；
  · 逐源验证同样把重复文件各验一遍（`共 N 个源` 虚高）。

修法：`load_enabled()` 按 uid 去重（组内保留首个 enabled 条目）；
`source_verify.start` 同样去重。`load_all()` **保持不变**——书源页要能照实
提示"有 N 个文件同 uid（配置重复）"，那是另一件事。

判据：
  1. 源目录里放 2 份同 uid 文件 → `load_enabled()` 只返回 1 条；
  2. `load_all()` 仍返回 2 条（重复要可见，不能偷偷藏掉）；
  3. 去重后每个 uid 唯一（不同 uid 不会被误并）；
  4. 逐源验证的源队列也按 uid 去重。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


def _write(d, name, uid, enabled=True, url="https://dup.test"):
    o = {"uid": uid, "bookSourceName": uid, "bookSourceUrl": url,
         "enabled": enabled}
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        json.dump(o, f, ensure_ascii=False)


@pytest.fixture()
def dup_dir(tmp_path, monkeypatch):
    d = tmp_path / "sources"
    d.mkdir()
    _write(str(d), "a.json", "同站_uidA")
    _write(str(d), "a_dup.json", "同站_uidA")      # 同 uid 的第二份文件
    _write(str(d), "b.json", "站点_uidB")
    _write(str(d), "c.json", "停用_uidC", enabled=False)
    from engine import config
    monkeypatch.setattr(config, "SOURCES_DIR", str(d), raising=False)
    import engine.source_mgr as sm
    monkeypatch.setattr(sm, "SOURCES_DIR", str(d), raising=False)
    return d


def test_load_enabled_dedupes_by_uid(dup_dir):
    from engine.source_mgr import load_all, load_enabled
    alls = load_all()
    en = load_enabled()
    assert len(alls) == 4, f"load_all 应照实返回 4 个文件，实际 {len(alls)}"
    assert len(en) == 2, f"启用源应按 uid 去重为 2 条，实际 {len(en)}"
    uids = [s.get("uid") for s in en]
    assert len(uids) == len(set(uids)), f"去重后仍重复: {uids}"
    assert "同站_uidA" in uids and "站点_uidB" in uids
    assert "停用_uidC" not in uids, "停用源不该混进来"


def test_duplicate_files_stay_visible_in_load_all(dup_dir):
    """重复必须仍能被书源页看到（否则用户没法清理它）"""
    from engine.source_mgr import load_all
    from collections import Counter
    c = Counter(s.get("uid") for s in load_all())
    assert c["同站_uidA"] == 2, "重复文件被藏起来了，书源页就无法提示"


def test_verification_queue_dedupes(monkeypatch, dup_dir):
    """逐源验证的队列也按 uid 去重（否则同一站点验两遍、总数虚高）"""
    from engine import source_verify as sv
    # 不真跑网络：把 single-source 验证换成计数桩
    seen = []

    def fake_verify_one(src, keyword=None, budget=None, now=None):
        seen.append(src.get("uid"))
        return {"uid": src.get("uid"), "name": src.get("name", ""),
                "status": "verified", "reason": "", "tested_at": "now",
                "stages": {}, "keyword": keyword or ""}

    monkeypatch.setattr(sv, "verify_one", fake_verify_one)
    monkeypatch.setattr(sv, "_save", lambda *a, **k: None)
    r = sv.start(keyword="测试", skip_verified_days=0, limit=10)
    assert r.get("started"), r
    import time
    for _ in range(100):
        if sv.status().get("status") != "running":
            break
        time.sleep(0.05)
    assert seen.count("同站_uidA") <= 1, f"同 uid 被验了多次: {seen}"
    assert len(seen) == len(set(seen)), seen
