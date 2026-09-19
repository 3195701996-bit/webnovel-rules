"""爱下电子书（ixdzs8.com 等镜像）专属适配器。

站点特性（2026-09 实测 ixdzs8.com）：
- 搜索：GET /bsearch?q={关键词} → ul.u-list li.burl（封面 img / h3.bname 书名 /
  span.bauthor 作者），作者另有独立 /author/ 页
- 详情：/read/{bid}/ → meta og:title / og:image / og:description(简介) +
  meta description（作者/分类/状态/字数/章节）
- 目录：POST /novel/clist/  data{bid} → JSON {"rs":200,"data":[
  {"ctype":"0","ordernum":"1","title":"第X章 ..."}, ...]}；
  ctype=="1" 为卷标（跳过）；章节 URL = /read/{bid}/p{ordernum}.html
- 正文：章节页 <article class="page-content"><h3>章名</h3><section><p>...</p>
  </section></article>，单页无分页
- 反爬：正文/部分路径偶发 JS 挑战页（<title>正在验证浏览器</title>，
  内含 `let token = "..."` + `location.href = ..."?challenge="+token`）。
  该挑战无需执行 JS/无需 cookie，带 ?challenge={token} 同会话重请求即放行。
  注意：挑战页为繁体文案（安全驗證），fetcher 的简体特征词不会命中，
  挑战页会当普通文本返回，必须由适配器检测并跟随（_get_c）。
- 偶发 SSL 瞬断：上层 fetcher 已有重试；适配器内再兜一轮重试
"""
import json
import re
from urllib.parse import urljoin

from . import BaseSourceAdapter

_TOKEN_RE = re.compile(r'let\s+token\s*=\s*"([^"]+)"')
_CHALLENGE_TITLE = re.compile(r"正在验证浏览器|安全驗證|请稍等")
_BID_RE = re.compile(r"/read/(\d+)/")


