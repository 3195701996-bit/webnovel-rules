# -*- coding: utf-8 -*-
"""探索/榜单（Legado 标准的 exploreUrl + ruleExplore）。

背景（对照 Venera 的"探索"分页）：我们的源是内置的 Legado 书源，
其中一部分声明了 exploreUrl（分类/榜单入口）与 ruleExplore（列表解析规则）。
本模块只做**有数据支撑**的探索：源没声明探索规则就不出现在探索页，
更不会拿搜索结果或推荐位冒充榜单（诊断 §4 明令禁止）。

实现上复用既有的取页与规则链路（engine/source_mgr._search_one 负责
SSRF 校验/headers/重试/取页），避免另写一套网络与校验逻辑。
"""
import json
import os
import re

from engine.rules import RuleEngine, normalize_url

MAX_PAGE = 50


class ExploreError(Exception):
    """探索取数失败（取页失败 / 规则解析失败）"""


def _loads_lenient(raw):
    """宽松解析 exploreUrl 里的 JSON 数组。

    实测：内置书源的 exploreUrl 是 **JS 风格 JSON**——末尾带尾随逗号
    （`{...},\n]`），严格 json.loads 会直接抛 JSONDecodeError。这里依次尝试：
    严格解析 → 去掉尾随逗号再解析 → 逐条正则提取 title/url（都失败返回 None）。
    不做"猜内容"的修补：只有这两种明确格式，其它一律如实报错。
    """
    try:
        return json.loads(raw)
    except Exception:
        pass
    cleaned = re.sub(r",(\s*[\]}])", r"\1", raw)
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    items = []
    for m in re.finditer(
            r'\{\s*"title"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"url"\s*:\s*"((?:[^"\\]|\\.)*)"',
            raw):
        def _u(x):
            try:
                return json.loads(f'"{x}"')
            except Exception:
                return x
        items.append({"title": _u(m.group(1)), "url": _u(m.group(2))})
    return items or None


def parse_explore(src):
    """解析 exploreUrl → [{"title", "group", "url"}]。

    支持两种写法：
      1. Legado 新版 JSON 数组：[{"title":"排行榜","url":""}, {"title":"总点击","url":"/Ranking_allvisit/{{page}}.html"}]
         其中 url 为空的条目是**分组标题**，随后的条目归到该分组下；
      2. 旧版按行："分组名::/path/{{page}}.html" 或直接 "/path/{{page}}.html"。
    """
    raw = (src.get("exploreUrl") or "").strip()
    if not raw:
        return []
    out = []
    if raw[0] in "[":
        arr = _loads_lenient(raw)
        if arr is None:
            raise ExploreError("exploreUrl 无法解析（既不是严格 JSON，也提取不到 title/url 对）")
        group = ""
        for it in arr if isinstance(arr, list) else []:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip()
            url = str(it.get("url") or "").strip()
            if not url:
                group = title
                continue
            out.append({"title": title or url, "group": group, "url": url})
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if "::" in line:
            title, url = line.split("::", 1)
            out.append({"title": title.strip() or url.strip(), "group": "",
                        "url": url.strip()})
        else:
            out.append({"title": line, "group": "", "url": line})
    return out


def sources_with_explore(sources=None):
    """返回声明了探索规则的源：{uid, name, categories:[{title,group,url}]}。

    只读不写、不联网：没有探索规则的源一律不出现（不生成假榜单）。
    """
    if sources is None:
        from engine.source_mgr import load_all
        sources = load_all() or []
    out = []
    for s in sources:
        try:
            cats = parse_explore(s)
        except ExploreError:
            cats = []
        if not cats:
            continue
        out.append({"uid": s.get("uid") or "", "name": s.get("bookSourceName") or "",
                    "enabled": bool(s.get("enabled", True)),
                    "categories": cats})
    return out


def explore(src, url, page=1, timeout=15):
    """按分类地址取一页书籍列表（复用搜索链路的取页与 SSRF 校验）。"""
    from engine.fetcher import Fetcher
    from engine.source_mgr import _search_one

    base = (src.get("bookSourceUrl") or "").rstrip("/")
    page = max(1, min(int(page or 1), MAX_PAGE))
    clean = (url or "").replace("{{page}}", str(page))
    if "{{" in clean:      # 其它占位符（{{key}} 等）在探索里没有意义，去掉
        clean = re.sub(r"\{\{[^}]*\}\}", "", clean)
    if not clean:
        raise ExploreError("分类地址为空")
    if not clean.startswith("http"):
        clean = base + clean

    fetcher = Fetcher()
    engine = RuleEngine(source=src)
    html, err = _search_one(fetcher, engine, src, clean, {}, "", timeout)
    if html is None:
        raise ExploreError(err or "取页失败")

    rs = src.get("ruleExplore") or {}
    if not rs:
        raise ExploreError("该书源未配置 ruleExplore，无法解析榜单")
    rule_list = rs.get("bookList") or ""
    try:
        elems = engine.get_elements(rule_list, html)
    except Exception as e:
        raise ExploreError(f"规则解析异常：{type(e).__name__}: {str(e)[:80]}") from e
    if not elems:
        return []

    books = []
    for el in elems:
        try:
            name = engine.get_string(rs.get("name", ""), el, base_url=base)
            book_url = engine.get_string(rs.get("bookUrl", ""), el, base_url=base)
            if not name or not book_url:
                continue
            books.append({
                "name": name,
                "author": engine.get_string(rs.get("author", ""), el, base_url=base),
                # 榜单里的链接常是相对路径（实测 "/97310/"）。必须归一成绝对地址：
                # 界面拿它去 POST /api/tasks 建任务，服务端会按公网地址做 SSRF 校验，
                # 相对路径会被直接拒绝（表现为"加入书架失败"）。
                "book_url": normalize_url(book_url, base),
                "cover": engine.get_string(rs.get("coverUrl") or rs.get("cover", ""),
                                           el, base_url=base),
                "intro": engine.get_string(rs.get("intro", ""), el, base_url=base),
                "last_chapter": engine.get_string(rs.get("lastChapter", ""), el, base_url=base),
                "source_uid": src.get("uid") or "",
                "source_name": src.get("bookSourceName") or "",
            })
        except Exception:
            continue
    return books
