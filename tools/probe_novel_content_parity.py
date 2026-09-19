# -*- coding: utf-8 -*-
"""小说正文**等长普查**：同一本书、同一章，跨源比字数，抓截断/串章/内容错。

**抽样密度的坑（实测教训）**：默认 `--pick 6` 对 1300 章的书是"每 180 章取 1 点"，
**窄带缺陷会被跳过**——精华书阁《剑来》第 1188~1309 章（122 章 = 9.3%）正文被站点
换成了别的作品，`--pick 6/7` 两轮都判"0 条可疑"（抽样点恰好落在正常区间与末章上）。
长书查内容质量请用 `--pick 20` 以上（每 ~65 章 1 点），或对可疑区间单独加密扫描。

**按"章名核心"对齐**（去掉"第X章/节/回…"前缀后的标题，如"太阳和野草"），
不是按位置也不是按章号：
  · 按**位置**对齐会因各源粒度不同而误报——实测 yuzhaiwuh 把《剑来》拆成
    2666"节"（每节约半章），同一位置在它那里与别的源不是同一章（首版工具
    就因此把 4157 字误报成"疑似截断"，实为该源正常的半章粒度）；
  · 按**章号**对齐也不行：章号本身有错号/重号，且不同源编号体系不同。
标题是唯一跨源稳定且用户可辨认的锚点。

判据（与 `tools/probe_novel_sources.py --compare-chapter` 同一口径）：
  · 显著低于中位（< 0.7×）= 疑似**分页没取全/静默截断**；
  · 显著高于中位（> 1.6×）= 疑似**跟随分页链接走出本章**，把后续章节拼进来
    （实测精华书阁曾把 5.8k 的章存成 24.9k）；
  · 只差百分之几 = 源站编辑差异，正常。

用法：
  WR_PROFILE=mobile ./venv/bin/python tools/probe_novel_content_parity.py \
      --keyword 剑来 --positions 60,650,1200 --timeout 25
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SEED_POLICY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "android", "mobile-seed-policy.json")
DEFAULT_KW = "剑来"


def enabled_sources():
    """手机口径的启用源（内置源 − 种子策略停用；同 uid 去重）——与 APK 一致"""
    from engine.source_mgr import load_all
    try:
        pol = json.load(open(SEED_POLICY, encoding="utf-8"))
        disabled = set((pol.get("disable") or {}).keys())
    except Exception as e:                                       # noqa: BLE001
        print(f"⚠ 读不到种子策略（{e}），按文件 enabled 字段")
        disabled = set()
    srcs, seen = [], set()
    for s in (load_all() or []):
        uid = s.get("uid") or ""
        if uid in disabled or uid in seen:
            continue
        seen.add(uid)
        srcs.append(s)
    return srcs, disabled


def title_key(name):
    """章名核心：去掉"第X章/节/回/话/集/篇"前缀与卷前缀，留下标题本身。
    取不到标题时退回整名（仍可用于对齐，只是精度差）。"""
    n = re.sub(r"^\s*第\s*[0-9零〇一二三四五六七八九十百千两]+\s*[章节節回话話集篇]\s*", "", name or "")
    n = re.sub(r"^\s*第[0-9零〇一二三四五六七八九十百千两]+卷\s*", "", n)
    n = re.sub(r"\s+", "", n)
    return n or re.sub(r"\s+", "", name or "")


PICK = 6          # 每源抽多少章（首章 + 等距）
def parity(sources, keyword, timeout, pick=PICK, frac=(0.0, 1.0)):
    """逐源取数：返回 {uid: {title_key: {...}}}，供跨源按标题对齐"""
    from engine.crawler import SourceCrawler
    out = {}
    for s in sources:
        uid = s.get("uid") or ""
        row = {"title": s.get("bookSourceName") or "", "chapters": 0,
               "samples": {}, "stage": "", "reason": ""}
        try:
            c = SourceCrawler(s)
            hits = c.search(keyword) or []
            exact = [h for h in hits if (h.get("name") or "").strip() == keyword]
            pick_hit = (exact or hits or [None])[0]
            if not pick_hit:
                row.update(stage="搜索", reason="0 结果")
                out[uid] = row
                continue
            # 预算必须显式传下去：默认目录 deadline 较小，实测分页目录源
            # （精华书阁 66 页）会撞 "request deadline exceeded" 而整源无结论。
            book = c.get_book(pick_hit.get("book_url") or pick_hit.get("url"),
                              fast=True, deadline=timeout * 2)
            chs = c.get_toc(book, timeout=timeout, deadline=timeout * 4) or []
            row["chapters"] = len(chs)
            if not chs:
                row.update(stage="目录", reason="目录为空")
                out[uid] = row
                continue
            n = len(chs)
            # 抽样区间：缺陷常集中在后段（实测站点把尾部若干章换成别的书），
            # 全量铺开取样反而会漏掉窄带，故支持 --frac-from/--frac-to 只扫那一段。
            lo = max(0, min(n - 1, int(n * frac[0])))
            hi = max(lo, min(n - 1, int(n * frac[1]) - 1))
            span = hi - lo + 1
            step = max(1, span // pick)
            idxs = sorted(set(list(range(lo, hi + 1, step))[:pick] + [n - 1]))
            row["range"] = [lo + 1, hi + 1]
            # 抽样前先统计全书同标题的出现次序：书里可能有**重复标题**
            # （实测精华书阁《剑来》尾部 idx≥1226 是"目录重启块"：第6章 老酒、
            #  第14章 野草… 与开头章名重复）。不区分次序就会把"开头的正常章"
            #  与"尾部的重启块"混成同一组比较，把差异平均掉 → 漏报。
            _occur = {}
            _seq_of = {}
            for _i, _c in enumerate(chs):
                _k = title_key(_c.get("name"))
                _occur[_k] = _occur.get(_k, 0) + 1
                _seq_of[_i] = _occur[_k]
            for i in idxs:
                ch = chs[i]
                key = f"{title_key(ch.get('name'))}#{_seq_of.get(i, 1)}"
                t0 = time.time()
                try:
                    txt = c.get_content(ch["url"], timeout=timeout,
                                        deadline=timeout * 2) or ""
                    row["samples"][key] = {
                        "index": i + 1, "name": ch.get("name", ""), "chars": len(txt),
                        "ms": int((time.time() - t0) * 1000),
                        "head": re.sub(r"\s+", " ", txt)[:40],
                        "tail": re.sub(r"\s+", " ", txt).strip()[-40:],
                    }
                except Exception as e:                           # noqa: BLE001
                    row["samples"][key] = {
                        "index": i + 1, "name": ch.get("name", ""),
                        "error": f"{type(e).__name__}: {str(e)[:70]}",
                        "ms": int((time.time() - t0) * 1000)}
        except Exception as e:                                   # noqa: BLE001
            row.update(stage=row["stage"] or "异常",
                       reason=f"{type(e).__name__}: {str(e)[:70]}")
        out[uid] = row
        got = [v.get("chars") or 0 for v in row["samples"].values()]
        print(f"  {uid[:34]:34s} 章 {row['chapters'] or '—'!s:5s} 抽样 "
              f"{'/'.join(str(x) if x else '✗' for x in got) or '—'}")
    return out


def analyze(rows, lo=0.7, hi=1.6):
    """纯函数：把逐源样本比对结果算成结论（便于单测，不依赖当天站点状态）。

    返回 {"suspects": [...], "thin_titles": n, "covered_titles": n,
          "verdict": [(bad, uid, chapters, range, got_n, err_n), ...],
          "no_data": [uid, ...]}
    判据：同一标题（含"第几次出现"）在 ≥3 个源之间比字数，显著偏短/偏长即可疑。
    **没有样本的源不算"正常"**——旧输出把取数失败印成"✓ 正常"，
    是被本仓库反复修掉的那类"假绿灯"。"""
    keys = {}
    for uid, row in rows.items():
        for k, v in (row.get("samples") or {}).items():
            if v.get("chars"):
                keys.setdefault(k, []).append((uid, v["chars"], v.get("name", ""), v))
    suspects = []
    for k, vals in keys.items():
        if len(vals) < 3:
            continue
        srt = sorted(v[1] for v in vals)
        med = srt[len(srt) // 2]
        if not med:
            continue
        for uid, n, name, smp in vals:
            if n < med * lo:
                suspects.append({"title": k, "uid": uid, "chars": n, "median": med,
                                 "why": "明显偏短", "name": name, "sample": smp})
            elif n > med * hi:
                suspects.append({"title": k, "uid": uid, "chars": n, "median": med,
                                 "why": "明显偏长", "name": name, "sample": smp})
    judged_uids = {u for k, vals in keys.items() if len(vals) >= 3 for u, *_ in vals}
    verdict, no_data = [], []
    for uid, row in rows.items():
        got_n = len([v for v in (row.get("samples") or {}).values() if v.get("chars")])
        err_n = len([v for v in (row.get("samples") or {}).values() if v.get("error")])
        bad = sum(1 for s in suspects if s["uid"] == uid)
        verdict.append((bad, uid, row.get("chapters") or 0, row.get("range") or [0, 0],
                        got_n, err_n))
        if got_n == 0:
            no_data.append(uid)
    verdict.sort(key=lambda t: (-t[0], t[1]))
    thin = [k for k, v in keys.items() if len(v) < 3]
    return {"suspects": suspects, "thin_titles": len(thin),
            "covered_titles": len(keys), "verdict": verdict, "no_data": no_data,
            "judged_uids": sorted(judged_uids)}


def report(rows):
    """按标题对齐打印逐条可疑与逐源结论（判定逻辑在 analyze()）"""
    res = analyze(rows)
    print("\n" + "=" * 92)
    print(f"按章名核心对齐：可比对标题 {res['covered_titles']} 个")
    for sp in res["suspects"]:
        med = sp["median"]
        print(f"\n标题「{sp['title'].split('#')[0]}」"
              f"（第 {sp['title'].split('#')[-1]} 次出现）：中位 {med} 字")
        print(f"  ⚠ {sp['uid'][:34]:36s} {sp['chars']:7d} 字"
              f"（{sp['chars'] / med:.2f}× 中位）{sp['why']}  [{sp['name'][:22]}]")
        print(f"      头：{sp['sample']['head']!r}")
        print(f"      尾：{sp['sample']['tail']!r}")
    for uid, row in rows.items():
        for v in (row.get("samples") or {}).values():
            if v.get("error"):
                nm = (v.get("name") or "")[:18]
                print(f"  {uid[:34]:36s} 取数失败[{nm}]：{v['error'][:80]}")
        if row.get("reason"):
            stage = row.get("stage") or ""
            print(f"  {uid[:34]:36s} 未取到正文：{stage} {(row.get('reason') or '')[:80]}")
    print(f"\n可疑条目合计 {len(res['suspects'])} 条")
    print(f"可比对标题 {res['covered_titles']} 个 · 因覆盖不足（<3 源）未判定 "
          f"{res['thin_titles']} 个")
    print("\n逐源结论（抽样区间内异常章数）：")
    for bad, uid, chs, rng, got_n, err_n in res["verdict"]:
        if got_n == 0:
            tag = ("✗ 无结论（本次未取到正文"
                   + (f"，{err_n} 章失败" if err_n else "") + "）")
        elif bad:
            tag = f"⚠ {bad} 处异常（该源这部分内容不可信）"
        elif uid in set(res.get("judged_uids") or []):
            tag = "✓ 区间内正常"
        else:
            # 该源的样本**没有和任何 ≥3 源的同名章比过**（例如只跑了它一个源）——
            # 这时说"正常"是假绿灯，必须说清"没判定"。
            tag = "◇ 未判定（可比源不足 3 个）"
        print(f"  {uid[:34]:36s} 共 {chs} 章 · 区间 {rng[0]}~{rng[1]} · {tag}")


def _write_report(path, keyword, rows, frac):
    """把一次巡检写成 markdown（归档用：日期/关键词/区间/逐源结论/可疑明细）"""
    lines = [f"# 逐源内容巡检 · {time.strftime('%Y-%m-%d %H:%M')}",
             "",
             ("**方法**：同一本书、同一章名，跨源比字数（按**章名核心**对齐）。"
              "显著偏短 = 分页没取全或**站点内容错**；显著偏长 = 串入了后续章节。"),
             "",
             "| 项 | 值 |", "|---|---|",
             f"| 关键词 | `{keyword}` |",
             f"| 抽样区间 | 全书 {frac[0] * 100:.0f}% ~ {frac[1] * 100:.0f}% |",
             f"| 时间 | {time.strftime('%Y-%m-%d %H:%M:%S')} |",
             "", "## 逐源结果", "",
             "| 源 | 章数 | 抽样 | 判定 |", "|---|---|---|---|"]
    for uid, row in rows.items():
        got = [v for v in (row.get("samples") or {}).values() if v.get("chars")]
        errs = [v for v in (row.get("samples") or {}).values() if v.get("error")]
        lines.append(f"| `{uid}` | {row.get('chapters') or '—'} | "
                     f"{len(got)} 成功 / {len(errs)} 失败 | "
                     f"{row.get('reason') or ('取数正常' if got else '未取到')} |")
    got_rows = sum(1 for r in rows.values()
                   if any(v.get("chars") for v in (r.get("samples") or {}).values()))
    lines += ["", "## 本次覆盖", "",
              f"- 有正文样本的源：**{got_rows}/{len(rows)}**",
              "- 无样本的源**没有结论**（不代表正常）；某标题可比源 <3 个时也不判定。",
              "", "> 桌面结论，不等于目标机可用；可疑明细见运行输出。", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n巡检报告已写出：{path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", default=DEFAULT_KW)
    ap.add_argument("--pick", type=int, default=PICK,
                    help="每源抽样章数（区间内等距 + 末章）")
    ap.add_argument("--frac-from", type=float, default=0.0,
                    help="抽样区间起点（占全书比例，0~1）")
    ap.add_argument("--frac-to", type=float, default=1.0,
                    help="抽样区间终点（占全书比例，0~1）")
    ap.add_argument("--report", default="",
                    help="把结果写成 markdown 报告到该路径（便于归档成巡检记录）")
    ap.add_argument("--only", default="", help="只测 uid 含该子串")
    ap.add_argument("--timeout", type=float, default=25.0,
                    help="单阶段请求超时（目录/正文的实际 deadline 由它放大）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    srcs, disabled = enabled_sources()
    if args.only:
        srcs = [s for s in srcs if args.only in (s.get("uid") or "")]
    print(f"数据目录：{os.environ.get('WR_DATA_DIR') or '⚠ 未隔离'}")
    print(f"手机口径启用 {len(srcs)} 个源 · 种子策略停用 {len(disabled)} 项 · "
          f"关键词 {args.keyword} · 每源抽 {args.pick} 章")
    print(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 92)
    frac = (max(0.0, min(1.0, args.frac_from)), max(0.01, min(1.0, args.frac_to)))
    print(f"抽样区间：全书 {frac[0] * 100:.0f}% ~ {frac[1] * 100:.0f}%")
    rows = parity(srcs, args.keyword, args.timeout, pick=args.pick, frac=frac)
    report(rows)
    if args.report:
        _write_report(args.report, args.keyword, rows, frac)
    print("\n注：桌面结论，**不等于**目标手机可用（指南 §2.5）。")
    if args.json:
        print(json.dumps({"rows": rows}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
