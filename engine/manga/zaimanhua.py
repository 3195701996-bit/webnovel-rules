# -*- coding: utf-8 -*-
"""再漫画（zaimanhua.com）适配器（App v4 API，匿名访问无需签名，登录可选 Bearer token）"""
import json
import re
import urllib.parse
import urllib.request

from .base import Comic, ComicDetails, Chapter, MangaAdapter, MangaError

API = "https://v4api.zaimanhua.com/app/v1/"
SITE = "https://www.zaimanhua.com/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android) Mobile",
    "authorization": "Bearer ",
}

_CH_NAME_RE = re.compile(r"^(?:连载版?)?(\d+\.?\d*)([话卷])?$")


def _fmt_chapter_title(title):
    r"""JS: chapter_title.replace(/^(?:连载版?)?(\d+\.?\d*)([话卷])?$/, '第{n}{t||话}')"""
    m = _CH_NAME_RE.match(title or "")
    if not m:
        return title
    return f"第{m.group(1)}{m.group(2) or '话'}"


def _safe(value):
    return str(value) if value is not None else ""


def _parse_comic(item):
    cid = ""
    for cand in (item.get("comic_id"), item.get("id")):
        if cand and str(cand) != "0":
            cid = _safe(cand)
            break
    tags = []
    if item.get("status"):
        tags.append(_safe(item.get("status")))
    tags.extend([t for t in _safe(item.get("types")).split("/") if t])
    desc = ""
    for cand in (item.get("description"),
                 item.get("last_update_chapter_name"),
                 item.get("last_name")):
        if cand:
            desc = _safe(cand)
            break
    return Comic(
        id=cid,
        title=item.get("title") or item.get("name") or "",
        author=_safe(item.get("authors")),
        cover=item.get("cover") or "",
        tags=tags,
        url=f"{SITE}comics/id/{cid}" if cid else "",
        source_key="zaimanhua",
    )


class Zaimanhua(MangaAdapter):
    key = "zaimanhua"
    name = "再漫画"
    version = "1.0.2"
    concurrent = 2

    def _get(self, path, timeout=15):
        url = API + path
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
        except Exception as e:
            raise MangaError(f"再漫画 请求失败: {e}") from e
        try:
            return json.loads(body)
        except Exception as e:
            raise MangaError(f"再漫画 响应解析失败: {e}") from e

    def search(self, keyword, page=1):
        kw = urllib.parse.quote(keyword)
        data = self._get(f"search/index?keyword={kw}&page={page}&sort=0&size=20")
        lst = ((data.get("data") or {}).get("list")) or []
        return [_parse_comic(item) for item in lst]

    def comic_info(self, comic_id):
        resp = self._get(f"comic/detail/{comic_id}?channel=android")
        if resp.get("errno") != 0:
            raise MangaError(f"再漫画 详情加载失败: {resp.get('errmsg') or resp}")
        data = (resp.get("data") or {}).get("data") or {}

        authors = [t.get("tag_name", "") for t in data.get("authors") or []]
        status = [t.get("tag_name", "") for t in data.get("status") or []]
        types = [t.get("tag_name", "") for t in data.get("types") or []]
        last_ch = data.get("last_update_chapter_name") or ""
        tags = status + types + ([last_ch] if last_ch else [])

        return ComicDetails(
            id=str(comic_id),
            title=data.get("title") or "",
            cover=data.get("cover") or "",
            sub_title=self.name,
            description=data.get("description") or "",
            author=", ".join(authors),
            tags=[t for t in tags if t],
            update_time=self._fmt_ts(data.get("last_updatetime")),
            url=f"{SITE}comics/id/{comic_id}",
            chapters=self._parse_chapters(data),
        )

    def _parse_chapters(self, data):
        chs = []
        for group in data.get("chapters") or []:
            gtitle = group.get("title") or "默认"
            for ch in reversed(group.get("data") or []):
                chs.append(Chapter(
                    id=str(ch.get("chapter_id")),
                    name=_fmt_chapter_title(ch.get("chapter_title")),
                    group=gtitle,
                ))
        return chs

    @staticmethod
    def _fmt_ts(ts):
        if not ts:
            return ""
        try:
            import datetime
            return datetime.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
        except Exception:
            return ""

    def chapters(self, comic_id):
        return self.comic_info(comic_id).chapters

    def images(self, comic_id, chapter_id):
        data = self._get(f"comic/chapter/{comic_id}/{chapter_id}")
        dd = (data.get("data") or {}).get("data") or {}
        return list(dd.get("page_url_hd") or dd.get("page_url") or [])

    def image_headers(self, image_url):
        return {
            "User-Agent": HEADERS["User-Agent"],
            "Referer": SITE,
        }
