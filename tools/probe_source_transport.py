#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐源"降级是否真的必要"探针（离线可重复，2026-09-17 新增）。

## 为什么要它

`server/capabilities.py` 的 `image_soft: ["curl_cffi"]` 口径是"缺 curl_cffi 就标
**degraded**"。于是**手机端（APK 没有 curl_cffi）几乎每个源都是 degraded**——
但"降级"是否真的影响可用性，从来没有逐源量过。本脚本把这件事变成一张事实表：

对每个已注册的漫画源量四件事：
  1. **可达性**：直连 / 经本地代理，哪个通；
  2. **API 通道**：`requests` 与 `curl_cffi` 分别搜索一次，比较状态与耗时；
  3. **图片通道**：同一张图分别用 `requests` 与 `curl_cffi` 拉取，比较状态/字节/耗时；
  4. **结论**：该源在"没有 curl_cffi"的 Android 上到底是
     `same`（无差别，degraded 标签是过度保守）/ `worse`（真的更差）/ `broken`（不可用）。

用法：
    PYTHONDONTWRITEBYTECODE=1 ./venv/bin/python tools/probe_source_transport.py \
        [--proxy http://127.0.0.1:7897] [--source jm] [--timeout 15]

输出为纯文本表格，可直接贴进报告归档。**只读**：不写缓存、不改任何状态。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KEYWORDS = {"copymanga": "巨人", "copymanga_web": "巨人", "jm": "汉化",
            "mangadex": "giant", "nhentai": "school", "baozi": "巨人",
            "komiic": "剑", "mxs": "剑", "ykmh": "剑", "zaimanhua": "剑"}


def _has_curl():
    try:
        import curl_cffi  # noqa: F401
        return True
    except Exception:
        return False


def _timed(fn):
    t0 = time.time()
    try:
        v = fn()
        return True, int((time.time() - t0) * 1000), v, ""
    except Exception as e:                                    # noqa: BLE001
        return False, int((time.time() - t0) * 1000), None, \
            "%s: %s" % (type(e).__name__, str(e)[:90])


def _requests_get(url, headers, timeout, proxy=None):
    import requests
    from requests.adapters import HTTPAdapter
    sess = getattr(_requests_get, "_sess", None)
    if sess is None:
        sess = requests.Session()
        sess.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=8))
        sess.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=8))
        _requests_get._sess = sess
    if proxy:
        sess.proxies.update({"http": proxy, "https": proxy})
    return sess.get(url, headers=headers, timeout=timeout)


def _curl_get(url, headers, timeout, proxy=None):
    from curl_cffi import requests as creq
    return creq.get(url, headers=headers, impersonate="chrome",
                    timeout=timeout, proxies=({"http": proxy, "https": proxy}
                                              if proxy else None))


