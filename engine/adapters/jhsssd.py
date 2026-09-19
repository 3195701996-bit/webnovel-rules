"""精华书阁（m.jhsssd.com）专属适配器。

站点特性（实测确认）：
- 详情/目录页：https://m.jhsssd.com/{bookId}/ 的 ul.chapter li a，
  分页 nextTocUrl: index_N.html；章节 URL 形如 /{bookId}/{chapterId}.html
- 正文：div.nr_nr（<br> 分段），分页 nextContentUrl: id.pb_next@href
  形如 {chapterId}_N.html
- 编码：页面可能 gbk/乱码，Fetcher 负责解码；正文清洗时处理 <br>
"""
import re
from urllib.parse import urljoin

from . import (BaseSourceAdapter, chapter_url_num, page_cap_error,
               CONTENT_PAGE_CAP, same_chapter_page, toc_cap_error,
               TOC_PAGE_CAP)
from ._jhsssd_clean import clean_chapter_name


class Adapter(BaseSourceAdapter):
    uid_prefix = "精华书阁"

    # ── 搜索 ──
    def search(self, keyword, page=1):
        import urllib.parse as up
        url = (self.base + "/search.html?ie=utf-8&word="
               + up.quote(keyword) + f"&page={page}")
        html = self._get(url, timeout=12, retries=2)
        if not html:
            return []
        books = []
        # 结果项：<div class="searchbook"> 内 bookimg 封面 + bookinfo
        # （书名 h4.bookname + 分类 div.cat + 作者 div.author + 最新章节 div.update）
        for m in re.finditer(r'<div class="searchbook">(.*?)</div>\s*</div>', html, re.S):
            seg = m.group(1)
            um = re.search(r'<a[^>]*href="(/\d+/)"[^>]*>\s*<img[^>]*src="([^"]+)"', seg)
            if not um:
                continue
            u, cover = um.group(1), um.group(2)
            nm = re.search(r'<h4 class="bookname"><a[^>]*>([^<]{2,60})</a></h4>', seg)
            if not nm:
                continue
            name = re.sub(r"\s+", " ", nm.group(1)).strip()
            full = urljoin(self.base, u)
            if not name or any(b["book_url"].endswith(u) for b in books):
                continue
            # 作者：<div class="author">作者：xxx</div>
            _am = re.search(r'作者[:：]?\s*([^<]{1,30})</div>', seg)
            author = (_am.group(1).strip() if _am else "")
            if author == "佚名":
                author = ""
            books.append({
                "name": name,
                "author": author,
                "intro": "",
                "kind": "",
                "cover": urljoin(self.base, cover),
                "book_url": full,
                "source_uid": self.source.get("uid", ""),
                "source_name": self.source.get("bookSourceName", ""),
            })
        return books

    # ── 详情 ──
    def get_book(self, book_url, fast=False):
        html = self._get(book_url, timeout=12 if not fast else 8, retries=2)
        if not html:
            return None
        name = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
        author = re.search(r"作者[:：]?\s*[^<]*<[^>]*>?([^<]{2,30})", html)
        info = {
            "name": name.group(1).strip() if name else "",
            "author": author.group(1).strip() if author else "",
            "intro": "",
            "cover": "",
            "kind": "",
            "last_chapter": "",
            "word_count": "",
            "update_time": "",
        }
        return info

    # ── 目录（含分页）──
    def get_toc(self, book):
        url = getattr(book, "toc_url", "") or book.book_url
        chapters = []
        seen = set()
        visited = set()
        # 目录分页上限 30 → TOC_PAGE_CAP(120)，且撞上限仍有下一页时**报错**。
        # 实测（2026-09-18）：《剑来》目录 **66 页 / 1309 章**，旧上限 30 只给到 605 章
        # ——用户从该源下载的书后半本读不到，而且目录里看不出少了什么。
        _cap = TOC_PAGE_CAP
        _more = False
        for _pi in range(_cap):
            if url in visited:
                break
            visited.add(url)
            html = self._get(url, timeout=15, retries=2)
            if not html:
                break
            for m in re.finditer(
                    r'<a[^>]*href="(/\d+/\d+\.html)"[^>]*>([^<]{1,80})</a>', html):
                u, name = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
                name = clean_chapter_name(name)
                full = urljoin(self.base, u)
                if full in seen or not name:
                    continue
                seen.add(full)
                chapters.append({"name": name, "url": full})
            # 下一页（精华书阁："下一页" 文字在 href 之前）
            m2 = re.search(r"下一页[^<]*<a[^>]*href=\"([^\"]+)\"", html)
            if not m2:
                m2 = re.search(r"<a[^>]*href=\"([^\"]+)\"[^>]*>\s*下一页", html)
            if not m2:
                break
            nxt = m2.group(1)
            if not re.search(r"index_\d+\.html", nxt):
                break
            _next_url = nxt if nxt.startswith("http") else urljoin(url, nxt)
            if _pi == _cap - 1:
                _more = True
                break
            url = _next_url
        if _more:
            raise toc_cap_error("精华书阁", _cap, url)
        if not chapters:
            raise RuntimeError("精华书阁目录解析为空")
        # 过滤纯导航项
        nav_names = {"返回目录", "下一页", "上一页", "阅读全文"}
        chapters = [c for c in chapters if c["name"] not in nav_names]
        # "开始阅读"导航占位（指向真实章节）：从正文页取真实章节名
        for c in chapters:
            if c["name"] == "开始阅读":
                try:
                    ch_html = self._get(c["url"], timeout=10, retries=1)
                    m = re.search(
                        r'class="[^"]*title[^"]*"[^>]*>\s*([^<]{2,60})', ch_html or "")
                    if m:
                        c["name"] = clean_chapter_name(m.group(1).strip())
                except Exception:
                    pass
        # 完结感言等无序号的：截掉尾部无意义字（如"婉绊嘅軭"）
        for c in chapters:
            if not re.search(r"\d+、", c["name"]):
                c["name"] = re.sub(r"[婉绊嘅軭腙銆嬅]+$", "", c["name"]).strip()
        # 按 URL 数字升序（第1章在前）
        # 公共实现：engine/adapters/__init__.py chapter_url_num
        chapters.sort(key=lambda c: chapter_url_num(c["url"]))
        # 清理分页标记 (1/2) (1/3) 等
        for c in chapters:
            c["name"] = re.sub(r"\s*\(\d+/\d+\)\s*$", "", c["name"]).strip()
        chapters = [c for c in chapters if c["name"]]
        return chapters

    # ── 正文（含分页）──
    def get_content(self, chapter_url):
        parts = []
        url = chapter_url
        visited = set()
        # 分页上限 20 → **40**，且撞上限仍有下一页时**报错**而非静默返回半章。
        # 实测（2026-09-18）：《剑来》第301章需要 ≥26 页，旧上限 20 让后 6 页被丢掉，
        # 章节结尾断在词中；且缓存后不会自愈（用户永久读半章）。
        _cap = CONTENT_PAGE_CAP
        for _i in range(_cap):
            if url in visited:
                break
            visited.add(url)
            html = self._get(url, timeout=15, retries=2)
            if not html:
                break
            m = re.search(
                r'<div[^>]*class="[^"]*nr_nr[^"]*"[^>]*>(.*?)</div>', html, re.S)
            if m:
                parts.append(m.group(1))
            m2 = re.search(r'id="pb_next"[^>]*href="([^"]+)"', html)
            if not m2:
                break
            nxt = m2.group(1)
            _next_url = nxt if nxt.startswith("http") else urljoin(chapter_url, nxt)
            # 跨章保护：本章最后一页的 pb_next 指向**下一章**，必须在此停下
            # （否则本章缓存会含下一章内容、导出 txt 章节重复）
            if not same_chapter_page(chapter_url, _next_url):
                break
            if _i == _cap - 1:
                raise page_cap_error("精华书阁", _cap, _next_url)
            url = _next_url
        if not parts:
            raise RuntimeError("精华书阁正文为空")
        text = "\n".join(parts)
        text = self._clean_content(text)
        # 删阅读提示
        for pat in (r"阅读提示：为防止内容获取不全，请勿使用浏览器阅读模式。?",
                    r"本章未完，请点击下一页继续阅读》》?",
                    r"（本章未完，请点击下一页继续阅读）"):
            text = re.sub(pat, "", text)
        return text.strip()
