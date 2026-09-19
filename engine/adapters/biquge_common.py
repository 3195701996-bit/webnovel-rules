"""笔趣阁经典模板通用适配器（覆盖多个同模板站点）。

模板特征（实测 biquge.company / yuzhaiwx / win10city 等一致）：
- 搜索：POST {base}/modules/article/search.php  data={searchkey: keyword}
  结果项 <a href="{base}/book/{id}.html">书名</a>
- 详情/目录：GET {base}/book/{id}.html，章节 <dd><a href="{base}/read/{id}/{ch}.html">
  目录倒序（最新章节在前）→ 需反转
- 正文：GET {base}/read/{id}/{ch}.html，容器 class="readcontent"
  正文含 &emsp; 缩进实体

注册表见 engine/adapters/__init__.py 的 _REGISTRY（按域名批量挂载）。
"""
import re
from itertools import pairwise
from urllib.parse import urljoin

from . import BaseSourceAdapter

# ── 目录序号解析（仅本模块用，不改共享 chapter_name_num 的语义）────────────
# 共享的 `chapter_name_num` 只认"阿拉伯数字+章"：实测对"第2666节"、"第一章"
# 一律返回 0。它的语义被 jhsssd / tadu / kanshuw 一起使用，放宽会波及其它源
# 的既有行为，故这里独立实现一个**广义序号**解析器，只服务于"完整 1..N 判定"。
_NUM_RE = re.compile(r"第\s*(\d+|[零〇一二三四五六七八九十百千两]{1,7})\s*"
                     r"(?:章|节|節|回|话|話|集|篇)")
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def _cn_to_int(s):
    """中文数字 → 整数。两种写法都支持：
    · 带单位（一百零八 / 一千二百三十四 / 十二 / 二十 / 十）；
    · 逐位读（一〇二 → 102，纯数字串无单位时按位拼接）。
    无法解析返回 0。"""
    if not s:
        return 0
    if not any(ch in _CN_UNIT for ch in s):
        v = 0
        for ch in s:
            if ch not in _CN_DIGIT:
                return 0
            v = v * 10 + _CN_DIGIT[ch]
        return v
    total, sec = 0, 0
    for ch in s:
        if ch in _CN_DIGIT:
            sec = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            total += (sec or 1) * _CN_UNIT[ch]
            sec = 0
        else:
            return 0
    return total + sec


def _seq_num(name):
    """章节名的广义序号；无法解析返回 0（0 表示"不确定"，调用方据此放弃重排）"""
    m = _NUM_RE.search(name or "")
    if not m:
        return 0
    g = m.group(1)
    return int(g) if g.isdigit() else _cn_to_int(g)


