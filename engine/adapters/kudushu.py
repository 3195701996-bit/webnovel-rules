"""苦读书（m.kudushu.org）专属适配器。

模板特征：
- 搜索：GET {base}/modules/article/search.php?searchkey={key}
- 目录：div.list_xm ul li，分页 /html/{id}/asc-{n}/
- 正文：#novelcontent，分页 {chapterId}_{n}.html

当前状态：站点 Cloudflare 交互式挑战（403 Just a moment），适配器按模板
写好，待绕过挑战或站点关闭挑战后即可使用。
"""
import re
from urllib.parse import urljoin, quote

from . import (BaseSourceAdapter, CONTENT_PAGE_CAP, page_cap_error,
               same_chapter_page)


class Adapter(BaseSourceAdapter):
    uid_prefix = "苦读书"

    def search(self, keyword, page=1):
        url = self.base + f"/modules/article/search.php?searchkey={quote(keyword)}"
        html = self._get(url, timeout=15, retries=7)
        if not html:
            return []
        if "Just a moment" in html[:500]:
            raise RuntimeError("苦读书 Cloudflare 挑战拦截")
        books = []
        for m in re.finditer(
                r'<a[^>]*href="([^"]*)"[^>]*>([^<]{2,60})</a>', html):
            u, name = m.group(1), m.group(2).strip()
            full = u if u.startswith("http") else urljoin(self.base, u)
            if not name or not re.search(r"/html/\d+/", full):
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
        if "Just a moment" in html[:500]:
            raise RuntimeError("苦读书 Cloudflare 挑战拦截")
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
        # R48: 目录百余页,不受 crawler 60s 预算限制(自控并发+限流由 fetcher 兜底)
        self._deadline = None
        # 章 URL 形态 /html/{id}/{chid}/ (数字尾斜杠),排除导航/分页 li
        m0 = re.search(r"(https?://[^/]+/html/\d+/)", url)
        base = m0.group(1) if m0 else url
        first = self._get(url, timeout=20, retries=7)
        if not first:
            raise RuntimeError("苦读书目录页获取失败")
        if "Just a moment" in first[:500]:
            raise RuntimeError("苦读书 Cloudflare 挑战拦截")

        def _chap_re(html):
            bid = re.search(r"html/(\d+)/", html or "")
            bid = bid.group(1) if bid else r"\d+"
            out = []
            for m in re.finditer(
                    rf'<a[^>]*href="(/html/{bid}/\d+/)"[^>]*>(.*?)</a>',
                    html or "", re.S):
                name = re.sub(r"<[^>]+>", "", m.group(2)).strip()
                if not name:
                    continue
                # 导航杂质(苦读书 asc 页顶部"从头阅读/返回目录"等)
                if re.match(r"^(从头阅读|返回目录|上一页|下一页|目录|尾页|首页|第\d+页)$", name):
                    continue
                out.append((m.group(1), name))
            return out

        # 页1 章 + 总页数(导航最大 asc-N)
        chs = []
        seen = set()
        for u, name in _chap_re(first):
            if u not in seen:
                seen.add(u)
                chs.append((1, u, name))
        pages = {int(x) for x in re.findall(r'asc-(\d+)/', first)}
        total = max(pages) if pages else 1
        # 3 并发抓 asc-2..total(实测 3 并发稳定过 CF;受 fetcher 每域限流保护)
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = {}
        todo = [f"{base}asc-{n}/" for n in range(2, total + 1)]
        for _round in range(3):  # 失败页补抓最多 3 轮
            if not todo:
                break
            batch = {}
            with ThreadPoolExecutor(max_workers=3) as ex:
                futs = {ex.submit(self._get, u, 20, 4): u for u in todo}
                for fu in as_completed(futs):
                    u = futs[fu]
                    try:
                        html = fu.result()
                    except Exception as e:
                        batch[u] = f"{type(e).__name__}: {str(e)[:60]}"
                        continue
                    if not html:
                        batch[u] = "empty"
                        continue
                    no = int(re.search(r"asc-(\d+)/", u).group(1))
                    results[no] = _chap_re(html)
            print(f"[kudushu] 目录轮{_round}: 待抓{len(todo)} "
                  f"成功{len(results)} 失败{len(batch)} "
                  f"样例:{list(batch.values())[:2]}", flush=True)
            todo = [u for u, h in batch.items()]
        for no in range(2, total + 1):
            for u, name in results.get(no, []):
                if u not in seen:
                    seen.add(u)
                    chs.append((no, u, name))
        chs.sort(key=lambda x: x[0])
        chapters = [{"name": name, "url": urljoin(url, u)}
                    for _, u, name in chs]
        if not chapters:
            raise RuntimeError("苦读书目录解析为空")
        return chapters

    def get_content(self, chapter_url):
        parts = []
        url = chapter_url
        visited = set()
        _cap = CONTENT_PAGE_CAP
        for _i in range(_cap):
            if url in visited:
                break
            visited.add(url)
            html = self._get(url, timeout=15, retries=7)
            if not html:
                break
            if "Just a moment" in html[:500]:
                raise RuntimeError("苦读书 Cloudflare 挑战拦截")
            m = re.search(r'id="novelcontent"[^>]*>(.*?)</div>', html, re.S)
            if m:
                parts.append(m.group(1))
            m2 = re.search(r'href="(/html/\d+/\d+_\d+/)"[^>]*>[^<]*下', html)
            if not m2:
                break
            nxt = m2.group(1)
            _next_url = nxt if nxt.startswith("http") else urljoin(chapter_url, nxt)
            # 跨章保护（链接形态像分页，但仍要确认章号一致才继续）
            if not same_chapter_page(chapter_url, _next_url):
                break
            if _i == _cap - 1:
                raise page_cap_error("苦读书", _cap, _next_url)
            url = _next_url
        if not parts:
            raise RuntimeError("苦读书正文为空")
        text = self._clean_content("\n".join(parts))
        # 删分页标记/站点文案
        for pat in (r"[（(]第\d+/\d+页[)）]", r"第[^（(]{1,30}[（(]第\d+/\d+页[)）]",
                    r"最新网址[^\n]*", r"m\.kudushu\.org[^\n]*",
                    r"返回目录|进入书架|加入书签|上一页|下一章|下一页|从头阅读"):
            text = re.sub(pat, "", text)
        return text.strip()
