#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新书源批量快速初筛：搜索可达 → 目录章数(≥50为长篇候选)。
用法: python tools/quick_source_screen.py [--max 74] [--kw 斗破苍穹]
输出: data/logs/screen_result.json + 控制台汇总
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.source_mgr import load_enabled
from engine.crawler import SourceCrawler

RESULT = os.path.join("data", "logs", "screen_result.json")


def screen(src, kw, toc_timeout=28):
    uid = src.get("uid", "")
    name = src.get("bookSourceName", "") or uid
    out = {"uid": uid, "name": name, "url": src.get("bookSourceUrl", "")}
    try:
        c = SourceCrawler(src)
        t0 = time.time()
        books = c.search(kw)
        out["search_n"] = len(books or [])
        out["search_t"] = round(time.time() - t0, 1)
        if not books:
            out["verdict"] = "搜索无结果"
            return out
        b = books[0]
        out["sample"] = b.get("name", "")
        t0 = time.time()
        # toc 用进程内限时:直接调用并在线程超时
        import concurrent.futures as cf
        def _toc():
            try:
                info = c.get_book(b["book_url"], fast=True)
                info.chapters = []
                c.get_toc(info)
                return len(info.chapters)
            except Exception as e:
                raise RuntimeError(f"{type(e).__name__}: {str(e)[:60]}")
        ex = cf.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_toc)
        try:
            n = fut.result(timeout=toc_timeout)
            out["chapters"] = n
        except cf.TimeoutError:
            out["chapters"] = -1
            out["toc_timeout"] = True
        finally:
            # 不等待卡死线程:墙面时间真受 toc_timeout 约束
            ex.shutdown(wait=False, cancel_futures=True)
        out["toc_t"] = round(time.time() - t0, 1)
        if out.get("chapters", 0) and out["chapters"] > 0:
            out["verdict"] = "通过" if out["chapters"] >= 50 else f"章数少({out['chapters']})"
        else:
            out["verdict"] = out.get("toc_timeout") and "目录超时" or "目录失败"
        return out
    except Exception as e:
        out["verdict"] = f"异常:{type(e).__name__}:{str(e)[:50]}"
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kw", default="斗破苍穹")
    ap.add_argument("--max", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--start", type=int, default=0)
    args = ap.parse_args()
    srcs = load_enabled()
    srcs = srcs[args.start: args.start + args.max]
    print(f"初筛 {len(srcs)} 源, 关键词={args.kw}", flush=True)

    done_old = {}
    if os.path.exists(RESULT):
        try:
            done_old = {r["uid"]: r for r in json.load(open(RESULT, encoding="utf-8"))}
        except Exception:
            pass

    results = list(done_old.values())
    todo = [s for s in srcs if s.get("uid") not in done_old]
    import concurrent.futures as cf

    def _one(s):
        return screen(s, args.kw)

    i = 0
    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(_one, s): s for s in todo}
        for f in cf.as_completed(futs):
            i += 1
            try:
                r = f.result()
            except Exception as e:
                r = {"uid": futs[f].get("uid", "?"), "name": "?", "verdict": f"EXC:{e}"}
            results = [x for x in results if x.get("uid") != r["uid"]]
            results.append(r)
            print(f"[{i}/{len(todo)}] {r.get('verdict','?')[:24]:24s} "
                  f"{(r.get('name') or '?')[:18]:18s} 搜索{r.get('search_n',0)} "
                  f"章{r.get('chapters','-')}", flush=True)
            json.dump(results, open(RESULT, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            time.sleep(0.4)
    json.dump(results, open(RESULT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    ok = [r for r in results if r.get("verdict") == "通过"]
    print(f"\n汇总: 通过 {len(ok)} 源（章数≥50）")
    for r in sorted(results, key=lambda x: -(x.get("chapters") or 0)):
        print(f"  {str(r.get('verdict'))[:22]:22s} {str(r.get('chapters','')):>6} "
              f"{(r.get('name') or '?')[:20]:20s} {r.get('uid','')[:36]}")


if __name__ == "__main__":
    main()