class Adapter(BaseSourceAdapter):
    uid_prefix = "爱下"

    # ── GET + JS 挑战跟随 ──
    def _get_c(self, url, timeout=15, retries=2, **kw):
        """请求页面；若返回 ixdzs 弱 JS 挑战页则带 ?challenge=token 重请求"""
        html = self._get(url, timeout=timeout, retries=retries, **kw)
        if html and len(html) < 4096 and _TOKEN_RE.search(html):
            m = _TOKEN_RE.search(html)
            sep = "&" if "?" in url else "?"
            try:
                html2 = self._get(url + sep + "challenge=" + m.group(1),
                                  timeout=timeout, retries=retries, **kw)
            except Exception:
                html2 = None
            if html2 and len(html2) > 2000 and not _TOKEN_RE.search(html2):
                return html2
        return html

    # ── 搜索 ──
    def search(self, keyword, page=1):
        import urllib.parse as up
        url = f"{self.base}/bsearch?q=" + up.quote(keyword)
        if page and page > 1:
            url += f"&page={page}"
        html = self._get_c(url, timeout=15, retries=2)
        if not html:
            return []
        books = []
        seen = set()
        for m in re.finditer(r'<li class="burl"[^>]*>(.*?)</li>', html, re.S):
            seg = m.group(1)
            um = re.search(r'href="(/read/\d+/)"', seg)
            if not um:
                continue
            u = um.group(1)
            full = urljoin(self.base, u)
            if full in seen:
                continue
            nm = re.search(
                r'<h3[^>]*class="[^"]*bname[^"]*"[^>]*>\s*'
                r'<a[^>]*>([^<]{1,80})</a>', seg)
            name = re.sub(r"\s+", " ", nm.group(1)).strip() if nm else ""
            if not name:
                continue
            cm = re.search(r'<img[^>]*src="([^"]+)"', seg)
            am = re.search(r'class="[^"]*bauthor[^"]*"[^>]*>\s*'
                           r'<a[^>]*>([^<]{1,40})</a>', seg)
            im = re.search(r'class="[^"]*l-p2[^"]*"[^>]*>([^<]{1,200})', seg)
            seen.add(full)
            books.append({
                "name": name,
                "author": re.sub(r"\s+", "", am.group(1)) if am else "",
                "intro": (im.group(1).strip() if im else ""),
                "kind": "",
                "cover": urljoin(self.base, cm.group(1)) if cm else "",
                "book_url": full,
                "source_uid": self.source.get("uid", ""),
                "source_name": self.source.get("bookSourceName", ""),
            })
        return books

    # ── 详情 ──
    def get_book(self, book_url, fast=False):
        html = self._get_c(book_url, timeout=12 if not fast else 8,
                           retries=2)
        if not html:
            return None
        def _og(name):
            m = re.search(r'<meta[^>]*property="og:%s"[^>]*content="([^"]*)"'
                          % re.escape(name), html)
            return m.group(1).strip() if m else ""
        name = _og("title")
        intro = _og("description")
        cover = _og("image")
        # meta description：免费在线阅读，作者：遇牧烧绳，分类：都市青春，
        # 状态：连载中，字数：1824.37万字，章节：5026章
        author = kind = status = word_count = ""
        dm = re.search(r'<meta[^>]*name="description"[^>]*content="([^"]*)"',
                       html)
        if dm:
            dc = dm.group(1)
            for key, dest in (("作者", "author"), ("分类", "kind"),
                              ("状态", "status"), ("字数", "word_count")):
                m = re.search(key + r"[:：]\s*([^，,。]+)", dc)
                if m:
                    val = m.group(1).strip()
                    if key == "作者":
                        author = val
                    elif key == "分类":
                        kind = val
                    elif key == "状态":
                        status = val
                    else:
                        word_count = val
        # 最新章节：详情页最新章链接 /read/{bid}/p{max}.html 的标题
        last_chapter = ""
        lm = re.search(r'<a[^>]*href="(/read/\d+/p\d+\.html)"[^>]*>'
                       r'([^<]{1,60})</a>', html)
        if lm:
            last_chapter = lm.group(2).strip()
        return {
            "name": name,
            "author": author,
            "intro": intro,
            "cover": cover,
            "kind": kind,
            "status": status,
            "last_chapter": last_chapter,
            "word_count": word_count,
            "update_time": "",
        }

    # ── 目录（全量 JSON 接口）──
    def get_toc(self, book):
        url = getattr(book, "toc_url", "") or book.book_url
        bid_m = _BID_RE.search(url)
        if not bid_m:
            raise RuntimeError("爱下书 URL 无法识别书ID")
        bid = bid_m.group(1)
        html = self._post(f"{self.base}/novel/clist/",
                          data={"bid": bid}, timeout=25, retries=2)
        if not html:
            raise RuntimeError("爱下书目录接口无响应")
        try:
            data = json.loads(html)
        except Exception as e:
            raise RuntimeError(f"爱下书目录接口非JSON: {e}") from e
        chapters = []
        for it in (data or {}).get("data") or []:
            if str(it.get("ctype")) == "1":
                continue                      # 卷标
            num = str(it.get("ordernum", "")).strip()
            title = re.sub(r"\s+", " ", str(it.get("title", "") or "")).strip()
            if not num or not title:
                continue
            chapters.append({
                "name": title,
                "url": f"{self.base}/read/{bid}/p{num}.html",
            })
        if not chapters:
            raise RuntimeError("爱下书目录解析为空")
        return chapters

    # ── 正文 ──
    def get_content(self, chapter_url):
        html = self._get_c(chapter_url, timeout=15, retries=2)
        if not html:
            raise RuntimeError("爱下书正文页无响应")
        m = re.search(
            r'<article[^>]*class="[^"]*page-content[^"]*"[^>]*>(.*?)'
            r'</article>', html, re.S)
        if not m:
            # 兼容无 class 的 article
            m = re.search(r'<article[^>]*>(.*?)</article>', html, re.S)
        if not m:
            raise RuntimeError("爱下书正文容器缺失")
        body = m.group(1)
        # 去掉章内重复标题(<h3>章名</h3>)
        body = re.sub(r"<h3[^>]*>.*?</h3>", "", body, flags=re.S)
        text = self._clean_content(body)
        # 尾部/行内站点提示清理
        for pat in (r"请记住[^。\n]{0,30}网址[^。\n]{0,40}。?",
                    r"天才一秒记住[^。\n]{0,50}。?",
                    r"(.{0,20}(?:首发|最新章节|无弹窗)[^。\n]{0,60}。?)"):
            text = re.sub(pat, "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
