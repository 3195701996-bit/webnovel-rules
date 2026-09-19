#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小说源逐源盘点（真实外站）· **按手机端口径**跑。

## 为什么要按"手机口径"

桌面 `sources/*.json` 的启用/停用是**用户自己的桌面配置**（当前只有 4 个启用），
而 APK 里内置的是同一批 34 个文件、按 `android/mobile-seed-policy.json` 决定默认启用——
当前是"默认启用全部，显式停用 6 个"= **28 个启用**。
所以"小说侧哪些源真能用"必须按这 28 个来量，照桌面的 4 个量会得出完全错误的结论。

## 量什么（三阶段，与设备用例 NovelRegressionTest 同口径的前三段）

    搜索（关键词逐级回退）→ 目录 → 首章正文（字数 + 开头片段）

**只读**：不建任务、不写书源、不写书库；建议 `WR_DATA_DIR` 指向临时目录。
**串行**：每个源最多 3 个请求，不做并发、不做压力测试；DNS 被墙/解析到 0.0.0.0 直接判失败。
**有时限**：每阶段有硬性时限（默认 search 12s / toc 15s / content 12s），
超时即放弃并如实记录——不靠适配器自己的超时链（那条链可能被地址数量乘出来）。

用法：
    WR_DATA_DIR=/tmp/wr-novel WR_TEST=1 ./venv/bin/python tools/probe_novel_sources.py
    ... --keyword 剑来 --only 精华书阁 --json
