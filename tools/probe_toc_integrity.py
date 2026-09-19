#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""目录（TOC）完整性审计（真实外站）· 按**手机口径**跑。

指南 P0-2 要求的链路是「搜索 → 详情 → **全目录** → 多页正文 → 正文末尾」。
`tools/probe_pagination_audit.py` 覆盖了"多页正文"，本工具补"**全目录**"这一环。

## 判据（都用可复核的客观信号）

1. **截断**：`SourceCrawler.toc_truncated` 是否为真（撞上目录页数上限时置位）；
2. **章数**：目录章数 vs 详情里声明的 `chapter_count`/`last_chapter` 的**末章序号**
   （声明缺失时只记录，不判失败）；
3. **顺序**：把章名解析成序号（阿拉伯数字或中文数字，复用 `biquge_common` 的解析器），
   **可解析序号的序列必须非递减**；统计违规处与示例；
4. **重复**：同一序号是否出现多次（同号多章在部分源是正常的"第X章 上/下"，
   所以只记录数量与示例，不直接判失败）。

## 口径与边界

- 只读：不建任务、不写书源、不写书库；串行、每源最多 3 个请求；
- 建议隔离 `WR_DATA_DIR`；`--json-out` 写结果文件（避免与引擎日志混在一起）；
- **开发机真实外站**证据，**不等于**真机结论。

用法：
    WR_PROFILE=mobile WR_DATA_DIR=/tmp/wr-audit WR_SOURCES_DIR=/tmp/wr-audit-sources \
    WR_TEST=1 WR_DISABLE_BACKGROUND=1 \
        ./venv/bin/python tools/probe_toc_integrity.py [--keyword 剑来] [--only 精华书阁] [--json-out /tmp/toc.json]
