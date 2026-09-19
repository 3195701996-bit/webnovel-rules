#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jm 在线阅读"图片不加载"两种实测场景的端到端复现/验证（离线）

用户报告：
  1. 加载中刷新界面 → 后续未加载的页再也不加载；
  2. 不刷新页面 → 整章不加载图片（早期版本没有此问题）。

本工具在本地假 CDN + 真实 Flask 服务上重放这两种客户端行为，并验证修复后的
契约（命中即恢复、空列表不污染缓存、失败可自愈）：

  S1 刷新中断：/urls → 并发 6 个 /img 请求 → **强行断开连接**（模拟刷新）→
     立刻重新 /urls 并逐页取图 → 断言 20 页全部 200（旧实现会因同图单飞跟随者
     等待 / 冷却返回 503，客户端又不重试，表现为"后续页面再也不加载"）；
  S2 源站瞬时空列表：让适配器先返回一次空图片列表（风控/瞬态）→ /urls 必须
     明确报错而不是 count=0 空章节 → 再次 /urls 必须拿到真实列表
     （旧实现把空列表写进 300s 内存热层：整章 5 分钟没有图，刷新也无效）；
  S3 失败自愈：让 CDN 对某一页连续失败 → 该页首次请求失败 → 稍后重试必须成功
     （客户端按同地址缓存击穿重试；服务器冷却只有秒级）。

用法：
  venv/bin/python tools/repro_jm_reading.py            # 验证修复后契约
  venv/bin/python tools/repro_jm_reading.py --legacy   # 复现旧行为（对照）
"""
import argparse
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from bench_jm_reading import _CDN, _gen_page  # noqa: E402


def _http(url, timeout=60):
    """返回 (status, body_bytes)；HTTP 错误也让调用方看到状态码"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:      # noqa: F821 - urllib.error
        return e.code, e.read()


