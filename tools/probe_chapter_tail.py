#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""正文完整性离线核对（指南 P0-2 的「正文末尾」一环）· 跑在**已下载的书库**上。

## 查什么

对每本书里**相邻且都已下载**的章对 (N, N+1) 做三项客观判据：

1. **跨章污染**：本章**末尾 400 字**里是否出现**下一章开头 40 字**。
   这是真出现过的缺陷类——精华书阁第301章曾把下一章内容拼进本章（一个 5.8k 字的章
   被存成 24 966 字，结尾断在下一章的句子中间）；适配器分页撞"下一章"链接会这样。
2. **页缝重叠**：下一章**开头**是否与本章**末尾**重复（站点分页"回读"）。
   量级小时属站点行为（读者会重读一小段），记录但不判失败。
3. **截断信号**：本章结尾是否落在句末标点（`。！？…”』」` 等）。
   **只作说明，不判失败** —— 实测书库里最短的"章节"47 字节是作者真实发布的请假条，
   以标点判"截断"会误杀正常章（这条教训写在这里，避免以后又拿它当判据）。

## 用法（只读、不联网、不写任何用户数据）

    WR_PROFILE=mobile ./venv/bin/python tools/probe_chapter_tail.py [--book 凡人] [--json-out /tmp/tail.json]
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

END_PUNCT = "。！？…”\"』」）).!?~～"
HEAD_CHARS = 40        # 下一章开头取多少字
TAIL_CHARS = 400       # 本章末尾取多少字
MIN_LEN = 400          # 太短的章不参与比较（更新通知/请假条这类本来就短）


def _body(path):
    try:
        t = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    ls = [x.strip() for x in t.split("\n") if x.strip()]
    return "".join(ls[1:] if len(ls) > 1 else ls)     # 去掉缓存的章名行


def scan(root="data/books", only=""):
    from engine.app_utils import cache_key_of
    rows = []
    for d in sorted(glob.glob(os.path.join(root, "*", ""))):
        st = os.path.join(d, "_state.json")
        if not os.path.exists(st):
            continue
        try:
            j = json.load(open(st, encoding="utf-8"))
        except Exception:                                        # noqa: BLE001
            continue
        name = (j.get("book") or {}).get("name") or os.path.basename(d.rstrip("/"))
        if only and only not in name and only not in d:
            continue
        chs = j.get("chapters") or []
        have = {os.path.basename(f) for f in glob.glob(os.path.join(d, "*.cache"))}
        texts = {}
        for i, c in enumerate(chs):
            fn = cache_key_of(c.get("url") or "") + ".cache"
            if fn in have:
                texts[i] = _body(os.path.join(d, fn))
        pairs = contam = seam = noend = 0
        examples = []
        keys = sorted(texts)
        for idx, i in enumerate(keys):
            t = texts[i]
            if len(t) >= MIN_LEN and t and t[-1] not in END_PUNCT:
                noend += 1
            if idx + 1 >= len(keys) or keys[idx + 1] != i + 1:
                continue
            a, b = texts[i], texts[i + 1]
            if len(a) < MIN_LEN or len(b) < MIN_LEN:
                continue
            pairs += 1
            head, tail = b[:HEAD_CHARS], a[-TAIL_CHARS:]
            if len(head) >= 30 and head in tail:
                contam += 1
                if len(examples) < 3:
                    examples.append(["跨章污染", chs[i].get("name", "")[:20],
                                     chs[i + 1].get("name", "")[:20], head[:26]])
            if len(tail) >= 60 and len(b) >= 60 and tail[-60:] in b[:200]:
                seam += 1
        rows.append({"book": name[:26], "cached": len(texts), "pairs": pairs,
                     "contamination": contam, "seam_overlap": seam,
                     "no_end_punct": noend, "examples": examples})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default="")
    ap.add_argument("--json-out", default="")
    a = ap.parse_args()
    os.environ.setdefault("WR_PROFILE", "mobile")
    rows = scan(only=a.book)
    print(f"{'书':28s} {'已下载':>6s} {'可比章对':>7s} {'跨章污染':>7s} {'页缝重叠':>7s} {'结尾非句末':>8s}")
    for r in rows:
        print(f"{r['book']:28s} {r['cached']:6d} {r['pairs']:7d} {r['contamination']:7d} "
              f"{r['seam_overlap']:7d} {r['no_end_punct']:8d}")
    tot_c = sum(r["contamination"] for r in rows)
    print(f"\n合计：跨章污染 {tot_c} 处（0 = 正文末尾未被下一章内容污染）")
    for r in rows:
        for e in r["examples"]:
            print(f"   ✗ {r['book']}: {e[1]} → {e[2]} 含下一章开头 {e[3]!r}")
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        print(f"结果已写入 {a.json_out}")
    return 1 if tot_c else 0


if __name__ == "__main__":
    sys.exit(main())