class Adapter(BaseSourceAdapter):
    uid_prefix = ""

    def __init__(self, source, fetcher):
        super().__init__(source, fetcher)
        self.base = (source.get("bookSourceUrl") or "").rstrip("/")
        # 清理 URL 尾部的规则残留（如 #🎃 / ##@遗憾）
        self.base = re.sub(r"#.*$", "", self.base).strip()

    # ── 搜索 ──
    def search(self, keyword, page=1):
        url = self.base + "/modules/article/search.php"
        html = self._post(url, data={"searchkey": keyword},
                          timeout=12, retries=2,
                          extra_headers={"Content-Type": "application/x-www-form-urlencoded"})
        if not html:
            return []
        books = []
        # 结果项：<div class="bookbox"> 内 bookinfo（书名 h4.bookname +
        # 作者 div.author + 最新章节 div.cat）
        for m in re.finditer(
                r'<div class="bookbox">(.*?)(?:</div>\s*){2}', html, re.S):
            seg = m.group(1)
            um = re.search(r'<h4 class="bookname"><a[^>]*href="(https?://[^"]*?/book/\d+\.html)"[^>]*>([^<]{2,60})</a></h4>', seg)
            if not um:
                continue
            u, name = um.group(1), um.group(2).strip()
            if not name or not u.startswith("http"):
                continue
            if any(b["book_url"] == u for b in books):
                continue
            # 作者：<div class="author">作者：xxx</div>
            _am = re.search(r'作者[:：]?\s*([^<]{1,30})</div>', seg)
            author = (_am.group(1).strip() if _am else "")
            if author == "佚名":
                author = ""
            books.append({
                "name": name,
                "author": author,
                "intro": "",
                "kind": "",
                "cover": "",
                "book_url": u,
                "source_uid": self.source.get("uid", ""),
                "source_name": self.source.get("bookSourceName", ""),
            })
        return books

    # ── 详情 ──
    def get_book(self, book_url, fast=False):
        html = self._get(book_url, timeout=12 if not fast else 8, retries=2)
        if not html:
            return None
        name = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
        author = re.search(r"作者[:：]?\s*(?:</?\w+[^>]*>)*([^<]{2,30})", html)
        intro = re.search(r'class="[^"]*intro[^"]*"[^>]*>(.*?)</div>', html, re.S)
        cover = re.search(r'class="[^"]*cover[^"]*"[^>]*>\s*<img[^>]*src="([^"]+)"', html)
        last = re.search(r"最新章节[:：]?\s*<a[^>]*>([^<]+)</a>", html)
        info = {
            "name": name.group(1).strip() if name else "",
            "author": author.group(1).strip() if author else "",
            "intro": re.sub(r"<[^>]+>", "", intro.group(1)).strip() if intro else "",
            "cover": cover.group(1) if cover else "",
            "kind": "",
            "last_chapter": last.group(1).strip() if last else "",
            "word_count": "",
            "update_time": "",
        }
        return info

    # ── 目录 ──
    def get_toc(self, book):
        url = getattr(book, "toc_url", "") or book.book_url
        html = self._get(url, timeout=15, retries=2)
        if not html:
            raise RuntimeError("笔趣阁目录页获取失败")
        chapters = []
        seen = set()
        for m in re.finditer(
                r"<dd>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>([^<]{1,60})</a>", html):
            u, name = m.group(1), m.group(2).strip()
            full = u if u.startswith("http") else urljoin(url, u)
            if full in seen or not name:
                continue
            seen.add(full)
            chapters.append({"name": name, "url": full})
        if not chapters:
            raise RuntimeError("笔趣阁目录解析为空")
        # 目录结构：[最近更新倒序 N 章] + [完整目录 1..N 正序]
        # R42 起的两类站结构处理，R78 按实测重新收紧判据（旧判据会**静默丢章**）：
        #   A（biquge.company 型）：最新章节区与完整目录尾部重叠（同 URL 去重后
        #     成了 [5,4,3,1,2]）→ 从"第 1 章"切片，再把比主体新的前缀补到最后；
        #   B（yuzhaiwuh 型）：最新章节区只含最新 N 章，完整目录只到 N-12 →
        #     单纯切片会丢最新章（长线"连载最新✗"根因）→ 同样补到尾部。
        # 收紧点（每条都有实测依据）：
        #   ① 定位章首改用**广义序号 == 1**，不再用 `第\s*1\s*章` 字面量：
        #      本书目录同时存在"第一章 惊蛰"（真首章）与后文重启编号的
        #      "第1章 少年游"（idx≈1227），字面量匹配到的是**后面那个** →
        #      前面 1200+ 章被整段丢掉且不报错。
        #   ② 只有"前缀**每一条**都能解析出序号、且都比主体最大序号更新"时，
        #      才认定它是最新章节区。否则保持原样——例如前缀是"序章"这类
        #      正规章节（序号不可解析）时，旧写法会把它当成最新章块丢掉。
        seq = [_seq_num(c.get("name")) for c in chapters]
        start = next((i for i, n in enumerate(seq) if n == 1), None)
        if start:
            body_max = max(seq[start:]) if any(seq[start:]) else 0
            if all(seq[:start]) and body_max and all(n > body_max for n in seq[:start]):
                chapters = chapters[start:] + chapters[:start]
        elif start is None:
            # 没有章首标记：整体**严格倒序**（逐条不升）才反转
            # （旧的"首章号 > 末章号"判据会被一个错号的首章误触发：
            #  如 [999(错号),1,2,3] 会被反转成 [3,2,1,999]）
            if len(seq) > 1 and all(seq) and \
                    all(b <= a for a, b in pairwise(seq)) and seq[0] > seq[-1]:
                chapters.reverse()
        # R78: "完整 1..N 序号 ⇒ 按序号升序"兜底（2026-09-18 实测缺陷）
        # 实测站点（www.yuzhaiwuh.xyz《剑来》2666 节）：页首是"最新章节"**倒序块**
        # （2666→2655 共 12 条），其后才是完整目录（1→2654）。旧实现里章名是
        # "第N节"而 `chapter_name_num` 只认"阿拉伯数字+章"→ 一律返回 0 →
        # ① 找不到章首标记走不到切片分支；② 倒序判断成了 `0 > 0` 为假。
        # 结果是站点页序原样透传：目录以"第2666节"开头、12 条最新章倒序挂最前，
        # 阅读顺序与下载顺序全乱（实测 2666 条全部唯一，不是重复条目）。
        # 判据**只在序号唯一且恰好覆盖 1..N** 时成立——此时阅读顺序没有第二种解，
        # 且对本来就正序的目录是恒等变换。跨源实测反向印证了这条闸门的必要性：
        # 多个彼此独立的源都带同样的**错号章名**（"第四五百五十二章"实为 452、
        # "第两百二十三章"插在 212 与 214 之间）而 URL 页序始终正确——
        # 若按章名排序会毁掉正确顺序，所以序号不唯一/不连续时一律不动。
        seq = [_seq_num(c.get("name")) for c in chapters]
        if len(chapters) > 1 and all(seq) and len(set(seq)) == len(seq) \
                and sorted(seq) == list(range(1, len(seq) + 1)):
            chapters = [c for _, c in sorted(zip(seq, chapters), key=lambda t: t[0])]
        return chapters

    # ── 正文 ──
    def get_content(self, chapter_url):
        html = self._get(chapter_url, timeout=15, retries=2)
        if not html:
            raise RuntimeError("笔趣阁章节页获取失败")
        m = re.search(r'class="[^"]*readcontent[^"]*"[^>]*>(.*?)</div>', html, re.S)
        if not m:
            # 兜底：常见正文容器
            m = re.search(r'id="content"[^>]*>(.*?)</div>', html, re.S)
        if not m:
            return ""
        text = m.group(1)
        # 实体反转（&emsp; &nbsp;）
        import html as _html
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        text = _html.unescape(text)
        text = re.sub(r"[ \t\xa0\u3000]{2,}", " ", text)
        text = "\n".join(l.strip() for l in text.split("\n"))
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
