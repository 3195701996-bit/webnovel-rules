# -*- coding: utf-8 -*-
"""大帝书阁 wap 适配器(ddsk.la / dingdiansk.com,同库双域名):
- 搜索: GET /s.php?keyword={key}&t=1 → div.hot_sale 内 书名/作者 链接 /{a}_{b}/all.html
- 目录: 书页 /{a}_{b}/all.html → <p class="p"><a href="/wapbook/{a}_{ch}.html">第N章...</a>
- 正文: /wapbook/{a}_{ch}.html 分页 _2/_3.html,容器 #chaptercontent(含广告行与页标需清洗)
"""
import re
from urllib.parse import urljoin, quote

from . import BaseSourceAdapter


class Adapter(BaseSourceAdapter):
    uid_prefix = "ddsk"

    # ── 搜索 ──
    def search(self, keyword, page=1):
        url = self.base + f"/s.php?keyword={quote(keyword)}&t=1"
        html = self._get(url, timeout=15, retries=2)
        if not html:
            return []
        books = []
        seen = set()
        for m in re.finditer(
                r'<a[^>]*href="(/\d+_\d+/all\.html)"[^>]*>'
                r'\s*<p class="title">([^<]{2,60})</p>',
                html):
            u, name = m.group(1), re.sub(r"\|.*", "", m.group(2)).strip()
            full = urljoin(self.base, u)
            if not name or full in seen:
                continue
            seen.add(full)
            books.append({
                "name": name, "author": "", "intro": "", "kind": "",
                "cover": "", "book_url": full,
                "source_uid": self.source.get("uid", ""),
                "source_name": self.source.get("bookSourceName", ""),
            })
        return books

    # ── 详情 ──
    def get_book(self, book_url, fast=False):
        html = self._get(book_url, timeout=15 if not fast else 10, retries=2)
        if not html:
            return None
        name = re.search(r"<title>([^<]{2,60})", html)
        info = {
            "name": re.sub(r"_[^_]+$", "", name.group(1).strip()) if name else "",
            "author": "", "intro": "", "cover": "", "kind": "",
            "last_chapter": "", "word_count": "", "update_time": "",
        }
        return info

    # ── 目录 ──
    def get_toc(self, book):
        url = getattr(book, "toc_url", "") or book.book_url
        html = self._get(url, timeout=15, retries=2)
        if not html:
            raise RuntimeError("ddsk 目录页获取失败")
        chapters = []
        seen = set()
        for m in re.finditer(
                r"<a[^>]*href=['\"](/wapbook/\d+_\d+\.html)['\"][^>]*>([^<]{1,60})</a>",
                html):
            u, name = m.group(1), m.group(2).strip()
            full = urljoin(self.base, u)
            if full in seen or not name:
                continue
            seen.add(full)
            chapters.append({"name": name, "url": full})
        if not chapters:
            raise RuntimeError("ddsk 目录解析为空")
        return chapters

    # ── 正文(分页拼接 + 广告/页标清洗)──
    # 页数口径（2026-09-18 实测取证，改前是"盲探 _2.._6 直到失败"）：
    #   站点在正文里自报页数 `(第N/M页)`——普通章为 `第1/3页/第2/3页/第3/3页`，
    #   短章（新书感言/公告）为 `第1/1页`。**这是唯一权威口径**。
    #   盲探的实测后果有两个，都是真缺陷：
    #     ① 缺页返回**软 404**：`_2`/`_3` 对 1 页章返回 HTTP 200 且带正文容器
    #        （内容就是第 1 页），于是旧实现把同一页拼了 3 遍——实测《剑来》
    #        新书感言 576 字被存成 2068 字（指纹落在第 17/570/1123 字，3 遍）；
    #     ② 分页中途抓取失败（超时/被封锁）旧实现 `except: break` 直接返回
    #        **半章**且不报错——比重复更危险：读者以为读完了，任务还记成"已下载"。
    # 故改为：按自报页数取满；缺任何一页且重试无果就**抛错**（宁可失败可重试，
    # 也不静默给半章，与 jhsssd/biqutu/kudushu 的截断防护同一口径）。
    _PAGE_MARK = re.compile(r"第\s*(\d+)\s*/\s*(\d+)\s*页")

    def _fetch_page(self, url):
        """取一页 → (seg, plain, total_pages)；total_pages 来自页内自报页码。"""
        html = self._get(url, timeout=12, retries=2)
        if not html:
            raise RuntimeError(f"ddsk 分页返回空: {url}")
        m = re.search(r'<div id="chaptercontent"[^>]*>(.*?)</div>', html, re.S)
        if not m:
            return None, "", None
        seg = m.group(1)
        plain = re.sub(r"<[^>]+>", "", seg)
        pm = self._PAGE_MARK.search(plain)
        total = int(pm.group(2)) if pm else None
        return seg, plain, total

    def get_content(self, chapter_url):
        base_ch = chapter_url.rstrip("/")
        m0 = re.search(r"/(\d+_\d+)(\.html)$", base_ch)
        if not m0:
            # 非常规 URL（不带 {a}_{b}.html）：单页取，不做任何分页猜测
            seg, _plain, _t = self._fetch_page(base_ch)
            if seg is None:
                raise RuntimeError("ddsk 正文为空")
            return self._clean_pages([seg])
        stem = m0.group(1)
        def page_url(n):
            return base_ch if n == 1 else re.sub(
                r"/\d+_\d+\.html$", f"/{stem}_{n}.html", base_ch)
        parts, seen_text = [], set()
        total, n = None, 1
        while n <= 12:                      # 硬上限：防站点给出异常大页数
            try:
                seg, plain, t = self._fetch_page(page_url(n))
            except Exception as e:
                if n == 1:
                    raise
                # 第 1 页已自报还要更多页，却取不到 → 半章风险，必须报错
                raise RuntimeError(
                    f"ddsk 分页第 {n}/{total or '?'} 页抓取失败"
                    f"（已取到 {len(parts)} 页，不返回半章）：{str(e)[:80]}") from e
            if seg is None:
                if n == 1:
                    raise RuntimeError("ddsk 正文为空")
                break                       # 结构不同的尾页 → 结束
            if total is None:
                total = t
            elif t and t != total:
                # 页内自报页码与首屏不一致（换章/跳转）→ 停止，绝不跨章拼接
                break
            key = re.sub(r"\s+", "", re.sub(r"第\s*\d+\s*/\s*\d+\s*页", "", plain))
            if key and key in seen_text:
                # 站点对缺页返回第 1 页的重复内容（软 404）→ 去重即停
                break
            seen_text.add(key)
            parts.append(seg)
            if total and n >= total:
                break
            n += 1
        if not parts:
            raise RuntimeError("ddsk 正文为空")
        return self._clean_pages(parts)

    def _clean_pages(self, parts):
        text = self._clean_content("\n".join(parts))
        # 清除广告行与页标。页标在原文里是 "(第N/M页)"：只删中间会留下空括号
        # （实测每章标题后都挂着 "()"），故连括号一起去掉。
        text = re.sub(r"天才一秒记住本站地址.*?无广告!", "", text)
        text = re.sub(r"[（(]\s*第\s*\d+\s*/\s*\d+\s*页\s*[）)]", "", text)
        text = re.sub(r"第\s*\d+/\d+\s*页", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
