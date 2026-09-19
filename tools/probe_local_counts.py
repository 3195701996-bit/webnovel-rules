#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机计数核对（**按界面真正显示的字段**，跑在正在运行的服务上，只读 GET）。

## 为什么要"按界面显示的字段"比

2026-09-18 的两条教训（都是"自己的核对口径写窄/写错"，不是产品缺陷）：
1. 第一版拿 `/api/manga/library` 的 `images` 去和磁盘比，报"12/28 部数字不符" ✗ ——
   而 **App 书架显示的是 `local_images`（磁盘实扫数）**，`images` 是"下载记录的全量图片数"
   （**刻意**不回退，避免增量下载把 1097 覆盖成 49）。
   → **核对的字段必须与用户看到的字段是同一个**。
2. 第二版只数"话目录内的图"，每部都比服务端少 1 张 → 报"27 部不一致" ✗ ——
   封面 `cover.jpg` 在**漫画目录顶层**，服务端把它算进去了（932 = 931 + 1）。
   → **核对的范围也要与生产代码一致**。

## 现在核什么

- `/api/storage`：`total_bytes` = 分类之和？逐书 `bytes` = 磁盘？
- `/api/manga/library`：`local_images` = 磁盘图片数（按扩展名）？`chapters` = 磁盘话目录数？
  差异逐条打印（`chapters` 来自后台快照，可能滞后 —— 如实打印，不直接判失败）。
- `/api/books`：书架每本的 `total` vs 磁盘 `.cache` 数。

用法（只读、不联网外站）：
    ./venv/bin/python tools/probe_local_counts.py [--port 8766] [--json-out /tmp/counts.json]
"""
import argparse
import glob
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

IMG = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")


def _get(port, path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--json-out", default="")
    a = ap.parse_args()
    out = {"port": a.port}

    st = _get(a.port, "/api/storage")
    cats = st.get("categories") or []
    cat_sum = sum(int(c.get("bytes") or 0) for c in cats)
    out["storage_total"] = int(st.get("total_bytes") or 0)
    out["storage_cat_sum"] = cat_sum
    bad_book = []
    for row in (st.get("novel_books") or []):
        d = os.path.join("data", "books", str(row.get("key") or ""))
        if not os.path.isdir(d):
            bad_book.append([row.get("key"), "目录不存在", row.get("bytes")])
            continue
        real = sum(os.path.getsize(os.path.join(r, f))
                   for r, _dd, fs in os.walk(d) for f in fs)
        if int(row.get("bytes") or 0) != real:
            bad_book.append([row.get("key"), int(row.get("bytes") or 0), real])
    out["storage_book_mismatch"] = bad_book

    ml = _get(a.port, "/api/manga/library")
    mism = []
    for c in (ml.get("comics") or []):
        p = os.path.join("data", "manga", "downloads", str(c.get("source") or ""),
                         str(c.get("comic_id") or ""))
        if not os.path.isdir(p):
            continue
        dirs = [x for x in os.listdir(p) if os.path.isdir(os.path.join(p, x))]
        # **顶层也要数**：封面 `cover.jpg` 就在漫画目录顶层，服务端的 local_images
        # 把它算进去了（实测 932 = 话内 931 + 封面 1）。第一版只数话目录 →
        # 每部都差 1 张，报出"27 部不一致"的假警报 ✗（又是我核对范围写窄了）。
        imgs = sum(1 for cd in dirs for f in os.listdir(os.path.join(p, cd))
                   if f.lower().endswith(IMG))
        imgs += sum(1 for f in os.listdir(p)
                    if os.path.isfile(os.path.join(p, f)) and f.lower().endswith(IMG))
        li = int(c.get("local_images") or 0)
        ch = int(c.get("chapters") or 0)
        if li != imgs or ch != len(dirs):
            mism.append({"title": str(c.get("title"))[:20], "api_local_images": li,
                         "disk_images": imgs, "api_chapters": ch, "disk_dirs": len(dirs)})
    out["manga_mismatch"] = mism

    bk = _get(a.port, "/api/books")
    bm = []
    for b in (bk.get("books") or []):
        d = os.path.join("data", "books", str(b.get("key") or ""))
        have = len(glob.glob(os.path.join(d, "*.cache"))) if os.path.isdir(d) else 0
        if b.get("total") is not None and int(b.get("total") or 0) != have:
            bm.append({"name": str(b.get("name"))[:18], "api_total": b.get("total"),
                       "disk_cache": have})
    out["books_mismatch"] = bm

    print("== /api/storage ==")
    print("  total_bytes %d | 分类之和 %d | 一致: %s"
          % (out["storage_total"], cat_sum, out["storage_total"] == cat_sum))
    print("  逐书字节不一致: %d" % len(bad_book))
    for x in bad_book[:5]:
        print("    ", x)
    print("== /api/manga/library（界面显示 local_images / chapters）==")
    print("  不一致: %d 部" % len(mism))
    for x in mism[:8]:
        print("    ", x)
    print("== /api/books ==")
    print("  total 与磁盘 .cache 不一致: %d 本" % len(bm))
    for x in bm[:5]:
        print("    ", x)
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print("结果已写入 %s" % a.json_out)
    return 1 if (bad_book or mism or bm or out["storage_total"] != cat_sum) else 0


if __name__ == "__main__":
    sys.exit(main())
