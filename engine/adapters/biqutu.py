"""爱笔楼（m.biqutu.info / biqutxt 模板）专属适配器。

模板特征：
- 搜索：GET {base}/SearchBook.php?keyword={key}
- 目录：#chapterlist p a（不含 #bottom）
- 正文：div#chaptercontent，分页 a#pt_next

当前状态：站点服务器 RemoteDisconnected（断连），适配器按模板写好，
站点恢复后即可使用。注册表见 engine/adapters/__init__.py。
"""
import re
from urllib.parse import urljoin, quote

from . import (BaseSourceAdapter, CONTENT_PAGE_CAP, page_cap_error,
               same_chapter_page)


class Adapter(BaseSourceAdapter):
    uid_prefix = "爱笔楼"

    def search(self, keyword, page=1):
        url = self.base + f"/SearchBook.php?keyword={quote(keyword)}"
        html = self._get(url, timeout=15, retries=3)
        if not html:
            return []
        books = []
        for m in re.finditer(
                r'<a[^>]*href="([^"]*)"[^>]*>([^<]{2,60})</a>', html):
            u, name = m.group(1), m.group(2).strip()
            full = u if u.startswith("http") else urljoin(self.base, u)
            if not name or not re.search(r"/\d+/\d+\.html$", full):
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
        intro = re.search(r'class="[^"]*intro[^"]*"[^>]*>(.*?)</div>', html, re.S)
        info = {
            "name": name.group(1).strip() if name else "",
            "author": author.group(1).strip() if author else "",
            "intro": re.sub(r"<[^>]+>", "", intro.group(1)).strip() if intro else "",
            "cover": "", "kind": "", "last_chapter": "",
            "word_count": "", "update_time": "",
        }
        return info

    def get_toc(self, book):
        url = getattr(book, "toc_url", "") or book.book_url
        html = self._get(url, timeout=15, retries=3)
        if not html:
            raise RuntimeError("爱笔楼目录页获取失败")
        chapters = []
        seen = set()
        for m in re.finditer(
                r'<a[^>]*href="([^"]+)"[^>]*>([^<]{1,60})</a>', html):
            u, name = m.group(1), m.group(2).strip()
            full = u if u.startswith("http") else urljoin(url, u)
            # R26: 只收章节链接（/{id}/{ch}.html）——移动站导航（首页/我的书架/
            # 阅读记录/sitemap.xml 等）会被宽泛正则误收成假章节
            if not re.search(r"/\d+/\d+\.html$", full):
                continue
            if full in seen or not name or "#bottom" in full:
                continue
            seen.add(full)
            chapters.append({"name": name, "url": full})
        if not chapters:
            raise RuntimeError("爱笔楼目录解析为空")
        return chapters

    def get_content(self, chapter_url):
        parts = []
        url = chapter_url
        visited = set()
        # 分页上限 + **跨章保护**（2026-09-18 全仓排查）：
        # 笔趣阁模板的 pt_next 在部分站上就是"下一章"，无保护地跟随会把后续章节
        # 拼进本章（导出 txt 章节内容重复）。判据见 same_chapter_page。
        _cap = CONTENT_PAGE_CAP
        for _i in range(_cap):
            if url in visited:
                break
            visited.add(url)
            html = self._get(url, timeout=15, retries=3)
            if not html:
                break
            m = re.search(r'id="chaptercontent"[^>]*>(.*?)</div>', html, re.S)
            if m:
                parts.append(m.group(1))
            m2 = re.search(r'id="pt_next"[^>]*href="([^"]+)"', html)
            if not m2:
                break
            nxt = m2.group(1)
            _next_url = nxt if nxt.startswith("http") else urljoin(chapter_url, nxt)
            if not same_chapter_page(chapter_url, _next_url):
                break
            if _i == _cap - 1:
                raise page_cap_error("爱笔楼", _cap, _next_url)
            url = _next_url
        if not parts:
            raise RuntimeError("爱笔楼正文为空")
        return self._clean_content("\n".join(parts))