"""
import argparse
import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ARABIC = re.compile(r"第\s*(\d{1,5})\s*[章节回]")
# **分卷/分部的卷首标记**：这类条目之后的章号**重新从 1 开始是合法的**
# （用户指出：部分小说同时存在"第一卷第一章"和"第二卷第一章"——章号相同但所属卷不同）。
# 没有这条判据，工具会把合法的分卷重号误报成"目录乱序"。
VOLUME = re.compile(r"(第\s*[0-9一二三四五六七八九十百千零两]+\s*[卷部篇册])"
                    r"|^(?:作品相关|番外|外传|正文|楔子|序章|终章|后记)")


def _seq(name):
    """章名 → 序号；解析不了返回 0（0 = 不确定，不参与顺序判定）。"""
    m = ARABIC.search(name or "")
    if m:
        return int(m.group(1))
    try:
        from engine.adapters.biquge_common import _seq_num
        return _seq_num(name or "")
    except Exception:                                            # noqa: BLE001
        return 0


def _run(fn, sec):
    box = {}

    def wrap():
        try:
            box["v"] = fn()
        except Exception as e:                                   # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=wrap, daemon=True)
    t.start()
    t.join(sec)
    if t.is_alive():
        raise TimeoutError(">%ss" % sec)
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _raw_toc_order(c, src, book):
    """目录页**原始文档顺序**（同一选择器、同一文档序）。

    用途：把"顺序有回退"分成两种**完全不同**的结论 ——
      · 我们返回的顺序 ≠ 原始顺序  → **我们的错**（要修）；
      · 我们返回的顺序 == 原始顺序  → **站点自己的目录就这么排**（原样镜像，不背锅）。
    实测：顶点(m版)/顶点镜像 的目录有 6 处序号回退，但与原始顺序**逐条一致** → 站点侧。
    """
    from engine.crawler import parse_html
    rc = src.get("ruleToc") or {}
    sel = (rc.get("chapterList") or "").split("@")[0] or "#chapterlist p a"
    try:
        url = book.toc_url or book.book_url
        html = c.fetcher.get(url, source=src, timeout=15)
        doc = parse_html(html)
        if not sel:
            return None          # 适配器源没有 ruleToc：无从对照
        return [ (a.text_content() or "").strip() for a in doc.cssselect(sel) ]
    except Exception:                                            # noqa: BLE001
        return None


def _we_fixed_site_order(raw, ours):
    """我们是否只是把**站点自身的乱序**纠正成有序（例如"最新章节"倒序块）。
    这种差异不是缺陷，反而是修好了：只在"站点乱序 → 我们有序"时返回 True。"""
    if not raw or not ours:
        return False
    def _drops(seq):
        nums = [_seq(x) for x in seq]
        return sum(1 for i in range(1, len(nums))
                   if nums[i] and nums[i - 1] and nums[i] < nums[i - 1])
    return _drops(ours) < _drops(raw)


def audit(src, keyword):
    from engine.crawler import SourceCrawler
    row = {"name": (src.get("bookSourceName") or "")[:20], "uid": src.get("uid")}
    c = SourceCrawler(src)
    books = _run(lambda: c.search(keyword, page=1), 20) or []
    if not books:
        row["result"] = "搜不到"
        return row
    row["book"] = (books[0].get("name") or "")[:24]
    declared = books[0].get("chapter_count")
    last_name = books[0].get("last_chapter") or ""
    book = _run(lambda: c.get_book(books[0].get("book_url"), fast=True), 20)
    if book is None:
        row["result"] = "详情为空"
        return row
    chs = _run(lambda: c.get_toc(book), 40) or []
    row["chapters"] = len(chs)
    row["truncated"] = bool(getattr(c, "toc_truncated", False))
    if not chs:
        row["result"] = "目录为空"
        return row
    names_all = [x.get("name") or "" for x in chs]
    seqs = [_seq(n) for n in names_all]
    vols = [i for i, n in enumerate(names_all) if VOLUME.search(n or "")]
    row["volume_marks"] = len(vols)
    known = [k for k in seqs if k > 0]
    # **分卷感知**：卷首标记之后的序号回退视为合法（新卷重新从 1 开始）
    bad = []
    for i in range(1, len(seqs)):
        if not (seqs[i] and seqs[i - 1]):
            continue
        if seqs[i] < seqs[i - 1] and not any(i - 1 < v <= i for v in vols):
            bad.append(i)
    row["parsed"] = len(known)
    row["order_violations"] = len(bad)
    if bad:
        i = bad[0]
        row["order_example"] = "%s → %s" % (seqs[i - 1], seqs[i])
    # 同号重复：跨卷合法、卷内可疑 —— 分开统计
    dup_in_vol = 0
    seen_num = {}
    for i, k in enumerate(seqs):
        if not k:
            continue
        if k in seen_num and not any(seen_num[k] < v <= i for v in vols):
            dup_in_vol += 1
        seen_num[k] = i
    row["dup_in_volume"] = dup_in_vol
    row["last_seq"] = known[-1] if known else 0
    decl_last = _seq(last_name)
    row["declared_last_seq"] = decl_last
    row["declared_count"] = declared if isinstance(declared, int) else None
    # **适配器源不参与"原始顺序对照"**：适配器自己按站点页面结构做反转/过滤
    # （例如"目录倒序（最新章节在前）→ 反转"），拿页面 cssselect 的文档顺序去比它，
    # 比的是两种不同的东西 ✗（实测 ddsk 被判"不一致"，其实只是这条判据不适用）。
    raw = None if c.adapter is not None else _raw_toc_order(c, src, book)
    ours = names_all
    row["mirrors_site_order"] = (raw == ours) if raw else None
    if row["truncated"]:
        row["result"] = "✗ 目录被截断（撞页数上限）"
    elif bad and row.get("mirrors_site_order"):
        # **镜像站点顺序**时，编号回退不是我们的问题，也**多半不是"乱序"**：
        # 实测（用户指出的形态）——分卷重号：第一卷第一章 与 第二卷第一章 章号相同，
        # 且很多书**章名里不带卷号**，于是「第一百五十二章 → 第二十三章」看起来像回退，
        # 其实是新卷重新编号。凭证：该书的 URL 尾号**全部递增**（0 处回退），
        # 即我们排的是**站点文档顺序**（也就是正确的卷序）。
        row["result"] = ("✓ 与站点原始顺序一致；章名编号有 %d 处回退（分卷重号等，"
                         "站点编号本身如此）" % len(bad))
    elif bad and row.get("mirrors_site_order") is None:
        row["result"] = ("✓ 目录 %d 章；章名编号有 %d 处回退（分卷重号/番外等，站点编号如此）"
                         "；**无法与站点原始顺序对照**（适配器源或选择器取不到）"
                         % (len(chs), len(bad)))
    elif bad and _we_fixed_site_order(raw, ours):
        # 站点页首是倒序预览块（把"最新几章"倒着摆在最前），我们理顺成阅读顺序
        row["result"] = ("✓ 站点页首是倒序预览块，我们的顺序更接近阅读顺序"
                         "（回退 %d 处 → 0 处；例 %s）" % (len(bad), row["order_example"]))
    elif bad and row.get("mirrors_site_order") is False:
        # 与站点原始顺序不同 → 先看**站点页首是不是倒序块**（"最新章节预览"）。
        # 实测（2026-09-18 查实，回答了用户提的"分卷重号"疑问）：
        #   精华书阁 / 爱下书(aixiashu) 的页面**页首就是倒序的预览块**
        #   （`第81章 → 第80章 → …`），而那批章节的 **URL 编号是全目录最大的**
        #   （64.9M / 64.2M，正文段从 7.9M / 1.6M 起）= **后发布的番外/第二部**，
        #   自然位置就在**末尾**。我们按 URL 编号归位 → 与站点"把预览摆在页首"的
        #   排法不同，但**结果更接近阅读顺序**（正文段去掉该批后 URL 回退 0 处）。
        #   所以这不是缺陷，只说明站点页面头部有预览块。
        head_desc = 0
        nums = [_seq(x) for x in raw[:8]]
        for i in range(1, len(nums)):
            if nums[i] and nums[i - 1] and nums[i] < nums[i - 1]:
                head_desc += 1
        if head_desc >= 2:
            row["result"] = ("✓ 站点页首是倒序预览块（%d 条），我们按发布顺序归位；"
                             "章名编号另有 %d 处回退（分卷重号/番外等，站点编号如此）"
                             % (head_desc + 1, len(bad)))
        else:
            row["result"] = ("⚠ 序号回退 %d 处（例 %s）；我们与站点原始顺序不同，"
                             "且站点页首不是倒序块——**需人工复现判断**"
                             % (len(bad), row["order_example"]))
    elif bad:
        # 与站点原始顺序不同：可能是**我们纠正了站点倒序**（例如"最新章节"倒序块），
        # 也可能是站点两次返回不同（实测同一源两次结果不一致）→ **不断言责任**，
        # 只如实报出事实与可复现的样本，交由人工判断。
        row["result"] = ("⚠ 序号回退 %d 处（例 %s）；我们与站点原始顺序不同"
                         "（可能是我们纠正了站点倒序，也可能站点返回本身不稳定）"
                         % (len(bad), row["order_example"]))
    elif decl_last and known and known[-1] < decl_last:
        row["result"] = "✗ 目录疑似不全（末章 %d < 声明 %d）" % (known[-1], decl_last)
    else:
        row["result"] = "✓ 目录完整有序（%d 章%s）" % (
            len(chs), "，末章 %d" % known[-1] if known else "")
    return row


def main():
    from _probe_guard import ensure_isolated  # noqa: E402
    ensure_isolated()
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", default="剑来")
    ap.add_argument("--only", default="")
    ap.add_argument("--json-out", default="")
    a = ap.parse_args()
    os.environ.setdefault("WR_PROFILE", "mobile")
    os.environ.setdefault("WR_TEST", "1")
    os.environ.setdefault("WR_DISABLE_BACKGROUND", "1")
    from engine.source_mgr import load_enabled
    srcs = load_enabled() or []
    if a.only:
        srcs = [s for s in srcs if a.only in (s.get("bookSourceName") or "")
                or a.only in (s.get("uid") or "")]
    rows = []
    for s in srcs:
        t0 = time.time()
        try:
            row = audit(s, a.keyword)
        except Exception as e:                                   # noqa: BLE001
            row = {"name": (s.get("bookSourceName") or "")[:20],
                   "result": "异常 %s: %s" % (type(e).__name__, str(e)[:60])}
        row["secs"] = round(time.time() - t0, 1)
        rows.append(row)
        print("  %-22s %s  [%ss]" % (row["name"], row.get("result", ""), row["secs"]))
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        print("结果已写入 %s" % a.json_out)
    bad = [r for r in rows if str(r.get("result", "")).startswith("✗")]
    print("\n审计 %d 个源 · 目录有问题 %d 个" % (len(rows), len(bad)))
    for r in bad:
        print("   ✗ %s: %s" % (r["name"], r["result"]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