def probe(source, keyword, timeout=15, proxy=None):
    from server.state import _load_manga_adapters, _manga_adapter, \
        _manga_read_adapter
    from engine.manga.downloader import fetch_image_checked
    _load_manga_adapters()
    ad = _manga_read_adapter(source) or _manga_adapter(source)
    out = {"source": source}
    if ad is None:
        out["verdict"] = "未注册"
        return out
    out["name"] = getattr(ad, "name", source)

    # 1) API：requests vs curl_cffi（走适配器自身，保证头/参数真实）
    ok_r, ms_r, res_r, err_r = _timed(lambda: ad.search(keyword))
    out["api_requests"] = {"ok": ok_r, "ms": ms_r,
                           "n": len(res_r or []) if ok_r else 0, "err": err_r}
    out["cid"] = str(res_r[0].id) if ok_r and res_r else ""

    # 2) 图片：同一张图两条传输各拉一次
    if out["cid"]:
        ok_d, ms_d, d, err_d = _timed(lambda: ad.comic_info(out["cid"]))
        out["detail"] = {"ok": ok_d, "ms": ms_d,
                         "chapters": len(getattr(d, "chapters", []) or []) if ok_d else 0,
                         "err": err_d}
        if ok_d and d.chapters:
            ok_i, ms_i, imgs, err_i = _timed(lambda: ad.images(out["cid"], d.chapters[0].id))
            out["chapter"] = {"ok": ok_i, "ms": ms_i, "n": len(imgs or []),
                              "err": err_i}
            if ok_i and imgs:
                url = imgs[0]
                try:
                    hdr = dict(ad.image_headers(url) or {})
                except Exception:
                    hdr = {}
                r1 = _timed(lambda: fetch_image_checked(url, hdr, timeout=timeout))
                out["img_requests"] = {"ok": r1[0], "ms": r1[1],
                                       "bytes": len(getattr(r1[2], "content", b"") or b"")
                                       if r1[0] else 0, "err": r1[3]}
                if _has_curl():
                    r2 = _timed(lambda: _curl_get(url, hdr, timeout, proxy))
                    out["img_curl"] = {"ok": r2[0],
                                       "ms": r2[1],
                                       "bytes": len(getattr(r2[2], "content", b"") or b"")
                                       if r2[0] else 0, "err": r2[3]}

    # 3) 结论：手机端（无 curl_cffi）与该源在桌面的差异
    ir = out.get("img_requests") or {}
    ic = out.get("img_curl") or {}
    if not ir:
        out["verdict"] = "未测到图片（前置阶段失败）"
    elif not ir.get("ok"):
        out["verdict"] = "broken（requests 取图失败）"
    elif ic and ic.get("ok"):
        ratio = (ir.get("ms") or 0) / max(1, ic.get("ms") or 1)
        out["verdict"] = ("same（两条通道都可用，耗时比 %.2fx）" % ratio
                          if ratio < 2.0 else
                          "worse（requests 明显更慢，%.2fx）" % ratio)
    elif ic:
        out["verdict"] = "worse（curl_cffi 可用而 requests 失败）"
    else:
        out["verdict"] = "same（无 curl_cffi 对照，requests 可用）"
    return out


def main():
    from _probe_guard import ensure_isolated  # noqa: E402
    ensure_isolated()
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", action="append", default=None)
    ap.add_argument("--proxy", default=os.environ.get("WR_PROBE_PROXY", ""),
                    help="本地代理（部分源在本机需代理才可达）")
    ap.add_argument("--timeout", type=int, default=15)
    args = ap.parse_args()
    sources = args.source or ["jm", "mangadex", "nhentai", "baozi",
                              "copymanga", "copymanga_web"]
    print("逐源传输能力探针（requests = Android 实际走的通道）")
    print("代理：%s | 本机有 curl_cffi：%s" % (args.proxy or "（直连）", _has_curl()))
    print("")
    for s in sources:
        try:
            d = probe(s, KEYWORDS.get(s, "剑来"), timeout=args.timeout,
                      proxy=args.proxy or None)
        except Exception as e:                                # noqa: BLE001
            print("%-16s 探针异常 %s: %s" % (s, type(e).__name__, str(e)[:80]))
            continue
        print("=== %s（%s）" % (d.get("source"), d.get("name", "?")))
        a = d.get("api_requests") or {}
        print("  API   requests : ok=%-5s %6s ms  命中 %-3s %s"
              % (a.get("ok"), a.get("ms"), a.get("n"), a.get("err") or ""))
        for key, label in (("detail", "详情"), ("chapter", "章节"),
                           ("img_requests", "图片 requests"),
                           ("img_curl", "图片 curl_cffi")):
            v = d.get(key)
            if not v:
                continue
            print("  %-6s %-14s ok=%-5s %6s ms  %s %s"
                  % ("", label, v.get("ok"), v.get("ms"),
                     ("%s 字节" % v["bytes"]) if v.get("bytes") else
                     ("%s 章/张" % (v.get("chapters") or v.get("n") or "")),
                     v.get("err") or ""))
        print("  ⇒ 结论：%s" % d.get("verdict"))
        print("")


if __name__ == "__main__":
    main()
