#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""禁漫在线阅读链路基准（离线可复现）

真实 jm CDN 在受限环境不可达，本工具用**本地模拟 CDN**替代：可控的每请求
延迟与图片体积，其余链路（/urls → /img → 单飞 → ImageDownloader → 会话池
→ fetch_image_checked → PIL 还原 → 落盘 → 发送）全部走生产代码。

流程：
- 生成 N 页真实 JPEG（PIL），起本地 CDN（ThreadingHTTPServer，每请求 sleep
  指定延迟）
- 注入假 jm 适配器（继承真实 Jm，images() 指向本地 CDN、保留真实
  unscramble_image/image_headers/image_url），其余代码不动
- 用 werkzeug make_server(threaded=True) 起真实 HTTP 服务（隔离数据目录）
- 量测：/urls、冷首图、首屏 3 图、整章 20 图（并发 6）、整章热缓存、
  下一章首图（预取生效性）

用法：
  venv/bin/python tools/bench_jm_reading.py [--delay 0.15] [--pages 20] [--label before]
  venv/bin/python tools/bench_jm_reading.py --no-warm --label baseline   # A/B 基线
输出：JSON（stdout）+ 人类可读表（stderr）

指标（--no-warm 与默认对比即为本轮阅读提速的 A/B）：
  m2 拿到 /urls 后首屏 3 图耗时、m3 模拟 0.4s/页滚动阅读的累计"等图"时间、
  m4 整章热读、m5 下一话首图（下一话预热是否生效）、m6 单页冷/热中位
