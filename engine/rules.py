#!/usr/bin/env python3
"""
legado（阅读 App）书源规则执行引擎（Python 子集实现）
=====================================================
支持规则类型：
  - CSS 选择器（默认 / @css: / @jsoup:）—— lxml.cssselect
  - JSONPath（@json: / $.）—— jsonpath-ng
  - 正则（@正则: / @regex: / @reg:）
  - 模板 {{key}} / {{page}} / {{searchKey}} / {{$.x}} / {{book.x}}
  - ##正则##替换 结果清洗（##pat##repl### = replaceFirst）
  - || 多规则依次尝试；@@ 链式（前段结果作为后段输入）
  - 元素操作：@text @href @src @html @content @all @attr:xx @ownText @textNodes
  - !N 后缀：丢弃前 N 个元素（如 "dl@dd!0"）
不支持：@js: / @webjs:（JS 执行），@XPath:，登录流程。
"""
import re
import json
import functools
from urllib.parse import urljoin

from lxml import html as lxml_html
from lxml import etree as lxml_etree
from lxml.cssselect import CSSSelector as _CSSSelector
from jsonpath_ng import parse as jp_parse


# ── 编译缓存（P2）：cssselect/JSONPath 表达式 → 编译结果 ──
# lru_cache 自带锁，线程安全；非法表达式抛异常不会被缓存（每次重抛），
# 与原 cssselect()/jp_parse() 行为一致。键为表达式字符串本身，无歧义。
@functools.lru_cache(maxsize=512)
def _css_compile(sel):
    return _CSSSelector(sel)


@functools.lru_cache(maxsize=512)
def _jp_compile(path):
    return jp_parse(path)

# ── HTML / 文本工具 ──────────────────────────

def parse_html(text):
    """解析 HTML 为 lxml 元素；失败返回 None。
    幂等（P0-3）：输入已是 lxml 元素时直接返回，不重复解析；
    仅 str/bytes 走 lxml.fromstring。所有公共入口（get_string/
    get_strings/get_elements 经 _select_css/_select_xpath 到此）
    因此既能接受原始 HTML 字符串，也能接受已解析文档。"""
    if isinstance(text, lxml_etree._Element):
        return text
    try:
        return lxml_html.fromstring(text)
    except Exception:
        try:
            return lxml_html.fromstring(text.encode('utf-8', 'ignore'))
        except Exception:
            return None


# 块级标签（与旧实现 cssselect 标签列表一致）
_BLOCK_TAGS = frozenset(('p', 'div', 'section', 'article',
                         'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li'))


def _el_text(el):
    """元素文本（含后代）。<br> 与块级闭合标签转成换行，避免整段粘连（如精华书阁正文）。
    P2：单趟遍历替代旧的 deepcopy + 13 趟 cssselect，逐字节语义等价。
    关键交互（旧实现两阶段顺序产生，必须保留）：
      br 先全部 drop_tag——其 '\\n'+tail 并入前一存活兄弟的 tail（无前序兄弟时
      并入父 text）；随后块级 pass 对 tail 非空的块级元素再前置一个 '\\n'。
      故 `<p>x</p><br>` 旧输出 'x\\n\\n'（br 的换行落入 p.tail 后又补一次）。
    等价性由 /tmp/diff_el_text.py 对真实书源样本全量 diff 验证（10 万+ 样本 0 diff）。"""
    if not isinstance(getattr(el, 'tag', None), str):
        # 注释/PI 根（cssselect 不会产生，防御）：走旧 fallback 语义
        try:
            return el.text_content() or ""
        except Exception:
            return str(el or "")
    try:
        out = []
        _emit_text(el, out)
        return ''.join(out) or ""
    except Exception:
        try:
            return el.text_content() or ""
        except Exception:
            return str(el or "")


