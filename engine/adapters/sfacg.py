"""SF轻小说（book.sfacg.com / 动漫之家）专属适配器。

站点特性：
- 详情页可直连（书名/作者/简介/封面）
- 目录页 MainIndex 由 JS 渲染（原始 HTML 无目录数据）→ 用 headless Chrome
  渲染后解析 .s-list li 结构
- 正文页：部分章节免费直连（ChapterBody 容器），部分需登录（返回"出错了"）
  → 直连尝试，失败返回明确错误；不引入登录

依赖：本机安装 Google Chrome（/Applications/Google Chrome.app）。
"""
import re
import subprocess
import logging

from . import BaseSourceAdapter

log = logging.getLogger("adapter.sfacg")

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://book.sfacg.com/",
}


def _chrome_dom(url, budget_ms=8000):
    """headless Chrome 渲染页面，返回 DOM 文本。失败返回 None。

    注意：
    - 不能带 --user-data-dir / --remote-debugging-port（Chrome 151 下会报
      "Multiple targets are not supported in headless mode"）
    - 不能带 --virtual-time-budget（与 --dump-dom 组合在 subprocess 环境
      触发 Chrome bug 退出码 13），改用 --timeout 等待 JS 渲染
    """
    try:
        r = subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu",
             "--timeout", str(budget_ms),
             "--dump-dom", url],
            capture_output=True, text=True, timeout=budget_ms // 1000 + 10,
            check=False)
        if r.returncode == 0 and r.stdout:
            return r.stdout
        return None
    except Exception as e:
        log.warning("chrome render failed %s: %s", url, e)
        return None


class Adapter(BaseSourceAdapter):
    uid_prefix = "SF轻小说"

    # ── 搜索：走 SF 搜索接口 ──
    def search(self, keyword, page=1):
        url = (f"https://book.sfacg.com/Search/?keyword="
               f"{__import__('urllib.parse', fromlist=['quote']).quote(keyword)}")
        html = self._get(url, timeout=12, retries=2)
        if not html:
            return []
        books = []
        # 搜索结果项
        for m in re.finditer(
                r'<a[^>]*href="(/Novel/\d+/)"[^>]*title="([^"]+)"[^>]*>',
                html):
            u, name = m.group(1), m.group(2)
            if not name or not re.match(r'/Novel/\d+/$', u):
                continue
            books.append({
                "name": name.strip(),
                "author": "",
                "intro": "",
                "kind": "轻小说",
                "cover": "",
                "book_url": "https://book.sfacg.com" + u,
                "source_uid": self.source.get("uid", ""),
                "source_name": self.source.get("bookSourceName", ""),
            })
        return books

    # ── 详情 ──
    def get_book(self, book_url, fast=False):
        html = self._get(book_url, timeout=12, retries=2)
        if not html:
            return None
        name = re.search(r'<h1[^>]*class="[^"]*title[^"]*"[^>]*>\s*<span[^>]*class="[^"]*text[^"]*"[^>]*>([^<]+)', html)
        author = re.search(r'author-name[^>]*>\s*<a[^>]*>([^<]+)', html)
        intro = re.search(r'p[^>]*class="[^"]*introduce[^"]*"[^>]*>(.*?)</p>', html, re.S)
        cover = re.search(r'summary-pic[^>]*>\s*<img[^>]*src="([^"]+)"', html)
        toc = re.search(r'<a[^>]*href="([^"]*MainIndex[^"]*)"[^>]*>(?:点击阅读|阅读)</a>', html)
        last = re.search(r'chapter-title[^>]*>\s*<a[^>]*>([^<]+)', html)
        info = {
            "name": name.group(1).strip() if name else "",
            "author": author.group(1).strip() if author else "",
            "intro": re.sub(r"<[^>]+>", "", intro.group(1)).strip() if intro else "",
            "cover": cover.group(1) if cover else "",
            "kind": "轻小说",
            "last_chapter": last.group(1).strip() if last else "",
            "word_count": "",
            "update_time": "",
        }
        if toc:
            info["toc_url"] = "https://book.sfacg.com" + toc.group(1)
        return info

    # ── 目录：Chrome 渲染 MainIndex ──
    def get_toc(self, book):
        toc_url = getattr(book, "toc_url", "") or (book.book_url + "MainIndex/")
        if not toc_url.startswith("http"):
            toc_url = "https://book.sfacg.com" + toc_url
        dom = _chrome_dom(toc_url)
        if not dom:
            raise RuntimeError("SF轻小说目录渲染失败（Chrome 不可用或超时）")
        chapters = []
        seen = set()
        # 解析 .s-list li 结构
        for m in re.finditer(
                r'<li[^>]*>\s*<a[^>]*href="(/Novel/[\d]+/[\d]+/[\d]+/)"[^>]*title="([^"]*)"',
                dom):
            u, name = m.group(1), m.group(2).strip()
            full = "https://book.sfacg.com" + u
            if not name or full in seen:
                continue
            seen.add(full)
            chapters.append({"name": name, "url": full})
        if not chapters:
            # 兜底：直接匹配任意章节 href
            for m in re.finditer(
                    r'href="(/Novel/[\d]+/[\d]+/[\d]+/)"[^>]*>\s*([^<]{1,60})',
                    dom):
                u, name = m.group(1), m.group(2).strip()
                full = "https://book.sfacg.com" + u
                if not name or full in seen or len(name) > 60:
                    continue
                seen.add(full)
                chapters.append({"name": name, "url": full})
        # 章节名清洗：去 SVG 图标字符
        for ch in chapters:
            ch["name"] = re.sub(r"[\ue900-\uf8ff\ue000-\ue8ff]", "", ch["name"]).strip()
            ch["name"] = re.sub(r"\s+", " ", ch["name"])
        chapters = [c for c in chapters if c["name"]]
        return chapters

    # ── 正文：直连 ChapterBody；需登录的返回明确错误 ──
    def get_content(self, chapter_url):
        html = self._get(chapter_url, timeout=15, retries=2)
        if not html:
            raise RuntimeError("章节页面获取失败")
        if "出错了" in html[:500] or "章节内容当前不可用" in html:
            raise RuntimeError("章节内容当前不可用（可能需要登录/会员，或非免费章节）")
        m = re.search(r'ChapterBody[^>]*>(.*?)</div>', html, re.S)
        if not m:
            m = re.search(r'id="chapter-content"[^>]*>(.*?)</div>', html, re.S)
        if not m:
            return ""
        return self._clean_content(m.group(1))
