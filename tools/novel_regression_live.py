#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小说最小回归源集 · **桌面只读复跑**（无设备时的等价替代，不是设备结论）。

## 为什么有这个工具

《升级版Venera-当前纠偏与开发指南》§4 P1 要求："每次修改**抓取、缓存、网络判断**、
移动快照或正文净化时，必须运行具名小说最小回归集。" 已冻结的那一组（见
`报告归档/03-验证结果/小说最小回归源集-2026-09-16.md`）原本只在设备用例
`NovelRegressionTest` 里跑——本机 Android 模拟器已不存在，于是这里提供**同一口径**的
桌面复跑：同三个源、同关键词、同三步契约（搜索 → 目录 → 首章正文）。

**它证明什么 / 不证明什么**：
- 证明：改动（本轮是"出站代理"，属网络判断）没有降低这三个源的实际取数能力；
- **不证明**：目标手机上的表现。设备结论必须由真机用例另行记录（本工具不得被
  当成设备验收）。

## 只读契约

- 只调 `load_all()` / `search()` / `get_toc()` / `get_content()`，**不建任务、不写书源、
  不写书库**（不调用任何 `_atomic_write` / `task*` / `save_*`）；
- 运行时建议 `WR_DATA_DIR` 指向临时目录（脚本自己也会提示），避免任何偶然缓存写进用户数据；
- 输出只到 stdout；**不修改 `sources/`**（`load_all()` 返回深拷贝）。

用法：
    WR_DATA_DIR=/tmp/wr-novel-regress ./venv/bin/python tools/novel_regression_live.py
    ... --proxy socks5://127.0.0.1:1080     # 顺带验证"经代理时小说源是否仍可用"
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 冻结集（与 03-验证结果/小说最小回归源集-2026-09-16.md 一致；改这里必须同步该文件）
FROZEN = (
    {"uid": "爱下书_ixdzs8__ixdzs8.com", "kw": "剑来",
     "min_chapters": 300, "min_chars": 300, "site": "ixdzs8.com"},
    {"uid": "精华书阁_m.jhsssd.com_", "kw": "剑来",
     "min_chapters": 300, "min_chars": 300, "site": "m.jhsssd.com"},
    {"uid": "啃书网_kenshuwx__www.kenshuzw.la", "kw": "剑来",
     "min_chapters": 300, "min_chars": 300, "site": "kenshuzw.la"},
)

# 断言口径说明：设备冻结时实测为 1278 / 605 / 1396 章、首章 642 / 22388 / 554 字。
# 这里取"≥300 章 + 首章 ≥300 字"作为**退化判定**（源站目录会变，锁死数字会误报）；
# 与设备用例 NovelRegressionTest 的断言口径一致。


def _snip(text, n=60):
    return (text or "").replace("\n", " ").strip()[:n]


def _pick(results, kw):
    """从搜索结果里挑"这次要读的那本书"。

    **不能blindly取第一条**：源站的模糊匹配会把同名前缀的书排到前面——
    2026-09-17 复跑实测，ixdzs8 对「剑来」的第一条变成了《青冥等剑来》（89 章），
    而《剑来》本身（1278 章）掉到第 6 条。若按"第一条"判定，会把它误报成
    "目录退化"，属于指南明令禁止的"把源站行为写成代码退化"。
    因此优先取**标题精确等于关键词**的结果，其次"标题包含关键词"，最后才回退第一条；
    并把选择过程打印出来，谁都能复核。
    """
    if not results:
        return None, "0 结果"
    exact = [r for r in results if (r.get("name") or "").strip() == kw]
    if exact:
        return exact[0], "标题精确匹配（第 %d 条）" % (results.index(exact[0]) + 1)
    contains = [r for r in results if kw in (r.get("name") or "")]
    if contains:
        return contains[0], "标题包含关键词（第 %d 条）" % (results.index(contains[0]) + 1)
    return results[0], "回退第一条（无标题匹配；可能源站改版）"