"""
import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


def _gen_page(path, size=(760, 1140), quality=82):
    from PIL import Image
    import random
    rnd = random.Random(hash(os.path.basename(path)) & 0xFFFF)
    im = Image.new("RGB", size)
    px = im.load()
    # 造有结构的内容：分块渐变 + 噪点（贴近真实漫画页体积/解码成本）
    for y in range(0, size[1], 4):
        for x in range(0, size[0], 4):
            v = (x * 255 // size[0], y * 255 // size[1], rnd.randrange(256))
            for dy in range(4):
                for dx in range(4):
                    if x + dx < size[0] and y + dy < size[1]:
                        px[x + dx, y + dy] = v
    im.save(path, "JPEG", quality=quality)


class _CDN(BaseHTTPRequestHandler):
    delay = 0.15
    payload = b""
    hits = 0
    fail_next = 0        # 接下来 N 个请求返回 500（故障注入，供复现工具用）

    def do_GET(self):
        p = urlparse(self.path).path
        if not p.startswith("/media/photos/"):
            self.send_response(404)
            self.end_headers()
            return
        time.sleep(self.delay)
        type(self).hits += 1
        if type(self).fail_next > 0:
            type(self).fail_next -= 1
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.payload
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _req(url, timeout=60):
    t0 = time.time()
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data = r.read()
    return time.time() - t0, len(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=0.15, help="模拟 CDN 每请求延迟(秒)")
    ap.add_argument("--pages", type=int, default=20)
    ap.add_argument("--chapter-pages", type=int, default=20,
                    help="两话各多少页")
    ap.add_argument("--concurrency", type=int, default=6, help="模拟浏览器并发")
    ap.add_argument("--label", default="run")
    ap.add_argument("--quiesce", type=float, default=4.0,
                    help="阶段间静默等待(秒)：让后台预热跑完再切下一阶段")
    ap.add_argument("--no-warm", action="store_true",
                    help="关闭服务器端预热(WR_DISABLE_MANGA_WARM=1)做 A/B")
    args = ap.parse_args()

    work = tempfile.mkdtemp(prefix="wr_jmbench_")
    os.environ["WR_DATA_DIR"] = os.path.join(work, "data")
    os.environ["WR_SOURCES_DIR"] = os.path.join(work, "sources")
    os.environ["WR_DISABLE_BACKGROUND"] = "1"
    if args.no_warm:
        os.environ["WR_DISABLE_MANGA_WARM"] = "1"
    os.makedirs(os.environ["WR_SOURCES_DIR"], exist_ok=True)

    # 1) 生成页面并起本地 CDN
    pdir = os.path.join(work, "pages")
    os.makedirs(pdir, exist_ok=True)
    img_path = os.path.join(pdir, "p.jpg")
    _gen_page(img_path)
    _CDN.payload = open(img_path, "rb").read()
    _CDN.delay = args.delay
    cdn = ThreadingHTTPServer(("127.0.0.1", 0), _CDN)
    cdn_port = cdn.server_address[1]
    threading.Thread(target=cdn.serve_forever, daemon=True).start()

    # 1b) 基准内放开回环地址：模拟 CDN 跑在 127.0.0.1，会被生产 SSRF 校验
    # 正确拦截（私网地址不得建连）。只在**基准进程**内放开，不改生产代码。
    import engine.urlsec as US
    import engine.manga.downloader as DL
    US.ip_is_public = lambda ip: True
    US.url_is_public = lambda u: True
    US.url_is_public_resolved = lambda u: (True, "")
    DL.url_is_public_resolved = lambda u: (True, "")

    # 2) 注入假 jm 适配器（保留真实还原/请求头逻辑）
    import app as APP
    import server.state as ST
    from engine.manga.jm import Jm

    class FakeJm(Jm):
        key = "jm"

        def __init__(self, *a, **kw):
            super().__init__(state_dir=kw.get("state_dir"))
            self._img_domain = f"http://127.0.0.1:{cdn_port}"

        def images(self, comic_id, chapter_id):
            return [f"{self._img_domain}/media/photos/{chapter_id}/"
                    f"{i:05d}.jpg" for i in range(args.chapter_pages)]

        def _ensure_img_domain(self):
            return None

    fake = FakeJm(state_dir=ST.MANGA_STATE_DIR)
    ST._manga_read_instances["jm"] = fake
    APP.app.config["TESTING"] = False

    # 章节顺序缓存（下一话预取依赖）
    from engine.config import MANGA_CACHE_DIR
    # jm 的 comic_id 必须是数字（normalize_id 语义）
    cid, ch1, ch2 = "399123", "300001", "300002"
    info_dir = os.path.join(MANGA_CACHE_DIR, "jm", cid)
    os.makedirs(info_dir, exist_ok=True)
    with open(os.path.join(info_dir, "_info_full.json"), "w",
              encoding="utf-8") as f:
        json.dump({"data": {"chapters": [{"id": ch1, "name": "第1話"},
                                         {"id": ch2, "name": "第2話"}]}}, f)

    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, APP.app, threaded=True)
    port = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def _cache_dir(ch):
        return os.path.join(MANGA_CACHE_DIR, "jm", cid, ch)

    def _clear(ch):
        shutil.rmtree(_cache_dir(ch), ignore_errors=True)

    def _urls(ch):
        return json.load(urllib.request.urlopen(
            f"{base}/api/manga/jm/{cid}/chapter/{ch}/urls", timeout=60))

    def _page(ch, i):
        return _req(f"{base}/api/manga/jm/{cid}/chapter/{ch}/img/{i}")

    def _load_batch(ch, idxs):
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            list(ex.map(lambda i: _page(ch, i), idxs))
        return time.time() - t0

    out = {"label": args.label, "delay": args.delay,
           "pages": args.chapter_pages, "concurrency": args.concurrency}

    def _urls_for(ch):
        return json.load(urllib.request.urlopen(
            f"{base}/api/manga/jm/{cid}/chapter/{ch}/urls", timeout=60))

    # ── M1. 无 /urls 的冷首图（纯 CDN + 还原）──
    _clear(ch1); _clear(ch2)
    t1, size_first = _page(ch1, 0)
    out["m1_cold_first_page_s"] = round(t1, 3)
    out["page_bytes"] = size_first

    # ── M2. /urls 后（预热窗口）首屏 3 图 ──
    _clear(ch1); _clear(ch2)
    t0 = time.time(); d = _urls_for(ch1); out["urls_s"] = round(time.time() - t0, 3)
    out["urls_count"] = d.get("count")
    time.sleep(1.5)                      # 阅读器加载/布局的用户思考窗口
    t_view = _load_batch(ch1, [0, 1, 2])
    out["m2_first_viewport_after_urls_s"] = round(t_view, 3)

    # ── M3. 模拟滚动阅读：按 0.4s/页 的节奏翻页，统计"等图卡顿" ──
    time.sleep(args.quiesce)             # 等 M2 触发的预热收尾，避免跨阶段干扰
    _clear(ch1); _clear(ch2)
    _urls_for(ch1)
    time.sleep(1.5)
    hits_before = _CDN.hits
    _stall = 0.0
    _max = 0.0
    for i in range(args.chapter_pages):
        _t, _ = _page(ch1, i)
        _stall += max(0.0, _t - 0.05)    # 超过 50ms 视为用户可感的等图
        _max = max(_max, _t)
        time.sleep(0.4)
    out["m3_scroll_total_stall_s"] = round(_stall, 3)
    out["m3_scroll_max_page_s"] = round(_max, 3)
    out["m3_cdn_hits"] = _CDN.hits - hits_before

    # ── M4. 整章热缓存（并发 6）──
    out["m4_chapter_warm_s"] = round(
        _load_batch(ch1, list(range(args.chapter_pages))), 3)
    time.sleep(args.quiesce)

    # ── M5. 下一章首图（下一话预热是否生效）──
    _clear(ch2)
    _urls_for(ch1)                       # 触发下一话预热
    time.sleep(3.0)
    t_next, _ = _page(ch2, 0)
    out["m5_next_chapter_first_page_s"] = round(t_next, 3)

    # ── M6. 冷/热单页延迟中位数 ──
    _clear(ch1)
    colds = [round(_page(ch1, i)[0], 3) for i in range(5)]
    warms = [round(_page(ch1, i)[0], 3) for i in range(5)]
    out["m6_cold_ms_median"] = round(statistics.median(colds) * 1000)
    out["m6_warm_ms_median"] = round(statistics.median(warms) * 1000)

    srv.shutdown()
    cdn.shutdown()
    shutil.rmtree(work, ignore_errors=True)

    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("\n=== {} ===".format(args.label), file=sys.stderr)
    for k in ("urls_s", "m1_cold_first_page_s",
              "m2_first_viewport_after_urls_s",
              "m3_scroll_total_stall_s", "m3_scroll_max_page_s", "m3_cdn_hits",
              "m4_chapter_warm_s", "m5_next_chapter_first_page_s",
              "m6_cold_ms_median", "m6_warm_ms_median"):
        print(f"  {k:28s} {out[k]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