def _emit_text(node, out):
    """把 node 的子树文本（不含 node.tail）按旧 _el_text 语义写入 out。
    树深受 libxml2 解析上限（≈256 层）约束，递归安全。"""
    if node.text:
        out.append(node.text)
    # 前一"存活"兄弟（br 被 drop 不算存活）的 tail 累积器：
    # cur_block=None 表示尚无前序存活兄弟（br 字节直接进父文本区）
    cur_block = None
    cur_parts = []

    def flush():
        nonlocal cur_block, cur_parts
        if cur_block is None:
            return
        total = ''.join(cur_parts)
        if total:
            if cur_block:
                out.append('\n')   # 块级 pass：tail 非空 → 前置 '\n'
            out.append(total)
        cur_block = None
        cur_parts = []

    for child in node:
        tag = child.tag
        if not isinstance(tag, str):
            # 注释/PI：文本不进 text_content；tail 属于文本流且永不补换行。
            # 同时它是 br drop_tag 的合法并入目标 → 算存活兄弟。
            flush()
            cur_block = False
            cur_parts = [child.tail] if child.tail else []
            continue
        if tag == 'br':
            # drop_tag 语义：br.text + '\n' + br.tail 并入前一存活兄弟 tail
            # （无存活兄弟 → 直接落入父文本区，不触发块级补换行）
            piece = cur_parts if cur_block is not None else out
            if child.text:
                piece.append(child.text)
            piece.append('\n')
            if child.tail:
                piece.append(child.tail)
            continue
        flush()
        _emit_text(child, out)
        cur_block = tag in _BLOCK_TAGS
        cur_parts = [child.tail] if child.tail else []
    flush()


def _el_outer(el):
    try:
        return lxml_html.tostring(el, encoding='unicode', method='html')
    except Exception:
        return ""


def _el_attr(el, name):
    try:
        return el.get(name, "")
    except Exception:
        return ""


# ── 元素操作符 ───────────────────────────────
_OPS = ('@textNodes', '@outerHtml', '@ownText', '@text', '@href', '@src',
        '@html', '@content', '@all', '@attr:', '@tagName', '@index')


def _apply_op(value, op):
    """对结果应用元素操作符；value 为元素或元素列表或文本"""
    if op is None or not value:
        return value
    if isinstance(value, list):
        items = value
    else:
        items = [value]

    out = []
    for it in items:
        if hasattr(it, 'tag'):  # lxml 元素
            if op in ('@text', '@textNodes', '@all'):
                t = _el_text(it).strip()
                if op == '@all':
                    t = re.sub(r'\s+', ' ', _el_text(it)).strip()
                out.append(t)
            elif op == '@ownText':
                # 仅直接文本节点
                t = "".join(it.xpath('text()')).strip()
                out.append(t)
            elif op == '@outerHtml':
                out.append(_el_outer(it))
            elif op == '@html':
                # 内部 HTML（不含元素自身标签）
                try:
                    if hasattr(it, 'tag'):
                        children = list(it)
                        inner = "".join(
                            lxml_html.tostring(c, encoding='unicode', method='html')
                            for c in children)
                        # 无子元素时取自身（文本节点）
                        if not inner and it.text:
                            inner = it.text
                    else:
                        inner = str(it)
                except Exception:
                    inner = _el_outer(it) if hasattr(it, 'tag') else str(it)
                out.append(inner)
            elif op == '@attr:':
                out.append("")
            elif op == '@content':
                out.append(_el_attr(it, 'content'))
            elif op == '@href':
                out.append(_el_attr(it, 'href'))
            elif op == '@src':
                out.append(_el_attr(it, 'src'))
            elif op.startswith('@attr:'):
                out.append(_el_attr(it, op[6:]))
            elif op.startswith('@') and len(op) > 1 \
                    and op[1:] not in ('text', 'html', 'all', 'ownText',
                                       'textNodes', 'outerHtml', 'index',
                                       'tagName', 'content'):
                # legado 裸属性操作符：@title/@id/@class 等
                out.append(_el_attr(it, op[1:]))
            elif op == '@tagName':
                out.append(str(it.tag))
            elif op == '@index':
                out.append("")
            else:
                out.append(_el_text(it).strip())
        else:
            # 非元素（字符串等）
            out.append(str(it))
    return out


# ── 规则分段 ─────────────────────────────────

def split_or(rule):
    """按 || 分段（多个候选规则）"""
    if not rule:
        return []
    return [p for p in re.split(r'\|\|', rule) if p.strip()]


