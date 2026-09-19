# -*- coding: utf-8 -*-
"""Komiic 适配器（GraphQL API，图片需 Referer 防盗链）"""
import json
import urllib.request

from .base import Comic, ComicDetails, Chapter, MangaAdapter, MangaError

API = "https://komiic.com/api/query"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _query(payload, timeout=20):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(API, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
    except Exception as e:
        raise MangaError(f"Komiic 请求失败: {e}") from e
    if d.get("errors"):
        raise MangaError(f"Komiic: {d['errors'][0]['message']}")
    return d


class Komiic(MangaAdapter):
    key = "komiic"
    name = "Komiic"
    version = "1.0.3"
    concurrent = 3

    def search(self, keyword, page=1):
        q = ("query searchComicAndAuthorQuery($keyword: String!) {\n"
             "  searchComicsAndAuthors(keyword: $keyword) {\n"
             "    comics { id title status year imageUrl authors { name } "
             "categories { name } dateUpdated views favoriteCount }\n"
             "  }\n}")
        d = _query({"operationName": "searchComicAndAuthorQuery",
                    "variables": {"keyword": keyword}, "query": q})
        comics = (d.get("data", {}).get("searchComicsAndAuthors", {}) or {}).get("comics", [])
        out = []
        for c in comics:
            out.append(Comic(
                id=str(c.get("id", "")), title=c.get("title", ""),
                author=", ".join(a.get("name", "") for a in c.get("authors", []) or []),
                cover=c.get("imageUrl", ""),
                tags=[t.get("name", "") for t in c.get("categories", []) or []],
                source_key=self.key))
        return out

    def comic_info(self, comic_id):
        q = ("query chapterByComicId($comicId: ID!) {\n"
             "  chaptersByComicId(comicId: $comicId) { id serial type }\n"
             "}")
        d = _query({"operationName": "chapterByComicId",
                    "variables": {"comicId": comic_id}, "query": q})
        chs = (d.get("data", {}).get("chaptersByComicId", []) or [])
        chapters = []
        for c in chs:
            chapters.append(Chapter(
                id=str(c.get("id", "")),
                name=f"第{c.get('serial')}话" if c.get("serial") else str(c.get("id")),
                group="正篇" if c.get("type") == 0 else "番外"))
        return ComicDetails(id=comic_id, title=comic_id, sub_title=self.name,
                            chapters=chapters)

    def chapters(self, comic_id):
        return self.comic_info(comic_id).chapters

    def images(self, comic_id, chapter_id):
        q = ("query imagesByChapterId($chapterId: ID!) {\n"
             "  imagesByChapterId(chapterId: $chapterId) { id kid }\n"
             "}")
        d = _query({"operationName": "imagesByChapterId",
                    "variables": {"chapterId": chapter_id}, "query": q})
        imgs = d.get("data", {}).get("imagesByChapterId", []) or []
        return [f"https://komiic.com/api/image/{i.get('kid')}" for i in imgs if i.get("kid")]

    def image_headers(self, image_url):
        return {"User-Agent": UA, "Referer": "https://komiic.com/"}
