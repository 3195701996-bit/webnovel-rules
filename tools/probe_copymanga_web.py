#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拷贝漫画**网页通道**端到端探针（真实外站，属 P0-2 那类必须隔离执行的工具）。

## 它做什么

按"用户真实路径"走一遍网页通道，并把每一步的实测值打出来：

    搜索 → 详情/目录 → 章节图片直链 → 取一张图（校验真图不是占位）

走的是**适配器本身**（`engine.manga.copymanga.CopyManga`），不是旁路实现——
否则"探针过了、App 挂了"就毫无意义。

## 副作用边界（重要）

- **会访问真实外站**：一次运行的总请求数有硬上限（见 `--budget`，默认 8），
  单发、不并发、不重试轰炸；
- 建议隔离数据目录：`WR_DATA_DIR=/tmp/wr-copy-verify`（适配器会落风控/指纹状态，
  不该写进用户数据）；
- **不写书源、不写书库、不建任务**，只打印；
- 遇 `WebBlocked`（403/CF 挑战）**立即停止**，不换域硬打。

用法：
    WR_DATA_DIR=/tmp/wr-copy-verify ./venv/bin/python tools/probe_copymanga_web.py
    ... --keyword 剑来 --path-word shenmingyuchulian --budget 8
    ... --transport web      # 强制只走网页通道（默认 auto）
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BUDGET = {"used": 0, "limit": 8}


def _spend(n=1):
    _BUDGET["used"] += n
    if _BUDGET["used"] > _BUDGET["limit"]:
        raise RuntimeError("请求预算超限：%d/%d（探针不允许无限打源站）"
                           % (_BUDGET["used"], _BUDGET["limit"]))


def _line(ok, step, detail, ms=None):
    print("%s %-12s %s%s" % ("✓" if ok else "✗", step, detail,
                             "" if ms is None else "（%dms）" % ms))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", default="剑来")
    ap.add_argument("--path-word", default="", help="跳过搜索，直接测这部作品")
    ap.add_argument("--budget", type=int, default=8)
    ap.add_argument("--transport", default="web", choices=["web", "app", "auto"])
    args = ap.parse_args()
    _BUDGET["limit"] = max(3, int(args.budget))
    os.environ["WR_COPY_TRANSPORT"] = args.transport

    dd = os.environ.get("WR_DATA_DIR", "")
    print("数据目录：%s" % (dd or "⚠ 未隔离（建议 WR_DATA_DIR=/tmp/wr-copy-verify）"))
    print("通道：WR_COPY_TRANSPORT=%s · 请求预算：%d" % (args.transport, _BUDGET["limit"]))
    print("-" * 74)

    from engine.manga.copymanga import CopyManga
    ad = CopyManga()

    # 1) 搜索
    pw = args.path_word
    if not pw:
        _spend()
        t0 = time.time()
        try:
            hits = ad.search(args.keyword)
        except Exception as e:                                  # noqa: BLE001
            _line(False, "搜索", "%s: %s" % (type(e).__name__, str(e)[:160]),
                  int((time.time() - t0) * 1000))
            return 2
        ms = int((time.time() - t0) * 1000)
        _line(bool(hits), "搜索", "「%s」命中 %d 部" % (args.keyword, len(hits)), ms)
        for c in (hits or [])[:5]:
            print("      · %-28s %s" % (c.title[:28], c.id))
        if not hits:
            print("  ⇒ 搜索 0 结果：先确认网络能到源站（探针不会把空结果算成功）")
            return 2
        pw = hits[0].id

    # 2) 详情 + 目录
    _spend()
    t0 = time.time()
    try:
        info = ad.comic_info(pw)
    except Exception as e:                                      # noqa: BLE001
        _line(False, "详情", "%s: %s" % (type(e).__name__, str(e)[:160]),
              int((time.time() - t0) * 1000))
        return 3
    ms = int((time.time() - t0) * 1000)
    _line(True, "详情", "%s / 作者 %s / 标签 %s / 封面 %s"
          % (info.title[:20], (info.author or "—")[:12],
             ",".join((info.tags or [])[:3]) or "—",
             "有" if info.cover else "无"), ms)
    chs = list(info.chapters or [])
    _line(bool(chs), "目录", "%d 话（分组：%s）"
          % (len(chs), "、".join(sorted({c.group for c in chs if c.group})) or "—"))
    if not chs:
        print("  ⇒ 目录为空：网页通道章节接口返回空，需按「页面结构已变更」排查")
        return 3
    print("      首话：%s（%s）" % (chs[0].name, chs[0].id))
    print("      末话：%s（%s）" % (chs[-1].name, chs[-1].id))

    # 3) 章节图片直链
    _spend()
    t0 = time.time()
    try:
        imgs = ad.images(pw, chs[0].id)
    except Exception as e:                                      # noqa: BLE001
        _line(False, "图片直链", "%s: %s" % (type(e).__name__, str(e)[:160]),
              int((time.time() - t0) * 1000))
        return 4
    ms = int((time.time() - t0) * 1000)
    _line(bool(imgs), "图片直链", "%d 张" % len(imgs), ms)
    for u in (imgs or [])[:3]:
        print("      · %s" % u[:110])
    if not imgs:
        return 4

    # 4) 真取第一张（判"真图/占位"用的是字节数与魔数，不是 HTTP 200）
    _spend()
    import requests
    t0 = time.time()
    try:
        r = requests.get(imgs[0], headers=ad.image_headers(imgs[0]), timeout=25)
        head = r.content[:4]
        is_img = head[:2] == b"\xff\xd8" or head[:4] == b"\x89PNG" \
            or head[:4] == b"RIFF" or r.content[:6] in (b"GIF87a", b"GIF89a")
        _line(bool(r.ok and is_img), "取首图",
              "HTTP %s · %d 字节 · %s · 魔数=%r"
              % (r.status_code, len(r.content),
                 r.headers.get("content-type", "")[:24], head),
              int((time.time() - t0) * 1000))
        if not (r.ok and is_img):
            return 5
    except Exception as e:                                      # noqa: BLE001
        _line(False, "取首图", "%s: %s" % (type(e).__name__, str(e)[:160]))
        return 5

    print("-" * 74)
    print("汇总：四段全部通过；本次请求 %d 次（预算 %d）"
          % (_BUDGET["used"], _BUDGET["limit"]))
    print("注：这只证明**当前这台机器、当前网络**可用；手机真机结论必须另行记录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
