# -*- coding: utf-8 -*-
"""小说源移动可用性台账（把路线 P0-3 的纪律应用到小说侧）。

为什么需要它：P0-3 给**漫画源**建了"注册/依赖/实测/分类"的单一事实来源，但小说侧一直没有
对应的东西——用户看到的是"书源管理"里 34 个源 + 一句"部分源可用"，**没人知道到底哪几个
可用、卡在哪一步**。P1-3 把"可用的源"冻结成了回归集，但剩下的源是什么状态仍然没有出口。

本模块把三样事实合成一份小说源台账（唯一事实来源）：
  · 用户侧状态：`engine.source_mgr.load_all()`（启用/停用、名称、地址）
  · 实测结论：`engine.source_verify` 的落盘记录（搜索/目录/正文三阶段 + 失败阶段 + 时间）
  · 能力判定：规则是否需要 JS / 缺依赖（由验证流程写入 unsupported 的原因）

分类口径（**未验证 ≠ 可用；实测失败也算没通过**）：

  已验证     三阶段（搜索/目录/正文）都拿到真实内容
  部分可用   搜索通过，目录或正文失败——界面必须写出**卡在哪一步**
  不可用     搜索阶段就失败
  不支持     规则需要 JS 或缺少依赖（当前引擎做不到）
  待验证     没有任何实测记录，或记录已过期
  已停用     用户/出厂停用，本轮不纳入

"记录过期"单独一个布尔标记（`stale`）而不是一个分类：过期不等于不能用，
但界面要能说清"这条结论是 N 天前测的"。
"""
import time

from engine.config import SOURCES_DIR            # noqa: F401  (文档/测试用)
from engine.source_mgr import load_all
from engine import source_verify

# 实测结论多久算过期（天）。过期不改分类，只加提示——避免把"没重测"说成"不能用"。
STALE_DAYS = 7

CATEGORY_LABELS = {
    "android_verified": "已验证",
    "android_partial": "部分可用",
    "android_failed": "不可用",
    "android_unsupported": "不支持",
    "untested": "待验证",
    "disabled": "已停用",
}

# 分类与实测状态的映射（实测状态来自 engine.source_verify.classify）
_STATUS_TO_CATEGORY = {
    "verified": "android_verified",
    "partial": "android_partial",
    "failed": "android_failed",
    "unsupported": "android_unsupported",
}

# 阶段顺序：报告"最早失败的那一步"用它
_STAGE_ORDER = ("search", "book", "toc", "content")
_STAGE_LABELS = {"search": "搜索", "book": "详情", "toc": "目录", "content": "正文"}


