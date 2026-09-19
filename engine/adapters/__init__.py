"""专属书源适配器基类与注册表。

设计目标（用户需求）：不再依赖 legado 套用规则引擎，而是为每个网站编写
独立的 Python 适配器，硬编码该站点的真实结构（搜索/目录/正文），因为不同
网站的结构、反爬、渲染方式都不一样。

架构：
- BaseSourceAdapter: 每个适配器实现 search()/get_toc()/get_content()。
- registry: 按 bookSourceUrl 域名 + uid 前缀匹配到具体适配器类。
- SourceCrawler 构造时若命中适配器，则完全用适配器逻辑，绕过规则引擎。

适配器产出与规则引擎保持一致的数据契约：
- search() -> [{name, author, intro, kind, cover, book_url, source_uid, source_name}]
- get_toc(book) -> 填充 book.chapters (list of dict {name,url}) / book.chapter_count
- get_content(chapter_url) -> 清洗后的正文纯文本
"""
import importlib
import logging
import re
import threading
import time
from contextlib import contextmanager

log = logging.getLogger("adapters")

# ── 注册表：每个条目 (匹配域名列表, 适配器模块名) ──
# 按实际在用的源优先；后续可继续补充
_REGISTRY = [
    # (uid 前缀 或 域名子串, 模块名)
    ("book.sfacg.com", "sfacg"),      # SF轻小说（已删源，适配器备用）
    ("tadu.com", "tadu"),             # 塔读（正文API）
    ("m.jhsssd.com", "jhsssd"),       # 精华书阁
    ("ixdzs8.com", "ixdzs"),          # 爱下电子书(ixdzs8 现役镜像)
    ("ixdzs.com", "ixdzs"),           # 爱下电子书(主域镜像)
    ("imaxreader.com", "imaxreader"), # 醉读小说（站点封锁待恢复）
    ("m.biqutu.info", "biqutu"),      # 爱笔楼（站点封锁待恢复）
    ("kudushu.org", "kudushu"),       # 苦读书（Cloudflare挑战）
    ("quanben.io", "quanben_io"),      # 全本小说网 quanben.io（AMP目录list.html/正文）
    ("quanben5.com", "quanben"),       # 全本小说网 quanben5.com（AMP镜像）
    ("kanshuw.com", "kanshuw"),       # 看书网
    # 笔趣阁经典模板组（同模板批量挂载）
    ("yuzhaiwx.com", "biquge_common"),
    ("yuzhaiwuh.xyz", "biquge_common"),
    ("po18.asia", "biquge_common"),
    ("po18bl.com", "biquge_common"),
    ("po18m.com", "biquge_common"),
    ("po18m.vip", "biquge_common"),
    ("po18r.com", "biquge_common"),
    ("rouwenwu.com", "biquge_common"),
    ("wcshuba.com", "biquge_common"),
    ("win10city.com", "biquge_common"),
    ("biquge.company", "biquge_common"),
    ("haogushi88.cc", "biquge_common"),
    ("hqrgjtcw.com", "biquge_common"),
    ("huilingtian.com", "biquge_common"),
    ("maojiuxs.com", "biquge_common"),
    ("yuanzunxs.cc", "biquge_common"),
    ("ddsk.la", "ddsk"),                # 大帝书阁 wap(ddsk.la/dingdiansk 同库)
    ("dingdiansk.com", "ddsk"),
]

_loader_lock = threading.Lock()
_loaded = {}


