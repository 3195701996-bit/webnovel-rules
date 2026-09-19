# -*- coding: utf-8 -*-
"""移动漫画源**目录**（唯一事实来源）：注册 / 依赖 / 实测 / 移动可用性策略。

依据《0.51.0-移动端风险评估与纠偏路线》：
  §4.2 "代码文件存在不等于它会出现在 App 中，更不等于能使用。应建立一份**唯一的
       移动源目录**，以注册状态、依赖状态、合法验证状态和默认启用状态为同一事实来源。"
  §4.4 五分类（Android 已验证 / 降级 / 不支持 / 待验证 / 已弃用未注册），并要求
       "删除源来让通过率变好不是修复"——未注册或不可用的源必须**保留原因**，
       从用户可见列表移出但留在开发台账里。

本模块只做**汇总与分类**，不自己探测、不自己联网：
  · 注册状态 ← engine.manga.manager 的注册表（按**类**读，不实例化适配器）；
  · 依赖状态 ← server.capabilities.source_status（依赖级判定，不代表可用）；
  · 实测结果 ← engine.manga.verify 的落盘记录（真实跑过搜索→详情→取图）；
  · 未注册但仓库里存在的适配器 ← RETIRED_LEDGER（原因写在台账里，不删代码）。

分类规则（"未知"绝不当成"可用"）：
  依赖缺失            → android_unsupported（本机缺运行依赖，展示原因，不伪装可用）
  依赖降级            → android_degraded    （能跑但缺增强能力，标注风险）
  依赖 OK + 实测通过   → android_verified    （四步都拿到真实数据）
  依赖 OK + 实测部分通过 → android_degraded  （写明失败阶段）
  依赖 OK + 实测失败/无记录 → pending        （未验证 ≠ 可用；失败带上阶段与时间）
  未注册/已弃用        → retired             （移出用户可见列表，保留原因）
"""
from __future__ import annotations

import time

# 已弃用 / 未注册适配器台账：**保留原因**，不删代码、不在用户列表里伪装出现。
# key → 原因（"为什么不在移动交付清单里"）
RETIRED_LEDGER = {
    "komiic": "适配器已编写但未纳入注册表：2026-09-18 复测 komiic.com 与 api.komiic.com "
              "均连接被重置（ConnectionResetError 54），本机网络不可达（保留台账）",
    "mxs": "仓库内有适配器，但未纳入注册表（不在移动交付清单内，保留台账）",
    "ykmh": "仓库内有适配器，但未纳入注册表（不在移动交付清单内，保留台账）",
    "zaimanhua": "仓库内有适配器，但未纳入注册表（不在移动交付清单内，保留台账）",
}

CATEGORY_LABELS = {
    "android_verified": "Android 已验证",
    "android_degraded": "Android 降级",
    "android_unsupported": "Android 不支持",
    "pending": "待验证",
    "retired": "已弃用/未注册",
}

# 仓库里存在适配器文件的 key（用于把"未注册"与"不存在"分开；由调用方注入或自检）
_ADAPTER_MODULES = {
    "baozi": "engine.manga.baozi",
    "copymanga": "engine.manga.copymanga",
    "copymanga_web": "engine.manga.copymanga_web",
    "jm": "engine.manga.jm",
    "komiic": "engine.manga.komiic",
    "mangadex": "engine.manga.mangadex",
    "mxs": "engine.manga.mxs",
    "nhentai": "engine.manga.nhentai",
    "ykmh": "engine.manga.ykmh",
    "zaimanhua": "engine.manga.zaimanhua",
}


def adapter_keys_present():
    """仓库里**有适配器文件**的 key（不导入模块，只看文件在不在）"""
    import importlib.util
    out = {}
    for key, mod in _ADAPTER_MODULES.items():
        try:
            out[key] = importlib.util.find_spec(mod) is not None
        except Exception:
            out[key] = False
    return out


def _first_failed_stage(stages):
    """实测记录里第一个失败阶段（供"失败阶段"一栏）"""
    for name in ("search", "detail", "images"):
        info = (stages or {}).get(name) or {}
        if isinstance(info, dict) and info.get("ok") is False:
            return name, str(info.get("detail") or "")[:160]
    return "", ""


