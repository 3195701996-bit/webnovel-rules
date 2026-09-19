"""搜索服务：慢源管理 + 单源搜索 + 详情增强（供普通/流式搜索共用）"""
import time
import threading
from .crawler import SourceCrawler
from .config import (SEARCH_SOURCE_TIMEOUT, ENHANCE_MAX_WORKERS,
                     ENHANCE_WAIT_SECONDS, SLOW_COOLDOWN)


class SlowSourceTracker:
    """慢源记忆（R14 Kimi 加固）：失败计数退避 + 成功清除 + 延迟平滑"""

    MAX_FAIL_MULTIPLIER = 5   # 最大失败乘数
    LATENCY_ALPHA = 0.3       # 延迟滑动平均：新值权重

    def __init__(self):
        self._slow = {}       # uid -> {ts, reason, fail_count}
        self._latency = {}    # uid -> 平滑耗时秒
        self._lock = threading.Lock()

    def mark_slow(self, uid, reason):
        with self._lock:
            rec = self._slow.get(uid)
            if rec is None:
                rec = {"ts": time.time(), "reason": reason, "fail_count": 1}
            else:
                rec["ts"] = time.time()
                rec["reason"] = reason
                rec["fail_count"] = min(rec.get("fail_count", 1) + 1,
                                        self.MAX_FAIL_MULTIPLIER)
            self._slow[uid] = rec

    def mark_ok(self, uid):
        """成功调用：清除慢源记录（重置退避）"""
        with self._lock:
            self._slow.pop(uid, None)

    def record_latency(self, uid, seconds):
        with self._lock:
            old = self._latency.get(uid)
            self._latency[uid] = (seconds if old is None
                                  else old * (1 - self.LATENCY_ALPHA) + seconds * self.LATENCY_ALPHA)

    def is_skippable(self, uid):
        with self._lock:
            rec = self._slow.get(uid)
            if rec is None:
                return False
            ts = rec["ts"]
            fail_count = rec.get("fail_count", 1)
        cooldown = SLOW_COOLDOWN * fail_count  # 指数退避
        return time.time() - ts < cooldown

    def latency_of(self, uid, default=5.0):
        with self._lock:
            return self._latency.get(uid, default)

    def snapshot(self):
        with self._lock:
            return dict(self._slow), dict(self._latency)


class SearchWorker:
    """单个书源搜索执行器（慢源记录/延迟画像 + 每源业务 deadline 熔断）

    P1-3：内层限时线程池已删除——search_one 在调用线程（外层池 worker）
    内同步执行搜索，不再 submit 到第二层池（旧实现一次搜索峰值 70+ 线程）。
    单源时限改由业务 deadline 保证：适配器搜索包在 _deadline_scope 内，
    到点后下一次网络操作立即抛 DeadlineExceeded（fetcher 重试退避链
    随之中止），被外层 wait 放弃的请求不再在后台烧完整条重试链。
    """

    def __init__(self, tracker):
        self.tracker = tracker

    def _raw_search(self, source, keyword):
        c = SourceCrawler(source)
        if c.adapter is not None:
            # 单源业务 deadline（线程本地，异常安全）：贯穿 适配器._get →
            # fetcher._request 的 deadline 参数，到点熔断后续网络操作。
            # quanben/quanben_io 的代理重试循环同样自检 _deadline 提前退出。
            with c.adapter._deadline_scope(SEARCH_SOURCE_TIMEOUT):
                return c.search(keyword)
        # 规则引擎兜底路径（crawler.search 自带 timeout=6/retries=0 快速失败，
        # 且当前无启用源走此路径），不在此处接线 deadline（需改 crawler）
        return c.search(keyword)

    def search_one(self, source, keyword, tag="search"):
        """搜索一个书源，返回书籍列表；失败/空结果/超时记慢源。
        在调用线程内同步执行；耗时达到 SEARCH_SOURCE_TIMEOUT 视为超时。"""
        uid = source.get("uid", "")
        t0 = time.time()
        try:
            books = self._raw_search(source, keyword)
        except Exception as e:
            print(f"[{tag}] {source.get('bookSourceName')}: {type(e).__name__}: {e}", flush=True)
            # 2026-09-15（真断网）：本机没有路由时（飞行模式/无信号），只记
            # "URLError" 这类类型名既看不懂、又会把本机没网说成源站问题。
            # 如实写成"本机网络不可达"（engine/neterr.py）。
            from . import neterr as _ne
            _kind = _ne.classify(e)
            self.tracker.mark_slow(uid,
                                   _ne.reason_for(_kind) if _kind
                                   else type(e).__name__)
            return []
        el = time.time() - t0
        if el >= SEARCH_SOURCE_TIMEOUT:
            print(f"[{tag}] {source.get('bookSourceName','?')} "
                  f"超时({SEARCH_SOURCE_TIMEOUT}s) 按慢源处理", flush=True)
            if books:
                # 源只是慢、非死：清除慢源标记（对齐旧版 watch 线程
                # "后台最终成功则清除"语义），但本次结果仍按超时丢弃
                self.tracker.mark_ok(uid)
            else:
                self.tracker.mark_slow(uid, f"超时{SEARCH_SOURCE_TIMEOUT}s")
            return []
        self.tracker.record_latency(uid, el)
        if not books:
            self.tracker.mark_slow(uid, f"空结果 {el:.1f}s")
        else:
            self.tracker.mark_ok(uid)  # R14: 成功清除慢源记录
        return books