class BaseSourceAdapter:
    """适配器基类。子类实现站点专属逻辑。"""

    # 每个适配器声明的 uid 前缀（用于识别；可选）
    uid_prefix = ""

    def __init__(self, source, fetcher):
        self.source = source
        self.fetcher = fetcher  # engine.fetcher.Fetcher 实例（域名限流/UA 处理）
        self.base = (source.get("bookSourceUrl") or "").rstrip("/")
        # P2-4: deadline 存储改为线程本地——同一适配器实例会被 CrawlTask 的
        # 多路并发 _fetch_one 共享，原普通实例属性会被各线程互相覆盖，
        # 导致超时预算失效或误抛 DeadlineExceeded。
        self._tls = threading.local()

    # ── 必须实现 ──
    def search(self, keyword, page=1):
        """搜索，返回书籍列表（与规则引擎 search() 同契约）"""
        raise NotImplementedError

    def get_book(self, book_url, fast=False):
        """返回 dict: {name, author, intro, cover, kind, last_chapter,
        word_count, update_time, toc_url}；不实现则返回 None 用规则引擎兜底"""
        return None

    def get_toc(self, book):
        """获取目录，填充 book.chapters / book.chapter_count。返回章节列表"""
        raise NotImplementedError

    def get_content(self, chapter_url):
        """获取单章正文（清洗后纯文本）"""
        raise NotImplementedError

    # ── 工具方法 ──
    @property
    def _deadline(self):
        """业务级截止时间（time.monotonic() 基准）。线程本地，默认 None。
        保留属性接口供子类直接读/写（如 quanben/kudushu），但存储在线程
        本地，并发线程互不覆盖。"""
        return getattr(self._tls, "deadline", None)

    @_deadline.setter
    def _deadline(self, value):
        self._tls.deadline = value

    def _remaining_timeout(self, timeout, deadline=None):
        # deadline 显式参数优先（沿调用链透传）；未传时回退线程本地值
        dl = deadline if deadline is not None else self._deadline
        if dl is None:
            return timeout
        remaining = dl - time.monotonic()
        if remaining <= 0:
            from ..fetcher import DeadlineExceeded
            raise DeadlineExceeded("adapter deadline exceeded")
        return min(float(timeout), remaining)

    def _get(self, url, timeout=15, retries=2, deadline=None, **kw):
        dl = deadline if deadline is not None else self._deadline
        return self.fetcher.get(url, source=self.source,
                                timeout=self._remaining_timeout(timeout, dl),
                                retries=retries, deadline=dl, **kw)

    def _post(self, url, data=None, timeout=15, retries=2, deadline=None, **kw):
        dl = deadline if deadline is not None else self._deadline
        return self.fetcher.post(url, data=data, source=self.source,
                                 timeout=self._remaining_timeout(timeout, dl),
                                 retries=retries, deadline=dl, **kw)

    def _with_deadline(self, seconds):
        """为适配器的一次详情/目录/正文调用设置业务级截止时间（线程本地）。
        新代码优先用 _deadline_scope()（异常安全、可嵌套恢复）。"""
        self._deadline = time.monotonic() + seconds if seconds else None
        return self

    @contextmanager
    def _deadline_scope(self, seconds):
        """P2-4: 业务级 deadline 作用域——进入时为当前线程设置截止时间，
        退出时恢复前值（异常安全）。替代"手动 _with_deadline + finally
        _deadline=None"模式，避免异常路径漏清理与跨线程覆盖。"""
        prev = self._deadline
        self._deadline = time.monotonic() + seconds if seconds else None
        try:
            yield self._deadline
        finally:
            self._deadline = prev

    def _clean_content(self, text):
        """统一正文清洗：HTML→文本（R47 收敛到 cleaner.html_to_text 单一实现）、
        清分页标记"""
        from ..cleaner import html_to_text
        text = html_to_text(text)
        # 清除分页/翻页标记（"第(1/3)页" 等）
        text = re.sub(r"第\(\d+/\d+\)页", "", text)
        return text.strip()


def resolve_adapter(source, fetcher):
    """按书源 URL/uid 查找适配器实例；未命中返回 None（继续用规则引擎）"""
    if not source:
        return None
    url = (source.get("bookSourceUrl") or "") + " " + (source.get("uid") or "")
    uid = source.get("uid") or ""
    for key, module in _REGISTRY:
        if key in url or key in uid:
            try:
                with _loader_lock:
                    if module not in _loaded:
                        _loaded[module] = importlib.import_module(
                            f"engine.adapters.{module}")
                cls = getattr(_loaded[module], "Adapter", None)
                if cls is None:
                    log.warning("adapter %s: no Adapter class", module)
                    continue
                return cls(source, fetcher)
            except Exception as e:
                log.warning("adapter %s load failed: %s", module, e)
                continue
    return None


# ── 公共章节序号提取 ──
# 原 biquge_common / jhsssd / tadu / kanshuw 各持一份私有 _num，语义相同的
# 收敛于此（2026-09 收敛）：名称取号与 URL 取号是两类不同语义，不互相合并；
# manga 的 download_manager._num_key 是多层级排序键（话/卷/附加分级），
# 语义不同，保持独立，勿用本函数替换。
def chapter_name_num(name):
    """从章节名提取"第X章"序号（用于目录排序/正倒序判断）；无匹配返回 0。"""
    m = re.search(r"第\s*(\d+)\s*章", name or "")
    return int(m.group(1)) if m else 0