def _verify_of(verify_rec, key):
    rec = (verify_rec or {}).get(key) or {}
    stage, detail = _first_failed_stage(rec.get("stages"))
    return {
        "status": rec.get("status") or "未实测",
        "reason": str(rec.get("reason") or "")[:200],
        "tested_at": rec.get("tested_at") or "",
        "failed_stage": stage,
        "failed_detail": detail,
    }


def classify(dep_status, verify_status):
    """(依赖状态, 实测状态) → 移动可用性分类"""
    if dep_status == "unsupported":
        return "android_unsupported"
    if dep_status == "degraded":
        return "android_degraded"
    if verify_status == "verified":
        return "android_verified"
    if verify_status == "partial":
        return "android_degraded"
    # 失败的实测 / 没有记录：都**不**算可用（未验证 ≠ 可用）
    return "pending"


_CATEGORY_DEFAULT = {
    "android_verified": "依赖满足且四步实测通过",
    "android_degraded": "主流程可运行，但缺少增强能力或部分阶段未通过",
    "android_unsupported": "本机缺少该源需要的运行依赖",
    "pending": "尚未完成功能验证（未验证 ≠ 可用）",
}


def _reason_text(verify_reason, st):
    """源的说明文字：实测结论 > 已知条件+依赖原因 > 分类默认。

    拼装复用 capabilities.combined_reason（与 App 走的 API 路径同一实现）。

    为什么"已知条件"要排在"依赖原因"前面：已知条件是我们**实测**出来的
    "能不能用取决于什么"（源站迁域/挑战门、本机不可达、需代理），
    而依赖原因常常只是一句通用的传输降级（缺 curl_cffi）——
    前者才是用户排查时真正需要的信息，后者作为补充并列显示、不丢。
    """
    vr = (verify_reason or "").strip()
    if vr:
        return vr
    from server import capabilities as _cap
    return _cap.combined_reason(st)