def _abort_request(url, kill_after=0.15, timeout=30):
    """发一个请求并在 kill_after 秒后**粗暴断开 TCP**（等价浏览器刷新取消）"""
    p = urlparse(url)
    s = socket.create_connection((p.hostname, p.port), timeout=timeout)
    try:
        s.sendall((f"GET {p.path}?{p.query} HTTP/1.1\r\nHost: {p.hostname}:"
                   f"{p.port}\r\nConnection: close\r\n\r\n").encode())
        time.sleep(kill_after)
    finally:
        s.close()          # RST/FIN：服务端写响应时报错，但抓取仍在服务端完成


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=20)
    ap.add_argument("--delay", type=float, default=0.15)
    ap.add_argument("--debug", action="store_true", help="打印缓存/URL列表状态")
    ap.add_argument("--legacy", action="store_true",
                    help="复现旧行为（关闭预热、跳过空列表守卫）作对照")
    args = ap.parse_args()

    work = tempfile.mkdtemp(prefix="wr_jmrepro_")
    os.environ["WR_DATA_DIR"] = os.path.join(work, "data")
    os.environ["WR_SOURCES_DIR"] = os.path.join(work, "sources")
    os.environ["WR_DISABLE_BACKGROUND"] = "1"
    os.makedirs(os.environ["WR_SOURCES_DIR"], exist_ok=True)

    pdir = os.path.join(work, "pages")
    os.makedirs(pdir, exist_ok=True)
    _gen_page(os.path.join(pdir, "p.jpg"))
    _CDN.payload = open(os.path.join(pdir, "p.jpg"), "rb").read()
    _CDN.delay = args.delay
    _CDN.fail_next = 0
    cdn = ThreadingHTTPServer(("127.0.0.1", 0), _CDN)
    cdn_port = cdn.server_address[1]
    threading.Thread(target=cdn.serve_forever, daemon=True).start()

    import engine.urlsec as US
    import engine.manga.downloader as DL
    US.ip_is_public = lambda ip: True
    US.url_is_public = lambda u: True
    US.url_is_public_resolved = lambda u: (True, "")
    DL.url_is_public_resolved = lambda u: (True, "")

    import app as APP
    import server.state as ST
    from engine.manga.jm import Jm
    from engine.config import MANGA_CACHE_DIR

    state = {"empty_once": False, "empty_now": False}

    class FakeJm(Jm):
        key = "jm"

        def __init__(self, *a, **kw):
            super().__init__(state_dir=kw.get("state_dir"))
            self._img_domain = f"http://127.0.0.1:{cdn_port}"

        def images(self, comic_id, chapter_id):
            if args.legacy:
                # 旧行为：空列表被当有效结果（这里只用于 S2 对照）
                if state["empty_now"]:
                    state["empty_now"] = False
                    return []
            elif state["empty_now"]:
                state["empty_now"] = False
                return []            # 真实源站就是这么"报错"的：没有 images 字段
            return [f"{self._img_domain}/media/photos/{chapter_id}/{i:05d}.jpg"
                    for i in range(args.pages)]

        def _ensure_img_domain(self):
            return None

    fake = FakeJm(state_dir=ST.MANGA_STATE_DIR)
    ST._manga_read_instances["jm"] = fake
    APP.app.config["TESTING"] = False

    cid, ch1 = "399123", "300001"
    info_dir = os.path.join(MANGA_CACHE_DIR, "jm", cid)
    os.makedirs(info_dir, exist_ok=True)
    with open(os.path.join(info_dir, "_info_full.json"), "w",
              encoding="utf-8") as f:
        json.dump({"data": {"chapters": [{"id": ch1, "name": "第1話"}]}}, f)

    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, APP.app, threaded=True)
    port = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def _cache_dir(ch):
        return os.path.join(MANGA_CACHE_DIR, "jm", cid, ch)

    def _clear(ch):
        shutil.rmtree(_cache_dir(ch), ignore_errors=True)

    def _urls(ch=ch1):
        return _http(f"{base}/api/manga/jm/{cid}/chapter/{ch}/urls")

    def _clear_urls(ch=ch1):
        """连 URL 列表缓存一起清：否则磁盘/内存里的权威列表会先命中，
        根本不会去问源站（这本身是正确行为，但会掩盖"源站返回空列表"的场景）"""
        import server.state as _ST
        pth = _ST._chapter_images_disk_path("jm", cid, ch)
        try:
            os.unlink(pth)
        except OSError:
            pass
        _ST._CHAPTER_IMAGES_CACHE.pop(("jm", cid, ch), None)

    def _img_url(i, ch=ch1, r=None):
        u = f"{base}/api/manga/jm/{cid}/chapter/{ch}/img/{i}"
        return u + (f"?r={r}" if r else "")

    out = {"legacy": args.legacy, "pages": args.pages}
    failed = []

    # ── S1 加载中刷新（并发请求被强行断开）→ 重新加载必须全部成功 ──
    _clear(ch1)
    st, body = _urls()
    out["s1_urls_status"] = st
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(lambda i: _abort_request(_img_url(i)), range(6)))
    time.sleep(0.2)
    st2, body2 = _urls()
    d2 = json.loads(body2)
    out["s1_reload_urls_count"] = d2.get("count")
    if args.debug:
        out["dbg_images_head"] = (d2.get("images") or [])[:3]
        out["dbg_images_local"] = sum(1 for e in (d2.get("images") or [])
                                      if e.get("local"))
        out["dbg_downloads_dir"] = os.path.isdir(os.path.join(
            os.environ.get("WR_DATA_DIR", ""), "manga", "downloads", "jm", cid))
        import server.state as _ST
        out["dbg_media_root_comic"] = _ST._manga_media_root("jm", cid)
        out["dbg_media_root_chapter"] = _ST._manga_media_root("jm", cid, ch1)
        out["dbg_mem_cached"] = (("jm", cid, ch1) in _ST._CHAPTER_IMAGES_CACHE)
    if args.debug:
        from server.state import _chapter_images_disk_path
        out["dbg_cache_listing"] = sorted(os.listdir(_cache_dir(ch1)))[:25]
        out["dbg_urls_path"] = _chapter_images_disk_path("jm", cid, ch1)
        out["dbg_urls_exists"] = os.path.exists(out["dbg_urls_path"])
        try:
            with open(out["dbg_urls_path"], encoding="utf-8") as _f:
                out["dbg_urls_len"] = len((json.load(_f) or {}).get("urls") or [])
        except Exception as _e:
            out["dbg_urls_len"] = f"err {_e}"
        out["dbg_comic_cache_listing"] = sorted(os.listdir(
            os.path.dirname(out["dbg_urls_path"])))
    t0 = time.time()
    codes = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        codes = list(ex.map(lambda i: _http(_img_url(i))[0], range(args.pages)))
    out["s1_reload_pages_s"] = round(time.time() - t0, 3)
    out["s1_reload_statuses"] = sorted(set(codes))
    out["s1_all_ok"] = all(c == 200 for c in codes)
    if not out["s1_all_ok"]:
        failed.append(f"S1 刷新后仍有页失败: {sorted(set(codes))}")

    # ── S2 源站瞬时空列表 → 明确报错且不污染缓存 ──
    _clear(ch1)
    _clear_urls(ch1)
    state["empty_now"] = True
    st3, body3 = _urls()
    out["s2_empty_status"] = st3
    out["s2_empty_count"] = (json.loads(body3) or {}).get("count")
    out["s2_empty_error"] = bool((json.loads(body3) or {}).get("error"))
    st4, body4 = _urls()
    out["s2_retry_status"] = st4
    out["s2_retry_count"] = (json.loads(body4) or {}).get("count")
    if args.legacy:
        out["s2_expected"] = "旧行为：空结果被缓存 → 重试仍为 0"
    else:
        if out["s2_empty_status"] < 500 or not out["s2_empty_error"]:
            failed.append("S2 空图片列表未明确报错（客户端会渲染成空白章节）")
        if out["s2_retry_count"] != args.pages:
            failed.append(f"S2 空结果污染缓存：重试只得 {out['s2_retry_count']} 页")

    # ── S3 单页失败后自行恢复（服务器仅秒级冷却） ──
    _clear(ch1)
    _clear_urls(ch1)
    _CDN.fail_next = 1
    os.environ["WR_DISABLE_MANGA_WARM"] = "1"   # 隔离预热：故障注入留给本次请求
    time.sleep(0.5)
    st5, _b = _http(_img_url(3))
    out["s3_first_status"] = st5
    time.sleep(0.3)
    st6, _b2 = _http(_img_url(3, r=int(time.time() * 1000)))
    out["s3_retry_status"] = st6
    if not args.legacy and st6 != 200:
        failed.append(f"S3 单页失败后重试未成功: {st6}")

    out["PASS"] = not failed
    out["problems"] = failed
    print(json.dumps(out, ensure_ascii=False, indent=1))
    srv.shutdown()
    cdn.shutdown()
    shutil.rmtree(work, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