def enhance_group(g, toc_cache, toc_ttl, lock=None, srcmap=None):
    """详情增强单组（章节数/最新章节/简介等），toc 缓存兜底。供搜索分组后用。
    srcmap：可选的 uid→source 预载映射（run_enhance 批量调用时避免每组
    find_source 都全量重读 sources 目录）"""
    from .source_mgr import find_source
    import re
    if not g.get('sources'):
        return
    src0 = g['sources'][0]
    # find_source 带 URL/名称回退（get_by_uid 精确匹配常落空 → 增强静默失效）
    _key = src0.get('source_uid', '') or src0.get('book_url', '')
    src = (srcmap or {}).get(_key) or find_source(_key)
    if not src:
        return
    # toc 缓存兜底
    if lock:
        with lock:
            _tc = toc_cache.get(src0.get('book_url', ''))
    else:
        _tc = toc_cache.get(src0.get('book_url', ''))
    if _tc and time.time() - (_tc.get('ts') or 0) < toc_ttl:
        if _tc.get('chapter_count'):
            src0['chapter_count'] = _tc['chapter_count']
        if _tc.get('last_chapter'):
            src0['last_chapter'] = _tc['last_chapter']
        return
    try:
        c = SourceCrawler(src)
        book = c.get_book(src0.get('book_url', ''), fast=True)
        if book.chapter_count:
            src0['chapter_count'] = book.chapter_count
        if book.last_chapter:
            src0['last_chapter'] = book.last_chapter
        if book.update_time:
            src0['update_time'] = book.update_time
        if book.word_count:
            src0['word_count'] = book.word_count
        if not g.get('intro') and book.intro:
            _bi = book.intro
            if re.search(r'\$1|\}\}', _bi) or len(_bi) < 12:
                _bi = ''
            else:
                _bi = re.sub(r'<br\s*/?>|</?[a-z]+>', '', _bi).strip()
            if _bi:
                g['intro'] = _bi
        if not g.get('author') and book.author:
            g['author'] = re.sub(r'^\s*(作者|作\s*者)[:：\s]+', '',
                                 book.author).strip() or book.author
        if not g.get('cover') and book.cover:
            g['cover'] = book.cover
    except Exception:
        pass


def run_enhance(groups, toc_cache, toc_ttl, lock=None):
    """并发详情增强全部组，总时限内完成"""
    from concurrent.futures import ThreadPoolExecutor as TPE, wait
    # 有 toc 缓存的组优先（秒回）
    # 与 enhance_group 一致用 .get：缺 sources 的组不应让整个增强流程 KeyError
    sorted_g = sorted(groups, key=lambda g: (
        0 if ((g.get('sources') or [])
              and toc_cache.get((g.get('sources') or [{}])[0].get('book_url', '')))
        else 1))
    # R47 性能：批量增强时预载 uid→source 映射一次，避免每组 find_source
    # 都全量读 sources 目录（30 组 = 30 次目录扫描 + 全量 JSON 解析）
    from .source_mgr import load_all
    try:
        srcmap = {s.get('uid', ''): s for s in load_all()}
        srcmap.update({(s.get('bookSourceUrl') or '').rstrip('/'): s
                       for s in srcmap.values()})
    except Exception:
        srcmap = None
    ex = TPE(max_workers=ENHANCE_MAX_WORKERS)
    try:
        futs = [ex.submit(enhance_group, g, toc_cache, toc_ttl, lock, srcmap)
                for g in sorted_g]
        done, pending = wait(futs, timeout=ENHANCE_WAIT_SECONDS)
        for f in done:
            try:
                f.result()
            except Exception:
                pass
        for f in pending:
            f.cancel()
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
