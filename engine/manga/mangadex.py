# -*- coding: utf-8 -*-
"""MangaDex 适配器（公开 API，无需签名）"""
import json
import urllib.parse
import urllib.request

from .base import Comic, ComicDetails, Chapter, MangaAdapter, MangaError

API = "https://api.mangadex.org"
UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"}


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        raise MangaError(f"MangaDex 请求失败: {e}") from e


def _title_of(attrs):
    titles = dict(attrs.get("title") or {})
    for at in attrs.get("altTitles") or []:
        for lang, v in at.items():
            titles.setdefault(lang, v)
    for lang in ("zh", "zh-hk", "zh-tw", "en", "ja"):
        if titles.get(lang):
            return titles[lang]
    return next(iter(titles.values()), "")


def _cover_of(data):
    for rel in data.get("relationships") or []:
        if rel.get("type") == "cover_art":
            fn = (rel.get("attributes") or {}).get("fileName")
            if fn:
                return f"https://mangadex.org/covers/{data['id']}/{fn}.256.jpg"
    return ""


def _authors_of(data):
    return [r.get("attributes", {}).get("name", "")
            for r in (data.get("relationships") or [])
            if r.get("type") in ("author", "artist")
            and r.get("attributes", {}).get("name")]


class MangaDex(MangaAdapter):
    key = "mangadex"
    name = "MangaDex"
    version = "1.0.0"
    concurrent = 3

    def search(self, keyword, page=1):
        kw = urllib.parse.quote(keyword)
        url = (f"{API}/manga?title={kw}&limit=30&offset={(page-1)*30}"
               f"&includes[]=cover_art&includes[]=author&hasAvailableChapters=true")
        data = _get(url)
        out = []
        for item in data.get("data", []):
            attrs = item.get("attributes", {})
            out.append(Comic(
                id=item["id"], title=_title_of(attrs),
                author=", ".join(_authors_of(item))[:40],
                cover=_cover_of(item),
                tags=[t.get("attributes", {}).get("name", {}).get("en", "")
                      for t in attrs.get("tags", [])],
                url=f"https://mangadex.org/title/{item['id']}",
                source_key=self.key))
        return out

    # ── 排行/分类浏览（Legado 的 exploreUrl 在漫画侧没有对应物，
    # 因此由适配器自己声明可用的分类；顺序即界面展示顺序）──
    BROWSE = (
        ("popular", "按热度", "order[followedCount]=desc"),
        ("latest", "最近更新", "order[latestUploadedChapter]=desc"),
        ("new", "新上架", "order[createdAt]=desc"),
        ("rating", "按评分", "order[rating]=desc"),
    )

    def categories(self):
        """返回可浏览的分类（key, 显示名, 分组）。四个入口都是排序口径，
        因此统一归到「排行」组——分组会原样出现在探索页，不能乱标。"""
        return [{"key": k, "name": n, "group": "排行"} for k, n, _ in self.BROWSE]

    def browse(self, category="popular", page=1):
        """按分类浏览。参数写法实测很关键：contentRating[]=safe 才对，
        contentRating[safe] 会直接 400（本项目探测时踩过）。"""
        order = next((o for k, _, o in self.BROWSE if k == category),
                     self.BROWSE[0][2])
        url = (f"{API}/manga?limit=30&offset={(page - 1) * 30}"
               f"&{order}&includes[]=cover_art&includes[]=author"
               f"&hasAvailableChapters=true"
               f"&contentRating[]=safe&contentRating[]=suggestive")
        data = _get(url)
        out = []
        for item in data.get("data", []):
            attrs = item.get("attributes", {})
            out.append(Comic(
                id=item["id"], title=_title_of(attrs),
                author=", ".join(_authors_of(item))[:40],
                cover=_cover_of(item),
                tags=[t.get("attributes", {}).get("name", {}).get("en", "")
                      for t in attrs.get("tags", [])],
                url=f"https://mangadex.org/title/{item['id']}",
                source_key=self.key))
        return out

    def comic_info(self, comic_id):
        d = _get(f"{API}/manga/{comic_id}?includes[]=cover_art&includes[]=artist&includes[]=author")
        data = d.get("data", {})
        attrs = data.get("attributes", {})
        tags = [t.get("attributes", {}).get("name", {}).get("en", "")
                for t in attrs.get("tags", [])]
        # 章节（按卷分组）
        chs = []
        try:
            feed = _get(f"{API}/manga/{comic_id}/feed?limit=500&translatedLanguage[]=zh&translatedLanguage[]=en&order[chapter]=asc")
            for ch in feed.get("data", []):
                ca = ch.get("attributes", {})
                if ca.get("externalUrl"):
                    continue  # 外部链接章节（无图片托管）
                num = ca.get("chapter") or "Oneshot"
                ttl = ca.get("title")
                name = f"{num}: {ttl}" if ttl else num
                vol = f"Vol.{ca.get('volume')}" if ca.get("volume") else "No Volume"
                chs.append(Chapter(id=ch["id"], name=name, group=vol))
        except Exception:
            pass
        return ComicDetails(
            id=comic_id, title=_title_of(attrs), cover=_cover_of(data),
            sub_title=self.name, description=attrs.get("description", {}).get("en", ""),
            author=", ".join(_authors_of(data))[:60], tags=tags,
            update_time=attrs.get("updatedAt", ""), upload_time=attrs.get("createdAt", ""),
            url=f"https://mangadex.org/title/{comic_id}", chapters=chs)

    def chapters(self, comic_id):
        return self.comic_info(comic_id).chapters

    def images(self, comic_id, chapter_id):
        d = _get(f"{API}/at-home/server/{chapter_id}")
        ch = d.get("chapter", {})
        h = ch.get("hash", "")
        files = ch.get("dataSaver") or ch.get("data") or []
        base = "data-saver" if ch.get("dataSaver") else "data"
        return [f"https://uploads.mangadex.org/{base}/{h}/{f}" for f in files]

    def image_headers(self, image_url):
        return {"User-Agent": UA["User-Agent"], "Referer": "https://mangadex.org/"}