"""
import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SEED_POLICY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "android", "mobile-seed-policy.json")
DEFAULT_KW = "剑来"
KW_FALLBACK = ("剑来", "斗破苍穹", "赘婿")


class StageTimeout(Exception):
    pass


def call_bounded(fn, seconds):
    """有时限调用（与 engine.manga.verify.call_bounded 同一思路）。

    适配器自己的 timeout 不可靠：多地址域会逐个试，等待时间会被地址数量乘出来。
    """
    box = {}

    def _w():
        try:
            box["v"] = fn()
        except Exception as e:                                   # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=_w, daemon=True)
    t.start()
    t.join(timeout=max(1.0, float(seconds)))
    if t.is_alive():
        raise StageTimeout("阶段超过硬性时限 %.0fs" % seconds)
    if "e" in box:
        raise box["e"]
    return box.get("v")


def phone_source_set():
    """(启用的源 dict 列表, 被策略停用的 uid 集合)——与 APK 内置口径一致"""
    try:
        pol = json.load(open(SEED_POLICY, encoding="utf-8"))
        disabled = set((pol.get("disable") or {}).keys())
    except Exception as e:                                       # noqa: BLE001
        print("⚠ 读不到种子策略（%s），按文件里的 enabled 字段" % e)
        disabled = set()
    return disabled


def _short(e, n=90):
    return ("%s: %s" % (type(e).__name__, str(e)))[:n]


# 正文质量检查直接用引擎实现（engine/cleaner.check_purify_quality）——
# 避免探针与 App 各写一份判据导致结论不一致。
from engine.cleaner import check_purify_quality as check_content_quality  # noqa: E402
from engine.app_utils import pick_title_match  # noqa: E402


def probe_one(src, kw, budget, deep=False):
    """单源三阶段。返回结果 dict（失败也返回，带失败阶段与原因）。"""
    from engine.crawler import SourceCrawler
    out = {"uid": src.get("uid") or "", "name": src.get("bookSourceName") or "",
           "url": src.get("bookSourceUrl") or "", "search": None, "toc": None,
           "content": None, "stage": "", "reason": "", "ms": {}}
    t0 = time.time()
    try:
        c = SourceCrawler(src)

        # 1) 搜索（首词没结果才换词；连接类失败不换词）
        hits, used_kw, derr = [], "", ""
        for k in [kw] + [x for x in KW_FALLBACK if x != kw]:
            try:
                hits = call_bounded(lambda kk=k: c.search(kk) or [], budget["search"])
            except Exception as e:                               # noqa: BLE001
                derr = _short(e)
                hits = []
                if isinstance(e, StageTimeout) or "resolve" in derr.lower() \
                        or "timeout" in derr.lower() or "name" in derr.lower():
                    break
            if hits:
                used_kw = k
                break
        out["ms"]["search"] = int((time.time() - t0) * 1000)
        if not hits:
            out.update(stage="搜索", reason=derr or "0 结果")
            return out
        # 选书口径与设备用例一致：**标题精确匹配优先**，其次包含关键词，最后回退第一条。
        # 不这么做就会踩到源站的模糊匹配顺序（实测 ixdzs8 对「剑来」第一条是
        # 《青冥等剑来》89 章，《剑来》本身 1278 章掉到第 6 条）——
        # 那会把"选错书"误读成"目录退化"。
        dicts = [x for x in hits if isinstance(x, dict)]
        exact = [x for x in dicts if (x.get("name") or "").strip() == used_kw]
        like = [x for x in dicts if used_kw and used_kw in (x.get("name") or "")]
        first = (exact or like or dicts or [{}])[0]
        book_url = first.get("book_url") or first.get("url") or ""
        out["search"] = {"hits": len(hits), "kw": used_kw,
                         "title": first.get("name") or "", "url": book_url}
        if not book_url:
            out.update(stage="搜索", reason="首条结果没有 book_url")
            return out

        # 2) 目录
        t1 = time.time()
        try:
            book = call_bounded(lambda: c.get_book(book_url, fast=True), budget["toc"])
            chapters = call_bounded(lambda: c.get_toc(book) or [], budget["toc"])
        except Exception as e:                                   # noqa: BLE001
            out["ms"]["toc"] = int((time.time() - t1) * 1000)
            out.update(stage="目录", reason=_short(e))
            return out
        out["ms"]["toc"] = int((time.time() - t1) * 1000)
        out["toc"] = {"chapters": len(chapters),
                      "first": (chapters[0].get("name") if chapters else "")}
        if not chapters:
            out.update(stage="目录", reason="目录为空")
            return out

        # 3) 首章正文
        t2 = time.time()
        try:
            text = call_bounded(lambda: c.get_content(chapters[0]["url"]),
                                budget["content"]) or ""
        except Exception as e:                                   # noqa: BLE001
            out["ms"]["content"] = int((time.time() - t2) * 1000)
            out.update(stage="正文", reason=_short(e))
            return out
        out["ms"]["content"] = int((time.time() - t2) * 1000)
        out["content"] = {"chars": len(text),
                          "head": text.replace("\n", " ").strip()[:60]}
        _ok, _probs = check_content_quality(text)
        out["content"]["problems"] = _probs
        if len(text) < 300:
            out.update(stage="正文", reason="首章仅 %d 字（<300）" % len(text))
            return out

        # --deep：再抽**中段一章**（分页/净化最容易在长书暴露问题）
        if deep and len(chapters) > 12:
            idx = min(len(chapters) - 1, max(1, len(chapters) // 2))
            ch = chapters[idx]
            t3 = time.time()
            try:
                mid = call_bounded(lambda: c.get_content(ch["url"]),
                                   budget["content"]) or ""
                m_ok, m_probs = check_content_quality(mid)
                out["mid"] = {"idx": idx, "name": ch.get("name") or "",
                              "chars": len(mid), "ok": m_ok, "problems": m_probs,
                              "head": mid.replace("\n", " ").strip()[:50],
                              "ms": int((time.time() - t3) * 1000)}
            except Exception as e:                               # noqa: BLE001
                out["mid"] = {"idx": idx, "ok": False,
                              "problems": ["取中段失败：" + _short(e)],
                              "ms": int((time.time() - t3) * 1000)}
        out["ok"] = True
        return out
    except Exception as e:                                       # noqa: BLE001
        out.update(stage=out["stage"] or "异常", reason=_short(e))
        return out


# 分页残留标记：正文本该已经拼完整章，出现这些说明**分页没取全**
PAGE_RESIDUE = (
    "下一页", "下页", "本章未完", "点击继续", "未完待续…", "（1/", "(1/",
    "第1/", "第 1/", "分页", "翻页",
)
_PAGE_NUM_RE = None


def compare_chapter(srcs, kw, chapter_name, budget, limit=8, verbose=True):
    """**同一本书、同一个章名**跨源取正文并比较字数 → 抓"分页没取全"的截断。

    为什么这样判：单源单独看，截断很隐蔽（正文读起来是通的，只是少了一半），
    而同一章在不同源的字数应当大体一致（差在净化与源站编辑，不该差几倍）。
    字数显著偏低 + 文中出现分页残留 = 我们的取数没取全，而不是源站没有。

    返回结果行列表（每行：源、章名、字数、页码残留、开头片段）。
    """
    import re as _re
    global _PAGE_NUM_RE
    if _PAGE_NUM_RE is None:
        _PAGE_NUM_RE = _re.compile(r"[(（]\s*\d+\s*/\s*\d+\s*[)）]|第\s*\d+\s*/\s*\d+\s*页")
    from engine.crawler import SourceCrawler
    rows = []
    for src in srcs[:limit]:
        row = {"uid": src.get("uid") or "", "name": src.get("bookSourceName") or "",
               "chars": None, "chapter": "", "page_residue": [], "err": ""}
        try:
            c = SourceCrawler(src)
            hits = call_bounded(lambda: c.search(kw) or [], budget["search"])
            b0 = pick_title_match(hits, kw)
            if not b0:
                row["err"] = "搜索无结果"
                rows.append(row)
                continue
            book = call_bounded(lambda: c.get_book(b0["book_url"], fast=True), budget["toc"])
            chs = call_bounded(lambda: c.get_toc(book) or [], budget["toc"])
            want = (chapter_name or "").replace(" ", "")
            hit = None
            for ch in chs:
                if want and want in (ch.get("name") or "").replace(" ", ""):
                    hit = ch
                    break
            if hit is None:
                row["err"] = "该源目录里没有这一章（可能章节名不同或目录不全）"
                rows.append(row)
                continue
            row["chapter"] = hit.get("name") or ""
            text = call_bounded(lambda: c.get_content(hit["url"]), budget["content"]) or ""
            row["chars"] = len(text)
            row["page_residue"] = [m for m in PAGE_RESIDUE if m in text][:4]
            if _PAGE_NUM_RE.search(text):
                row["page_residue"].append("页码形态")
            row["head"] = text.replace("\n", " ").strip()[:50]
            row["tail"] = text.replace("\n", " ").strip()[-40:]
        except Exception as e:                                   # noqa: BLE001
            row["err"] = _short(e)
        rows.append(row)
        if verbose:
            print("  %-30s %-22s %s" % (row["uid"][:30], (row["chapter"] or row["err"])[:22],
                                        ("%d 字" % row["chars"]) if row["chars"] else row["err"][:40]))
    return rows


def main():
    from _probe_guard import ensure_isolated  # noqa: E402
    ensure_isolated()
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", default=DEFAULT_KW)
    ap.add_argument("--only", default="", help="只测 uid 含该子串的源")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--compare-chapter", default="",
                    help="跨源对比：同一本书的这个章名，比较各源正文字数（抓分页截断）")
    ap.add_argument("--compare-limit", type=int, default=8)
    ap.add_argument("--deep", action="store_true",
                    help="额外抽中段一章并检查净化残留（广告/导航标记）")
    ap.add_argument("--search", type=float, default=12)
    ap.add_argument("--toc", type=float, default=15)
    ap.add_argument("--content", type=float, default=12)
    args = ap.parse_args()
    budget = {"search": args.search, "toc": args.toc, "content": args.content}

    dd = os.environ.get("WR_DATA_DIR", "")
    print("数据目录：%s" % (dd or "⚠ 未隔离（建议 WR_DATA_DIR=/tmp/wr-novel）"))
    disabled = phone_source_set()
    from engine.source_mgr import load_all
    allsrc = load_all() or []
    # 手机口径：全部内置源，但按种子策略剔除显式停用的
    srcs = [s for s in allsrc if (s.get("uid") or "") not in disabled]
    # 同一 uid 可能有多份文件（实测 34 文件 / 30 唯一 uid）。按 uid 去重，
    # 否则会把同一个源测两遍——既浪费请求，也会把"第二遍变慢"误读成源不稳定。
    _seen, _uniq = set(), []
    for s in srcs:
        u = s.get("uid") or ""
        if u in _seen:
            continue
        _seen.add(u)
        _uniq.append(s)
    dup_n = len(srcs) - len(_uniq)
    srcs = _uniq
    if args.only:
        srcs = [s for s in srcs if args.only in (s.get("uid") or "")]
    print("书源文件 %d 个 · 种子策略停用 %d 个 · 同 uid 重复 %d 份已去重 · "
          "**本次按手机口径测 %d 个**"
          % (len(allsrc), len(disabled), dup_n, len(srcs)))
    print("关键词：%s（回退 %s）· 阶段时限 search %.0fs / toc %.0fs / content %.0fs"
          % (args.keyword, "/".join(KW_FALLBACK), args.search, args.toc, args.content))
    print("时间：%s · 串行执行，每源最多 3 个请求" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("-" * 92)

    if args.compare_chapter:
        print("跨源对比章名：%s（最多 %d 个源）" % (args.compare_chapter, args.compare_limit))
        print("-" * 92)
        rows = compare_chapter(srcs, args.keyword, args.compare_chapter, budget,
                               limit=args.compare_limit)
        got = [(r["chars"] or 0) for r in rows if r.get("chars")]
        print("-" * 92)
        if got:
            got_sorted = sorted(got)
            med = got_sorted[len(got_sorted) // 2]
            print("取到正文 %d 个源 · 中位字数 %d · 区间 %d~%d"
                  % (len(got), med, min(got), max(got)))
            for r in rows:
                if not r.get("chars"):
                    continue
                if med and r["chars"] < med * 0.7:
                    print("  ⚠ %-28s 仅 %d 字（中位 %d）→ 疑似**分页没取全/截断**%s"
                          % (r["uid"][:28], r["chars"], med,
                             "；残留：" + "、".join(r["page_residue"]) if r["page_residue"] else ""))
                elif r.get("page_residue"):
                    print("  ⚠ %-28s %d 字，但文内出现分页标记：%s"
                          % (r["uid"][:28], r["chars"], "、".join(r["page_residue"])))
            print("判读：① 显著低于中位 + 分页标记 = 取数没取全（静默截断）；"
                  "② 显著高于中位（例如 4 倍）= 适配器跟随分页链接走出了本章、"
                  "把后续章节拼了进来（实测精华书阁 5.8k 的章被存成 24.9k）；"
                  "只差百分之几是源站编辑差异。")
            for r in rows:
                if (r.get("chars") or 0) > med * 1.6:
                    print("  ⚠ %-28s %d 字（中位 %d，%.1f 倍）→ 疑似把后续章节拼进本章"
                          % (r["uid"][:28], r["chars"], med,
                             r["chars"] / max(1, med)))
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
        return 0

    rows, okn = [], 0
    for s in srcs:
        r = probe_one(s, args.keyword, budget, deep=args.deep)
        rows.append(r)
        okn += 1 if r.get("ok") else 0
        s_ = r.get("search") or {}
        t_ = r.get("toc") or {}
        c_ = r.get("content") or {}
        ms = r.get("ms") or {}
        print("%s %-34s 搜索 %-4s %5sms → 目录 %-5s %6sms → 正文 %-6s %6sms"
              % ("✓" if r.get("ok") else "✗", (r["uid"] or "")[:34],
                 s_.get("hits", "—"), ms.get("search", "—"),
                 t_.get("chapters", "—"), ms.get("toc", "—"),
                 c_.get("chars", "—"), ms.get("content", "—")))
        if r.get("ok"):
            print("      《%s》%s… | 首章：%s"
                  % (s_.get("title", "")[:20], (c_.get("head") or "")[:40],
                     (t_.get("first") or "")[:24]))
            if c_.get("problems"):
                print("      ⚠ 首章质量：%s" % "；".join(c_["problems"]))
            m = r.get("mid")
            if m:
                print("      %s 中段[%d] %s：%s 字 %s"
                      % ("✓" if m.get("ok") else "⚠", m.get("idx", 0),
                         (m.get("name") or "")[:16], m.get("chars"),
                         "" if m.get("ok") else "→ " + "；".join(m.get("problems") or [])))
        else:
            print("      ⚠ 失败阶段=%s 说明=%s" % (r.get("stage"), r.get("reason")))
    print("-" * 92)
    print("汇总：按手机口径 %d 个源 · 三段全通 %d · 未通过 %d"
          % (len(rows), okn, len(rows) - okn))
    by_stage = {}
    for r in rows:
        if not r.get("ok"):
            by_stage[r.get("stage") or "?"] = by_stage.get(r.get("stage") or "?", 0) + 1
    if by_stage:
        print("失败分布：" + "、".join("%s %d" % kv for kv in sorted(by_stage.items())))
    print("注：桌面结论，**不等于**目标手机可用（指南 §2.5）；换网络/换代理结果会变。")
    if args.json:
        print(json.dumps({"rows": rows, "ok": okn}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