def _parse_ts(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return time.mktime(time.strptime((s or "")[:19], fmt))
        except Exception:
            continue
    return 0.0


def earliest_failed_stage(stages):
    """最早失败的阶段（给界面看"卡在哪一步"）；全部通过则返回空串"""
    for name in _STAGE_ORDER:
        st = (stages or {}).get(name)
        if st is None:
            continue
        if not st.get("ok"):
            return name
    return ""


def stage_label(name):
    return _STAGE_LABELS.get(name, name)


def classify(source, record, now_ts=None):
    """把"用户侧状态 + 实测记录"归类成一条台账条目"""
    now_ts = now_ts or time.time()
    uid = source.get("uid") or ""
    enabled = bool(source.get("enabled", True))
    item = {
        "uid": uid,
        "name": source.get("bookSourceName") or uid,
        "url": source.get("bookSourceUrl") or "",
        "enabled": enabled,
        "group": source.get("bookSourceGroup") or "",
        "category": "untested",
        "category_label": CATEGORY_LABELS["untested"],
        "reason": "",
        "failed_stage": "",
        "failed_stage_label": "",
        "tested_at": "",
        "keyword": "",
        # 原始实测状态（即使源已停用也保留）：界面可以说"已停用（上次实测：已验证）"
        "record_status": "",
        "stale": False,
        "stale_days": 0,
        "stages": {},
        "counts": {},
    }
    if not enabled:
        # 停用不是"不能用"——用户自己关的，界面要说清是停用而不是失败；
        # 但"上次实测过什么"照样带出来（信息不藏，用户才好决定要不要再打开）
        item.update(category="disabled", category_label=CATEGORY_LABELS["disabled"],
                    reason="该书源已停用（未纳入实测）")
        if record:
            item["tested_at"] = record.get("tested_at") or ""
            item["record_status"] = record.get("status") or ""
            item["keyword"] = record.get("keyword") or ""
            prev = _STATUS_TO_CATEGORY.get(item["record_status"])
            if prev:
                item["reason"] += f"；上次实测：{CATEGORY_LABELS[prev]}"
            fs = earliest_failed_stage(record.get("stages") or {})
            item["failed_stage"] = fs
            item["failed_stage_label"] = stage_label(fs) if fs else ""
        return item

    if not record:
        item["reason"] = "尚无实测记录：搜索/目录/正文能不能跑通还不确定"
        return item

    status = (record.get("status") or "").strip()
    if status == "skipped":
        # "本轮跳过"不是结论：可能因为最近已验证过（那次结论已单独保存）或源被停用。
        # 没有可沿用的结论时，只能如实列「待验证」并带上跳过原因。
        item["tested_at"] = record.get("tested_at") or ""
        item["keyword"] = record.get("keyword") or ""
        item["record_status"] = "skipped"
        item["reason"] = "本轮跳过：" + (record.get("reason") or "未说明原因")
        return item
    cat = _STATUS_TO_CATEGORY.get(status)
    if cat is None:
        item["reason"] = f"未知实测状态「{status}」"
        return item

    stages = record.get("stages") or {}
    item["record_status"] = status
    item.update(
        category=cat, category_label=CATEGORY_LABELS[cat],
        reason=record.get("reason") or "",
        tested_at=record.get("tested_at") or "",
        keyword=record.get("keyword") or "",
        stages={k: dict(v or {}) for k, v in stages.items() if isinstance(v, dict)},
    )
    fs = earliest_failed_stage(stages)
    item["failed_stage"] = fs
    item["failed_stage_label"] = stage_label(fs) if fs else ""
    if not item["reason"] and fs:
        item["reason"] = f"{stage_label(fs)}阶段失败"

    ts = _parse_ts(item["tested_at"])
    if ts:
        days = int((now_ts - ts) // 86400)
        item["stale_days"] = days
        item["stale"] = days >= STALE_DAYS
        if item["stale"]:
            item["reason"] = (item["reason"] + "；" if item["reason"] else "") + \
                f"实测结论已过期（{days} 天前），建议重测"

    # 证据数字（搜索条数 / 目录章数 / 正文字数）：界面与报告都直接引用，不二次加工
    for name in ("search", "toc", "content"):
        st = stages.get(name) or {}
        if name == "search":
            item["counts"]["search"] = st.get("count")
        elif name == "toc":
            item["counts"]["toc"] = st.get("count")
        else:
            item["counts"]["chars"] = st.get("chars")
    return item


def build(now_ts=None):
    """合成整份台账：每个源一条，带分类、原因、失败阶段与证据数字"""
    now_ts = now_ts or time.time()
    sources = load_all()
    try:
        items = (source_verify.results_payload() or {}).get("items") or []
    except Exception:
        items = []
    # 实测记录按 uid 取最新一条（多文件同 uid 时以最后一条为准）
    by_uid = {}
    for r in items:
        u = (r or {}).get("uid") or ""
        if not u:
            continue
        prev = by_uid.get(u)
        if prev is None or _parse_ts(r.get("tested_at")) >= _parse_ts(prev.get("tested_at")):
            by_uid[u] = r

    # 同 uid 可能有多个文件（仓库里实测有 4 组）：台账按 **uid** 汇总成一条，
    # 但把"有重复文件"写出来——否则用户看到两行一样的源，只会以为界面出错了。
    grouped = {}
    for s in sources:
        grouped.setdefault(s.get("uid") or "", []).append(s)
    rows = []
    for uid, group in grouped.items():
        # 同一 uid 的多个文件里，只要有一个启用就算"启用"（用户可以只启用其中一份）；
        # 但状态不一致本身要让用户知道——否则他看到的启用状态与实测结果会打架
        enabled_n = sum(1 for s in group if s.get("enabled", True))
        head = next((s for s in group if s.get("enabled", True)), group[0])
        row = classify(head, by_uid.get(uid), now_ts=now_ts)
        row["files"] = len(group)
        if len(group) > 1:
            dup = f"有 {len(group)} 个文件同 uid（配置重复），建议用书源页的「重复源清理」处理"
            if 0 < enabled_n < len(group):
                dup += f"；其中 {enabled_n}/{len(group)} 个处于启用状态（状态不一致）"
            row["reason"] = (row["reason"] + "；" if row["reason"] else "") + dup
        rows.append(row)
    # 排序：可用性低的排前面（用户最需要看到问题），同档按名称
    _rank = {"android_failed": 0, "android_partial": 1, "android_unsupported": 2,
             "untested": 3, "android_verified": 4, "disabled": 5}
    rows.sort(key=lambda r: (_rank.get(r["category"], 9), r["name"]))

    counts = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    enabled = [r for r in rows if r["enabled"]]
    verified_enabled = [r for r in enabled if r["category"] == "android_verified"]
    # "曾经测通"与"当前可用"是两件事：源被停用后仍应看得到它的历史结论
    record_verified = [r for r in rows if r["record_status"] == "verified"]
    disabled_verified = [r for r in record_verified if not r["enabled"]]
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sources": rows,
        "summary": {
            "total": len(rows),
            "enabled": len(enabled),
            "disabled": counts.get("disabled", 0),
            "verified": counts.get("android_verified", 0),
            "verified_enabled": len(verified_enabled),
            "record_verified": len(record_verified),
            "disabled_verified": len(disabled_verified),
            "partial": counts.get("android_partial", 0),
            "failed": counts.get("android_failed", 0),
            "unsupported": counts.get("android_unsupported", 0),
            "untested": counts.get("untested", 0),
            "stale": sum(1 for r in rows if r["stale"]),
            "duplicate_uids": sum(1 for r in rows if r.get("files", 1) > 1),
            "enabled_pass_rate": (round(len(verified_enabled) * 100 / len(enabled))
                                  if enabled else 0),
        },
        "note": "未验证 ≠ 可用：没有实测记录的一律算「待验证」；"
                "实测失败也归入不可用/部分可用，并写明卡在哪一步。",
    }


def render_markdown(payload=None):
    """导出可归档的 Markdown（与 /api/manga/catalog?format=md 同风格）"""
    data = payload or build()
    s = data["summary"]
    lines = ["# 小说源移动可用性台账", "",
             f"生成时间：{data['generated_at']}", "",
             data["note"], "",
             f"汇总：共 {s['total']} 个源（启用 {s['enabled']}）／当前可用（已验证）{s['verified']}"
             f"（启用中 {s['verified_enabled']}，启用口径通过率 {s['enabled_pass_rate']}%；"
             f"另有 {s['disabled_verified']} 个已停用的源曾测通）"
             f"／部分可用 {s['partial']}／不可用 {s['failed']}／不支持 {s['unsupported']}"
             f"／待验证 {s['untested']}／已停用 {s['disabled']}；结论过期 {s['stale']}；"
             f"同 uid 重复文件 {s['duplicate_uids']} 组",
             "",
             "| 源 | uid | 状态 | 可用性 | 卡在哪一步 | 实测时间 | 关键词 | 证据 | 说明 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in data["sources"]:
        c = r["counts"]
        ev = []
        if c.get("search") is not None:
            ev.append(f"搜索 {c['search']} 条")
        if c.get("toc") is not None:
            ev.append(f"目录 {c['toc']} 章")
        if c.get("chars") is not None:
            ev.append(f"正文 {c['chars']} 字")
        lines.append("| {} | `{}` | {} | {} | {} | {} | {} | {} | {} |".format(
            r["name"], r["uid"], "启用" if r["enabled"] else "停用",
            r["category_label"], r["failed_stage_label"] or "—",
            r["tested_at"] or "—", r["keyword"] or "—",
            "、".join(ev) or "—",
            (r["reason"] or "").replace("|", "/") or "—"))
    lines.append("")
    lines.append("> 说明：本表由 `server/novel_catalog.py` 依据**实测落盘记录**生成；"
                 "没有记录的一律列「待验证」，不推测可用性。")
    return "\n".join(lines)