def split_clean_suffix(rule):
    """
    提取 ## 清洗后缀，支持 legado 语义：
      - "...##pat"           移除 pat（单 ##，replaceRegex 常见）
      - "...##pat##repl"     正则替换（$1 组引用）
      - "...##pat##repl##"   末尾再跟 ## → replaceFirst（只处理第一处匹配）
    返回 (规则主体, [(pattern, repl, replace_first), ...])
    """
    if '##' not in rule:
        return rule, []
    parts = rule.split('##')
    body = parts[0]
    cleans = []
    i = 1
    while i < len(parts):
        pat = parts[i]
        if i + 1 >= len(parts):
            # "##pat" 结尾：删除匹配
            if pat:
                cleans.append((pat, '', False))
            break
        repl = parts[i + 1]
        # 末尾再跟一个 ##（parts 尾元素为空且 repl 后无更多段）→ replaceFirst
        replace_first = (i + 2 < len(parts) and parts[i + 2] == ''
                         and i + 2 == len(parts) - 1)
        if pat:
            cleans.append((pat, repl, replace_first))
        i += 2
    return body, cleans


# ── 模板替换 ─────────────────────────────────

def fill_template(tpl, ctx):
    """替换 {{key}} / {{page}} / {{searchKey}} / {{$.x}} / {{book.x}} / {{result}} / @get:{key}"""
    if '{{' not in tpl and '@get:' not in tpl:
        return tpl
    # @get:{key} 变量读取（legado 语法）
    if '@get:' in tpl:
        def _g(m):
            _k = m.group(1).strip()
            return str((ctx.get('vars') or {}).get(_k, ''))
        tpl = re.sub(r'@get:\s*\{([^}]+)\}', _g, tpl)
    if '{{' not in tpl:
        return tpl

    def repl(m):
        key = m.group(1).strip()
        if key == 'page':
            return str(ctx.get('page', 1))
        if key in ('key', 'searchKey', 'searchkey'):
            return str(ctx.get('key', ''))
        if key == 'result':
            return str(ctx.get('result', ''))
        if key.startswith('$.'):
            j = ctx.get('json')
            if j is not None:
                try:
                    r = [x.value for x in _jp_compile(key).find(j)]
                    return str(r[0]) if r else ''
                except Exception:
                    return ''
            return ''
        if key.startswith('book.'):
            b = ctx.get('book') or {}
            return str(b.get(key[5:], ''))
        if key.startswith('@'):
            # 嵌套规则（简化：忽略）
            return ''
        return str(ctx.get(key, ''))

    return re.sub(r'\{\{(.+?)\}\}', repl, tpl)


# ── 单段规则执行 ─────────────────────────────

def _strip_type_prefix(part):
    """识别类型前缀，返回 (type, body)"""
    m = re.match(r'@(css|jsoup|json|Json|regex|正则|reg|XPath|xpath|js|webjs|get|put):(.*)',
                 part, re.DOTALL)
    if m:
        t = m.group(1).lower()
        if t in ('json',):
            t = 'json'
        return t, m.group(2)
    if part.startswith('$.') or part.startswith('@json'):
        return 'json', part[1:] if part.startswith('@json') else part
    return 'default', part


def _extract_op(body):
    """从选择器主体尾部提取操作符（如 @text / @title）。返回 (selector, op)"""
    for op in _OPS:
        if op == '@attr:':
            m = re.search(r'@attr:[\w-]*$', body)
            if m:
                return body[:m.start()], m.group(0)
            continue
        if body.endswith(op):
            return body[: -len(op)], op
    # legado 裸属性操作符：@title / @id / @class 等（元素属性）
    m = re.search(r'@([\w-]+)$', body)
    if m and m.group(1) not in ('text', 'href', 'src', 'html', 'content',
                                'all', 'index', 'tagName', 'ownText', 'textNodes'):
        return body[:m.start()], '@' + m.group(1)
    return body, None


def _drop_prefix(body):
    """提取 !N 后缀（丢弃前 N 个）"""
    m = re.search(r'!(\d+)$', body)
    if m:
        return body[:m.start()], int(m.group(1))
    return body, 0


