#!/usr/bin/env python3
"""自动书源生成：对存活站点自动分析搜索页结构，生成 JSON 书源（搜索规则）+ 引擎兜底（目录/正文）"""
import re
from urllib.parse import quote, urljoin

from .fetcher import Fetcher
from .rules import parse_html


def _css_path(el):
    """生成元素 CSS 路径（tag + class 链）"""
    parts = []
    node = el
    while node is not None and node.tag not in ('html',):
        tag = node.tag or ''
        if not isinstance(tag, str):
            break
        cls = (node.get('class') or '').strip()
        sel = tag + ('.' + '.'.join(cls.split()[:2]) if cls else '')
        parts.append(sel)
        node = node.getparent()
        if len(parts) > 5:
            break
    return ' > '.join(reversed(parts))


def _is_book_url(href):
    """判断链接是否像书籍页（排除导航/搜索/标签页）"""
    if not href:
        return False
    if re.search(r'(search|so/|tag|sort|login|bookcase|history|javascript|#|sitemap|map)', href, re.I):
        return False
    if href.startswith('/') or href.startswith('http'):
        return True
    return False


def auto_generate(base_url, keyword='剑来', timeout=8):
    """
    自动分析站点搜索页生成书源。
    返回 (source_dict, sample) 或 (None, error)
    """
    fetcher = Fetcher()
    base = base_url.rstrip('/')
    kw = quote(keyword)
    # 常见 CMS/小说站搜索路径（GET）
    candidates = [
        f"/search?q={kw}", f"/search?keyword={kw}", f"/search/?q={kw}",
        f"/s?q={kw}", f"/so/?keyword={kw}", f"/search.php?keyword={kw}",
        f"/modules/article/search.php?searchkey={kw}",
        f"/search.html?keyword={kw}", f"/find?q={kw}", f"/so?keyword={kw}",
        f"/search.php?q={kw}", f"/s.php?q={kw}", f"/search.php?key={kw}",
        f"/search/?keyword={kw}", f"/novel/search?q={kw}",
        f"/book/search?q={kw}", f"/searchall?key={kw}",
        f"/module/search/index.php?q={kw}", f"/index.php?m=search&q={kw}",
        f"/so/search.php?keyword={kw}", f"/search.htm?keyword={kw}",
        f"/searchpage?keyword={kw}", f"/s.html?key={kw}",
    ]
    search_url = None
    html = None
    for path in candidates:
        try:
            r = fetcher.session.get(base + path, timeout=timeout,
                                    headers=fetcher.parse_header({}))
            if r.status_code == 200:
                t = r.text
                if keyword in t and len(t) > 800:
                    search_url = base + path
                    html = t
                    break
        except Exception:
            continue
    if not search_url:
        # 兜底 1：从首页解析搜索表单（GET 或 POST）action + 参数名
        try:
            r0 = fetcher.session.get(base + '/', timeout=timeout,
                                     headers=fetcher.parse_header({}))
            doc0 = parse_html(r0.text)
            if doc0 is not None:
                for form in doc0.cssselect('form'):
                    action = form.get('action') or ''
                    method = (form.get('method') or 'get').lower()
                    for inp in form.cssselect('input[name]'):
                        nm = inp.get('name', '')
                        if nm in ('q', 'key', 'keyword', 'searchkey', 'search_key',
                                  'wd', 'search', 'searchWord', 'keyWord',
                                  'searchKey', 's', 'name', 'bookname'):
                            act = urljoin(base, action) if action else base + '/'
                            if method == 'post':
                                # POST 搜索：尝试发 POST，成功则记录
                                # 注意：f-string 中 {{{{key}}}} 才渲染为模板 {{key}}——
                                # 此前写成 {{key}} 实际产出单花括号 {key}，引擎
                                # fill_template 不识别，生成的书源搜索永远发字面量
                                try:
                                    rr = fetcher.session.post(
                                        act, data={nm: keyword},
                                        timeout=timeout,
                                        headers=fetcher.parse_header({}))
                                    if rr.status_code == 200 and keyword in rr.text:
                                        search_url = (f"{act},{{\"method\":\"POST\","
                                                      f"\"body\":\"{nm}={{{{key}}}}\"}}")
                                        html = rr.text
                                        break
                                except Exception:
                                    search_url = None
                            else:
                                sep = '&' if '?' in act else '?'
                                search_url = f"{act}{sep}{nm}={{{{key}}}}"
                                try:
                                    rr = fetcher.session.get(search_url.replace('{{key}}', kw), timeout=timeout,
                                                             headers=fetcher.parse_header({}))
                                    if rr.status_code == 200 and keyword in rr.text:
                                        html = rr.text
                                        break
                                except Exception:
                                    search_url = None
                    if search_url:
                        break
        except Exception:
            pass
    if not search_url:
        return None, '未找到可用搜索接口'

    doc = parse_html(html)
    if doc is None:
        return None, 'HTML 解析失败'

    # 找书名链接（文本含关键词）
    links = [a for a in doc.cssselect('a[href]') if keyword in (a.text_content() or '')]
    book_links = [a for a in links if _is_book_url(a.get('href', ''))]
    if not book_links:
        return None, '搜索页未找到书籍链接'
    first_a = book_links[0]
    book_url = urljoin(base, first_a.get('href', ''))

    # 最小公共祖先容器（向上找到包含 >=2 个书籍链接的节点，最多 4 层）
    container = first_a
    for _ in range(4):
        parent = container.getparent()
        if parent is None:
            break
        cnt = len([a for a in parent.cssselect('a[href]') if _is_book_url(a.get('href',''))])
        if cnt >= 2 and len(parent.cssselect('a[href]')) <= 50:
            container = parent
            break
        container = parent

    book_list_sel = _css_path(container)
    source = {
        'bookSourceName': base.replace('https://', '').replace('http://', ''),
        'bookSourceUrl': base,
        'bookSourceType': 0,
        'bookSourceGroup': '自动生成',
        'bookSourceComment': '自动分析生成（搜索规则自适应，目录/正文用引擎兜底）',
        'enabled': True,
        'searchUrl': re.sub(re.escape(kw), '{{key}}', search_url),
        'ruleSearch': {
            'bookList': book_list_sel,
            'name': 'a@text',
            'author': '',
            'bookUrl': 'a@href',
            'intro': '', 'kind': '',
        },
        'ruleBookInfo': {},   # 引擎兜底
        'ruleToc': {},        # 引擎兜底
        'ruleContent': {},    # 引擎兜底
    }
    return source, {'name': first_a.text_content().strip()[:30],
                    'url': book_url[:60], 'list_sel': book_list_sel}
