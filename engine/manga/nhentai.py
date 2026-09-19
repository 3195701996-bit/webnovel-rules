# -*- coding: utf-8 -*-
"""nhentai 适配器（v2 API 优先，HTML 详情页兜底；无签名/加密）

转写自 venera nhentai.js（v1.1.0）：
- 搜索: GET /api/v2/search?query=...&page=N&sort=date
- 详情: GET /api/v2/galleries/{id}?include=related,favorite （失败回退 HTML 页解析）
- 图片: GET /api/v2/galleries/{id} -> pages[].path 拼 imageServer
- 防盗链: Referer=https://nhentai.net/
"""
import json
import re
import urllib.parse
import urllib.request

from .base import Comic, ComicDetails, Chapter, MangaAdapter, MangaError

BASE = "https://nhentai.net"
API = "https://nhentai.net/api/v2"
IMG_SERVER = "https://i3.nhentai.net"
THUMB_SERVER = "https://t3.nhentai.net"
API_UA = "Venera/1.0 (+https://github.com/venera-app/venera)"

# JS 内置语言 tag id
_LANG_IDS = {12227: "English", 6346: "日本語", 29963: "中文"}

_API_HEADERS = {"User-Agent": API_UA, "Accept": "application/json"}
_HTML_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
}


def _get(url, headers, timeout=10):
    """单次请求超时 **10 秒**（原 20s）。

    实测（2026-09-17）：该域在本机不可达时，`urlopen` 会**逐个地址**去连
    （IPv4/IPv6、多条 A 记录），单次搜索因此等了 120 秒。适配器的超时
    应当是"一次尝试"的上限，而不是能被地址数量乘出来的等待。
    """
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:
        raise MangaError(f"nhentai 请求失败: {e}") from e


def _get_json(url):
    status, body = _get(url, _API_HEADERS)
    if status != 200:
        raise MangaError(f"nhentai API HTTP {status}")
    return json.loads(body.decode("utf-8", "ignore"))


def _fix_ext(url):
    """JS _fixAndWrap: 折叠重复扩展名，cover 图强制走 t3 服务器"""
    if not url:
        return ""
    def _collapse(m):
        return re.search(r"\.(jpg|png|webp|gif)", m.group(0)).group(0)
    url = re.sub(r"(\.(?:jpg|png|webp|gif))+", _collapse, url)
    if "/cover." in url:
        url = re.sub(r"https?://[it]\d\.nhentai\.net", THUMB_SERVER, url)
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith("http"):
        url = "https://" + url.lstrip("/")
    return url


def _media_url(path, is_thumb=False):
    """JS toAbsoluteMediaUrl"""
    if not path:
        return ""
    if path.startswith("http"):
        return path
    if path.startswith("//"):
        return "https:" + path
    path = path.lstrip("/")
    if "cover" in path or "thumb" in path:
        is_thumb = True
    return f"{THUMB_SERVER if is_thumb else IMG_SERVER}/{path}"


def _normalize_id(cid):
    cid = str(cid or "")
    for prefix in ("nhentai", "nh"):
        if cid.startswith(prefix):
            return cid[len(prefix):]
    return cid


def _tag_namespace(tag_type):
    return {
        "language": "Languages", "artist": "Artists", "character": "Characters",
        "group": "Groups", "parody": "Parodies", "category": "Categories",
    }.get((tag_type or "").lower(), "Tags")