def _select_css(sel, result):
    """在 result（元素/元素列表/HTML 字符串）上执行 CSS 选择器。
    支持 legado 索引语法 tag.N（如 a.1 = 第2个 a 标签）。"""
    # legado 索引语法：tag.N（a.0/li.2）与 .class.N（.xsm.0）——
    # 后者是榜单/详情规则里的常见写法（实测 `name: ".xsm.0@a@text"`）。
    # 负数索引表示倒数第 N 个（legado 亦支持）。
    m_idx = re.fullmatch(r'([a-zA-Z][\w-]*|div|span|li|a|p|h\d)\.(-?\d+)', sel.strip()) \
        or re.fullmatch(r'(\.[\w-]+)\.(-?\d+)', sel.strip())
    if m_idx:
        tag, idx = m_idx.group(1), int(m_idx.group(2))
        items = result if isinstance(result, list) else [result]
        found = []
        for it in items:
            if hasattr(it, 'cssselect'):
                try:
                    found.extend(_css_compile(tag)(it))
                except Exception:
                    pass
            elif isinstance(it, str):
                doc = parse_html(it)
                if doc is not None:
                    try:
                        found.extend(_css_compile(tag)(doc))
                    except Exception:
                        pass
        if -len(found) <= idx < len(found):
            return [found[idx]]
        return []
    if isinstance(result, str):
        doc = parse_html(result)
        if doc is None:
            return []
        try:
            return _css_compile(sel)(doc)
        except Exception:
            return []
    items = result if isinstance(result, list) else [result]
    out = []
    for it in items:
        if hasattr(it, 'cssselect'):
            try:
                out.extend(_css_compile(sel)(it))
            except Exception:
                pass
    return out


def _select_json(path, result):
    """JSONPath 提取（兼容 legado 的 .0 索引语法 → [0]）"""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            return []
    # legado 索引语法：$.a.0.b → $.a[0].b（jsonpath-ng 用 [N]）
    path = re.sub(r'\.(\d+)(?=\.|$)', r'[\1]', path)
    try:
        expr = _jp_compile(path)
        return [m.value for m in expr.find(result)]
    except Exception:
        return []


def _select_regex(pattern, result):
    """正则提取：返回匹配（优先 group(1)，否则 group(0)）。

    **纯文本匹配不到时，再拿元素自身的 HTML 试一次**（2026-09-18 修）：
    规则里带标签的写法（实测 **6 个源**的 `nextContentUrl` 都是
    `<a[^>]*href="([^"]+)"[^>]*>\\s*下一章`）在**纯文本**里永远匹配不到，
    而这类规则失效是**静默**的 —— 分页当场停在第 1 页，多页章节被截断
    （站点页面自己标着 `第(1/3)页`；实测 quanben8 第1113章只取到 1234 字，
    整章 3 页共 4156 字）。

    只有"纯文本没命中"时才回退到 HTML，所以对既有能用的规则**零影响**。
    """
    if isinstance(result, str):
        candidates = [result]
    elif hasattr(result, 'tag'):
        candidates = [_el_text(result)]
        try:
            candidates.append(lxml_etree.tostring(result, encoding='unicode'))
        except Exception:                                        # noqa: BLE001
            pass
    else:
        candidates = [str(result)]
    for text in candidates:
        try:
            m = re.search(pattern, text, re.S)
        except Exception:                                        # noqa: BLE001
            return []
        if m:
            return [m.group(1) if m.groups() else m.group(0)]
    return []


def _select_xpath(path, result):
    """XPath 提取（lxml.xpath）。返回元素/字符串列表"""
    def _run(elm):
        try:
            return elm.xpath(path)
        except Exception:
            return []
    if isinstance(result, str):
        doc = parse_html(result)
        if doc is None:
            return []
        return _run(doc)
    items = result if isinstance(result, list) else [result]
    out = []
    for it in items:
        if hasattr(it, 'xpath'):
            r = _run(it)
            if isinstance(r, list):
                out.extend(r)
    return out


