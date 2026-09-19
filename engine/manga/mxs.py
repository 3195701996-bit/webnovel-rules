# -*- coding: utf-8 -*-
"""漫小肆适配器（HTML 解析型：www.mxshm.top，转写自 venera mxs.js）"""
import re
import urllib.parse
import urllib.request

from lxml import html as lhtml

from .base import Comic, ComicDetails, Chapter, MangaAdapter, MangaError

# 域名候选（对应 JS settings.domains，按顺序回退）
DOMAINS = [
    "https://www.mxshm.top",
    "https://www.jjmhw1.top",
    "https://www.jjmh.top",
    "https://www.jjmh.cc",
    "https://www.wzd1.cc",
    "https://www.wzdhm1.cc",
    "https://www.ikanwzd.cc",
]
BASE = DOMAINS[0]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        raise MangaError(f"漫小肆请求失败: {e}") from e


def _fetch_any(path, timeout=15):
    """按候选域名顺序请求，返回 (base_url, html)"""
    last_err = None
    for base in DOMAINS:
        try:
            return base, _fetch(base + path, timeout=timeout)
        except MangaError as e:
            last_err = e
    raise last_err or MangaError("漫小肆所有域名均不可用")


class Mxs(MangaAdapter):
    key = "mxs"
    name = "漫小肆"
    version = "1.0.0"
    concurrent = 2

    def __init__(self, state_dir=None):
        super().__init__(state_dir)
        self.base = BASE  # 实际命中的域名（images/headers 使用）

    # ---- 列表解析（对应 JS parseComicList） ----
    def _parse_comic_list(self, base, items):
        out = []
        seen = set()
        for item in items:
            links = item.cssselect("a[href^='/book/']")
            if not links:
                continue
            href = links[0].get("href", "")
            cid = href.rstrip("/").split("/")[-1]
            if not cid or cid in seen:
                continue
            title = ""
            els = item.cssselect(".title a")
            if els:
                title = els[0].text_content().strip()
            if not title:
                continue
            seen.add(cid)
            author = ""
            els = item.cssselect("span a")
            if els:
                author = els[0].text_content().strip()
            out.append(Comic(
                id=cid,
                title=title,
                author=author,
                cover=f"{base}/static/upload/book/{cid}/cover.jpg",
                url=f"{base}/book/{cid}",
                source_key=self.key,
            ))
        return out

    # ---- 搜索 ----
    def search(self, keyword, page=1):
        path = "/search?keyword=" + urllib.parse.quote(keyword)
        base, html = _fetch_any(path)
        self.base = base
        doc = lhtml.fromstring(html)
        return self._parse_comic_list(base, doc.cssselect(".mh-item"))

    # ---- 详情（对应 JS comic.loadInfo） ----
    def comic_info(self, comic_id):
        base, html = _fetch_any(f"/book/{comic_id}")
        self.base = base
        doc = lhtml.fromstring(html)

        title = ""
        els = doc.cssselect(".info h1")
        if els:
            title = els[0].text_content().strip()

        author = ""
        sub_title = ""
        for elem in doc.cssselect(".info .subtitle"):
            text = elem.text_content()
            if "别名：" in text:
                sub_title = text.replace("别名：", "").strip()
            if "作者：" in text:
                author = text.replace("作者：", "").strip()
        author = author.split("&")[0].strip() if author else ""

        status = area = update_time = clicks = ""
        for elem in doc.cssselect(".info .tip span"):
            text = elem.text_content()
            if "状态：" in text:
                subs = elem.cssselect("span")
                status = subs[0].text_content().strip() if subs else ""
            elif "地区：" in text:
                subs = elem.cssselect("a")
                area = subs[0].text_content().strip() if subs else ""
            elif "更新时间：" in text:
                update_time = text.replace("更新时间：", "").strip()
            elif "点击：" in text:
                clicks = text.replace("点击：", "").strip()

        description = ""
        els = doc.cssselect(".info .content")
        if els:
            description = els[0].text_content().strip()

        tags = []
        for elem in doc.cssselect(".info .tip a[href*='tag=']"):
            t = elem.text_content().strip()
            if t:
                tags.append(t)
        if status:
            tags.append(status)
        if area:
            tags.append(area)

        # 章节（对应 #detail-list-select li a，id 为路径末段）
        chapters = []
        seen = set()
        for a in doc.cssselect("#detail-list-select li a"):
            href = a.get("href", "")
            name = a.text_content().strip()
            if not href or not name:
                continue
            ep = href.rstrip("/").split("/")[-1]
            if not ep or ep in seen:
                continue
            seen.add(ep)
            chapters.append(Chapter(id=ep, name=name,
                                    url=href if href.startswith("http") else base + href))

        return ComicDetails(
            id=comic_id,
            title=title,
            cover=f"{base}/static/upload/book/{comic_id}/cover.jpg",
            sub_title=sub_title or self.name,
            description=description,
            author=author,
            tags=tags,
            update_time=update_time,
            views=clicks,
            url=f"{base}/book/{comic_id}",
            chapters=chapters,
        )

    def chapters(self, comic_id):
        return self.comic_info(comic_id).chapters

    # ---- 章节图片（对应 JS comic.loadEp） ----
    def images(self, comic_id, chapter_id):
        base = self.base or BASE
        try:
            html = _fetch(f"{base}/chapter/{chapter_id}")
        except MangaError:
            base, html = _fetch_any(f"/chapter/{chapter_id}")
            self.base = base
        doc = lhtml.fromstring(html)
        imgs = []
        for img in doc.cssselect("img.lazy"):
            src = img.get("data-original") or img.get("src") or ""
            if not src:
                continue
            # 将图片域名重写为当前站点域名
            src = re.sub(r"^https?://[^/]+", base, src)
            if src.startswith("/"):
                src = base + src
            if src not in imgs:
                imgs.append(src)
        if not imgs:
            raise MangaError("本章中未找到图片")
        return imgs

    def image_headers(self, image_url):
        # 防盗链：Referer 用站点域名（JS 中图片 URL 被重写为 baseUrl）
        return {"User-Agent": UA, "Referer": (self.base or BASE) + "/"}
