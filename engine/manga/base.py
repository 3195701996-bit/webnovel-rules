# -*- coding: utf-8 -*-
"""漫画适配器基类与数据模型（参考 venera ComicSource/ComicDetails 接口设计）"""
import codecs
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


def decode_html(raw: bytes, declared_charset: str = "") -> str:
    """按 BOM → 响应声明 → utf-8 → gb18030 的顺序解码 HTML 字节。

    为什么必须这么做：多个适配器原先一律 `read().decode("utf-8", "replace")`，
    遇到源站声明/实际使用非 UTF-8 编码时要么乱码、要么把字节解成含 NUL 的怪串，
    再交给 lxml 就得到 `XMLSyntaxError: encoding not supported USC4 little endian`
    （包子漫画实测就是这个）。
    """
    if not raw:
        return ""
    for bom, enc in ((codecs.BOM_UTF8, "utf-8-sig"),
                     (codecs.BOM_UTF32_LE, "utf-32"),
                     (codecs.BOM_UTF32_BE, "utf-32"),
                     (codecs.BOM_UTF16_LE, "utf-16"),
                     (codecs.BOM_UTF16_BE, "utf-16")):
        if raw.startswith(bom):
            try:
                return raw.decode(enc)
            except (LookupError, UnicodeDecodeError):
                break
    cands = []
    if declared_charset:
        cands.append(declared_charset)
    m = re.search(rb'charset=["\']?([\w-]+)', raw[:2048], re.I)
    if m:
        cands.append(m.group(1).decode("ascii", "ignore"))
    cands += ["utf-8", "gb18030"]
    for enc in cands:
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def html_fromstring_safe(lhtml, text: str):
    """容忍 BOM/NUL/XML 声明与畸形输入的 HTML 解析。

    lxml 解析**字符串**时遇到编码声明会直接报错；先剥掉声明再解析，
    失败再退回按 UTF-8 字节解析（lxml 处理字节时会遵守声明）。
    """
    t = (text or "").lstrip("\ufeff\x00 \t\r\n")
    if t.startswith("<?xml"):
        t = t.split("?>", 1)[-1].lstrip()
    t = t.replace("\x00", "")
    try:
        return lhtml.fromstring(t)
    except Exception:
        return lhtml.fromstring(t.encode("utf-8", "replace"))


class MangaError(Exception):
    """漫画源错误"""


class JsRequiredError(MangaError):
    """该源的内容需要浏览器执行 JS 才能拿到（当前引擎不执行 JS）。

    实测案例：包子漫画（www.baozimh.com → cn.bzmgcn.com）详情页的章节列表改为
    前端渲染，服务端 HTML 里只有 `<div class="l-content empty_chapters_tips">`，
    因此任何 HTML 规则都取不到目录。这类源必须如实标为"未支持"，
    而不是含糊地报"目录为空"或偷偷换通道（设计 §6 明确禁止静默换通道）。
    """


@dataclass
class Comic:
    """搜索结果漫画条目"""
    id: str                # 源内唯一 id（如拷贝漫画 path_word）
    title: str             # 标题
    author: str = ""       # 作者/副标题
    cover: str = ""        # 封面 URL
    tags: List[str] = field(default_factory=list)
    url: str = ""          # 源内详情地址
    source_key: str = ""   # 源 key
    total: int = 0         # R34: 源站搜索结果总数（total 字段，0=未知）


@dataclass
class Chapter:
    """章节（话）"""
    id: str                # 章节 id（如 epId）
    name: str = ""         # 章节名（如"第1話"）
    group: str = ""        # 分组（卷/话组），空为无分组
    url: str = ""          # 章节页地址


@dataclass
class ComicDetails:
    """漫画详情（对应 venera ComicDetails）"""
    id: str
    title: str
    cover: str = ""
    sub_title: str = ""       # 副标题/来源
    description: str = ""     # 简介
    author: str = ""
    tags: List[str] = field(default_factory=list)
    uploader: str = ""
    upload_time: str = ""
    update_time: str = ""
    views: str = ""           # 浏览量
    likes: str = ""           # 点赞数
    url: str = ""
    chapters: List[Chapter] = field(default_factory=list)
    recommend: List[Comic] = field(default_factory=list)


class MangaAdapter:
    """漫画源适配器基类：子类实现以下方法
    - search(keyword, page=1) -> List[Comic]
    - comic_info(comic_id) -> ComicDetails
    - chapters(comic_id) -> List[Chapter]
    - images(comic_id, chapter_id) -> List[str]（图片 URL 列表，按阅读顺序）
    - image_headers(image_url) -> Dict[str,str]（图片防盗链 headers，可选）
    - image_url(image_url) -> str（图片实际请求 URL 覆盖，可选，如签名 URL）
    - on_image_failed(image_url) -> Dict | None（图片加载失败时动态刷新配置，可选）
    """
    key: str = ""            # 源唯一 key
    name: str = ""           # 源显示名
    version: str = "1.0.0"
    concurrent: int = 2      # 该源并发上限（防封）

    def __init__(self, state_dir=None):
        self.state_dir = state_dir  # 状态持久化目录（设备指纹等）

    def search(self, keyword: str, page: int = 1) -> List[Comic]:
        raise NotImplementedError

    def comic_info(self, comic_id: str) -> ComicDetails:
        raise NotImplementedError

    def chapters(self, comic_id: str) -> List[Chapter]:
        raise NotImplementedError

    def images(self, comic_id: str, chapter_id: str) -> List[str]:
        raise NotImplementedError

    def image_headers(self, image_url: str) -> Dict[str, str]:
        """图片防盗链请求头（子类可覆盖）"""
        return {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}

    def image_url(self, image_url: str) -> str:
        """图片实际请求 URL（子类可覆盖，如动态签名）"""
        return image_url

    def on_image_failed(self, image_url: str) -> Optional[Dict]:
        """图片加载失败回调：返回新 {url?, headers?} 配置或 None"""
        return None
