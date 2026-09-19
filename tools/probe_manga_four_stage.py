#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐源四段实测（真实外站）· 桌面证据采集器。

## 与 App 里「逐源校验」的关系

App 里的漫画源校验走的是同一个引擎函数 `engine.manga.verify.verify_one`，
只是结果**落在设备自己的数据目录**（手机上一份、桌面上一份，互不覆盖）。
本脚本是它的**桌面驱动器**：同口径、可复现、输出纯文本，便于贴进报告归档。

四段契约（与指南 P0-1 一致）：

    搜索 → 详情/目录 → 章节图片 → 真实取一张图（校验是真图，不是占位/坏页）

## 副作用边界（重要）

- **会访问真实外站**：每个源约 4~6 个请求，单线程串行，不做压力测试；
- 请务必隔离数据目录：`WR_DATA_DIR=/tmp/wr-verify-$(date +%s)`——
  适配器会写风控/指纹/验证结果状态，不该污染用户数据；
- **不写书源、不写书库、不建下载任务**；
- 结论只代表**这台机器 + 当前网络**。要写"Android 已验证"必须在目标手机上跑
  （指南 §2.5：依赖满足、桌面跑通都不等于手机可用）。

用法：
    WR_DATA_DIR=/tmp/wr-verify ./venv/bin/python tools/probe_manga_four_stage.py
    ... --keys copymanga jm --json         # 只测指定源 / additionally 输出 JSON
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 默认只测"不依赖桌面浏览器"的源：Playwright 通道不在手机承诺内（指南 §5 停止项）
DEFAULT_KEYS = ("copymanga", "jm", "mangadex", "baozi", "nhentai")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", nargs="*", default=list(DEFAULT_KEYS))
    ap.add_argument("--keyword", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    dd = os.environ.get("WR_DATA_DIR", "")
    print("数据目录：%s" % (dd or "⚠ 未隔离！建议 WR_DATA_DIR=/tmp/wr-verify"))
    if not dd:
        print("  （本脚本会写适配器状态与验证结果；未隔离时会写进仓库 data/）")
    print("网络：直连（如需代理请设 WR_PROXY / WR_PROXY_FILE）")
    print("时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("-" * 78)

    # 注册表是**懒加载**的（服务端首次用到漫画源时才注册）。不先加载就会出现
    # "适配器未注册"这种假失败——第一次跑本脚本就踩到了，记在这里免得复发。
    try:
        from server.state import _load_manga_adapters
        _load_manga_adapters()
    except Exception as e:                                       # noqa: BLE001
        print("⚠ 适配器加载失败：%s: %s" % (type(e).__name__, e))

    from engine.manga import verify as mv
    rows = []
    for key in args.keys:
        t0 = time.time()
        try:
            r = mv.verify_one(key, keyword=(args.keyword or None))
        except Exception as e:                                   # noqa: BLE001
            r = {"key": key, "status": "error", "reason": "%s: %s"
                 % (type(e).__name__, str(e)[:120]), "stages": {}}
        r["_ms"] = int((time.time() - t0) * 1000)
        rows.append(r)
        st = r.get("status") or "?"
        mark = {"verified": "✓", "partial": "△", "failed": "✗",
                "unsupported": "—"}.get(st, "?")
        print("%s %-12s %-10s %5dms  %s" % (mark, key, st, r["_ms"],
                                            (r.get("reason") or "")[:60]))
        for name in ("search", "detail", "images", "first_image"):
            info = (r.get("stages") or {}).get(name)
            if not info:
                continue
            ok = info.get("ok")
            print("      %-11s %s %5sms  %s" % (
                name, "ok " if ok else "FAIL",
                info.get("ms", "—"), str(info.get("detail") or "")[:70]))
    print("-" * 78)
    ok = sum(1 for r in rows if r.get("status") == "verified")
    part = sum(1 for r in rows if r.get("status") == "partial")
    bad = [r for r in rows if r.get("status") not in ("verified", "partial")]
    print("汇总：共 %d 源 · verified %d · partial %d · 未通过 %d"
          % (len(rows), ok, part, len(bad)))
    if bad:
        print("未通过的源（逐条判断是源站问题还是解析退化，不要一概而论）：")
        for r in bad:
            print("  · %-12s %s" % (r.get("key"), (r.get("reason") or "")[:90]))
    print("注：这是**桌面当前网络**的结论，不等价于目标手机可用（指南 §2.5）。")
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
