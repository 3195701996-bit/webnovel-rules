# -*- coding: utf-8 -*-
"""包子漫画适配器（HTML 解析型：www.baozimh.com + appcn 章节页）"""
import re
import urllib.parse
import urllib.request

from lxml import html as lhtml

from .base import (Comic, ComicDetails, Chapter, JsRequiredError, MangaAdapter,
                   MangaError, decode_html, html_fromstring_safe)

BASE = "https://www.baozimh.com"
APP_CHAPTER = "https://appcn.baozimh.com/baozimhapp/comic/chapter/{cid}/0_{ep}.html"
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"


def _fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            cs = ""
            try:
                cs = r.headers.get_content_charset() or ""
            except Exception:
                cs = ""
            # 按声明编码解码：源站非 UTF-8 时旧实现会产出含 NUL 的怪串，
            # 交给 lxml 就是 "encoding not supported USC4 little endian"（实测）
            return decode_html(raw, cs)
    except MangaError:
        raise
    except Exception as e:
        raise MangaError(f"包子漫画请求失败: {e}") from e


def _norm_id(comic_id):
    # 可能带 .html 后缀
    return comic_id.replace(".html", "")


class Baozi(MangaAdapter):
    key = "baozi"
    name = "包子漫画"
    version = "1.0.0"
    concurrent = 3

    def search(self, keyword, page=1):
        html = _fetch(f"{BASE}/search?q={urllib.parse.quote(keyword)}")
        doc = html_fromstring_safe(lhtml, html)
        out = []
        seen = set()
        for card in doc.cssselect("div.comics-card"):
            a = card.cssselect("a[href]")
            if not a:
                continue
            href = a[0].get("href", "")
            m = re.search(r"/comic/([^/?#]+)", href)
            if not m:
                continue
            cid = m.group(1)
            if cid in seen:
                continue
            seen.add(cid)
            title = ""
            for sel in ("h3", "span.comics-card__title", ".comics-card__title"):
                els = card.cssselect(sel)
                if els:
                    title = "".join(els[0].itertext()).strip()
                    break
            img = card.cssselect("img")
            cover = img[0].get("src") or img[0].get("data-src", "") if img else ""
            out.append(Comic(id=cid, title=title, cover=cover, source_key=self.key))
            if len(out) >= 30:
                break
        return out

    def comic_info(self, comic_id):
        cid = _norm_id(comic_id)
        html = _fetch(f"{BASE}/comic/{cid}")
        doc = html_fromstring_safe(lhtml, html)
        title = ""
        for sel in ("h1.comics-detail__title", "h1"):
            els = doc.cssselect(sel)
            if els:
                title = "".join(els[0].itertext()).strip()
                break
        author = ""
        els = doc.cssselect("h2.comics-detail__author")
        if els:
            author = "".join(els[0].itertext()).strip()
        desc = ""
        els = doc.cssselect("p.comics-detail__desc")
        if els:
            desc = "".join(els[0].itertext()).strip()
        cover = ""
        imgs = doc.cssselect("div.l-content amp-img, div.l-content img")
        if imgs:
            cover = imgs[0].get("src") or ""
        tags = [e.text_content().strip() for e in doc.cssselect("div.tag-list span")]
        tags = [t for t in tags if t]
        # 章节（page_direct?chapter_slot=N）
        chapters = []
        seen = set()
        for a in doc.cssselect("div.comics-chapters > a"):
            href = a.get("href", "")
            m = re.search(r"chapter_slot=([0-9]+)", href)
            if not m:
                continue
            ep = m.group(1)
            if ep in seen:
                continue
            seen.add(ep)
            name = "".join(a.cssselect("div > span")[0].itertext()).strip() if a.cssselect("div > span") else ep
            chapters.append(Chapter(id=ep, name=name, url=href))
        # 正序（页面为倒序）
        chapters.reverse()
        if not chapters:
            # 站点改版实测：章节列表已改为前端渲染，服务端 HTML 只剩
            # <div class="l-content empty_chapters_tips">，规则取不到任何章节。
            # 明确区分"站点要求执行 JS"与"解析规则失效"，避免含糊报"目录为空"。
            _tips = doc.cssselect(".empty_chapters_tips")
            if _tips:
                raise JsRequiredError(
                    "目录由网页脚本渲染（服务端 HTML 只有空章节提示），"
                    "当前引擎不执行 JS，未支持该书源")
            raise MangaError("详情页解析不到章节（站点结构可能已变化）")
        return ComicDetails(id=cid, title=title, cover=cover, sub_title=self.name,
                            description=desc, author=author, tags=tags,
                            chapters=chapters)

    def chapters(self, comic_id):
        return self.comic_info(comic_id).chapters

    def images(self, comic_id, chapter_id):
        cid = _norm_id(comic_id)
        ep = chapter_id
        html = _fetch(APP_CHAPTER.format(cid=cid, ep=ep))
        doc = html_fromstring_safe(lhtml, html)
        imgs = []
        for node in doc.cssselect(".comic-contain .chapter-img .comic-contain__item"):
            url = node.get("data-src") or node.get("src") or ""
            if url and url not in imgs:
                imgs.append(url)
        return imgs

    def image_headers(self, image_url):
        # 包子 CDN 防盗链：Referer 用 appcn 或 baozimh
        return {"User-Agent": UA, "Referer": "https://appcn.baozimh.com/"}