def execute_part(part, result, ctx=None):
    """
    执行一段规则（不含 || 与 @@），返回结果（元素列表 / 字符串列表 / 字符串）。
    支持 @put:{...} 内嵌变量（求值后写入 ctx.vars）与 @get:{key} 读取。
    """
    part = part.strip()
    if not part:
        return result
    ctx = ctx or {}
    # @put:{...}：提取变量定义（值本身是规则，先剥离存储，随后在链内执行）
    _put_m = re.search(r'@put:\s*(\{[^}]+\})', part)
    if _put_m:
        try:
            _put_cfg = json.loads(_put_m.group(1))
            for _k, _v in _put_cfg.items():
                if isinstance(_v, str):
                    # 值含规则特征（@/{{/路径）→ 求值；否则原样存储
                    if re.search(r'@|{{|\$\.|//', _v):
                        _val = execute_chain(_v, result, ctx)
                        _val = (_val[0] if isinstance(_val, list) and _val
                                else _val if isinstance(_val, str) else str(_val or ''))
                    else:
                        _val = _v
                    ctx.setdefault('vars', {})[_k] = _val
        except Exception:
            pass
        part = part.replace(_put_m.group(0), '').strip()
        if not part:
            return result
    rtype, body = _strip_type_prefix(part)
    body = fill_template(body, ctx or {})

    # legado 快捷规则：body 为 text/href/src/html/content 时取元素自身（无需选择器）
    if body in ('text', 'href', 'src', 'html', 'content', 'textNodes',
                'ownText', 'outerHtml', 'all') and not isinstance(result, str):
        items = result if isinstance(result, list) else [result]
        return _apply_op(items, '@' + body)

    # 正则类型：整个 body 是 pattern（可能含操作符？罕见，跳过）
    if rtype in ('regex', '正则', 'reg'):
        return _select_regex(body, result)

    # XPath 类型：@XPath: 前缀，或规则以 / 开头
    if rtype in ('xpath',) or (rtype == 'default' and body.strip().startswith('/')):
        # XPath 无需 _extract_op（取值在 XPath 内表达）
        return _select_xpath(body.strip(), result)

    # 提取操作符与 !N
    body, op = _extract_op(body)
    body, drop_n = _drop_prefix(body)

    if rtype == 'json' or (body.startswith('$.') and rtype == 'default'):
        r = _select_json(body if not body.startswith('@json') else body[5:], result)
    elif rtype == 'get':
        # @get:{key} / @get:key —— 从 ctx 变量读取
        var = body.strip().strip('{}').strip()
        return ctx.get('vars', {}).get(var, '')
    else:
        # legado 的 @ 链式步骤：`li.0@a@href`、`.xsm.0@a@text`——
        # 中间的 @ 段必须**依次**执行（先选 li.0，再在其下选 a，最后取 @href）。
        # 原实现只用 _extract_op 取尾部操作符，剩下的 "li.0@a" 被整串当 CSS 选择器
        # 交给 cssselect（无效 → 空），于是这类规则恒为空（实测榜单书单取不到书名/链接）。
        steps = [x for x in body.split('@') if x.strip()] if '@' in body else [body]
        if len(steps) > 1:
            cur = result
            for st in steps:
                cur = _select_css(st.strip(), cur)
                if not cur:
                    break
            r = cur
        else:
            r = _select_css(body, result)

    if drop_n:
        r = r[drop_n:] if isinstance(r, list) else r

    if op and r:
        applied = _apply_op(r, op)
        # 歧义处理：尾段 @x 既可能是"取属性 x"，也可能是"选子元素 x"。
        # 若按属性取值全为空、而按选择器能选到元素，则按选择器处理
        # （legado 里 `ul.xbk@li`、`.book@a` 这类写法更常见；
        #   而 `img@src` / `a@href` 属于 _OPS，不受影响）。
        if (op.startswith('@') and op not in _OPS and op != '@attr:'
                and all((not isinstance(x, str)) or x.strip() == '' for x in applied)):
            alt = _select_css(op[1:], r)
            if alt:
                return alt
        r = applied
    return r


_TAG_ATTR_FILTER_RE = re.compile(r'([A-Za-z][\w-]*)@(class|id)\.([\w-]+)')


def normalize_selector_rule(rule):
    """把 legado 的 `tag@class.value` / `tag@id.value` 写法归一成 CSS 选择器。

    为什么需要：内置书源里 bookList 常见写法是 `ul@class.xbk`（实测精华书阁的
    exploreUrl 榜单规则就是它）。本引擎把 `@` 当"取值操作符"分段，于是
    `ul@class.xbk` 被解析成"取 ul 的 class 属性"→ 拿到字符串而不是元素列表 →
    书单恒为 0 条（实测：`ul@class.xbk` 返回 0 个元素，`ul.xbk` 返回 20 个）。
    这里只做**等价的写法归一**（class→.、id→#），不改变任何语义判断。
    """
    if not rule or '@' not in rule:
        return rule
    return _TAG_ATTR_FILTER_RE.sub(
        lambda m: f"{m.group(1)}.{m.group(3)}" if m.group(2) == 'class'
        else f"{m.group(1)}#{m.group(3)}", rule)


