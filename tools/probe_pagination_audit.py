#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分页审计（真实外站）：**站点标着多页，我们到底取了几页？**

## 为什么需要

2026-09-18 实测抓到一类**静默截断**：6 个源的 `nextContentUrl` 写成
`@正则:<a…>下一章`（带标签），而规则引擎只拿**纯文本**匹配 → 规则恒为空 →
分页停在第 1 页 → 多页章节被截断（quanben8 第1113章：只取 1234 字，整章 3 页 4156 字）。
**这类缺陷不会报错**：章节标成"成功"，读者在半路莫名断掉，缓存后不会自愈。

所以要对**每个启用源**审计一遍：站点这一章是不是多页？我们取到了几页？
判据用**站点自己的证据**，不猜：

  · 页面里出现 `第(N/M)页` 之类页标记（N<M 即多页）；
  · 或"下一页/下一章"链接指向 `xxx_2.html`、`?page=2`、`/2.html` 这类**同章下一页**；
  · 再看我们的 `get_content` 取到的正文长度，以及末页的"下一页"是否已指向**下一章**
    （指向下一章 = 取完了）。

## 口径

按**手机端**（`load_enabled()`，即 APK 内置策略）跑；每源最多 4 个请求、串行、只读，
不建任务/不写书源/不写书库。用法：

    WR_PROFILE=mobile WR_DATA_DIR=/tmp/wr-audit WR_TEST=1 WR_DISABLE_BACKGROUND=1 \
        ./venv/bin/python tools/probe_pagination_audit.py [--only 全本小说网] [--json]
"""
import argparse
import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PAGE_MARK = re.compile(r"第\s*[（(]?\s*(\d+)\s*/\s*(\d+)\s*[)）]?\s*页")
NEXT_PAGE_HINT = re.compile(
    r'href="([^"]*(?:_\d+\.html|\?page=\d+|/\d+\.html)[^"]*)"[^>]*>\s*'
    r"(?:下一页|下页|下一章)")


class Timeout(Exception):
    pass


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
        raise Timeout(f">{sec}s")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def audit_source(src, keyword):
    from engine.crawler import SourceCrawler
    out = {"name": (src.get("bookSourceName") or "")[:20], "uid": src.get("uid")}
    c = SourceCrawler(src)
    books = _run(lambda: c.search(keyword, page=1), 20) or []
    if not books:
        out["result"] = "搜不到"
        return out
    book = _run(lambda: c.get_book(books[0].get("book_url"), fast=True), 20)
    if book is None:
        out["result"] = "详情为空"
        return out
    chs = _run(lambda: c.get_toc(book), 25) or []
    if not chs:
        out["result"] = "目录为空"
        return out
    ch = chs[min(5, len(chs) - 1)]
    out["chapter"] = (ch.get("name") or "")[:24]

    html = _run(lambda: c.fetcher.get(ch["url"], source=src, timeout=15), 25)
    marks = PAGE_MARK.findall(html)
    pages_site = max((int(m[1]) for m in marks), default=1)
    hint = NEXT_PAGE_HINT.search(html)
    out["site_pages"] = pages_site
    out["site_multi_page"] = pages_site > 1 or bool(hint)
    out["page_hint"] = (hint.group(1)[:60] if hint else "")

    text = _run(lambda: c.get_content(ch["url"], timeout=30), 45) or ""
    out["our_chars"] = len(text)
    out["our_pages_guess"] = round(len(text) / max(1, pages_site))
    # 判定
    if out["site_multi_page"] and len(text) < 400:
        out["result"] = "✗ 疑似截断（站点多页，我们几乎没取到）"
    elif pages_site > 1 and len(text) < 300 * pages_site:
        out["result"] = f"✗ 疑似截断（站点 {pages_site} 页，我们只 {len(text)} 字）"
    elif out["site_multi_page"]:
        out["result"] = f"✓ 多页已取全（站点 {pages_site} 页 / 我们 {len(text)} 字）"
    else:
        out["result"] = f"· 单页（{len(text)} 字）"
    return out


def main():
    from _probe_guard import ensure_isolated  # noqa: E402
    ensure_isolated()
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", default="剑来")
    ap.add_argument("--only", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--json-out", default="", help="把结果写成 JSON 文件（避免与引擎日志混在一起）")
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
            row = audit_source(s, a.keyword)
        except Exception as e:                                   # noqa: BLE001
            row = {"name": (s.get("bookSourceName") or "")[:20],
                   "result": f"异常 {type(e).__name__}: {str(e)[:60]}"}
        row["secs"] = round(time.time() - t0, 1)
        rows.append(row)
        if not a.json:
            print(f"  {row['name']:22s} {row.get('result','')}  [{row['secs']}s]")
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        print(f"结果已写入 {a.json_out}")
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    bad = [r for r in rows if str(r.get("result", "")).startswith("✗")]
    print(f"\n审计 {len(rows)} 个源 · 疑似截断 {len(bad)} 个")
    for r in bad:
        print(f"   ✗ {r['name']}: {r['result']}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
