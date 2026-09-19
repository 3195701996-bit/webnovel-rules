#!/usr/bin/env python3
"""
书源驱动爬虫：搜索 → 详情 → 目录 → 正文 → 整书下载
所有解析都基于 legado 书源规则，不硬编码任何站点。
"""
import os
import re
import json
import time
import hashlib
import threading

from .rules import (RuleEngine, fill_template, normalize_url,
                    parse_search_config, parse_html)
from urllib.parse import urljoin
from .fetcher import Fetcher, DeadlineExceeded
from .cleaner import clean_text as _clean_text, html_to_text as _html_to_text
from .app_utils import atomic_write as _atomic_write, \
    atomic_write_text as _atomic_write_text, cache_key_of as _cache_key_of
from .config import (CONTENT_DEADLINE, CONTENT_MAX_PAGES, TOC_PAGE_CAP,
                     CRAWL_DEFAULT_CONCURRENCY, CRAWL_MAX_CONCURRENCY,
                     CRAWL_RETRY_MAX, CRAWL_RETRY_DELAY, TOC_PAGE_SLEEP)


def now_iso():
    """本地时间戳字符串（与 server 侧 _state.json 的 updated_at 同格式）"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ── 导出版本机制（2026-09-10 结构性优化）────────────────────────────
# 问题：TXT 导出新鲜度此前用"state.updated_at 与 book.txt mtime 比较"推断
# （时间戳推断）——重爬单章、修复补章、时钟回拨都与时间戳无必然关系，会
# 漏判陈旧或误判新鲜。这里改为**由章节缓存文件派生的确定性内容版本**：
#   rev = md5(每章 "序位:mtime_ns:size" 序列 + 总数)
# 直接反映"当前磁盘上可导出的内容"，跨进程/重启有效，无需在各写点埋钩子。
EXPORT_META_NAME = "_export.json"


def content_revision(book_dir, chapters):
    """由章节 .cache 文件指纹 + 章节身份（url + 名）派生内容版本。

    - **章 url 身份**参与哈希：若只用序位/名/指纹，两章同名且缓存
      mtime/size 相同（同批写入常见）时，交换二者位置仍得到完全相同的
      (idx,name,mtime,size) 序列 → 版本不变 → 交换后误判新鲜；纳入 url
      后交换必然改变版本。
    - **章节名**参与哈希：导出 txt 的标题取自章节名，仅重命名而缓存
      size/mtime 不变时旧全文仍带旧标题，必须判陈旧触发重建。
    - 序位取目录位置（与 merge_txt 的章节顺序一致）——目录重排/补章
      同样改变版本；缓存被重写（size 或纳秒 mtime 变）也改变版本。
    - 记录用 **JSON 一致序列化**（分隔符不进字段、无歧义、确定顺序），
      不用 "a:b;c" 之类可被字段内容污染的朴素拼接。
    无任何可用缓存时返回空串（表示"无可导出内容"，与"版本一致"区分）。
    """
    records = []
    for idx, c in enumerate(chapters or [], 1):
        url = (c or {}).get("url") if isinstance(c, dict) else None
        if not url:
            continue
        p = os.path.join(book_dir, f"{_cache_key_of(url)}.cache")
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_size <= 0:
            continue
        name = ((c.get("name") if isinstance(c, dict) else None) or "").strip()
        records.append([idx, str(url), name, st.st_mtime_ns, st.st_size])
    if not records:
        return ""
    blob = json.dumps(records, separators=(",", ":")).encode()
    return hashlib.md5(blob).hexdigest()[:16]


def read_export_meta(book_dir):
    """读导出元数据（rev / txt 指纹）；缺失或损坏返回 {}"""
    try:
        with open(os.path.join(book_dir, EXPORT_META_NAME),
                  encoding="utf-8") as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def write_export_meta(book_dir, rev, txt_path):
    """原子写导出元数据（rev + book.txt 的 mtime_ns/size），失败静默"""
    try:
        st = os.stat(txt_path)
    except OSError:
        return
    _atomic_write(os.path.join(book_dir, EXPORT_META_NAME),
                  {"rev": rev, "txt_mtime_ns": st.st_mtime_ns,
                   "txt_size": st.st_size, "built_at": now_iso()})


def export_is_fresh(book_dir, chapters, txt_path):
    """book.txt 是否与当前章节缓存内容一致（版本 + 文件指纹双重校验）"""
    if not os.path.exists(txt_path):
        return False
    rev = content_revision(book_dir, chapters)
    if not rev:
        return False            # 无可导出内容 → 不视为新鲜
    meta = read_export_meta(book_dir)
    if meta.get("rev") != rev:
        return False
    try:
        st = os.stat(txt_path)
    except OSError:
        return False
    # 外部改写/截断（大小与记录不符）→ 视为陈旧，触发重建
    if meta.get("txt_size") and meta["txt_size"] != st.st_size:
        return False
    return True


# ── 合并写串行化（同一 book_dir 只允许一个合并写）──────────────────
# 问题：merge_txt 先写 book.txt 再写 _export.json（其中 txt_size 由 stat
# 取得）。若同一本书的两个合并并发交错（旧列表任务 + 新列表任务、导出
# 重建与爬取收尾），可能出现 A 写 txt、B 写 txt、A 写 meta 的交错 →
# meta.rev 指向 A 而 txt_size 取自 B 写的文件，于是"旧内容 + A 的 rev"
# 被 export_is_fresh 误判为新鲜。用按 book_dir 的锁把
# （计算 rev → 读缓存 → 写 txt → 写 meta）整段串行化，保证 txt 与
# meta 始终成对一致。
# 实现为**固定条带锁**：book_dir 经稳定 md5 映射到固定 64 把锁之一，
# 同一 book_dir 永久映射同一对象；锁表不增长、不淘汰 → 从根上消除
# "持有者/等待者的锁被驱逐后拿到不同锁对象 → 单飞退化"的竞态，无需
# `not locked()` 之类的近似租约。
_MERGE_LOCK_STRIPES = 64
_merge_lock_stripes = tuple(threading.Lock() for _ in range(_MERGE_LOCK_STRIPES))


def _merge_lock(book_dir):
    """稳定条带锁：同一 book_dir 永久映射到固定锁对象（表不增长/不淘汰）"""
    key = os.path.abspath(book_dir)
    idx = int(hashlib.md5(key.encode("utf-8", "surrogatepass"))
              .hexdigest(), 16) % _MERGE_LOCK_STRIPES
    return _merge_lock_stripes[idx]


def _disk_chapters(book_dir):
    """读磁盘 _state.json 的 chapters 作为权威目录；缺失/非法返回 None。

    合并以磁盘最新目录为准，防止旧任务携带的短列表把已完整导出的全文
    回退覆盖。合法性：state 为 dict、chapters 为非空 list、且至少一个
    带 url 的 dict 章节。
    """
    try:
        with open(os.path.join(book_dir, "_state.json"),
                  encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    chs = state.get("chapters")
    if not isinstance(chs, list) or not chs:
        return None
    for c in chs:
        if isinstance(c, dict) and str(c.get("url") or "").strip():
            return chs
    return None


def merge_book_txt(book_dir, chapters, force=False):
    """纯本地合并：chapters + .cache → book.txt + _export.json。

    不触网、不依赖 CrawlTask/书源（旧书书源已删除时仍可离线导出）。
    - **权威目录**：锁内优先取磁盘 _state.json 的 chapters（存在且合法时），
      否则回退传入 chapters —— 防止旧任务携带的短列表把已完整导出的全文
      回退覆盖；
    - 整个合并写在同一 book_dir 的锁内完成，txt 与 _export.json 成对写入，
      避免并发合并交错导致版本元数据与内容错配；
    - 章节缓存"存在且非空但读取失败/内容为空"→ 返回 None（未完整读取），
      不写出部分 txt、不更新 meta → 旧全文保留且不会被误判新鲜。
    """
    out = os.path.join(book_dir, 'book.txt')
    with _merge_lock(book_dir):
        # 磁盘最新目录优先（旧任务短列表不得回退覆盖完整全文）
        chapters = _disk_chapters(book_dir) or chapters
        if not chapters:
            return None
        # rev 在读取缓存前计算：若读取期间缓存被并发改写，记录的 rev 会
        # 早于实际内容 → export_is_fresh 判陈旧并重建（保守但不误报新鲜）
        rev = content_revision(book_dir, chapters)
        if not force and rev and export_is_fresh(book_dir, chapters, out):
            return out
        texts = []
        seen_sig = set()   # 内容指纹去重：防止分页爬虫把后续章节内容拼入本章导致重复
        for idx, c in enumerate(chapters, 1):
            url = (c or {}).get('url') if isinstance(c, dict) else None
            if not url:
                continue
            p = os.path.join(book_dir, f"{_cache_key_of(url)}.cache")
            try:
                st = os.stat(p)
            except OSError:
                continue           # 无缓存文件：与 content_revision 一致地跳过
            if st.st_size <= 0:
                continue           # 空缓存：视为"无可用内容"
            try:
                with open(p, encoding='utf-8') as f:
                    t = f.read()
            except (UnicodeDecodeError, OSError):
                # 宣称有内容却读不出来 → 未完整读取：绝不出部分 txt，
                # 否则会写出缺章全文并被 export_is_fresh 误判为新鲜
                return None
            if not t.strip():
                return None        # 非空文件却无有效正文 → 未完整读取
            # 章节名标题（无名称时用"第N章"兜底）
            name = (c.get('name') or '').strip() or f"第{idx}章"
            body = t.strip()
            # 正文开头与章节名重复（源站正文首段=章节名，部分源缓存为
            # "标题\n\n标题\n\n正文" 双标题模式）→ 全部去掉，
            # 避免导出 txt 出现"标题+正文首行同章节名"的视觉重复
            _lines = body.split("\n")
            if _lines and name:
                _norm = lambda s: re.sub(r"[\s\W]", "", s)[:15]
                # 跳过前导空行后检查首行
                _k = 0
                while _k < len(_lines) and not _lines[_k].strip():
                    _k += 1
                if _k < len(_lines) and _norm(_lines[_k]) and \
                        _norm(_lines[_k]) == _norm(name):
                    # 去掉标题行及后续连续重复标题行（可隔空行）
                    _j = _k
                    while _j < len(_lines):
                        if not _lines[_j].strip():
                            _j += 1
                            continue
                        if _norm(_lines[_j]) == _norm(name):
                            _j += 1
                            continue
                        break
                    body = "\n".join(_lines[_j:]).strip() or body
            # 指纹 = 正文中部 300 字（跨页拼接重复时中段最稳定）
            _mid = body[len(body) // 2: len(body) // 2 + 300]
            _sig = hashlib.md5(_mid.encode()).hexdigest()[:16]
            if _sig in seen_sig:
                continue  # 与前面章节内容重叠 → 跳过（防重复）
            seen_sig.add(_sig)
            texts.append(f"{name}\n{body}")
        if not texts:
            return None
        sep = '\n\n' + '─' * 48 + '\n\n'
        # R29(技术评审5.2): book.txt 原子替换——阅读接口并发读取时
        # 不会读到半截文件（R47: 收敛到 app_utils.atomic_write_text）
        _atomic_write_text(out, sep.join(texts))
        # 导出版本：记录本次合并对应的内容版本 + txt 指纹（原子写）。
        # txt 与 meta 是两次独立原子写，非单一事务；若在两次之间崩溃/被
        # 外部改写，meta 缺失或与 txt 不符 → export_is_fresh 保守判陈旧 →
        # 导出端点重建，绝不把错配内容当新鲜下发。
        write_export_meta(book_dir, rev, out)
        return out


class Book:
    """一本书的元数据 + 目录"""
    def __init__(self, source, book_url):
        self.source = source
        self.book_url = book_url
        self.base = (source.get('bookSourceUrl') or '').rstrip('/')
        self.name = ''
        self.author = ''
        self.intro = ''
        self.cover = ''
        self.toc_url = ''
        self.last_chapter = ''
        self.update_time = ''
        self.word_count = ''
        self.chapters = []          # [{'name':..., 'url':...}]
        self.chapter_count = 0

    def to_dict(self):
        return {
            'name': self.name, 'author': self.author, 'intro': self.intro,
            'cover': self.cover, 'toc_url': self.toc_url,
            'book_url': self.book_url,
            'chapter_count': self.chapter_count,
            'last_chapter': self.last_chapter,
            'update_time': self.update_time,
            'word_count': self.word_count,
        }


class SourceCrawler:
    """基于单个书源的爬虫。命中专属适配器时优先使用适配器（硬编码站点结构），
    否则回退到 legado 规则引擎。"""

    def __init__(self, source, progress_callback=None):
        self.source = source
        self.base = (source.get('bookSourceUrl') or '').rstrip('/')
        self.engine = RuleEngine(source=source)
        self.fetcher = Fetcher()
        self.progress_callback = progress_callback or (lambda p: None)
        # 目录分页是否撞上限（即本次 get_toc 的结果**可能不完整**）。
        # 只打日志不够：调用方（检查更新）会据此对用户说"已是最新"，
        # 撞上限时必须让上层读得到，见 server/state.py::_run_check_update。
        self.toc_truncated = False
        # 专属适配器（无则 None → 全部走规则引擎）
        from .adapters import resolve_adapter
        self.adapter = resolve_adapter(source, self.fetcher)
        if self.adapter:
            print(f"[adapter] {source.get('bookSourceName','?')} → "
                  f"{type(self.adapter).__name__}", flush=True)

    # ── 搜索 ──
    def search(self, keyword, page=1):
        """按书名/作者搜索，返回书籍列表（支持 GET 与 POST searchUrl，URL JSON 选项）
        适配器源：适配器结果即权威（含空结果）——不再回退规则引擎。
        规则引擎只服务无专属适配器的旧源（现已无启用源走此路径）。"""
        if self.adapter is not None:
            try:
                return self.adapter.search(keyword, page=page)
            except Exception as e:
                print(f"[adapter] {self.source.get('bookSourceName','?')} 搜索失败: "
                      f"{type(e).__name__}: {e}", flush=True)
                return []
        su = self.source.get('searchUrl') or ''
        if not su:
            return []
        clean, cfg = parse_search_config(su)
        url = fill_template(clean,
                            {'key': keyword, 'searchKey': keyword, 'page': page})
        if not url.startswith('http'):
            url = self.base + url
        # URL JSON 选项：retry / headers（charset 由 Fetcher._decode 处理）
        retry = int(cfg.get('retry') or 0) + 1
        headers = cfg.get('headers') if isinstance(cfg.get('headers'), dict) else None
        # 搜索场景快速失败：短超时、零重试（搜索是尽力而为，慢源由冷却机制接管）
        if str(cfg.get('method', '')).upper() == 'POST':
            body = str(cfg.get('body') or '')
            data = {}
            for kv in body.split('&'):
                if '=' in kv:
                    k, v = kv.split('=', 1)
                    data[k.strip()] = fill_template(v, {'key': keyword,
                                                        'searchKey': keyword,
                                                        'page': page})
            html = self.fetcher.post(url, data=data, source=self.source,
                                     timeout=6, retries=0,
                                     extra_headers=headers)
        else:
            # URL 统一百分号编码（保留 URL 结构字符）：源站对未编码中文
            # 关键字常解析失败返回空结果（如 jhsssd /search.html?word=中文）
            import urllib.parse as _up
            url = _up.quote(url, safe=':/?&=%,+#@')
            html = self.fetcher.get(url, source=self.source, timeout=6,
                                    retries=0,
                                    extra_headers=headers)
        rs = self.source.get('ruleSearch') or {}
        elems = self.engine.get_elements(rs.get('bookList', ''), html)
        books = []
        for el in elems:
            name = self.engine.get_string(rs.get('name', ''), el, base_url=self.base)
            bu = self.engine.get_string(rs.get('bookUrl', ''), el, base_url=self.base)
            if not name or not bu:
                continue
            books.append({
                'name': name,
                'author': self.engine.get_string(rs.get('author', ''), el, base_url=self.base),
                'intro': self.engine.get_string(rs.get('intro', ''), el, base_url=self.base),
                'kind': self.engine.get_string(rs.get('kind', ''), el, base_url=self.base),
                'cover': self.engine.get_string(rs.get('coverUrl', ''), el, base_url=self.base),
                'book_url': normalize_url(bu, self.base),
                'source_uid': self.source.get('uid', ''),
                'source_name': self.source.get('bookSourceName', ''),
            })
        return books

    # ── 详情 ──
    def get_book(self, book_url, fast=False, deadline=None):
        """获取详情；deadline 为本次业务操作总秒数，默认 12/30 秒。"""
        budget = deadline if deadline is not None else (12 if fast else 30)
        end = time.monotonic() + budget
        if self.adapter is not None:
            try:
                # P2-4: deadline 作用域（线程本地+异常恢复），不再共享实例属性
                with self.adapter._deadline_scope(budget):
                    info = self.adapter.get_book(book_url, fast=fast)
                if info:
                    b = Book(self.source, book_url)
                    for k, v in (info or {}).items():
                        setattr(b, k, v)
                    if not b.name:
                        b.name = book_url
                    return b
            except Exception as e:
                # 适配器源失败不回退规则引擎（规则引擎对该源无效且加重负载）
                print(f"[adapter] {self.source.get('bookSourceName','?')} 详情失败: "
                      f"{type(e).__name__}: {e}", flush=True)
                raise
        remaining = max(0.1, end - time.monotonic())
        html = self.fetcher.get(book_url, source=self.source,
                                timeout=min(8 if fast else 20, remaining),
                                retries=1 if fast else 2, deadline=end)
        rbi = self.source.get('ruleBookInfo') or {}
        b = Book(self.source, book_url)
        # P1-6：详情页 HTML 暂存到 Book 实例（瞬态属性，不进 to_dict/状态
        # 序列化）。当 tocUrl 为空（toc_url == book_url）时 get_toc 直接复用
        # 这份 HTML 作为目录首页，整书启动省掉一次同 URL 重复请求。
        b._detail_html = html
        # P0-3：详情页只解析一次，后续 9 次取值复用同一文档（此前每次
        # get_string 都对整页重新 lxml.fromstring）。parse_html 幂等，
        # 规则引擎 API 签名不变。html 原始字符串仍保留给下方正则兜底。
        doc = parse_html(html)
        b.name = self.engine.get_string(rbi.get('name', ''), doc, base_url=self.base)
        b.author = self.engine.get_string(rbi.get('author', ''), doc, base_url=self.base)
        b.intro = self.engine.get_string(rbi.get('intro', ''), doc, base_url=self.base)
        b.cover = self.engine.get_string(rbi.get('coverUrl', ''), doc, base_url=self.base)
        b.kind = self.engine.get_string(rbi.get('kind', ''), doc, base_url=self.base)
        b.last_chapter = self.engine.get_string(rbi.get('lastChapter', ''), doc, base_url=self.base)
        wc = self.engine.get_string(rbi.get('wordCount', ''), doc, base_url=self.base)
        b.word_count = wc
        # 更新时间：优先书源扩展字段 updateTime，否则从 kind/lastChapter 文本提取
        ut = self.engine.get_string(rbi.get('updateTime', ''), doc, base_url=self.base)
        if ut:
            b.update_time = ut.strip()
        else:
            for src_txt in (b.kind, b.last_chapter):
                m = re.search(r'(?:更新时间|更新)[:：]\s*([\d\-\s:年月日/]+)', src_txt)
                if m:
                    b.update_time = m.group(1).strip()
                    break
        toc = self.engine.get_string(rbi.get('tocUrl', ''), doc, base_url=self.base)
        b.toc_url = normalize_url(toc, book_url) if toc else book_url
        if not b.name:
            b.name = book_url
        # 详情页自动提取总章节数（"共X章"等，无需爬目录）
        if not b.chapter_count:
            plain = re.sub(r'<[^>]+>', ' ', html)
            plain = re.sub(r'\s+', ' ', plain)
            for pat in (r'(?:全书|共|总|约)\s*(\d+)\s*章',
                        r'(\d+)\s*章\s*(?:内容|正文|小说|章节)',
                        r'章节数[:：]?\s*(\d+)',
                        r'共\s*(\d+)\s*(?:个|条)',
                        r'最新章节[:：]?\s*第?(\d+)章',
                        r'总字数[:：]?\s*[\d.万]+.*?共\s*(\d+)\s*章',
                        r'(\d+)\s*章\s*$'):
                m = re.search(pat, plain)
                if m:
                    b.chapter_count = int(m.group(1))
                    break
        return b

    # ── 目录 ──
    def _parse_toc_chapters(self, html, url, chapters, seen, page_map=None, page_no=1):
        """解析单页目录 HTML → 追加章节（规则 chapterList 或自动兜底启发式）。
        page_map：记录章节 → 所在目录页页码（用于并发抓取后恢复页序）"""
        rt = self.source.get('ruleToc') or {}
        # P0-3：html 可为原始字符串或已解析文档（parse_html 幂等直通），
        # 无论走规则还是兜底，本页只解析一次。
        doc = parse_html(html)
        if rt.get('chapterList'):
            elems = self.engine.get_elements(rt.get('chapterList', ''), doc)
        else:
            # 兜底：自动找章节链接（href 含数字.html 或书 URL 前缀，文本非导航）
            elems = []
            if doc is not None:
                for a in doc.cssselect('a[href]'):
                    h = a.get('href', '')
                    t = (a.text_content() or '').strip()
                    if not t or len(t) > 40 or re.search(r'(首页|上一|下一|目录|书签|书架|登录|返回|投票|推荐|^javascript)', t):
                        continue
                    if re.search(r'\d+\.html', h) and not re.search(r'(search|tag|sort|sitemap|map)', h, re.I):
                        elems.append(a)
        cn_rule = rt.get('chapterName') or 'text'
        cu_rule = rt.get('chapterUrl') or 'href'
        names = self.engine.get_string_on_elements(cn_rule, elems, base_url=url)
        urls = self.engine.get_string_on_elements(cu_rule, elems, base_url=url)
        for n, u in zip(names, urls):
            u = normalize_url(u, url)
            if u and u not in seen:
                seen.add(u)
                chapters.append({'name': n or u, 'url': u})
                if page_map is not None:
                    page_map[u] = page_no

    @staticmethod
    def _find_toc_page_links(html, url, visited):
        """从目录页收集 index_N.html 分页链接（未访问过的）"""
        candidates = []
        try:
            from .rules import parse_html
            doc = parse_html(html)
            if doc is not None:
                for a in doc.cssselect('a[href]'):
                    h = a.get('href', '')
                    m = re.search(r'index_(\d+)\.html', h)
                    if m:
                        u = normalize_url(h, url)
                        if int(m.group(1)) > 1 and u not in visited:
                            candidates.append((int(m.group(1)), u))
        except Exception:
            pass
        return candidates

    def _fetch_toc_pages_parallel(self, page_urls, timeout, deadline=None):
        """并发抓取目录分页，deadline 到期后不再发起新请求。"""
        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _AC
        out = {}
        if not page_urls:
            return out

        def _one(u):
            try:
                return u, self.fetcher.get(u, source=self.source,
                                           timeout=timeout, retries=2,
                                           deadline=deadline)
            except Exception as e:
                print(f"  ⚠ 目录分页 {u[-40:]} 失败: {type(e).__name__}", flush=True)
                return u, None

        with _TPE(max_workers=4) as ex:
            futs = [ex.submit(_one, u) for u in page_urls]
            for f in _AC(futs):
                u, html = f.result()
                if html:
                    out[u] = html
        return out

    def get_toc(self, book, max_pages=None, timeout=15, deadline=None):
        """获取章节列表；deadline 默认按 timeout 的 4 倍限制整个目录操作。

        目录分页上限默认 `TOC_PAGE_CAP`(120)：实测（2026-09-18）精华书阁《剑来》
        目录有 **66 页 / 1309 章**，而适配器当时写死 30 页 → 只给 605 章，
        用户从该源下载的书**后半本读不到**、目录里还看不出少了什么。
        目录页数远多于正文，上限必须给足；撞上限时打印告警（不静默）。
        """
        if max_pages is None:
            max_pages = TOC_PAGE_CAP
        self.toc_truncated = False      # 每次调用重置（上层据此判断结果完整性）
        budget = deadline if deadline is not None else max(30, timeout * 4)
        end = time.monotonic() + budget
        if self.adapter is not None:
            try:
                with self.adapter._deadline_scope(budget):
                    chapters = self.adapter.get_toc(book)
                if chapters:
                    book.chapters = chapters
                    book.chapter_count = len(chapters)
                    return chapters
            except Exception as e:
                print(f"[adapter] {self.source.get('bookSourceName','?')} 目录失败: "
                      f"{type(e).__name__}: {e}", flush=True)
                raise
        rt = self.source.get('ruleToc') or {}
        chapters = []
        seen = set()
        page_map = {}          # 章节 url → 目录页页码（并发抓取后恢复页序）
        _page_no = 1           # 当前目录页序号（nextTocUrl 链递增）
        _visited_toc_pages = set()
        url = book.toc_url or book.book_url
        _visited_toc_pages.add(url)
        _toc_errors = 0
        # P1-6：目录首页 == 详情页（tocUrl 为空 → toc_url == book_url）时，
        # 复用 get_book 已抓取的详情页 HTML，省掉一次同 URL 重复请求。
        # 仅复用首页；nextTocUrl 后续页仍正常走 Fetcher（含域名限流）。
        # 消费后立即释放，避免长爬取任务持有大字符串。
        _prefetched_html = None
        if url == book.book_url:
            _prefetched_html = getattr(book, '_detail_html', None) or None
        if getattr(book, '_detail_html', None) is not None:
            book._detail_html = None
        for _ in range(max_pages):
            try:
                if _prefetched_html is not None and url == book.book_url:
                    html = _prefetched_html
                    _prefetched_html = None
                    _toc_errors = 0
                else:
                    html = self.fetcher.get(url, source=self.source, timeout=timeout,
                                            retries=1, deadline=end)
                    _toc_errors = 0
            except DeadlineExceeded:
                raise
            except Exception as _te:
                # 目录翻页容错：连续失败 3 次停止，用已收集章节
                _toc_errors += 1
                print(f"  ⚠ 目录页请求失败（{_toc_errors}/3）: {type(_te).__name__} {str(_te)[:50]}", flush=True)
                if _toc_errors >= 3:
                    print("  ⛔ 目录获取连续失败，使用已获取的章节", flush=True)
                    break
                wait = min(2.0 * _toc_errors, max(0.0, end - time.monotonic()))
                if wait > 0:
                    time.sleep(wait)
                if time.monotonic() >= end:
                    break
                continue
            # P0-3：目录页只解析一次，chapterList/nextTocUrl/分页链接探测
            # 三处取值复用同一文档（此前每页 3 次全量解析）。
            doc = parse_html(html)
            self._parse_toc_chapters(doc, url, chapters, seen, page_map, _page_no)
            nxt = self.engine.get_string(rt.get('nextTocUrl', ''), doc, base_url=url)
            if not nxt or nxt == url:
                # 自动探测目录分页：收集 index_N.html 链接 → 迭代并发抓取直至无新页
                # （部分站点分页导航只显示邻近页，如 index_1,2,3 …26，需逐轮扩散；
                #   护栏：总页数 ≤ max_pages*2、扩散轮次 ≤8、deadline）
                candidates = self._find_toc_page_links(doc, url, _visited_toc_pages)
                if candidates:
                    pages = sorted(set(candidates))  # (页码, url)，按页码数值排序
                    budget = max_pages * 2
                    frontier = [p for p in pages
                                if p[1] not in _visited_toc_pages]
                    _visited_toc_pages.update(p[1] for p in frontier)
                    guard = 0
                    while frontier and guard < 8 and len(_visited_toc_pages) < budget:
                        guard += 1
                        batch = frontier[: max(0, budget - len(_visited_toc_pages))]
                        fetched = self._fetch_toc_pages_parallel(
                            [u for _, u in batch], timeout, deadline=end)
                        frontier = []
                        for pno, pu in batch:
                            ph = fetched.get(pu)
                            if not ph:
                                continue
                            # P0-3：并发抓回的分页同样只解析一次
                            pdoc = parse_html(ph)
                            self._parse_toc_chapters(pdoc, pu, chapters, seen,
                                                     page_map, pno)
                            more = self._find_toc_page_links(pdoc, pu,
                                                             _visited_toc_pages)
                            for mno, mu in more:
                                if mu not in _visited_toc_pages and \
                                   len(_visited_toc_pages) < budget:
                                    _visited_toc_pages.add(mu)
                                    frontier.append((mno, mu))
                    break  # 迭代收集完成，目录获取结束
            if not nxt or nxt == url:
                break
            # R47 修复：先归一化再判重——此前用原始（可能是相对路径）nxt
            # 与已访问的绝对 URL 集合比较，判重必然落空，相对路径的
            # nextTocUrl 循环会重复抓取同一页直到 max_pages 兜底
            nxt = normalize_url(nxt, url)   # 相对路径补全
            if nxt in _visited_toc_pages:
                break
            _visited_toc_pages.add(nxt)
            url = nxt
            _page_no += 1
            # P1-6：目录链式翻页不再固定 sleep(0.6~1.3s)——与 Fetcher 每域名
            # 滑动窗口限流（6 次/1000ms + AIMD 自适应）双重节流，50 页链式
            # 目录额外多花 ~50s。默认 TOC_PAGE_SLEEP=0 完全交给域名限流兜底
            # （与正文分页路径对齐，见 get_content）；个别敏感源可在
            # config.py 调回 0.1~0.5。
            if _page_no >= max_pages:
                # 目录分页撞上限：**必须可见**（用户看不到后面的章节，
                # 只会以为"这书就到这"；实测精华书阁 66 页曾被 30 页上限截成 46%）
                # 同时也置位供调用方读取——"有不完整风险"不能只留在日志里
                self.toc_truncated = True
                print(f"[toc] ⚠ 目录分页达到上限 {max_pages} 页，可能仍有后续章节未取: "
                      f"{str(book.toc_url or book.book_url)[:100]}", flush=True)
            if TOC_PAGE_SLEEP > 0:
                wait = min(TOC_PAGE_SLEEP, max(0.0, end - time.monotonic()))
                if wait > 0:
                    time.sleep(wait)  # 目录翻页固定间隔（默认关闭）
            if time.monotonic() >= end:
                break
        # 多页目录：按页码稳定排序恢复正确章序（并发抓取会打乱追加顺序）
        if page_map and len(set(page_map.values())) > 1:
            chapters.sort(key=lambda c: page_map.get(c['url'], 1 << 20))
        # 目录页混入推荐/导航链接时，按同书前缀过滤（如塔读目录页含其他书链接）
        if book.book_url:
            _bu = book.book_url.rstrip('/')
            _path = _bu.split('//', 1)[-1]
            if '/' in _path and not _path.endswith('.html'):
                _prefix = '/' + _path.split('/', 1)[1] + '/'
                _filtered = [c for c in chapters
                             if _prefix in c['url'].split('//')[-1]]
                # 过滤后为空说明启发式误伤，保留原样
                if _filtered:
                    chapters = _filtered
        # 书源可选：按 URL 数字排序（消除"最新章节预览"等顺序污染）
        if self.source.get('tocSort') == 'url' and chapters:
            # 刻意不收敛到 engine/adapters/chapter_url_num：后者要求数字是独立
            # 路径段（/(\d+)(?:\.html)?$），此处 (\d+)[^0-9]*$ 更宽松，兼容
            # "chapter_12.html" 等段内嵌数字——legado 书源的 tocSort:'url' 语义
            # 依赖该宽松取号，改用公共函数会漏取号（返回 0）改变排序行为。
            def _num(u):
                m = re.search(r'(\d+)[^0-9]*$', u.rstrip('/'))
                return int(m.group(1)) if m else 0
            chapters.sort(key=lambda c: _num(c['url']))
        book.chapters = chapters
        book.chapter_count = len(chapters)
        return chapters

    # ── 正文（含分页）──
    def get_content(self, chapter_url, max_pages=None, timeout=25, deadline=None):
        """取一章正文（跟随 nextContentUrl 顺序取页）。

        **预算语义（2026-09-18 修正）**：`timeout` 是**单页**请求超时，
        整章预算默认取 `CONTENT_DEADLINE`（90s）——旧实现拿 `timeout` 当整章预算
        （默认 25s），长分页章节（实测 13+ 页、每页 ~1.9s）会在中途耗尽预算并
        **静默返回残章**（结尾断在词中）。调用方要更紧的预算就显式传 `deadline`。

        预算/页数用尽而**仍有下一页**时抛 DeadlineExceeded —— 与代码里既有的
        "分页中途失败 = 整章失败，不静默截断"（P2-5）同一口径。
        """
        if max_pages is None:
            max_pages = CONTENT_MAX_PAGES
        budget = deadline if deadline is not None else max(timeout, CONTENT_DEADLINE)
        end = time.monotonic() + budget
        if self.adapter is not None:
            try:
                with self.adapter._deadline_scope(budget):
                    content = self.adapter.get_content(chapter_url)
                if content:
                    # R59: 适配器正文同样过通用净化管线(营销广告深度清除等)
                    return _clean_text(content, '')
                    # 适配器自身清洗已处理结构, 此处幂等
            except Exception as e:
                # 适配器源失败不回退规则引擎（规则引擎对该源无效且加重负载），
                # 直接抛出交给 CrawlTask 重排队
                print(f"[adapter] {self.source.get('bookSourceName','?')} 正文失败: "
                      f"{type(e).__name__}: {e}", flush=True)
                raise
        rc = self.source.get('ruleContent') or {}
        parts = []
        url = chapter_url
        deadline = end
        # 章节 ID 提取（用于检测分页是否跨章：URL 中连续数字段）
        def _chapter_id(u):
            m = re.search(r'/(\d+)(?:_\d+)?\.html', u.rstrip('/'))
            return m.group(1) if m else None
        base_cid = _chapter_id(chapter_url)
        _stopped = ""      # 非正常结束的原因（预算/页数），用于**显式报错**而不是静默截断
        _seen_page_urls = {chapter_url}   # 已取过的页 URL（防"下一页"指回已取页）
        for _page_no in range(max_pages):
            if time.monotonic() > deadline:
                _stopped = "整章预算 %.0fs 用尽" % budget
                break
            # 检测跨章：下一页 URL 的章节 ID 变化 → 停止分页
            cid = _chapter_id(url)
            if base_cid and cid and cid != base_cid:
                break
            try:
                html = self.fetcher.get(url, source=self.source,
                                        timeout=min(15, max(0.1, deadline - time.monotonic())),
                                        deadline=deadline)
            except DeadlineExceeded:
                raise
            except Exception:
                # P2-5: 分页中途（非第一页）抓取失败 = 整章失败——抛异常走
                # CrawlTask 失败重试/落盘路径，已抓残页不入缓存（此前 break
                # 静默截断，读者会永久读到只有第 1 页的残章）。
                # 第一页失败保持原语义：parts 为空 → 返回空 → 上层判
                # "empty content" 同样走失败路径。
                # 正常结束（无下一页 nextContentUrl / 跨章检测）不经过此分支。
                if _page_no > 0:
                    raise
                break
            # P0-3：正文页只解析一次，content 与 nextContentUrl 复用同一文档
            # （此前每页 2 次全量解析）。html 原始字符串仍保留给下方 API 兜底正则。
            doc = parse_html(html)
            if rc.get('content'):
                content = self.engine.get_string(rc.get('content', ''), doc, base_url=url)
            else:
                # 兜底：找页面最大文本块作为正文（去除 script/style）
                # 注意：此分支 drop_tree 会变异文档，必须独立解析，
                # 不能复用上面供 nextContentUrl 使用的 doc。
                fdoc = parse_html(html)
                content = ''
                if fdoc is not None:
                    for bad in fdoc.cssselect('script, style, nav, header, footer'):
                        bad.drop_tree()
                    best = ''
                    for el in fdoc.cssselect('div, article, p, section'):
                        t = (el.text_content() or '').strip()
                        if len(t) > len(best):
                            best = t
                    content = re.sub(r'\n{3,}', '\n\n', best.strip())
            # ── 正文规则解析为空时的通用 API 兜底 ──
            # 部分源（如塔读）正文页由 JS 渲染，但页面暴露 hidden input 指向正文 API
            # （id=bookPartResourceUrl / *ContentUrl / *ResourceUrl，值形如 /path/to/api/...）
            if not content:
                m_api = re.search(
                    r'<input[^>]+(?:id|name)=["\']([^"\']*(?:ResourceUrl|ContentUrl|ContentApi|ContentSource)[^"\']*)["\'][^>]+value=["\']([^"\']+)["\']',
                    html, re.I)
                if not m_api:
                    m_api = re.search(
                        r'<input[^>]+value=["\']([^"\']+)["\'][^>]+(?:id|name)=["\']([^"\']*(?:ResourceUrl|ContentUrl|ContentApi|ContentSource)[^"\']*)["\']',
                        html, re.I)
                    if m_api:
                        m_api = (m_api.group(2), m_api.group(1))
                if m_api:
                    _api_url = m_api.group(2)
                    if _api_url.startswith('/'):
                        _api_url = urljoin(url, _api_url)
                    if _api_url.startswith('http'):
                        try:
                            _jr = self.fetcher.get(_api_url, source=self.source,
                                                    timeout=min(12, max(0.1, deadline - time.monotonic())),
                                                    retries=1, deadline=deadline)
                            if _jr and (_jr.lstrip().startswith('{') or _jr.lstrip().startswith('[')):
                                _jd = json.loads(_jr)
                                _c = (_jd.get('data') or {}) if isinstance(_jd, dict) else {}
                                if isinstance(_c, dict) and _c.get('content'):
                                    content = _c['content']
                                elif isinstance(_jd, dict) and _jd.get('content'):
                                    content = _jd['content']
                        except Exception:
                            pass
            if content:
                # ── 重复页防护（实测缺陷，2026-09-18）────────────────────────
                # 站点"下一页"有时会**指回已取过的页**（或同一内容换个 URL 再来一次），
                # 直接 append 会让同一页正文出现两遍。实测：yetianlian 第1384章
                # 79 段里第 29~52 段与 53~76 段完全相同（重复 24 段，正好一页），
                # quanben8 第1113章重复 15 段；全库 11506 章里 6 章如此。
                # 这里做两件事：
                #   ① 新页开头若与**已累积正文的结尾**相同 → 去掉这段重叠前缀
                #      （站点分页常见的"回读上一页最后几段"）；
                #   ② 若整页内容都已出现过（去重后没剩下任何新内容）→ 视为重复页，
                #      不追加并停止翻页（否则会在 A→B→A 的环里空转到页数上限）。
                # 判据用**字符级**比较，不用段落级：规则抽出来的正文未必有段落换行
                # （实测 `div#c@text` 抽出来整页就是一个大字符串），按段比会永远不匹配。
                _acc_c = _html_to_text('\n\n'.join(parts)).strip() if parts else ""
                _nc = _html_to_text(content).strip()
                # ① 整页内容已经在累积正文里出现过 → 重复页：不追加，停止翻页
                if _nc and _acc_c and _nc in _acc_c:
                    print(f"[crawler] 分页重复页已忽略（整页 {len(_nc)} 字都与已有正文重复）"
                          f": {url[:100]}", flush=True)
                    _stopped = ""
                    break
                # ② 页缝重叠：累积正文的**结尾**就是新页的**开头** → 去掉这段前缀。
                #    只重叠很短时不算（"“……”"这种短句在两页都出现是巧合，
                #    删掉就等于删正文）——所以要求 ≥30 字完全相同。
                _k = 0
                for _n in range(min(len(_acc_c), len(_nc), 2000), 29, -1):
                    if _acc_c[-_n:] == _nc[:_n]:
                        _k = _n
                        break
                if _k:
                    print(f"[crawler] 分页重叠前缀已去掉 {_k} 字: {url[:100]}", flush=True)
                    content = _nc[_k:]
                if content.strip():
                    parts.append(content)
            nxt = self.engine.get_string(rc.get('nextContentUrl', ''), doc, base_url=url)
            if not nxt or nxt == url:
                break
            nxt = normalize_url(nxt, url)
            if nxt in _seen_page_urls:
                # "下一页"指回了本章已取过的页 → 直接停，不再空转
                print(f"[crawler] 下一页指回已取页面，停止翻页: {nxt[:100]}", flush=True)
                break
            _seen_page_urls.add(nxt)
            url = nxt
            if _page_no == max_pages - 1:
                # 还有下一页但已达分页上限：同样是"没取完"，不能静默当成功
                _stopped = "分页数达到上限 %d 页" % max_pages
            # 分页节流由 Fetcher 按域名限流替代（触限才等待）
        if _stopped and parts:
            # 还有下一页却没取完 → **不能**把残章当成功返回（用户会永久读到半章，
            # 且缓存后不会自愈）。抛异常走失败重试/如实标注路径。
            # 注意：这里**不要**再 `from .fetcher import DeadlineExceeded` ——
            # 函数内 import 会把它变成局部名，于是上面 `except DeadlineExceeded:`
            # 在真正发生分页抓取异常时抛 UnboundLocalError（实测撞到过），
            # 用户看到的是莫名其妙的报错而不是"整章失败"。模块顶部已导入。
            raise DeadlineExceeded(
                "%s（已取 %d 页/%d 字），为避免静默截断按整章失败处理: %s"
                % (_stopped, len(parts), sum(len(p) for p in parts),
                   chapter_url[:120]))
        text = '\n\n'.join(p for p in parts if p)
        # HTML 标签统一清洗：块级闭合标签与 <br> → 换行、去标签、实体反转
        # （部分源正文 API 返回 <p>…</p> 段落，仅去标签会粘成一段）
        # R47: 收敛到 cleaner.html_to_text 单一实现
        text = _html_to_text(text)

        # ── 全方位文本净化（engine/cleaner.py 分层管线）──
        text = _clean_text(text, rc.get('replaceRegex') or '')
        return text


# ── 整书爬取任务 ────────────────────────────
class CrawlTask:
    """基于书源的单本整书爬取（串行、缓存、断点续爬、失败优先重试）"""

    def __init__(self, source, book_url, book_dir, resume=True,
                 progress_callback=None, stop_callback=None,
                 alt_sources=None):
        self.source = source
        self.book_url = book_url
        self.book_dir = book_dir
        self.progress_callback = progress_callback or (lambda p: None)
        self.stop_callback = stop_callback or (lambda: False)
        # R49(多源混合下载)：显式备源(搜索分组里同书其它源)；None → 补章时自动发现
        self.alt_sources = alt_sources or None
        self.crawler = SourceCrawler(source, progress_callback)
        self.state_file = os.path.join(book_dir, '_state.json')
        self.chapters_file = os.path.join(book_dir, '_chapters.json')
        self.book: Book = None
        self.completed = []        # url 列表（已下载，持久化格式保持 list 不变）
        self.failed = {}           # url -> reason
        self._resume = resume
        self._cache_set = None    # 已有缓存 URL 集（爬取期惰性构建一次）
        # B06: completed 的运行时 set 镜像——断点检查 is_done 由 O(n) 列表
        # 扫描改 O(1) 集合判断；持久化仍写 list（磁盘格式不变）
        self._completed_set = set()
        # B06: 内容 revision——每写入一章缓存 +1；merge_txt 记录上次合并时
        # 的 revision，内容未变且 book.txt 在 → 跳过全量合并（幂等兜底）
        self._content_rev = 0
        self._merged_rev = None

    # ── 状态持久化（R29，技术评审5.1：统一原子写，防崩溃截断）──
    # R47: 实现收敛到 app_utils.atomic_write / atomic_write_text 单一来源
    @staticmethod
    def _atomic_json(path, data):
        _atomic_write(path, data)

    def _save_state(self):
        bd = self.book.to_dict() if self.book else {}
        bd['source_uid'] = self.source.get('uid', '')
        state = {
            'book': bd,
            'chapters': [{'name': c['name'], 'url': c['url']} for c in (self.book.chapters if self.book else [])],
            'completed': self.completed,
            'failed': self.failed,
            'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        self._atomic_json(self.state_file, state)
        self._atomic_json(self.chapters_file, state['chapters'])

    def _load_state(self):
        if not os.path.exists(self.state_file):
            return False
        try:
            with open(self.state_file, encoding='utf-8') as f:
                st = json.load(f)
            self.book = Book(self.source, st['book'].get('book_url', self.book_url))
            for k, v in st.get('book', {}).items():
                setattr(self.book, k, v)
            self.book.chapters = [{'name': c['name'], 'url': c['url']}
                                  for c in st.get('chapters', [])]
            self.book.chapter_count = len(self.book.chapters)
            self.completed = st.get('completed', [])
            self.failed = st.get('failed', {}) or {}
            self._completed_set = set(self.completed)  # B06: 运行时镜像重建
            self._cache_set = None   # 目录已替换，缓存集惰性重建
            return True
        except Exception:
            return False

    # ── 进度上报 ──
    def _done_count(self):
        """已完成章数 = **磁盘上真的有缓存**的章节数。
        R47 性能：缓存集合每次爬取只扫一次盘（此前每次 _emit 都按章
        os.path.exists → 2500 章的书整轮 O(n²) 次 stat）。任务运行期间
        新增缓存的唯一写入方是本任务自身（_save_cache 同步入集）。

        口径说明（0.55.0）：不再把 completed 直接并进来。completed 只是
        "爬取时成功过"的历史记录；缓存被清理（清缓存/删除书目录后任务重建）
        之后它仍会谎报已完成，界面就会显示"已下载"而正文是空的。
        用户看到的是缓存，所以进度也按缓存算。
        """
        if not (self.book and self.book.chapters):
            return len(self.completed)
        if self._cache_set is None:
            self._cache_set = {c['url'] for c in self.book.chapters
                               if self._has_cache(c['url'])}
        return len(self._cache_set)

    def _sync_completed_with_disk(self):
        """开爬前让 completed 与磁盘缓存对齐（缓存不在 = 不算已完成）。

        为什么必须做：is_done 判据含 completed。若某章的缓存文件已被删除
        （用户清理缓存、书目录被移动/重建），而 completed 里还留着它，
        那么**重启任务也永远不会重抓这一章**——界面标"已下载"、点进去空白、
        "继续下载"毫无反应，用户没有任何自救路径。这里把这类条目剔除，
        本章随后进入 pending 队列被重新抓取（重爬单章是同样的效果）。
        返回被剔除的条目数。
        """
        if not (self.book and self.book.chapters):
            return 0
        if self._cache_set is None:
            self._cache_set = {c['url'] for c in self.book.chapters
                               if self._has_cache(c['url'])}
        stale = [u for u in self.completed if u not in self._cache_set]
        if stale:
            self.completed = [u for u in self.completed if u in self._cache_set]
            self._completed_set = set(self.completed)
        return len(stale)

    def _emit(self, **extra):
        total = self.book.chapter_count if self.book else 0
        _done = self._done_count()
        payload = {'status': 'running', 'total': total,
                   'completed': _done,
                   'failed': len(self.failed), 'current': extra.pop('current', ''),
                   'book_name': (self.book.name if self.book else ''),
                   'updated_at': time.strftime('%Y-%m-%d %H:%M:%S')}
        payload.update(extra)
        try:
            self.progress_callback(payload)
        except Exception:
            pass

    # ── 缓存 ──
    def _cache_path(self, url):
        return os.path.join(self.book_dir, f"{_cache_key_of(url)}.cache")

    def _save_cache(self, url, text):
        # R29(技术评审5.1): 缓存原子写——先写临时文件，内容非空才替换，
        # 防崩溃留下半写缓存（_has_cache 只查存在，半写会被当成完成内容）
        # R47: 实现收敛到 app_utils.atomic_write_text
        if not text:
            return
        _atomic_write_text(self._cache_path(url), text)
        self._content_rev += 1   # B06: 内容 revision，驱动 merge_txt 延迟合并
        if self._cache_set is not None:
            self._cache_set.add(url)

    def _has_cache(self, url):
        return os.path.exists(self._cache_path(url))

    def _mark_completed(self, url):
        """B06: completed 列表与运行时 set 镜像同步写入（O(1) 成员判断）"""
        if url not in self._completed_set:
            self.completed.append(url)
            self._completed_set.add(url)

    def _concurrency(self):
        """并发度：书源 concurrentRate 字段可配（'8' 或 '8/1000'），默认 8"""
        cr = (self.source.get('concurrentRate') or '').strip()
        m = re.match(r'^(\d+)', cr)
        n = int(m.group(1)) if m else CRAWL_DEFAULT_CONCURRENCY
        return max(1, min(n, CRAWL_MAX_CONCURRENCY))

    # ── 主流程 ──
    def crawl(self, preset_chapters=None):
        """preset_chapters：检查更新模式，传入最新目录，自动合并旧目录并只爬新章节"""
        os.makedirs(self.book_dir, exist_ok=True)
        if preset_chapters is not None:
            # 增量模式：加载旧状态，合并新章节
            if self._resume and self._load_state():
                old_urls = {c['url'] for c in self.book.chapters}
                self.book.chapters = list(self.book.chapters) + [
                    c for c in preset_chapters if c['url'] not in old_urls]
                self.book.chapter_count = len(self.book.chapters)
                self._emit(message=f"检查更新：新增 {len(self.book.chapters) - len(old_urls)} 章")
            else:
                self.book = Book(self.source, self.book_url)
                self.book.chapters = list(preset_chapters)
                self.book.chapter_count = len(preset_chapters)
                self.completed = []
                self.failed = {}
                self._completed_set = set()
            self._save_state()
        elif self._resume and self._load_state():
            self._emit(message="断点恢复")
        elif self.book and self.book.chapters:
            # 外部已传入目录（preset 或预填充）→ 跳过重新发现，直接开爬
            self._emit(message=f"使用已提供目录（{len(self.book.chapters)} 章）…")
            if not self.completed and not self.failed:
                self.completed = []
                self.failed = {}
                self._completed_set = set()
                self._save_state()
        else:
            self._emit(status='discover', message='获取详情…')
            self.book = self.crawler.get_book(self.book_url)
            self._emit(status='discover', message='获取目录…')
            self.crawler.get_toc(self.book)
            self.completed = []
            self.failed = {}
            self._completed_set = set()
            self._save_state()

        if not self.book or not self.book.chapters:
            self._emit(status='error', message='目录为空，无法爬取')
            return

        # 0.55.0: completed 与磁盘缓存对齐（缓存被清理过的章节要重抓，
        # 否则任务会永久跳过它们，界面却标"已下载"）
        _stale = self._sync_completed_with_disk()
        if _stale:
            self._emit(message=f"{_stale} 章缓存已丢失，将重新下载")
            self._save_state()

        # 待下载 = 失败章节（优先） + 未完成章节
        chapters = self.book.chapters
        # B06: completed 成员判断走运行时 set 镜像（O(1)），不再扫列表
        is_done = lambda c: c['url'] in self._completed_set or self._has_cache(c['url'])
        pending = [c for c in chapters if c['url'] in self.failed]
        pending_urls = {c['url'] for c in pending}
        for c in chapters:
            if not is_done(c) and c['url'] not in pending_urls:
                pending.append(c)
        retried = len([c for c in pending if c['url'] in self.failed])
        total = len(chapters)
        done = total - len(pending)
        self._emit(message=f"先重试 {retried} 个失败章节，再下载 {len(pending)-retried} 个新章节")

        start = time.time()
        downloaded = 0
        checkpoint_downloaded = 0
        # ── 滑动窗口并发下载（借鉴 legado：16 章同时在飞）──
        # 并发度：书源可配 concurrentRate 覆盖，默认 8
        concurrency = self._concurrency()
        from concurrent.futures import (ThreadPoolExecutor as _TPE,
                                        wait as _fwait,
                                        FIRST_COMPLETED as _FC)
        retry_count = {}   # url -> 已重试次数（失败重排队 ≤3 次）
        delayed = []       # [(到期时间, chapter)] 延迟重排队的章节

        def _fetch_one(ch):
            """下载单章，返回 (ch, content 或 None, error 或 None)"""
            url, name = ch['url'], ch['name']
            try:
                content = self.crawler.get_content(url)
                if not content.strip():
                    return ch, None, 'empty content'
                return ch, content, None
            except Exception as e:
                return ch, None, f"{type(e).__name__}: {e}"

        self._emit(message=f"开始并发下载（{concurrency} 并发）…")
        stopping = False   # B06: 区分"停止中"（已请求、在飞请求收尾）与"已停止"（终态）
        try:
            with _TPE(max_workers=concurrency) as ex:
                # 初始填充：并发度个任务
                initial = [] if self.stop_callback() else pending[:concurrency]
                futs = {ex.submit(_fetch_one, c): c for c in initial}
                idx = len(initial)
                # 循环条件：有在飞任务，或延迟重排未清，或还有未提交章节
                while futs or delayed or idx < len(pending):
                    if stopping or self.stop_callback():
                        # B06: 停止中——不再补发新章，取消尚未开始的任务；
                        # 在飞 HTTP 请求不可中断，等其自身结束后由末尾终态
                        # emit 'stopped'（已停止）。周期唤醒保证 stop 请求
                        # ≤0.2s 内被看到。
                        if not stopping:
                            stopping = True
                            self._emit(status='stopping',
                                       message='正在停止：不再补发新章，等待在飞请求结束…')
                        for f in list(futs):
                            if f.cancel():
                                del futs[f]
                        delayed.clear()
                        idx = len(pending)
                        # 已运行的请求仍需等待；收下成功结果，续爬无需再下载。
                        if not futs:
                            break
                    if not futs:
                        # 无在飞任务：等待 delayed 到期或提交新章节
                        if delayed:
                            time.sleep(0.1)
                            now_t = time.time()
                            while delayed and delayed[0][0] <= now_t and len(futs) < concurrency:
                                _, dch = delayed.pop(0)
                                futs[ex.submit(_fetch_one, dch)] = dch
                            continue
                        if idx < len(pending):
                            futs[ex.submit(_fetch_one, pending[idx])] = pending[idx]
                            idx += 1
                            continue
                        break  # 全部完成
                    # B06: as_completed 会一直阻塞到下一章完成，期间停止请求
                    # 无法被看到；改 wait(timeout=0.2) 周期唤醒，超时空转回
                    # 循环顶检查停止标记
                    done_futs, _ = _fwait(list(futs), timeout=0.2,
                                          return_when=_FC)
                    if not done_futs:
                        continue  # 周期唤醒：回循环顶检查停止/补位
                    for f in done_futs:
                        ch, content, err = f.result()
                        del futs[f]
                        url = ch['url']
                        if content is not None:
                            self._save_cache(url, f"{ch['name']}\n\n{content}")
                            self._mark_completed(url)
                            self.failed.pop(url, None)
                            retry_count.pop(url, None)
                            downloaded += 1
                            eta = ((time.time() - start) / downloaded *
                                   (total - done - downloaded)) if downloaded else 0
                            self._emit(status='stopping' if stopping else 'running',
                                       current=ch['name'],
                                       message=f"{done+downloaded}/{total} · ETA {eta/60:.0f}m")
                        else:
                            # 失败：延迟重排队 ≤3 次（legado：delay 1s 后重排，超限跳过）
                            rc = retry_count.get(url, 0) + 1
                            if not stopping and rc <= CRAWL_RETRY_MAX:
                                retry_count[url] = rc
                                delayed.append((time.time() + CRAWL_RETRY_DELAY, ch))
                                self._emit(current=ch['name'],
                                           message=f"失败重试({rc}/3): {err}")
                            else:
                                self.failed[url] = err
                                retry_count.pop(url, None)
                                self._emit(status='stopping' if stopping else 'running',
                                           current=ch['name'], message=f"失败跳过: {err}")
                                self._save_state()  # 失败及时落盘（标注可见）
                    if downloaded - checkpoint_downloaded >= 20:
                        self._save_state()
                        checkpoint_downloaded = downloaded
                    # B06: 处理完成果后、补位前再看一次停止标记——
                    # 停止请求落在本章处理期间的，不得再补发新章
                    if stopping or self.stop_callback():
                        continue  # 回循环顶走停止分支
                    # 补位：先补延迟重排的（到期），再补新章节
                    now_t = time.time()
                    while delayed and delayed[0][0] <= now_t and len(futs) < concurrency:
                        _, dch = delayed.pop(0)
                        futs[ex.submit(_fetch_one, dch)] = dch
                    while idx < len(pending) and len(futs) < concurrency:
                        futs[ex.submit(_fetch_one, pending[idx])] = pending[idx]
                        idx += 1
        finally:
            # P2-4: 最终落盘兜底——完成/停止/异常任何退出路径都保存断点。
            # 崩溃窗口内的进度丢失由 is_done = completed ∪ 磁盘 .cache 兜底，
            # 重启续爬不会重下已缓存章节
            self._save_state()
        # R53: 多源混合下载已取消(用户决定)——失败章节不再自动换备源补章,
        # 任务如实标失败, 可手动重试主源或重下
        # 输出失败清单（供补抓）
        if self.failed:
            _fl = "; ".join(f"{c['name'][:20]}: {r}" for c in self.book.chapters
                            if c['url'] in self.failed
                            for r in [self.failed[c['url']]][:1])[:400]
            print(f"[crawl] 失败章节 {len(self.failed)} 个: {_fl}", flush=True)
        stopped = stopping or self.stop_callback()
        self._emit(status='stopped' if stopped else 'done',
                   message='已停止' if stopped else '完成',
                   failed=len(self.failed))

    def merge_txt(self, force=False):
        """合并所有缓存为 txt（每章带章节名标题）

        B06 + 导出版本机制：跳过全量合并的判据由"内存计数器"升级为
        **磁盘派生的内容版本**（content_revision）——book.txt 存在且
        _export.json 记录的 rev 与当前章节缓存一致时直接复用，跨进程/
        重启有效；单章重爬、补章、目录重排/重命名都会改变 rev 从而触发
        重建。暂停/停止路径（server/state.py）已不再调用本方法，重复调用
        由本守卫兜底幂等。实际合并写收敛到模块级 merge_book_txt（同一
        book_dir 串行，txt 与元数据成对），此处仅做任务态包装。"""
        if not self.book or not self.book.chapters:
            return None
        out = merge_book_txt(self.book_dir, self.book.chapters, force=force)
        self._merged_rev = (content_revision(self.book_dir, self.book.chapters)
                            if out else self._merged_rev)
        return out