def build(caps=None, verify_payload=None, registry=None):
    """生成移动源目录（矩阵）。返回 {sources: [...], summary: {...}}

    参数都可注入（测试用）；不传则从真实运行时读取。
    """
    from server import capabilities as _cap
    # 注册表是懒加载的：先确保加载过，否则会把"还没加载"误报成"没有已注册源"
    try:
        from server.state import _load_manga_adapters
        _load_manga_adapters()
    except Exception:
        pass
    try:
        from engine.manga.manager import list_adapters, adapter_meta
        ads = list_adapters()
    except Exception:
        ads = []
        adapter_meta = lambda k: {}          # noqa: E731
    if caps is None:
        try:
            caps = _cap.probe()
        except Exception:
            caps = {}
    if verify_payload is None:
        try:
            from engine.manga import verify as _mv
            verify_payload = _mv.results_payload() or {}
        except Exception:
            verify_payload = {}
    verify_rec = {it.get("key"): it for it in (verify_payload.get("items") or [])}

    present = adapter_keys_present()
    rows = []
    registered_keys = [a.get("key") for a in ads if a.get("key")]
    for a in ads:
        key = a.get("key") or ""
        try:
            st = _cap.source_status(key, caps=caps) or {}
        except Exception as e:
            st = {"status": "pending", "reason": f"能力台账异常：{e}", "transport": {}}
        v = _verify_of(verify_rec, key)
        cat = classify(st.get("status"), v["status"])
        meta = {}
        try:
            meta = adapter_meta(key) or {}
        except Exception:
            meta = {}
        rows.append({
            "key": key,
            "name": a.get("name") or key,
            "registered": True,
            "visible_in_app": True,          # 已注册 = 出现在 App 源清单里
            "category": cat,
            "category_label": CATEGORY_LABELS[cat],
            # 优先级：实测结论（真的跑过）> 依赖原因（缺什么）> 已知条件（实测得到的
            # "能不能用取决于什么"）> 分类默认说法。已知条件必须能露出来，
            # 否则用户看到的是笼统的"未验证"，而我们已经量清了原因。
            # 说明的拼装（0.74.10）：**具体条件在前、依赖降级在后**，两者都不丢。
            # 旧实现只显示依赖原因，于是手机上 baozi 显示的是笼统的
            # "图片通道缺少 curl_cffi…"，而我们实测到的真正原因是
            # "源站已迁域并启用 JS 挑战门" —— 用户看不出该去做什么。
            "category_reason": (_reason_text(v["reason"], st)
                                or _CATEGORY_DEFAULT.get(cat, "")),
            "dependency": {"status": st.get("status") or "pending",
                           "reason": st.get("reason") or "",
                           "conditional": st.get("conditional") or "",
                           "transport": st.get("transport") or {}},
            "verification": v,
            "capabilities": {"scrambled": bool(meta.get("scrambled")),
                             "supports_order": bool(meta.get("supports_order")),
                             "process_version": int(meta.get("process_version") or 0)},
            # 漫画源没有"启用开关"：进注册表即为默认可选（与小说源的文件级启停不同）
            "default_enabled": True,
        })
    # 未注册但仓库里有适配器：留在台账里，**不**出现在用户可见列表
    for key, reason in RETIRED_LEDGER.items():
        if key in registered_keys:
            continue
        rows.append({
            "key": key,
            "name": key,
            "registered": False,
            "visible_in_app": False,
            "file_present": bool(present.get(key)),
            "category": "retired",
            "category_label": CATEGORY_LABELS["retired"],
            "category_reason": reason,
            "dependency": {"status": "n/a", "reason": reason, "transport": {}},
            "verification": {"status": "未实测", "reason": "", "tested_at": "",
                             "failed_stage": "", "failed_detail": ""},
            "capabilities": {"scrambled": False, "supports_order": False,
                             "process_version": 0},
            "default_enabled": False,
        })
    summary = {c: sum(1 for r in rows if r["category"] == c) for c in CATEGORY_LABELS}
    summary["registered"] = sum(1 for r in rows if r["registered"])
    summary["visible_in_app"] = sum(1 for r in rows if r["visible_in_app"])
    summary["never_tested"] = sum(1 for r in rows
                                  if r["registered"] and r["verification"]["status"] == "未实测")
    return {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sources": rows, "summary": summary}


def render_markdown(catalog):
    """把矩阵渲染成可提交的 Markdown（报告归档用）"""
    s = catalog.get("summary") or {}
    lines = ["# 移动漫画源支持矩阵", "",
             f"生成时间：{catalog.get('generated_at', '')}", "",
             "分类口径见 server/manga_catalog.py 顶部注释（未验证 ≠ 可用；"
             "依赖满足 ≠ 源站可用）。", ""]
    lines.append("| 源 | key | 注册 | 移动可用性 | 依赖判定 | 实测 | 失败阶段 | 实测时间 | 说明 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in catalog.get("sources", []):
        v = r.get("verification") or {}
        lines.append("| {name} | `{key}` | {reg} | {cat} | {dep} | {ver} | {stage} | {at} | {why} |".format(
            name=r.get("name", ""), key=r.get("key", ""),
            reg="是" if r.get("registered") else "否（台账）",
            cat=r.get("category_label", ""),
            dep=(r.get("dependency") or {}).get("status", ""),
            ver=v.get("status", ""),
            stage=v.get("failed_stage") or "—",
            at=v.get("tested_at") or "—",
            why=(r.get("category_reason") or "").replace("|", "/")[:80],
        ))
    lines += ["", "## 汇总", "",
              f"- 注册（出现在 App 源清单）：{s.get('registered', 0)}",
              f"- Android 已验证：{s.get('android_verified', 0)}",
              f"- Android 降级：{s.get('android_degraded', 0)}",
              f"- Android 不支持：{s.get('android_unsupported', 0)}",
              f"- 待验证：{s.get('pending', 0)}",
              f"- 已弃用/未注册（仅台账）：{s.get('retired', 0)}",
              f"- 从未实测：{s.get('never_tested', 0)}", ""]
    return "\n".join(lines) + "\n"
