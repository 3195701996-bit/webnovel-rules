"""看书网（www.kanshuw.com）专属适配器。

站点特性（实测确认）：
- 搜索：GET /search.php?q={key}，结果 a[href="/{a}/{b}/"]
- 详情/目录：GET /{a}/{b}/，单页完整目录
  章节 a[href="/{a}/{b}/{n}.html"]（第1章在列表靠前）
- 正文：GET /{a}/{b}/{n}.html，容器 article.font_max（<br> 分段）
  分页：{n}_2.html（页内分页），next1 链接指向下一章
"""
import re
from urllib.parse import urljoin, quote

from . import BaseSourceAdapter, chapter_url_num


class Adapter(BaseSourceAdapter):
    uid_prefix = "kanshuw"

    # ── 搜索 ──
    def search(self, keyword, page=1):
        url = self.base + f"/search.php?q={quote(keyword)}"
        html = self._get(url, timeout=15, retries=3)
        if not html:
            return []
        books = []
        # 结果项为 <dl> 容器：dt 封面(<a href=书URL><img src=封面 alt=书名>) +
        # dd h3 书名 + dd.book_other 作者/状态/更新时间/最新章节
        for m in re.finditer(r'<dl[^>]*>(.*?)</dl>', html, re.S):
            seg = m.group(1)
            um = re.search(
                r'<a[^>]*href="(/\d+/\d+/)"[^>]*>\s*<img[^>]*src="([^"]+)"[^>]*'
                r'(?:alt="([^"]*)")?[^>]*>', seg)
            if not um:
                continue
            u, cover, alt = um.group(1), um.group(2), um.group(3) or ""
            nm = re.search(r'<h3><a[^>]*>([^<]{2,60})</a></h3>', seg)
            if not nm:
                continue
            name = re.sub(r"^\[[^\]]{1,8}\]\s*", "", nm.group(1).strip()) or re.sub(
                r"^\[[^\]]{1,8}\]\s*", "", alt)
            full = urljoin(self.base, u)
            if not name or any(b["book_url"] == full for b in books):
                continue
            # 作者：<dd class="book_other">作者：<span>xxx</span></dd>
            _am = re.search(r'作者[:：]?\s*<[^>]*>([^<]{1,30})</', seg)
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
        if not books:
            # 回退：无封面匹配时的旧解析
            for m in re.finditer(
                    r'<a[^>]*href="(/\d+/\d+/)"[^>]*>([^<]{2,60})</a>', html):
                u, name = m.group(1), m.group(2).strip()
                # 去除 [历史][网游] 分类前缀
                name = re.sub(r"^\[[^\]]{1,8}\]\s*", "", name)
                full = urljoin(self.base, u)
                if not name or any(b["book_url"] == full for b in books):
                    continue
                books.append({
                    "name": name,
                    "author": "",
                    "intro": "",
                    "kind": "",
                    "cover": "",
                    "book_url": full,
                    "source_uid": self.source.get("uid", ""),
                    "source_name": self.source.get("bookSourceName", ""),
                })
        return books

    # ── 详情 ──
    def get_book(self, book_url, fast=False):
        html = self._get(book_url, timeout=15 if not fast else 10, retries=2)
        if not html:
            return None
        name = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
        # 作者：og:novel:author meta（页面无正文"作者:"标签，旧正则误抓到搜索框 placeholder 的 HTML 属性）
        _author = ""
        _am = re.search(r'<meta[^>]*property="og:novel:author"[^>]*content="([^"]+)"', html) \
            or re.search(r'<meta[^>]*name="author"[^>]*content="([^"]+)"', html)
        if _am:
            _author = _am.group(1).strip()
        author = _author
        # 详情页封面 og:image（//www.kanshuw.com/images/...）
        _cov = ""
        m = re.search(r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"', html)
        if m:
            _cov = m.group(1)
            if _cov.startswith("//"):
                _cov = "https:" + _cov
        info = {
            "name": name.group(1).strip() if name else "",
            "author": author.strip() if author else "",
            "intro": "", "cover": _cov, "kind": "",
            "last_chapter": "", "word_count": "", "update_time": "",
        }
        return info

    # ── 目录（单页完整）──
    def get_toc(self, book):
        url = getattr(book, "toc_url", "") or book.book_url
        html = self._get(url, timeout=15, retries=3)
        if not html:
            raise RuntimeError("kanshuw 目录页获取失败")
        # 章节路径前缀：/{a}/{b}/
        pm0 = re.search(r"(/\d+/\d+/)", url)
        prefix = pm0.group(1) if pm0 else None
        # 目录可能分页：/87/87588/index_1.html 等（每页约110章）
        chapters = []
        seen = set()
        base = url.rstrip("/")
        # 分页 URL 模式：index_N.html（N≥1）
        pages = [base]
        m = re.search(r"(/\d+/\d+)/?$", base)
        book_path = (m.group(1) if m else None)
        if book_path and not book_path.endswith("/"):
            book_path += "/"
        if book_path:
            # 从第一页收集分页链接（index_1 是自身页，index_2+ 才算分页证据；
            # 99999 是"末页"占位链接，忽略）
            page_links = []
            for pm in re.finditer(r'href="([^"]*index_(\d+)\.html)"', html):
                n = int(pm.group(2))
                if 2 <= n <= 100:
                    page_links.append(pm.group(1))
            for u in page_links:
                full = u if u.startswith("http") else urljoin(self.base, u)
                if full not in pages:
                    pages.append(full)
            # 首页无 index_2+ 分页链接 → 快速验证 index_2 判断单页/多页
            if not page_links:
                cand2 = f"{self.base}{book_path}index_2.html"
                try:
                    rh = self._get(cand2, timeout=6, retries=1)
                    if rh and len(rh) > 3000:
                        pages.append(cand2)
                    else:
                        pages = pages[:1]  # 单页
                except Exception:
                    pages = pages[:1]  # 单页
            # 补齐缺失的中间页（首页链接可能只显示部分，如只有 index_2/3/10）
            max_page = 1
            known = set()
            for p in pages:
                m2 = re.search(r"index_(\d+)\.html", p)
                if m2:
                    max_page = max(max_page, int(m2.group(1)))
                    known.add(int(m2.group(1)))
            for n in range(2, max_page):
                if n in known:
                    continue
                cand = f"{self.base}{book_path}index_{n}.html"
                try:
                    rh = self._get(cand, timeout=6, retries=1)
                    if rh and len(rh) > 3000:
                        pages.append(cand)
                except Exception:
                    continue
        # 目录分页并发获取（页数多时串行可达 1 分钟；并发后 ~5s）。
        # 并发 6：fetcher 对 5xx 已是温和惩罚（间隔封顶 4s），偶发 502 不再拖 60s
        import time as _time
        from concurrent.futures import ThreadPoolExecutor as _TPE
        pages_to_fetch = list(pages)
        # R39c: 失败页整体重试一轮（间隔 1s）后仍失败 → raise——
        # 宁可目录抓取失败，也不静默跳过整页留下"913/2400章"的半截目录
        for _round in range(2):
            fetched = {}
            def _fetch_page(page_url):
                try:
                    ph = html if page_url == base else                         self._get(page_url, timeout=15, retries=2)
                    if not ph:
                        return page_url, None
                    # R39d: 校验页面确实含章节列表（源站限速/故障时可能返回
                    # "看似成功"的错误页——章节为空或极少，HTTP 200 但内容无效。
                    # 每页正常 100 章；不足 50 视为无效页，整体重试）
                    _n = len(re.findall(
                        r'href="(?:https?://[^/"]*)?/?\d+/\d+/\d+\.html"', ph))
                    if _n < 50:
                        return page_url, None
                    return page_url, ph
                except Exception:
                    return page_url, None
            with _TPE(max_workers=6) as _ex:
                for _pu, _ph in _ex.map(_fetch_page, pages_to_fetch):
                    fetched[_pu] = _ph
            _failed = [p for p in pages_to_fetch
                       if p != base and fetched.get(p) is None]
            if not _failed:
                break
            if _round == 0:
                print(f"[kanshuw] 目录分页 {len(_failed)} 页失败，"
                      f"1s 后重试: {_failed[:3]}…", flush=True)
                _time.sleep(1.0)
            else:
                raise RuntimeError(
                    f"kanshuw 目录分页抓取失败（重试后仍失败 {len(_failed)} 页："
                    f"{', '.join(p.rsplit('/', 1)[-1] for p in _failed[:5])}），"
                    f"拒绝写入不完整目录")
        for page_url in pages:
            ph = fetched.get(page_url)
            if not ph:
                continue
            for m in re.finditer(
                    r'<a[^>]*href="(/\d+/\d+/\d+\.html)"[^>]*>([^<]{1,60})</a>', ph):
                u, name = m.group(1), m.group(2).strip()
                full = urljoin(self.base, u)
                if prefix and prefix not in u:
                    continue
                if full in seen or not name:
                    continue
                seen.add(full)
                chapters.append({"name": name, "url": full})
        return self._finalize_toc(chapters)

    def _finalize_toc(self, chapters):
        """跨分页合并后按章节 URL 数字升序（kanshuw 章节 URL 数字=章序）"""
        if not chapters:
            raise RuntimeError("kanshuw 目录解析为空")

        # 公共实现：engine/adapters/__init__.py chapter_url_num
        chapters.sort(key=lambda c: chapter_url_num(c["url"]))
        # 去掉无章号的（导航项等）
        chapters = [c for c in chapters if chapter_url_num(c["url"]) > 0]
        return chapters

    # ── 正文（含页内分页）──
    def get_content(self, chapter_url):
        parts = []
        url = chapter_url
        visited = set()
        for _ in range(6):
            if url in visited:
                break
            visited.add(url)
            html = self._get(url, timeout=10, retries=1)
            if not html:
                break
            m = re.search(r'<article[^>]*class="font_max"[^>]*>(.*?)</article>', html, re.S)
            if m:
                parts.append(m.group(1))
            # 页内分页：{n}_2.html 等（在 next1 链接之前）
            m2 = re.search(r'id="next1"[^>]*href="([^"]+)"', html)
            if not m2:
                break
            nxt = m2.group(1)
            if re.search(r"_\d+\.html", nxt) and nxt not in visited:
                url = nxt if nxt.startswith("http") else urljoin(chapter_url, nxt)
            else:
                break
        if not parts:
            raise RuntimeError("kanshuw 正文为空")
        text = self._clean_content("\n".join(parts))
        # 清除分页标记"第(x/y)页"和重复章节标题首段
        text = re.sub(r"第\(\d+/\d+\)页", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
