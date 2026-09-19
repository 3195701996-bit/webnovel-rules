# -*- coding: utf-8 -*-
"""移动漫画源目录/支持矩阵的契约（离线，不触网）

依据 0.51.0 路线 §4.2（唯一事实来源）与 §4.4（五分类；未注册/不可用要**保留原因**，
"删源让通过率变好"不是修复）。本用例锁住分类与台账口径：

  1. 依赖缺失 → android_unsupported（不伪装可用）；
  2. 依赖降级 → android_degraded；
  3. 依赖 OK + 实测通过 → android_verified；
  4. 依赖 OK + 实测部分通过 → android_degraded；
  5. 依赖 OK + 实测失败/无记录 → pending（**未验证 ≠ 可用**），失败要带阶段与时间；
  6. 未注册但仓库里有文件的适配器 → retired（进台账、不出现在用户列表）；
  7. 已注册源不得出现在 retired 里（注册与台账冲突时以注册为准）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server.manga_catalog as mc  # noqa: E402


def _adapters(*keys):
    return [{"key": k, "name": k.upper()} for k in keys]


class _CapBundle:
    """直接给 build() 注入 caps 对象（build 内部会调用 capabilities.source_status）"""
    def __init__(self, status_map):
        self._m = status_map

    def probe(self):
        return {}

    def source_status(self, key, caps=None):
        st = self._m.get(key, "supported")
        return {"status": st, "reason": f"理由-{st}" if st != "supported" else "",
                "transport": {"api": "ok", "image": "requests_fallback"}}


def _run(monkeypatch, status_map, verify_items, adapters):
    """用替身跑 build()：替换 list_adapters / adapter_meta / capabilities"""
    import server.capabilities as cap
    monkeypatch.setattr(cap, "probe", lambda force=False: {})
    monkeypatch.setattr(cap, "source_status",
                        _CapBundle(status_map).source_status)
    import engine.manga.manager as mgr
    monkeypatch.setattr(mgr, "list_adapters", lambda: adapters)
    monkeypatch.setattr(mgr, "adapter_meta",
                        lambda k: {"scrambled": k == "jm", "supports_order": k == "jm",
                                   "process_version": 2 if k == "jm" else 0})
    return mc.build(caps=cap, verify_payload={"items": verify_items})


def test_dependency_missing_is_unsupported(monkeypatch):
    cat = _run(monkeypatch, {"copymanga_web": "unsupported"}, [], _adapters("copymanga_web"))
    row = [r for r in cat["sources"] if r["key"] == "copymanga_web"][0]
    assert row["category"] == "android_unsupported"
    assert "理由-unsupported" in row["category_reason"]
    assert row["registered"] is True and row["visible_in_app"] is True


def test_dependency_degraded_is_degraded(monkeypatch):
    cat = _run(monkeypatch, {"jm": "degraded"}, [], _adapters("jm"))
    row = cat["sources"][0]
    assert row["category"] == "android_degraded"
    assert row["capabilities"]["scrambled"] is True, "混淆源能力也要带出来"


def test_registered_baozi_has_capability_contract(monkeypatch):
    cat = _run(monkeypatch, {}, [], _adapters("baozi"))
    row = cat["sources"][0]
    assert row["registered"] is True
    assert row["dependency"]["status"] == "supported"
    assert row["category"] == "pending", "未实测不能因依赖满足而显示已验证"


def test_verified_requires_both_dependency_and_measurement(monkeypatch):
    items = [{"key": "mangadex", "status": "verified", "tested_at": "2026-09-16 01:00",
              "stages": {"search": {"ok": True}, "detail": {"ok": True},
                         "images": {"ok": True}}}]
    cat = _run(monkeypatch, {}, items, _adapters("mangadex"))
    row = cat["sources"][0]
    assert row["category"] == "android_verified"
    assert row["verification"]["tested_at"] == "2026-09-16 01:00"


def test_partial_is_degraded_with_failed_stage(monkeypatch):
    items = [{"key": "baozi", "status": "partial", "reason": "取图失败",
              "tested_at": "2026-09-16 01:00",
              "stages": {"search": {"ok": True}, "detail": {"ok": True},
                         "images": {"ok": False, "detail": "HTTP 403"}}}]
    cat = _run(monkeypatch, {}, items, _adapters("baozi"))
    row = cat["sources"][0]
    assert row["category"] == "android_degraded"
    assert row["verification"]["failed_stage"] == "images"
    assert "HTTP 403" in row["verification"]["failed_detail"]


def test_failed_or_untested_is_pending_not_available(monkeypatch):
    items = [{"key": "nhentai", "status": "failed", "reason": "连接失败",
              "tested_at": "2026-09-16 01:00",
              "stages": {"search": {"ok": False, "detail": "Remote end closed"}}}]
    cat = _run(monkeypatch, {}, items, _adapters("nhentai", "jm"))
    nhen = [r for r in cat["sources"] if r["key"] == "nhentai"][0]
    jm = [r for r in cat["sources"] if r["key"] == "jm"][0]
    assert nhen["category"] == "pending" and nhen["verification"]["failed_stage"] == "search"
    assert jm["category"] == "pending", "没有实测记录 = 待验证（未验证 ≠ 可用）"
    assert cat["summary"]["never_tested"] == 1


def test_unregistered_adapters_go_to_ledger_not_user_list(monkeypatch):
    cat = _run(monkeypatch, {}, [], _adapters("mangadex"))
    retired = [r for r in cat["sources"] if r["category"] == "retired"]
    keys = {r["key"] for r in retired}
    assert {"komiic", "mxs", "ykmh", "zaimanhua"} <= keys
    for r in retired:
        assert r["visible_in_app"] is False and r["registered"] is False
        assert r["category_reason"], "未注册必须保留原因（不许只是删掉）"
        assert r["file_present"] is True, "台账要说明文件其实还在仓库里"


def test_registered_source_is_never_listed_as_retired(monkeypatch):
    cat = _run(monkeypatch, {}, [], _adapters("mangadex", "mxs"))
    mxs = [r for r in cat["sources"] if r["key"] == "mxs"][0]
    assert mxs["registered"] is True and mxs["category"] != "retired"


def test_markdown_render_lists_every_source(monkeypatch):
    cat = _run(monkeypatch, {}, [], _adapters("mangadex", "jm"))
    md = mc.render_markdown(cat)
    assert "移动漫画源支持矩阵" in md
    assert "mangadex" in md and "jm" in md and "komiic" in md
    assert "Android 已验证" in md and "已弃用/未注册" in md