def execute_chain(rule, result, ctx=None):
    """执行规则链（@@ 分段）。返回最终结果（可能是字符串/元素列表/值列表）。"""
    ctx = ctx or {}
    if not rule:
        return result
    rule = normalize_selector_rule(rule)
    # 链式分段：@@ 分割（排除类型前缀 @css: 等）
    parts = re.split(r'@@(?=[^:])', rule)
    cur = result
    for part in parts:
        if not part.strip():
            continue
        cur = execute_part(part, cur, ctx)
    return cur


class RuleEngine:
    """书源规则引擎：get_string / get_elements 两个入口"""

    def __init__(self, source=None, vars=None):
        self.source = source or {}
        self.vars = vars or {}

    def _ctx(self, **extra):
        c = {'source': self.source, 'vars': self.vars}
        c.update(extra)
        return c

    # ---- 取值（字符串） ----
    def get_string(self, rule, result, base_url=None, **extra):
        """
        在 result 上执行规则，返回清洗后的字符串（第一个结果）。
        rule 支持 || 与 ## 后缀。
        """
        if not rule:
            return ''
        ctx = self._ctx(**extra)
        rule, cleans = split_clean_suffix(rule)
        # || 多候选
        out = ''
        for cand in split_or(rule):
            r = execute_chain(cand, result, ctx)
            out = self._to_string(r)
            if out:
                break
        # ## 清洗
        for pat, repl, replace_first in cleans:
            try:
                # JSON 双重转义还原 + Java $N 组引用 → Python \N
                _pat = pat.replace('\\\\', '\\')
                _repl = re.sub(r'\$(\d+)', r'\\\1', repl.replace('\\\\', '\\'))
                if replace_first:
                    # legado 语义：提取并加工第一处匹配（返回加工后的匹配本身）
                    m = re.search(_pat, out)
                    out = re.sub(_pat, _repl, m.group(0), count=1) if m else ''
                else:
                    out = re.sub(_pat, _repl, out)
            except Exception:
                pass
        return out

    def _to_string(self, r):
        if r is None:
            return ''
        if isinstance(r, list):
            # 取第一个非空结果（legado getString 语义）
            for item in r:
                if hasattr(item, 'tag'):
                    t = _el_text(item).strip()
                else:
                    t = str(item).strip()
                if t:
                    return t
            return ''
        if hasattr(r, 'tag'):
            return _el_text(r).strip()
        return str(r).strip()

    # ---- 列表（元素/值） ----
    def get_elements(self, rule, result, **extra):
        """
        执行列表规则，返回元素/值列表。
        用于 chapterList / bookList 等。
        """
        if not rule:
            return []
        ctx = self._ctx(**extra)
        r = execute_chain(rule, result, ctx)
        if isinstance(r, list):
            return r
        if r is None or r == '':
            return []
        return [r]

    # ---- 子规则在列表元素上取值 ----
    def get_string_on_elements(self, rule, elements, **extra):
        """对每个列表元素执行子规则，返回字符串列表"""
        ctx = self._ctx(**extra)
        out = []
        for el in elements:
            out.append(self.get_string(rule, el, **extra))
        return out


# ── 便捷封装 ─────────────────────────────────
def normalize_url(url, base_url):
    if not url:
        return ''
    if url.startswith('http://') or url.startswith('https://'):
        return url
    return urljoin(base_url or '', url)


def parse_search_config(url):
    """解析 legado searchUrl 的 ,{...} 请求配置：返回 (clean_url, config_dict)
    config 含 method/body/headers 等（引擎支持 method=POST 与 body 表单）"""
    if not url:
        return '', {}
    url = re.sub(r'<js>.*?</js>', '', url, flags=re.S)
    m = re.search(r',\s*(\{.*\})\s*$', url, flags=re.S)
    if not m:
        return url.strip(), {}
    cfg = {}
    try:
        cfg = json.loads(m.group(1))
    except Exception:
        cfg = {}
    return url[:m.start()].strip(), cfg


def clean_search_url(url):
    """剥离 legado searchUrl 的 JS 预处理块与 ,{...} 请求配置后缀"""
    clean, _ = parse_search_config(url)
    return clean
