"""醉读小说（www.imaxreader.com / 顶点小说模板）专属适配器。

模板特征：
- 搜索：GET {base}/search/result.html?searchkey={key}
- 目录：#readerlists li（前3项是导航，从第4项起是章节）
- 正文：.pt-read-text，分页 .pt-nextchapter

当前状态：站点服务器 RemoteDisconnected（.com 重定向到 .info 后断连），
适配器按模板写好，站点恢复后即可使用。
"""
import re
from urllib.parse import urljoin, quote

from . import BaseSourceAdapter


class Adapter(BaseSourceAdapter):
    uid_prefix = "醉读小说"

    def search(self, keyword, page=1):
        url = self.base + f"/search/result.html?searchkey={quote(keyword)}"
        html = self._get(url, timeout=15, retries=3)
        if not html:
            return []
        books = []
        for m in re.finditer(
                r'<a[^>]*href="([^"]*)"[^>]*>([^<]{2,60})</a>', html):
            u, name = m.group(1), m.group(2).strip()
            full = u if u.startswith("http") else urljoin(self.base, u)
            if not name or not re.search(r"/book/[^/]+/?$", full):
                continue
            if any(b["book_url"] == full for b in books):
                continue
            books.append({
                "name": name, "author": "", "intro": "", "kind": "",
                "cover": "",
                "book_url": full,
                "source_uid": self.source.get("uid", ""),
                "source_name": self.source.get("bookSourceName", ""),
            })
        return books

    def get_book(self, book_url, fast=False):
        html = self._get(book_url, timeout=15 if not fast else 10, retries=3)
        if not html:
            return None
        name = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
        author = re.search(r"作者[:：]?\s*(?:</?\w+[^>]*>)*([^<]{2,30})", html)
        info = {
            "name": name.group(1).strip() if name else "",
            "author": author.group(1).strip() if author else "",
            "intro": "", "cover": "", "kind": "",
            "last_chapter": "", "word_count": "", "update_time": "",
        }
        return info

    def get_toc(self, book):
        url = getattr(book, "toc_url", "") or book.book_url
        html = self._get(url, timeout=15, retries=3)
        if not html:
            raise RuntimeError("醉读小说目录页获取失败")
        chapters = []
        seen = set()
        # #readerlists li（跳过前3个导航项）
        for m in re.finditer(
                r'<li[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>([^<]{1,60})</a>', html):
            u, name = m.group(1), m.group(2).strip()
            full = u if u.startswith("http") else urljoin(url, u)
            if full in seen or not name:
                continue
            seen.add(full)
            chapters.append({"name": name, "url": full})
        if not chapters:
            raise RuntimeError("醉读小说目录解析为空")
        # 跳过前3个导航项
        return chapters[3:]

    def get_content(self, chapter_url):
        parts = []
        url = chapter_url
        visited = set()
        # 本章 URL 前缀（read_N[.html] 或 read_N_分页）：只有同前缀的 next 才算本章分页，
        # 跨章（pt-nextchapter 指向下一章）立即停止——避免把后续章节内容拼进本章
        # （曾导致每章缓存含后续章节、导出 txt 章节内容重复）
        base = re.sub(r"(_\d+)?\.html$", "", chapter_url.rstrip("/"))
        for _ in range(10):
            if url in visited:
                break
            visited.add(url)
            html = self._get(url, timeout=15, retries=3)
            if not html:
                break
            m = re.search(r'class="[^"]*pt-read-text[^"]*"[^>]*>(.*?)</div>', html, re.S)
            if m:
                parts.append(m.group(1))
            m2 = re.search(r'class="[^"]*pt-nextchapter[^"]*"[^>]*href="([^"]+)"', html)
            if not m2:
                break
            nxt = m2.group(1)
            nxt_full = nxt if nxt.startswith("http") else urljoin(chapter_url, nxt)
            # 关键：next 必须仍属于本章（同 read_N 前缀）才继续，否则是下一章，停止
            if re.sub(r"(_\d+)?\.html$", "", nxt_full.rstrip("/")) != base:
                break
            url = nxt_full
        if not parts:
            raise RuntimeError("醉读小说正文为空")
        text = self._clean_content("\n".join(parts))
        # 删站点推广语
        text = re.sub(r"百度一下[^\n]*顶点小说[^\n]*", "", text)
        return text.strip()