def chapter_url_num(url):
    """从章节 URL 提取末段数字 ID；无匹配返回 0。
    统一容忍两种 URL 契约：/{a}/{b}/{n}.html（jhsssd/kanshuw）与
    /book/{id}/{cid}/（tadu，尾斜杠先剥掉）。
    注意：对"裸数字结尾"（…/123，无 .html）的 URL 也会取号——
    原 jhsssd/kanshuw 实现返回 0；但两站目录 URL 均由 \\d+\\.html 正则
    采集，该形态不可达，无行为差异。"""
    m = re.search(r"/(\d+)(?:\.html)?$", (url or "").rstrip("/"))
    return int(m.group(1)) if m else 0

# ── 分页取数：撞上限必须报错，绝不静默截断（2026-09-18 实测缺陷）──────────
# 五个适配器的正文分页循环都写了硬上限（10/10/20/6/10 页）并且**撞上限就返回已拼好的半章**。
# 实测：精华书阁《剑来》第301章需要 ≥26 页，适配器上限 20 → 后 6 页被静默丢弃，
# 章节结尾断在词中（"…若是前"），下载与在线阅读都永久读到半章，而且缓存后不会自愈。
# 规则引擎那条路早有 P2-5 的规矩"分页中途失败 = 整章失败，不静默截断"，
# 适配器这条路当时漏了。现在统一走这里。
try:                                     # 单一事实来源：上限都定义在 engine/config.py
    from engine.config import CONTENT_MAX_PAGES as CONTENT_PAGE_CAP
    from engine.config import TOC_PAGE_CAP
except Exception:                        # 兜底（隔离导入场景）：
    CONTENT_PAGE_CAP = 40
    TOC_PAGE_CAP = 120


def page_cap_error(adapter_name, pages, url=""):
    """撞到分页上限时抛出——让上层按"整章失败"重试/如实标注，而不是把半章当成功"""
    return RuntimeError(
        "%s 正文分页达到上限 %d 页仍未取完（为避免静默截断按整章失败处理）: %s"
        % (adapter_name, pages, str(url)[:120]))

def chapter_num_of(url):
    """URL **末段开头的数字串** = 章号（分页后缀不算）：

        /15281/7968040.html      → 7968040
        /15281/7968040_13.html   → 7968040   ← 页号 13 不能被当成章号
        /html/12/3456_2/         → 3456

    为什么不能复用 chapter_url_num：那个取的是"末尾数字"，对 7968040_13.html
    会取到 **13**（页号）——用它做跨章判断会把同一章的不同页误判成不同章
    （本仓测试当场抓到）。取不到时返回 0（调用方按同章保守处理）。
    """
    seg = (url or "").rstrip("/").rsplit("/", 1)[-1]
    seg = re.sub(r"\.html?$", "", seg)
    m = re.match(r"(\d+)", seg)
    return int(m.group(1)) if m else 0


def same_chapter_page(base_url, candidate_url):
    """`candidate_url` 是否仍是 `base_url` 这一章的**分页**（而不是下一章）。

    为什么必须有它（2026-09-18 实测）：精华书阁的 pb_next 在**本章最后一页**
    会指向**下一章**（7968040_13.html → 7968041.html）。适配器若无保护地一路跟随，
    就会把后续章节的内容拼进本章——每章缓存含下一章开头、导出 txt 章节内容重复
    （imaxreader 早就在注释里记过这个坑并加了保护，jhsssd 当初漏了）。

    判据：两边的**章号**（chapter_num_of：末段开头的数字串）必须相等；
    任一侧取不到章号时**按同章处理**（保守：宁可不拦，也不误伤没有章号的站）。
    """
    a = chapter_num_of(base_url)
    b = chapter_num_of(candidate_url)
    if not a or not b:
        return True
    return a == b

def toc_cap_error(adapter_name, pages, url=""):
    """目录分页撞上限 → 抛出。

    目录被截断比正文更隐蔽：用户在目录里看不到后面的章节，只会以为"这书就到这"。
    实测（2026-09-18）：精华书阁目录 66 页 / 1309 章，适配器上限 30 页 → 只给 605 章。
    """
    return RuntimeError(
        "%s 目录分页达到上限 %d 页仍未取完（为避免静默漏章按失败处理）: %s"
        % (adapter_name, pages, str(url)[:120]))
