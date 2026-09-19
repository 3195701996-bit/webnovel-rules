"""塔读文学（www.tadu.com）专属适配器。

站点特性（实测确认）：
- 目录页：https://www.tadu.com/book/{bookId}/ 的 .boxCenter.boxT.clearfix a
  包含本书全部章节（前部是最新章节预览 409-401 倒序，后部是 1-400 正序，
  用章节 URL 数字升序排即可得到 1..N 正序）
- 正文：正文页由 JS 渲染，但页面内嵌 hidden input
  <input id="bookPartResourceUrl" value="/getPartContentByCodeTable/{bookId}/{章号}">
  直接 GET 该接口返回 JSON {"data":{"content":"<p>…</p>"}}
- 水印：正文内嵌"塔读…"广告段落（可能插字符混淆），按行删除
- 反爬：需要浏览器 UA header
"""
import re
import json
from urllib.parse import urljoin

from . import BaseSourceAdapter, chapter_url_num

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://www.tadu.com/",
}


class Adapter(BaseSourceAdapter):
    uid_prefix = "塔读"

    def __init__(self, source, fetcher):
        super().__init__(source, fetcher)
        # 覆盖请求头（塔读对默认 Android UA 反爬）
        self.source = dict(source)
        self.source["header"] = json.dumps(HEADERS, ensure_ascii=False)

    # ── 搜索 ──
    def search(self, keyword, page=1):
        import urllib.parse as up
        url = ("https://www.tadu.com/search?query="
               + up.quote(keyword) + "&pageSize=30&pageNum=" + str(page))
        html = self._get(url, timeout=12, retries=2)
        if not html:
            return []
        books = []
        # 搜索结果项（猜测结构：包含书名与链接的 dl/div）
        for m in re.finditer(
                r'<a[^>]*href="(https?://www\.tadu\.com/book/\d+/)"[^>]*>([^<]{2,60})</a>',
                html):
            u, name = m.group(1), m.group(2).strip()
            if not name:
                continue
            if any(b["book_url"] == u for b in books):
                continue
            books.append({
                "name": name,
                "author": "",
                "intro": "",
                "kind": "",
                "cover": "",
                "book_url": u,
                "source_uid": self.source.get("uid", ""),
                "source_name": self.source.get("bookSourceName", ""),
            })
        return books

    # ── 详情 ──
    def get_book(self, book_url, fast=False):
        html = self._get(book_url, timeout=12 if not fast else 8, retries=2)
        if not html:
            return None
        name = re.search(r'<h1[^>]*>([^<]+)</h1>', html)
        author = re.search(r'作者[:：]\s*<a[^>]*>([^<]+)</a>', html)
        intro = re.search(r'class="[^"]*intro[^"]*"[^>]*>(.*?)</(?:div|p)>', html, re.S)
        cover = re.search(r'class="[^"]*cover[^"]*"[^>]*>\s*<img[^>]*src="([^"]+)"', html)
        last = re.search(r'最新章节[:：]\s*<a[^>]*>([^<]+)</a>', html)
        info = {
            "name": name.group(1).strip() if name else "",
            "author": author.group(1).strip() if author else "",
            "intro": re.sub(r"<[^>]+>", "", intro.group(1)).strip() if intro else "",
            "cover": cover.group(1) if cover else "",
            "kind": "",
            "last_chapter": last.group(1).strip() if last else "",
            "word_count": "",
            "update_time": "",
        }
        return info

    # ── 目录 ──
    def get_toc(self, book):
        url = getattr(book, "toc_url", "") or book.book_url
        html = self._get(url, timeout=15, retries=2)
        if not html:
            raise RuntimeError("塔读目录页获取失败")
        chapters = []
        seen = set()
        book_id = re.search(r"/book/(\d+)/", book.book_url)
        book_id = book_id.group(1) if book_id else None
        for m in re.finditer(r'<a[^>]*href="([^"]*book/{}\d+/[^"]*)"[^>]*>'.format(book_id or ""), html):
            u = m.group(1)
            if not u.startswith("http"):
                u = urljoin(url, u)
            if u in seen:
                continue
            seen.add(u)
            # 章节名从后续文本或 title
            chapters.append({"name": "", "url": u})
        if not chapters:
            # 兜底：匹配任何 /book/{id}/{chapterId}/ 结构
            pat = r'href="(/book/(\d+)/(\d+)/)"'
            for m in re.finditer(pat, html):
                u = "https://www.tadu.com" + m.group(1)
                if u in seen:
                    continue
                seen.add(u)
                chapters.append({"name": "", "url": u})
        # 章节名提取（含"第X章"文本的链接）
        for ch in chapters:
            tail = ch["url"].rstrip("/").rsplit("/", 2)
            tail_path = "/" + "/".join(tail[-2:]) + "/"
            mm = re.search(
                r'<a[^>]*href="[^"]*' + re.escape(tail_path) + r'"[^>]*(?:title="([^"]+)")?[^>]*>(.{1,60}?)</a>',
                html)
            if mm:
                t = (mm.group(1) or mm.group(2) or "").strip()
                ch["name"] = re.sub(r"\s+", " ", t)
        # URL 数字升序（第1章在前）
        # 公共实现：engine/adapters/__init__.py chapter_url_num
        chapters.sort(key=lambda c: chapter_url_num(c["url"]))
        chapters = [c for c in chapters if c["name"]]
        if not chapters:
            raise RuntimeError("塔读目录解析为空")
        return chapters

    # ── 正文：hidden input API ──
    def get_content(self, chapter_url):
        html = self._get(chapter_url, timeout=15, retries=2)
        if not html:
            raise RuntimeError("塔读章节页获取失败")
        if "出错了" in html[:300] or "404" in html[:200]:
            raise RuntimeError("章节页面不可用")
        # 找正文 API（hidden input）
        m = re.search(
            r'<input[^>]+(?:id|name)=["\']([^"\']*ResourceUrl[^"\']*)["\'][^>]+value=["\']([^"\']+)["\']',
            html, re.I)
        if not m:
            # 规则正文容器直读
            mc = re.search(r'id="partContent"[^>]*>(.*?)</div>', html, re.S)
            if mc:
                return self._clean_content(mc.group(1))
            return ""
        api = m.group(2)
        if api.startswith("/"):
            api = urljoin(chapter_url, api)
        resp = self._get(api, timeout=12, retries=1)
        if not resp or not resp.lstrip().startswith("{"):
            return ""
        try:
            jd = json.loads(resp)
        except Exception:
            return ""
        data = (jd.get("data") or {}) if isinstance(jd, dict) else {}
        content = ""
        if isinstance(data, dict) and data.get("content"):
            content = data["content"]
        elif isinstance(jd, dict) and jd.get("content"):
            content = jd["content"]
        if not content:
            return ""
        text = self._clean_content(content)
        # 删除"塔读"水印段落（容忍中间插字符）
        text = re.sub(r"塔[^\n]{0,3}读[^\n]{0,80}", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
