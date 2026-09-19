# -*- coding: utf-8 -*-
"""小说净化质量逐源复核：抽样章节跑 `check_purify_quality`，抓残留垃圾。

为什么要单独跑：净化判据是**逐年累加**的（0.60.0 的换源 App 文案、0.74.13 的
JS 残句/报错提示/引流广告…）。改完判据必须回到**真实源站**验证"干净了"，
否则只是在离线样例上自证。本工具把"抽样→净化→质量自检→列出残留"固化成一条命令。

判据与产品一致（engine.cleaner.check_purify_quality）：过短 / 广告导航残留 /
段落空白异常。残留会**原样打印**，便于直接把新变体补进判据。

用法：
  WR_PROFILE=mobile ./venv/bin/python tools/probe_novel_purify_quality.py \
      --keyword 剑来 --pick 5 [--only isiluke]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED_POLICY = os.path.join(ROOT, "android", "mobile-seed-policy.json")


def enabled_sources():
    from engine.source_mgr import load_all
    try:
        pol = json.load(open(SEED_POLICY, encoding="utf-8"))
        disabled = set((pol.get("disable") or {}).keys())
    except Exception as e:                                       # noqa: BLE001
        print(f"⚠ 读不到种子策略（{e}）")
        disabled = set()
    srcs, seen = [], set()
    for s in (load_all() or []):
        uid = s.get("uid") or ""
        if uid in disabled or uid in seen:
            continue
        seen.add(uid)
        srcs.append(s)
    return srcs, len(disabled)


def check_source(src, keyword, pick, timeout):
    from engine.crawler import SourceCrawler
    from engine.cleaner import check_purify_quality
    row = {"uid": src.get("uid") or "", "name": src.get("bookSourceName") or "",
           "chapters": 0, "sampled": [], "bad": [], "stage": "", "reason": ""}
    try:
        c = SourceCrawler(src)
        hits = c.search(keyword) or []
        exact = [h for h in hits if (h.get("name") or "").strip() == keyword]
        hit = (exact or hits or [None])[0]
        if not hit:
            row.update(stage="搜索", reason="0 结果")
            return row
        book = c.get_book(hit.get("book_url") or hit.get("url"), fast=True)
        chs = c.get_toc(book) or []
        row["chapters"] = len(chs)
        if not chs:
            row.update(stage="目录", reason="目录为空")
            return row
        n = len(chs)
        step = max(1, n // max(1, pick))
        idxs = sorted({*range(0, n, step)}.intersection(range(n)))[:pick]
        for i in idxs:
            ch = chs[i]
            try:
                txt = c.get_content(ch["url"]) or ""
            except Exception as e:                               # noqa: BLE001
                row["sampled"].append({"idx": i + 1, "error": f"{type(e).__name__}: {str(e)[:50]}"})
                continue
            ok, probs = check_purify_quality(txt)
            item = {"idx": i + 1, "name": ch.get("name", ""), "chars": len(txt),
                    "ok": bool(ok), "problems": probs}
            row["sampled"].append(item)
            if not ok:
                # 残留原样带出来（便于把新变体补进判据），并给出可疑行
                lines = [ln.strip() for ln in txt.split("\n") if ln.strip()]
                marks = ("请记住", "最新网址", "app", "APP", "换源", "关注", "复制",
                         "阅读器", "本站", "章节错误", "阅读提示", "message", "render",
                         "已订阅", "答题", "网址", "域名", "http")
                sus = [ln for ln in lines if any(m in ln for m in marks)][:3]
                item["suspect_lines"] = [s[:110] for s in sus]
                item["tail"] = txt.replace("\n", " ").strip()[-90:]
                row["bad"].append(item)
    except Exception as e:                                       # noqa: BLE001
        row.update(stage=row["stage"] or "异常", reason=f"{type(e).__name__}: {str(e)[:60]}")
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", default="剑来")
    ap.add_argument("--pick", type=int, default=5, help="每源抽样章数")
    ap.add_argument("--only", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    srcs, ndis = enabled_sources()
    if args.only:
        srcs = [s for s in srcs if args.only in (s.get("uid") or "")]
    print("数据目录：%s" % (os.environ.get("WR_DATA_DIR") or "⚠ 未隔离"))
    print("手机口径启用 %d 个源（策略停用 %d 项）· 关键词 %s · 每源抽 %d 章"
          % (len(srcs), ndis, args.keyword, args.pick))
    print("时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("-" * 92)

    rows, clean_srcs, bad_srcs, fail_srcs = [], 0, 0, 0
    for s in srcs:
        r = check_source(s, args.keyword, args.pick, 25)
        rows.append(r)
        got = [x for x in r["sampled"] if "chars" in x]
        errs = [x for x in r["sampled"] if "error" in x]
        if r["bad"]:
            bad_srcs += 1
            tag = "⚠ 有残留"
        elif got:
            clean_srcs += 1
            tag = "✓ 干净"
        else:
            fail_srcs += 1
            tag = "✗ 未取到"
        print("%-36s %-8s 章 %-5s 抽样 %d 成功/%d 失败"
              % ((r["uid"] or "")[:34], tag, r["chapters"] or "—", len(got), len(errs)))
        if r["reason"]:
            print("      失败阶段=%s 说明=%s" % (r["stage"], r["reason"]))
        for b in r["bad"]:
            print("      ⚠ 第%d章 《%s》%d 字：%s"
                  % (b["idx"], b.get("name", "")[:18], b["chars"], "；".join(b["problems"])))
            for ln in b.get("suspect_lines", []):
                print("          · %s" % ln)
        for e in errs:
            print("      取数失败 第%d章: %s" % (e["idx"], e["error"]))
    print("-" * 92)
    print("汇总：干净 %d · 有残留 %d · 未取到 %d" % (clean_srcs, bad_srcs, fail_srcs))
    print("注：桌面结论，**不等于**目标手机可用；残留行已原样打印，便于补判据。")
    if args.json:
        print(json.dumps({"rows": rows}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
