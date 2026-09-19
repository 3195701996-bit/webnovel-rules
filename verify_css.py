# -*- coding: utf-8 -*-
"""CSS 修复前后对比实证：启动 app（隔离数据目录），Playwright 截图 + 实测对比度。
用法: venv/bin/python verify_css.py <before|after>
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

HUB = os.path.dirname(os.path.abspath(__file__))
PHASE = sys.argv[1] if len(sys.argv) > 1 else "before"
OUT = os.path.join("/Users/luoamnke/爬虫系统/verify_screenshots/2026-09-09-css-fix", PHASE)
os.makedirs(OUT, exist_ok=True)
PORT = 18933

# ── 隔离数据目录（不碰真实 data/）──
TMP = tempfile.mkdtemp(prefix="wr_css_verify_")
DATA = os.path.join(TMP, "data")
SRC = os.path.join(TMP, "sources")
os.makedirs(os.path.join(DATA, "books"), exist_ok=True)
os.makedirs(SRC, exist_ok=True)
for f in ("book_progress.json", "search_cache.json"):
    p = os.path.join(HUB, "data", f)
    if os.path.exists(p):
        shutil.copy2(p, os.path.join(DATA, f))
book = os.path.join(HUB, "data", "books", "kanshuw.com_ecf418c7b4")
if os.path.isdir(book):
    shutil.copytree(book, os.path.join(DATA, "books", "kanshuw.com_ecf418c7b4"))
for f in os.listdir(os.path.join(HUB, "sources")):
    if f.endswith(".json"):
        shutil.copy2(os.path.join(HUB, "sources", f), os.path.join(SRC, f))

env = dict(os.environ)
env.update({
    "WR_DATA_DIR": DATA,
    "WR_SOURCES_DIR": SRC,
    "WR_SSRF_SKIP_DNS": "1",
    "WR_DISABLE_BACKGROUND": "1",
    "PORT": str(PORT),
})
env.pop("WR_AUTH_PASSWORD", None)

proc = subprocess.Popen(
    [os.path.join(HUB, "venv/bin/python"), "app.py", "--host", "127.0.0.1", "--port", str(PORT)],
    cwd=HUB, env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/login", timeout=2)
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("FATAL: app 未启动"); sys.exit(2)

    from playwright.sync_api import sync_playwright

    CONTRAST_JS = """
    () => {
      function lum(rgb) {
        const m = rgb.match(/[\\d.]+/g); if (!m) return null;
        let [r,g,b] = m.slice(0,3).map(Number).map(v => v/255);
        const f = c => c <= 0.03928 ? c/12.92 : Math.pow((c+0.055)/1.055, 2.4);
        return 0.2126*f(r) + 0.7152*f(g) + 0.0722*f(b);
      }
      function ratio(fg, bg) {
        const l1 = lum(fg), l2 = lum(bg);
        if (l1 == null || l2 == null) return null;
        return ((Math.max(l1,l2)+0.05)/(Math.min(l1,l2)+0.05)).toFixed(2);
      }
      function bgOf(el) {  // 向上找第一个非透明背景
        let e = el;
        while (e) {
          const bg = getComputedStyle(e).backgroundColor;
          const m = bg.match(/[\\d.]+/g);
          if (m && (m.length === 3 || Number(m[3]) > 0)) return bg;
          e = e.parentElement;
        }
        return 'rgb(0,0,0)';
      }
      const out = {};
      const logo = document.querySelector('.topbar .logo');
      if (logo) {
        const c = getComputedStyle(logo);
        out.logo = {color: c.color, bg: bgOf(logo), ratio: ratio(c.color, bgOf(logo))};
      }
      const navAct = document.querySelector('.topbar nav a.active');
      if (navAct) {
        const c = getComputedStyle(navAct);
        out.navActive = {color: c.color, bg: bgOf(navAct), ratio: ratio(c.color, bgOf(navAct))};
      }
      const cont = document.querySelector('.fn-continue');
      if (cont) {
        const c = getComputedStyle(cont);
        out.continueBtn = {color: c.color, bg: c.backgroundColor, ratio: ratio(c.color, c.backgroundColor)};
      }
      const imp = document.querySelector('#import-area');
      if (imp) {
        const c = getComputedStyle(imp);
        out.importArea = {color: c.color, bg: c.backgroundColor,
          fontFamily: c.fontFamily.slice(0, 60), border: c.border,
          textAlign: c.textAlign, padding: c.padding};
      }
      const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
      out.accentVar = accent;
      out.bg2Var = getComputedStyle(document.documentElement).getPropertyValue('--bg-2').trim() || '(未定义)';
      return out;
    }
    """

    PAGES = [
        ("login", "/login"),
        ("index", "/"),
        ("sources", "/sources"),
        ("library", "/library"),
        ("novel_detail", "/novel_detail?key=kanshuw.com_ecf418c7b4"),
    ]
    report = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        for name, path in PAGES:
            url = f"http://127.0.0.1:{PORT}{path}"
            pg.goto(url, wait_until="networkidle", timeout=20000)
            pg.wait_for_timeout(800)
            pg.screenshot(path=os.path.join(OUT, f"{name}.png"), full_page=False)
            report[name] = pg.evaluate(CONTRAST_JS)
            print(f"[{PHASE}] {name}: {json.dumps(report[name], ensure_ascii=False)}")
        # 章节悬停态（小说详情页）
        pg.goto(f"http://127.0.0.1:{PORT}/novel_detail?key=kanshuw.com_ecf418c7b4",
                wait_until="networkidle", timeout=20000)
        pg.wait_for_timeout(800)
        chap = pg.query_selector(".chap-item")
        if chap:
            chap.hover()
            pg.wait_for_timeout(300)
            pg.screenshot(path=os.path.join(OUT, "novel_detail_hover.png"))
            hover = pg.evaluate("""
              () => { const el = document.querySelector('.chap-item:hover');
                if (!el) return null;
                const c = getComputedStyle(el);
                return {color: c.color, bg: c.backgroundColor}; }""")
            print(f"[{PHASE}] chap_hover: {json.dumps(hover, ensure_ascii=False)}")
            report["chap_hover"] = hover
        browser.close()
    with open(os.path.join(OUT, "contrast.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
finally:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    shutil.rmtree(TMP, ignore_errors=True)
print("DONE", OUT)