def run_one(src, spec, crawler_cls):
    out = {"uid": spec["uid"], "site": spec["site"], "kw": spec["kw"],
           "search": None, "toc": None, "content": None, "ms": {},
           "ok": False, "stage": "", "note": "", "pick": ""}
    t0 = time.time()
    try:
        c = crawler_cls(src)
        results = c.search(spec["kw"]) or []
        out["ms"]["search"] = int((time.time() - t0) * 1000)
        if not results:
            out.update(stage="搜索", note="0 结果（源站内容差异/风控，不算解析退化）")
            return out
        book, why = _pick(results, spec["kw"])
        out["pick"] = why
        book_url = book.get("book_url") or book.get("url") or ""
        out["search"] = {"hits": len(results), "title": book.get("name") or "",
                         "url": book_url}
        if not book_url:
            out.update(stage="搜索", note="选中的结果没有 book_url（返回结构变了？）")
            return out

        t1 = time.time()
        bk = c.get_book(book_url, fast=True)
        chapters = c.get_toc(bk) or []
        out["ms"]["toc"] = int((time.time() - t1) * 1000)
        out["toc"] = {"chapters": len(chapters),
                      "first": (chapters[0].get("name") if chapters else "")}
        if len(chapters) < spec["min_chapters"]:
            out.update(stage="目录",
                       note="章节数 %d < 阈值 %d" % (len(chapters), spec["min_chapters"]))
            return out

        t2 = time.time()
        text = c.get_content(chapters[0]["url"]) or ""
        out["ms"]["content"] = int((time.time() - t2) * 1000)
        out["content"] = {"chars": len(text), "head": _snip(text)}
        if len(text) < spec["min_chars"]:
            out.update(stage="正文",
                       note="首章 %d 字 < 阈值 %d" % (len(text), spec["min_chars"]))
            return out
        out["ok"] = True
        return out
    except Exception as e:                                       # noqa: BLE001
        out["stage"] = out["stage"] or "异常"
        out["note"] = "%s: %s" % (type(e).__name__, str(e)[:160])
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="", help="顺带验证经代理（会真的改本进程出站路径）")
    ap.add_argument("--json", action="store_true", help="额外打印一行 JSON 摘要")
    args = ap.parse_args()

    dd = os.environ.get("WR_DATA_DIR", "")
    if not dd:
        print("⚠ 未设置 WR_DATA_DIR：本工具不写数据，但建议隔离为临时目录再跑，例如：")
        print("   WR_DATA_DIR=/tmp/wr-novel-regress ./venv/bin/python tools/novel_regression_live.py")
    else:
        print("数据目录（隔离）：%s" % dd)
    print("书源目录：%s" % os.environ.get("WR_SOURCES_DIR", "<仓库内 sources/>"))

    if args.proxy:
        from engine import netproxy
        val = netproxy.set_proxy(args.proxy, persist=False)
        print("出站代理：%s（来源=命令行；不落盘）" % (val or "（值无效，保持直连）"))
    else:
        from engine import netproxy
        print("出站代理：%s" % (netproxy.redact_proxy(netproxy.current_proxy()) or "直连"))

    from engine.crawler import SourceCrawler
    from engine.source_mgr import load_all
    allsrc = {s.get("uid"): s for s in (load_all() or [])}

    print("")
    print("小说最小回归源集 · 桌面只读复跑（%d 个源；关键词 %s）"
          % (len(FROZEN), "、".join(sorted({f["kw"] for f in FROZEN}))))
    print("-" * 78)
    rows = []
    fails = []
    for spec in FROZEN:
        src = allsrc.get(spec["uid"])
        if src is None:
            row = {"uid": spec["uid"], "site": spec["site"], "ok": False,
                   "stage": "书源", "note": "sources/ 里找不到该 uid（被删/改名？）"}
        else:
            row = run_one(src, spec, SourceCrawler)
        rows.append(row)
        mark = "✓" if row.get("ok") else "✗"
        s = row.get("search") or {}
        t = row.get("toc") or {}
        ct = row.get("content") or {}
        ms = row.get("ms") or {}
        print("%s %-12s 搜索 %s（%s 命中 %sms）→ 目录 %s 章（%sms）→ 首章 %s 字（%sms）"
              % (mark, row.get("site", "?"),
                 "ok" if s else "—", s.get("hits", "—"), ms.get("search", "—"),
                 t.get("chapters", "—"), ms.get("toc", "—"),
                 ct.get("chars", "—"), ms.get("content", "—")))
        if s:
            print("     选中：%s  %s（%s）"
                  % (s.get("title", ""), s.get("url", "")[:90], row.get("pick", "")))
        if ct.get("head"):
            print("     正文开头：%s…" % ct["head"])
        if not row.get("ok"):
            fails.append(row)
            print("     ⚠ 失败阶段=%s 说明=%s" % (row.get("stage"), row.get("note")))
    print("-" * 78)
    print("汇总：%d 个源，通过 %d，失败 %d" % (len(rows), len(rows) - len(fails), len(fails)))
    if fails:
        print("**这不是被忽略的噪声**：下面这些必须逐条判断是源站问题还是解析退化——")
        for f in fails:
            print("  · %s（%s）：%s" % (f.get("site"), f.get("stage"), f.get("note")))
    if args.json:
        print(json.dumps({"rows": rows, "fails": len(fails)}, ensure_ascii=False))
    # 退出码：有失败即 1，便于脚本化（但失败原因仍需人工判定，别只看码）
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
