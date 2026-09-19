#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""书源长线测试工具（R40）：
每源至少测 10 本书（优先长篇），覆盖 搜索 / 章节目录完整性 / 章节正文完整性。
用法:
  ./venv/bin/python tools/source_lt_test.py                # 全部启用源
  ./venv/bin/python tools/source_lt_test.py --source quanben5.com
  ./venv/bin/python tools/source_lt_test.py --books 10 --max-toc 18
断点续跑：进度存 data/logs/lt_progress.json，已完成的源自动跳过。
报告：data/logs/source_lt_report.md + .json
"""
import argparse
import json
import os
import re
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.source_mgr import load_enabled
from engine.crawler import SourceCrawler

REPORT_MD = os.path.join("data", "logs", "source_lt_report.md")
REPORT_JSON = os.path.join("data", "logs", "source_lt_report.json")
PROGRESS = os.path.join("data", "logs", "lt_progress.json")

KEYWORDS = ["斗破苍穹", "剑来", "凡人修仙传", "完美世界", "诡秘之主",
            "赘婿", "雪中悍刀行", "遮天", "一念永恒", "大奉打更人",
            "仙逆", "庆余年", "万古神帝", "牧神记", "夜的命名术"]

# 正文"错误页/拦截"特征（命中即视为正文获取失败）
BAD_TEXT_PATTERNS = ["验证码", "访问过于频繁", "请开启JavaScript", "页面不存在",
                     "404 Not Found", "系统繁忙", "抱歉，您访问的页面", "被禁止访问"]

_TITLE_NUM_RE = [
    re.compile(r"第\s*(\d+(?:\.\d+)?)\s*(?:章|节|回|话|集|話|章節)", re.I),
    re.compile(r"第\s*([零一二三四五六七八九十百千万]+)\s*(?:章|节|回|话|集|話|章節)"),
    re.compile(r"(\d+)\s*(?:章|节|回|话|集|話|章節)", re.I),
]


def cn2int(s):
    """中文数字转 int（不支持返回 None）"""
    table = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100, "千": 1000}
    total = 0
    sec = 0
    for ch in s:
        if ch == "十":
            sec = sec * 10 if sec else 10
        elif ch in ("百", "千"):
            total += sec * table[ch]
            sec = 0
        elif ch in table:
            sec += table[ch]
        else:
            return None
    return total + sec


def chapter_no(name):
    """从章节标题解析序号（优先阿拉伯，中文数字兜底）"""
    for rx in _TITLE_NUM_RE:
        m = rx.search(name or "")
        if m:
            g = m.group(1)
            if g.isdigit():
                return int(g)
            n = cn2int(g)
            if n is not None:
                return n
    return None


class SourceTest:
    def __init__(self, src, books_target=10, max_toc=18, quiet=False):
        self.src = src
        self.uid = src.get("uid", "")
        self.name = src.get("bookSourceName", "") or self.uid
        self.books_target = books_target
        self.max_toc = max_toc
        self.crawler = SourceCrawler(src)
        self.results = {
            "search": {"keywords": 0, "ok": 0, "total_results": 0, "errors": []},
            "books": [],          # 每本: {name,url,chapters,dup,hole,unlabeled,content:{ok,fail,len_avg,samples}}
            "toc_ok": 0, "toc_fail": 0,
            "content_ok_chapters": 0, "content_fail_chapters": 0,
        }
        self.quiet = quiet

    def log(self, msg):
        if not self.quiet:
            print(f"  [{self.name}] {msg}", flush=True)

    # ── 1. 搜索 ──
    def test_search(self, words=6):
        picked = random.sample(KEYWORDS, min(words, len(KEYWORDS)))
        pool = []
        for kw in picked:
            self.results["search"]["keywords"] += 1
            try:
                books = self.crawler.search(kw)
                self.results["search"]["ok"] += 1
                self.results["search"]["total_results"] += len(books or [])
                for b in books or []:
                    if b.get("book_url") and b.get("name"):
                        pool.append(b)
            except Exception as e:
                self.results["search"]["errors"].append(f"{kw}: {type(e).__name__}: {str(e)[:80]}")
            time.sleep(0.6)
        # 去重
        seen = set()
        pool2 = []
        for b in pool:
            u = b.get("book_url", "")
            if u in seen:
                continue
            seen.add(u)
            pool2.append(b)
        self.log(f"搜索 {self.results['search']['keywords']} 词, 成功 "
                 f"{self.results['search']['ok']}, 候选书 {len(pool2)}")
        return pool2

    # ── 2. 目录 ──
    END_FLAGS = ("完本", "完结", "大结局", "后记", "全书完", "完結", "結局",
                 "终章", "大結局")

    def test_toc(self, book):
        t0 = time.time()
        try:
            info = self.crawler.get_book(book["book_url"], fast=True)
            # R40c: 源站"最新章节"声明（连载最新检查基准）
            _last_decl = (getattr(info, "last_chapter", "") or "").strip()
            info.chapters = []
            self.crawler.get_toc(info)
            chs = [{"name": c.get("name", ""), "url": c.get("url", "")}
                   for c in info.chapters]
            dt = time.time() - t0
            if len(chs) < 30:
                return {"name": book.get("name", "?"), "url": book["book_url"],
                        "chapters": len(chs), "dt": dt,
                        "note": "章节过少(可能短篇/源页损坏)"}
            # 完整性：序号重复 / 空洞 / 未编号
            nos = [chapter_no(c["name"]) for c in chs]
            valid = [n for n in nos if n is not None]
            dup = len(valid) - len(set(valid))
            holes = 0
            if len(valid) >= 20:
                lo, hi = min(valid), max(valid)
                holes = max(0, (hi - lo + 1) - len(set(valid)))
            unlabeled = len(nos) - len(valid)
            toc_max = max(valid) if valid else 0
            # 完结标志：目录末章名 或 源站最新章节声明
            _last_own = chs[-1]["name"] if chs else ""
            _end_mark = any(f in (_last_own + " " + _last_decl)
                            for f in self.END_FLAGS)
            # 连载最新检查：源站声明最新章节号 vs 目录最大章号
            latest_ok = None
            _decl_no = chapter_no(_last_decl)
            if _decl_no and toc_max:
                latest_ok = toc_max >= _decl_no
            # 完本完整性：有完结标志时目录空洞容忍 ≤2%（源站跳号），末章可读。
            # 卷制/未编号多的书(未编号>30%)空洞检测不适用 → 只看末章可读
            end_complete = None
            if _end_mark:
                if unlabeled > len(chs) * 0.3:
                    end_complete = bool(_last_own)
                else:
                    _tol = max(1, int(len(chs) * 0.02))
                    end_complete = (holes <= _tol) and bool(_last_own)
            return {"name": book.get("name", "?"), "url": book["book_url"],
                    "chapters": len(chs), "dt": dt,
                    "dup": dup, "holes": holes, "unlabeled": unlabeled,
                    "toc_max": toc_max, "last_decl": _last_decl[:40],
                    "end_mark": _end_mark, "latest_ok": latest_ok,
                    "end_complete": end_complete}
        except Exception as e:
            dt = time.time() - t0
            return {"name": book.get("name", "?"), "url": book["book_url"],
                    "chapters": 0, "dt": dt,
                    "error": f"{type(e).__name__}: {str(e)[:90]}"}

    # ── 3. 正文抽查 ──
    # 敏感源(挑战页/IP风控)的封锁窗口可达 30-60s：退避序列需覆盖
    _BACKOFF = (4.0, 12.0, 30.0)

    def test_content(self, book):
        """抽查首/中/尾三章正文（目录重拉失败时重试，避免源站偶发限流误判）"""
        info = None
        for attempt, wait in enumerate(self._BACKOFF):
            try:
                info = self.crawler.get_book(book["url"], fast=True)
                info.chapters = []
                self.crawler.get_toc(info)
                if info.chapters:
                    break
            except Exception:
                info = None
            time.sleep(wait)  # 限流窗口退避后重试
        chs = info.chapters if info else []
        if not chs:
            return None
        picks = []
        n = len(chs)
        for idx in (0, n // 2, n - 1):
            if idx not in [p for p in picks]:
                picks.append(idx)
        samples = []
        ok = 0
        for idx in picks:
            try:
                txt = self.crawler.get_content(chs[idx]["url"]) or ""
            except Exception as e:
                samples.append({"idx": idx, "len": 0,
                                "err": f"{type(e).__name__}: {str(e)[:70]}"})
                continue
            tl = len(txt.strip())
            flags = []
            _cname = (chs[idx].get("name") or "") if idx < len(chs) else ""
            # 公告/作品相关/卷前语等短章不是缺陷(作者公告常<300字)
            _ann = any(k in _cname for k in ("公告", "作品相关", "上传", "发布",
                                             "感言", "请假", "说明", "完本", "新书"))
            # 首章(idx0)与末章(idx==last)的公告/序言短文不判失败
            _edge = (idx == 0) or (idx == len(chs) - 1)
            if tl < 300 and not (_ann and _edge):
                flags.append("过短")
            for p in BAD_TEXT_PATTERNS:
                if p in txt[:600]:
                    flags.append(f"含拦截特征:{p}")
                    break
            if not flags:
                ok += 1
            samples.append({"idx": idx, "len": tl,
                            "flags": flags})
            time.sleep(2.5)  # 章间节流(敏感源高频触发风控,已含封锁窗口余量)
        return {"ok": ok, "total": len(picks), "samples": samples}

    def run(self):
        pool = self.test_search()
        if not pool:
            return None
        # 目录测试：候选池按预算测，收集长篇
        cand = []
        self.log(f"目录测试（预算 {self.max_toc} 本候选）…")
        for b in pool[: self.max_toc]:
            r = self.test_toc(b)
            if r.get("error"):
                self.results["toc_fail"] += 1
                self.log(f"  toc失败 {b.get('name','?')[:18]}: {r['error'][:60]}")
            else:
                self.results["toc_ok"] += 1
                cand.append(r)
            time.sleep(0.5)
        # 长篇优先:章节数降序
        cand.sort(key=lambda r: -(r.get("chapters") or 0))
        chosen = cand[: self.books_target]
        if len(chosen) < self.books_target:
            # 候选不足:把 toc 失败的也计入失败详情
            self.log(f"长篇候选不足（{len(chosen)}/{self.books_target}）")
        self.log(f"选定 {len(chosen)} 本做正文抽查（长篇优先）：")
        for b in chosen[:5]:
            extra = ""
            if b.get("chapters"):
                extra = (f"[重复{b.get('dup')} 空洞{b.get('holes')} "
                         f"未编号{b.get('unlabeled')}]")
                if b.get("end_mark"):
                    extra += (f" 完结:{'✓完整' if b.get('end_complete') else '✗有缺'}")
                if b.get("latest_ok") is not None:
                    extra += (f" 最新:{'✓' if b.get('latest_ok') else '✗目录落后'}")
            self.log(f"  - {b.get('name','?')[:24]} {b.get('chapters')} 章 {extra}")
        # 正文抽查
        for b in chosen:
            cr = self.test_content(b)
            b["content"] = cr  # None=目录重试仍不可用(正文未测),报告按 bad 计
            if cr:
                self.results["content_ok_chapters"] += cr["ok"]
                self.results["content_fail_chapters"] += cr["total"] - cr["ok"]
                if cr["ok"] < cr["total"]:
                    self.log(f"  正文问题 {b.get('name','?')[:20]}: "
                             f"{cr['ok']}/{cr['total']} 章通过 "
                             + str([s for s in cr["samples"] if s.get("flags") or s.get("err")][:2]))
            time.sleep(3.0)  # 书间节流(封锁窗口恢复余量)
        self.results["books"] = chosen
        return self.results


def save_progress(done_uids):
    try:
        os.makedirs(os.path.dirname(PROGRESS), exist_ok=True)
        json.dump({"done": sorted(done_uids), "ts": time.time()},
                  open(PROGRESS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass


def load_progress():
    try:
        return set(json.load(open(PROGRESS, encoding="utf-8")).get("done", []))
    except Exception:
        return set()


def gen_report(all_results):
    md = ["# 书源长线测试报告", "",
          f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    rows = []
    for uid, r in all_results.items():
        if r is None:
            rows.append({"uid": uid, "name": uid, "search_ok": 0,
                         "cand": 0, "toc_ok": 0, "books": 0,
                         "content": "-", "verdict": "无搜索结果"})
            continue
        books = r.get("books") or []
        content_ok = sum(1 for b in books if (b.get("content") or {}).get("ok", 0) > 0)
        total_ch = sum(b.get("chapters") or 0 for b in books)
        # 正文抽查未全过：ok<total（有失败章）或目录重试仍不可用(content=None→total=0)
        bad = [b for b in books
               if b.get("content") is None
               or (b.get("content") or {}).get("ok", 0) < (b.get("content") or {}).get("total", 1)]
        latest_bad = [b for b in books if b.get("latest_ok") is False]
        end_bad = [b for b in books if b.get("end_complete") is False]
        rows.append({"uid": uid, "name": r.get("_name", uid),
                     "search_ok": r["search"]["ok"], "cand": r["search"]["total_results"],
                     "toc_ok": r["toc_ok"], "books": len(books), "total_ch": total_ch,
                     "content": f"{content_ok}/{len(books)}", "bad": bad,
                     "latest_bad": latest_bad, "end_bad": end_bad})
    md.append("| 书源 | 搜索 | 候选 | toc | 测书(章) | 正文 | 连载最新✗ | 完本缺✗ | 结论 |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        verdict = "✅" if (row.get("search_ok") and row.get("toc_ok")
                           and row.get("books") and not row.get("bad")
                           and not row.get("latest_bad") and not row.get("end_bad")) else "⚠️"
        md.append(f"| {row['name'][:14]} | {row.get('search_ok')} | "
                  f"{row.get('cand')} | {row.get('toc_ok')} | {row.get('books')} "
                  f"({row.get('total_ch', 0)}章) | {row.get('content')} | "
                  f"{len(row.get('latest_bad') or [])} | {len(row.get('end_bad') or [])} | {verdict} |")
    md.append("")
    md.append("## 问题清单")
    for row in rows:
        for b in row.get("bad") or []:
            c = b.get("content") or {}
            if b.get("content") is None:
                detail = "目录重拉3次仍不可用（正文未测）"
            else:
                detail = f"{c.get('ok',0)}/{c.get('total',1)} 章通过 " + \
                         json.dumps(c.get("samples", [])[:2], ensure_ascii=False)[:130]
            md.append(f"- ❌ **{row['name']}** 《{b.get('name', '?')[:24]}》"
                      f"（{b.get('chapters')}章）正文抽查未全过: {detail}")
        for b in row.get("latest_bad") or []:
            md.append(f"- ⏳ **{row['name']}** 《{b.get('name', '?')[:24]}》连载最新检查失败："
                      f"目录最大章 {b.get('toc_max')} vs 源站声明 {b.get('last_decl')}")
        for b in row.get("end_bad") or []:
            md.append(f"- 📕 **{row['name']}** 《{b.get('name', '?')[:24]}》完本但不完整："
                      f"目录空洞 {b.get('holes')}（末章 {b.get('last_decl')}）")
        if row.get("books") == 0 and row.get("search_ok"):
            md.append(f"- ⚠️ **{row['name']}** 有搜索结果但候选书全部目录失败")
    open(REPORT_MD, "w", encoding="utf-8").write("\n".join(md))
    json.dump(all_results, open(REPORT_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return "\n".join(md)


def main():
    ap = argparse.ArgumentParser(description="书源长线测试（搜索/目录/正文）")
    ap.add_argument("--source", default="", help="只测指定 uid 的源")
    ap.add_argument("--books", type=int, default=10, help="每源测试书数（默认10）")
    ap.add_argument("--max-toc", type=int, default=18, help="每源目录测试候选上限")
    ap.add_argument("--no-resume", action="store_true", help="忽略断点全量重跑")
    args = ap.parse_args()

    srcs = load_enabled()
    if args.source:
        srcs = [s for s in srcs if args.source in (s.get("uid") or "")
                or args.source in (s.get("bookSourceName") or "")]
    print(f"参与测试源: {len(srcs)} 个", flush=True)
    for s in srcs:
        print(f"  - {s.get('uid')} ({s.get('bookSourceName')})", flush=True)

    done = set() if args.no_resume else load_progress()
    all_results = {}
    for i, s in enumerate(srcs):
        uid = s.get("uid", "")
        if uid in done:
            print(f"[{i+1}/{len(srcs)}] 跳过已完成: {uid}", flush=True)
            continue
        print(f"\n[{i+1}/{len(srcs)}] === 测试 {uid} ({s.get('bookSourceName')}) ===",
              flush=True)
        t0 = time.time()
        try:
            st = SourceTest(s, books_target=args.books, max_toc=args.max_toc)
            r = st.run()
            if r is not None:
                r["_name"] = st.name
            all_results[uid] = r
            done.add(uid)
            save_progress(done)
            print(f"  → 完成 {uid}，耗时 {int(time.time()-t0)}s", flush=True)
        except Exception as e:
            print(f"  → 源级异常 {uid}: {type(e).__name__}: {str(e)[:120]}",
                  flush=True)
            all_results[uid] = None
            done.add(uid)
            save_progress(done)
    report = gen_report(all_results)
    print("\n" + "=" * 70)
    print(report)
    print(f"\n报告已写入: {REPORT_MD}")


if __name__ == "__main__":
    main()
