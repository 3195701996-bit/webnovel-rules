# -*- coding: utf-8 -*-
"""优酷漫画适配器（HTML 解析型：www.ykmh.net 搜索 + m.ykmh.net 详情/章节）
转写自 venera-configs ykmh.js
- 搜索：www.ykmh.net/search/?keywords=...
- 详情：m.ykmh.net 移动页（BarTit/Cover/txtItme/comic-chapters 正则解析）
- 图片：章节页 var chapterImages = [...] JSON 数组
- 无签名/加密，图片防盗链 Referer 用 m.ykmh.net
"""
import json
import re
import urllib.parse
import urllib.request

from .base import Comic, ComicDetails, Chapter, MangaAdapter, MangaError

BASE = "https://www.ykmh.net"
MBASE = "https://m.ykmh.net"
UA = "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36 Edg/139.0.0.0"
UA_MOBILE = "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1"
DEFAULT_COVER = MBASE + "/images/default/cover.png"

STATUS_WORDS = ("连载中", "已完结", "完结", "连载", "暂停", "休刊")


def _fetch(url, ua=UA, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Referer": BASE + "/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        raise MangaError(f"优酷漫画请求失败: {e}") from e


def _abs(url, base):
    if not url:
        return ""
    if url.startswith("http"):
        return url
    return base + (url if url.startswith("/") else "/" + url)


class Ykmh(MangaAdapter):
    key = "ykmh"
    name = "优酷漫画"
    version = "1.0.0"
    concurrent = 2

    # ── 搜索 ──
    def search(self, keyword, page=1):
        kw = urllib.parse.quote(keyword)
        url = f"{BASE}/search/?keywords={kw}"
        if page and page > 1:
            url += f"&page={page}"
        html = _fetch(url)
        out = []
        pat = re.compile(
            r'<li class="list-comic" data-key="(\d+)"><a class="image-link"\s+'
            r'href="([^"]+)"\s+title="([^"]+)"><img src="([^"]+)"[^>]*></a>'
            r'\s*<p><a href="[^"]*"[^>]*>([^<]+)</a></p>'
            r'\s*<p class="auth"><a href="[^"]*">([^<]*)</a></p>'
            r'\s*<p class="newPage">([^<]*)</p>')
        for m in pat.finditer(html):
            author = m.group(6) or "未知作者"
            latest = m.group(7) or ""
            tags = [t for t in (author, latest) if t]
            out.append(Comic(
                id=m.group(2), title=m.group(3).strip(), author=author,
                cover=_abs(m.group(4), BASE), tags=tags,
                url=_abs(m.group(2), BASE), source_key=self.key))
        # 备用：宽松匹配 list-comic 块
        if not out:
            for m in re.finditer(
                    r'<li class="list-comic"[^>]*>[\s\S]*?<a[^>]+href="([^"]+)"[^>]*'
                    r'title="([^"]+)"[^>]*>[\s\S]*?<img src="([^"]+)"', html):
                out.append(Comic(id=m.group(1), title=m.group(2).strip(),
                                 cover=_abs(m.group(3), BASE),
                                 url=_abs(m.group(1), BASE), source_key=self.key))
        return out

    # ── 详情 ──
    def _mobile_url(self, comic_id):
        cid = comic_id or ""
        if cid.startswith("https://www.ykmh.net/"):
            url = cid.replace("https://www.ykmh.net/", MBASE + "/", 1)
        elif cid.startswith("http"):
            url = cid
        elif cid.startswith("/"):
            url = MBASE + cid
        else:
            url = MBASE + "/" + cid
        if not url.endswith("/"):
            url += "/"
        return url

    def _parse_info(self, html):
        title, cover, author, status, desc = "未知标题", DEFAULT_COVER, "未知作者", "未知状态", "暂无描述"
        tags = []
        m = re.search(r'<div class="BarTit" id="comicName">([^<]+)</div>', html)
        if m:
            title = m.group(1).strip()
        m = re.search(r'<div class="pic" id="Cover">\s*<mip-img src="([^"]+)"', html) \
            or re.search(r'<mip-img src="([^"]+)"', html)
        if m:
            cover = m.group(1)
        m = re.search(r'<p class="txtItme">\s*<span class="icon icon01"></span>\s*'
                      r'<a href="[^"]*">([^<]+)</a>\s*</p>', html) \
            or re.search(r'<span class="icon icon01"></span>\s*<a href="[^"]*">([^<]+)</a>', html)
        if m:
            author = m.group(1).strip()
        for item in re.findall(r'<p class="txtItme">[\s\S]*?</p>', html):
            if "icon icon02" not in item:
                continue
            for tm in re.finditer(r'<a href="[^"]*/list/[^"]*/">([^<]+)</a>', item):
                t = tm.group(1).strip()
                if t and t not in tags:
                    tags.append(t)
                    if t in STATUS_WORDS:
                        status = t
        m = re.search(r'<mip-showmore[^>]*id="showmore-des">\s*([^<]+(?:<[^>]+>[^<]*</[^>]+>[^<]*)*?)\s*</mip-showmore>', html)
        if m:
            desc = re.sub(r"<[^>]+>", "", m.group(1))
            desc = re.sub(r"^\s*介绍[:：]\s*", "", desc).strip() or desc
        return title, cover, author, status, tags, desc

    def _parse_chapters(self, html):
        """返回 List[Chapter]：多分组时带 group 名，否则合并"""
        chapters = []
        seen = set()
        group_pat = re.compile(
            r'<div class="comic-chapters">[\s\S]*?<span class="Title">([^<]+)</span>'
            r'[\s\S]*?<ul id="chapter-list-(\d+)"[^>]*>([\s\S]*?)</ul>')
        li_pat = re.compile(r'<li>\s*<a href="([^"]+)"[^>]*>\s*<span>([^<]+)</span>\s*</a>\s*</li>')
        groups = group_pat.findall(html)
        multi = len([g for g in groups if li_pat.search(g[2])]) > 1
        for gtitle, _gid, content in groups:
            gtitle = gtitle.strip()
            for cm in li_pat.finditer(content):
                curl, ctitle = cm.group(1), cm.group(2).strip()
                curl = _abs(curl, MBASE)
                if curl in seen:
                    continue
                seen.add(curl)
                group = "" if gtitle == "连载列表" or not multi else gtitle
                name = ctitle if gtitle == "连载列表" else f"[{gtitle}] {ctitle}"
                chapters.append(Chapter(id=curl, name=name, group=group, url=curl))
        if not chapters:
            for cm in li_pat.finditer(html):
                curl = _abs(cm.group(1), MBASE)
                if curl in seen:
                    continue
                seen.add(curl)
                chapters.append(Chapter(id=curl, name=cm.group(2).strip(), url=curl))
        return chapters

    def comic_info(self, comic_id):
        if not comic_id:
            raise MangaError("ID不能为空")
        url = self._mobile_url(comic_id)
        html = _fetch(url, ua=UA)
        title, cover, author, status, tags, desc = self._parse_info(html)
        chapters = self._parse_chapters(html)
        if status and status != "未知状态":
            tags = [status] + [t for t in tags if t != status]
        return ComicDetails(
            id=comic_id, title=title, cover=cover, sub_title=self.name,
            description=desc, author=author, tags=tags, url=url,
            chapters=chapters)

    def chapters(self, comic_id):
        return self.comic_info(comic_id).chapters

    # ── 章节图片 ──
    def images(self, comic_id, chapter_id):
        if not chapter_id:
            raise MangaError("章节ID不能为空")
        url = chapter_id
        if not url.startswith("http"):
            url = MBASE + (url if url.startswith("/") else "/" + url)
        elif url.startswith("https://www.ykmh.net/"):
            url = url.replace("https://www.ykmh.net/", MBASE + "/", 1)
        html = _fetch(url, ua=UA_MOBILE)
        images = []
        m = re.search(r'var\s+chapterImages\s*=\s*(\[.*?\]);', html)
        if m:
            try:
                for img in json.loads(m.group(1)):
                    if isinstance(img, str) and img:
                        images.append(_abs(img, MBASE))
            except Exception:
                pass
        if not images:
            for im in re.finditer(r'<img[^>]+src="([^"]+)"[^>]*>', html):
                src = im.group(1)
                if any(k in src for k in ("cover", "avatar", "logo", "icon", "banner")):
                    continue
                images.append(_abs(src, MBASE))
        return images

    def image_headers(self, image_url):
        # 防盗链：Referer 用移动站，移动端 UA
        return {"User-Agent": UA_MOBILE, "Referer": MBASE + "/"}