class Nhentai(MangaAdapter):
    key = "nhentai"
    name = "nhentai"
    version = "1.1.0"
    concurrent = 2

    # ---------- 解析 ----------

    def _comic_from_api(self, item):
        tag_ids = item.get("tag_ids") or []
        lang = "Unknown"
        for tid, name in _LANG_IDS.items():
            if tid in tag_ids:
                lang = name
                break
        tags = []
        api_tags = item.get("tags") or []
        if api_tags:
            tags = [t.get("name", "") for t in api_tags if t and t.get("name")]
        cover = _media_url(item.get("thumbnail") or "", True)
        return Comic(
            id=str(item.get("id", "")),
            title=item.get("english_title") or item.get("japanese_title") or str(item.get("id", "")),
            author=lang,
            cover=cover,
            tags=tags,
            url=f"{BASE}/g/{item.get('id', '')}/",
            source_key=self.key,
        )

    # ---------- 接口 ----------

    def search(self, keyword, page=1, sort="date"):
        kw = urllib.parse.quote(keyword)
        data = _get_json(f"{API}/search?query={kw}&page={page}&sort={sort}")
        return [self._comic_from_api(e) for e in data.get("result") or []]

    def comic_info(self, comic_id):
        cid = _normalize_id(comic_id)
        status, body = _get(f"{API}/galleries/{cid}?include=related,favorite", _API_HEADERS)
        if status == 200 and body:
            return self._info_from_api(cid, body)
        return self._info_from_html(cid)

    def _info_from_api(self, cid, body):
        data = json.loads(body.decode("utf-8", "ignore"))
        title = (data.get("title") or {}).get("pretty") or (data.get("title") or {}).get("english") or str(cid)
        cover = _media_url((data.get("cover") or {}).get("path") or (data.get("thumbnail") or {}).get("path") or "", True)

        tag_names, authors, groups = [], [], []
        for tag in data.get("tags") or []:
            ns = (tag.get("type") or "tag").lower()
            name = tag.get("name")
            if not name:
                continue
            if ns == "artist":
                authors.append(name)
            elif ns == "group":
                groups.append(name)
            else:
                tag_names.append(name)

        ts = data.get("upload_date")
        upload_time = ""
        if isinstance(ts, (int, float)):
            import datetime
            upload_time = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

        related = [self._comic_from_api(e) for e in data.get("related") or []]
        chs = [Chapter(id=str(cid), name=f"Gallery #{cid} ({data.get('num_pages', 0)}P)",
                       url=f"{BASE}/g/{cid}/")]
        return ComicDetails(
            id=str(cid), title=title, cover=cover,
            sub_title=(data.get("title") or {}).get("english", "") or "",
            description=f"nhentai #{cid}", author=", ".join(authors + groups)[:60],
            tags=tag_names, upload_time=upload_time,
            url=f"{BASE}/g/{cid}/", chapters=chs, recommend=related)

    def _info_from_html(self, cid):
        status, body = _get(f"{BASE}/g/{cid}/", _HTML_HEADERS)
        if status != 200:
            raise MangaError(f"nhentai 详情 HTTP {status}")
        html = body.decode("utf-8", "ignore")
        m = re.search(r'<h1 class="title">(.*?)</h1>', html, re.S)
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else str(cid)
        m = re.search(r'<div id="cover">.*?<img[^>]+(?:data-src|src)="([^"]+)"', html, re.S)
        cover = _fix_ext(m.group(1)) if m else ""
        tags = [re.sub(r"<[^>]+>", "", t).strip()
                for t in re.findall(r'<span class="name">(.*?)</span>', html, re.S)]
        m = re.search(r'<time[^>]+datetime="([^"]+)"', html)
        upload_time = m.group(1) if m else ""
        chs = [Chapter(id=str(cid), name=f"Gallery #{cid}", url=f"{BASE}/g/{cid}/")]
        return ComicDetails(id=str(cid), title=title, cover=cover, sub_title=self.name,
                            description=f"nhentai #{cid}", tags=tags,
                            upload_time=upload_time, url=f"{BASE}/g/{cid}/", chapters=chs)

    def chapters(self, comic_id):
        cid = _normalize_id(comic_id)
        return [Chapter(id=str(cid), name=f"Gallery #{cid}", url=f"{BASE}/g/{cid}/")]

    def images(self, comic_id, chapter_id=None):
        cid = _normalize_id(comic_id)
        data = _get_json(f"{API}/galleries/{cid}")
        pages = data.get("pages") or []
        out = [_media_url(p.get("path"), False) for p in pages if p.get("path")]
        if not out:
            raise MangaError(f"nhentai #{cid} 无图片")
        return out

    def image_headers(self, image_url):
        return {"User-Agent": "Mozilla/5.0", "Referer": f"{BASE}/"}

    def image_url(self, image_url):
        return _fix_ext(image_url)
