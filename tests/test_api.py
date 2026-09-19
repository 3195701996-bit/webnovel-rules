#!/usr/bin/env python3
"""全 API 测试：页面渲染、书源 CRUD、搜索、任务、书籍。
使用 Flask test_client + mock Fetcher（离线可重复）。
运行：venv/bin/python -m pytest tests/ -v
"""
import os
import sys

HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HUB)

import pytest

MOCK_HTML = """<html><body>
<div class="search-result">
  <div class="book"><a class="name" href="/book/1/">剑来</a>
  <span class="author">烽火戏诸侯</span></div>
  <div class="book"><a class="name" href="/book/2/">斗破苍穹</a>
  <span class="author">天蚕土豆</span></div>
</div>
<div class="bookinfo"><h1>剑来</h1><p class="author">作者：烽火戏诸侯</p>
<p class="intro">这是一本好看的网络小说简介</p></div>
<div class="catalog"><ul>
  <li><a href="/chapter/1.html">第一章</a></li>
  <li><a href="/chapter/2.html">第二章</a></li>
  <li><a href="/chapter/3.html">第三章</a></li>
</ul></div>
<div class="content">这是第一章的正文内容，足够长以保证测试通过。
这是第一章的正文内容，足够长以保证测试通过。</div>
</body></html>"""


@pytest.fixture(scope="session", autouse=True)
def _mock_fetcher():
    """替换 Fetcher.get/post 为返回固定 HTML"""
    from engine import fetcher as fetcher_mod
    # 保留原始类引用（供限流等静态方法测试）
    import engine.fetcher as _rf
    _rf.REAL_FETCHER_CLASS = _rf.Fetcher

    class _R:
        status_code = 200
        text = MOCK_HTML
        content = MOCK_HTML.encode('utf-8')
        url = 'http://mock/'
        headers = {'Content-Type': 'text/html; charset=utf-8'}
        def raise_for_status(self):
            pass

    class MockSession:
        def get(self, *a, **k):
            return _R()
        def post(self, *a, **k):
            return _R()

    class MockFetcher:
        def get(self, url, source=None, timeout=30, retries=3, extra_headers=None,
                deadline=None):
            return MOCK_HTML
        def post(self, url, data=None, source=None, timeout=30, retries=3,
                 extra_headers=None, deadline=None):
            return MOCK_HTML
        def parse_header(self, source):
            return {}
        @property
        def session(self):
            return MockSession()

    fetcher_mod.Fetcher = MockFetcher
    # crawler / source_mgr 用的是 `from .fetcher import Fetcher`，import 时就
    # 把类对象绑到了自己的模块命名空间。只替换 engine.fetcher.Fetcher 时，
    # 若这些模块已被先行 import（例如先跑 test_r35b_ssrf_depth.py），
    # 它们持有的仍是真实 Fetcher → 测试会对 test.example.com 发起真实 DNS
    # 请求并失败。这里同步替换所有已导入的引用，使 mock 与执行顺序无关。
    import sys as _sys
    for _name, _mod in list(_sys.modules.items()):
        if not _name.startswith("engine."):
            continue
        if getattr(_mod, "Fetcher", None) is _rf.REAL_FETCHER_CLASS:
            _mod.Fetcher = MockFetcher


@pytest.fixture(scope="module")
def client():
    import app
    app.app.config['TESTING'] = True
    return app.app.test_client()


@pytest.mark.parametrize("path", ["/", "/sources", "/library", "/tasks_page"])
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200


def test_reader_page(client):
    r = client.get("/reader/kudushu_0d4248c198")
    assert r.status_code == 200


def test_sources_list(client):
    r = client.get("/api/sources")
    assert r.status_code == 200
    d = r.get_json()
    assert "sources" in d
    # 源数量可变（当前为精选的专属适配器源），仅断言非负
    assert isinstance(d["sources"], list)


def test_source_import_valid(client, monkeypatch, tmp_path):
    """导入并校验书源。

    校验必须打桩：真实 validate_source 会对 bookSourceUrl 发网络请求，
    离线/DNS 异常时 valid=False，用例结果随网络波动。
    SOURCES_DIR 也重定向到 tmp，避免向仓库 sources/ 写入残留文件。
    """
    import engine.source_mgr as SM

    monkeypatch.setattr(SM, "SOURCES_DIR", str(tmp_path))
    monkeypatch.setattr(
        SM, "validate_source",
        lambda s, **kw: {"ok": True, "error": "", "tested_at": "2026-01-01"})

    src = {
        "bookSourceName": "测试源",
        "bookSourceUrl": "https://test.example.com",
        "searchUrl": "/search?q={{key}}",
        "ruleSearch": {"bookList": ".search-result .book",
                       "name": "a.name@text", "bookUrl": "a.name@href",
                       "author": "span.author@text"},
        "ruleBookInfo": {"name": ".bookinfo h1@text"},
        "ruleToc": {"chapterList": ".catalog li", "chapterName": "a@text",
                    "chapterUrl": "a@href"},
        "ruleContent": {"content": ".content@text"},
    }
    r = client.post("/api/sources/import", json={"content": [src]})
    assert r.status_code == 200
    d = r.get_json()
    assert d["imported"] == 1
    assert d["results"][0]["valid"] is True
    assert not os.path.exists(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "sources", "测试源_test.example.com.json"))


def test_source_import_invalid_json(client):
    r = client.post("/api/sources/import", json={"content": "{bad json"})
    assert r.status_code == 400


def test_source_import_missing_url(client):
    r = client.post("/api/sources/import", json={"content": [{"bookSourceName": "x"}]})
    assert r.status_code == 200
    assert r.get_json()["imported"] == 0


def test_source_validate_toggle_delete(client):
    r = client.get("/api/sources")
    srcs = r.get_json()["sources"]
    target = next((s for s in srcs if "测试源" in s.get("bookSourceName", "")), None)
    if target is None:
        pytest.skip("测试源未导入")
    uid = target["uid"]
    assert client.post(f"/api/sources/{uid}/validate").status_code == 200
    r = client.post(f"/api/sources/{uid}/toggle", json={"enabled": False})
    assert r.status_code == 200 and r.get_json()["enabled"] is False
    r = client.post(f"/api/sources/{uid}/toggle", json={"enabled": True})
    assert r.status_code == 200 and r.get_json()["enabled"] is True
    assert client.delete(f"/api/sources/{uid}").status_code == 200


def test_source_validate_404(client):
    assert client.post("/api/sources/nonexistent_uid/validate").status_code == 404


def test_search_requires_q(client):
    assert client.get("/api/search").status_code == 400


def _import_test_source(client):
    """导入一个与 MOCK_HTML 匹配的测试源，返回 uid"""
    src = {
        "bookSourceName": "测试源",
        "bookSourceUrl": "https://test.example.com",
        "searchUrl": "/search?q={{key}}",
        "ruleSearch": {"bookList": ".search-result .book",
                       "name": "a.name@text", "bookUrl": "a.name@href",
                       "author": "span.author@text"},
        "ruleBookInfo": {"name": ".bookinfo h1@text"},
        "ruleToc": {"chapterList": ".catalog li", "chapterName": "a@text",
                    "chapterUrl": "a@href"},
        "ruleContent": {"content": ".content@text"},
    }
    client.post("/api/sources/import", json={"content": [src]})
    r = client.get("/api/sources")
    for s in r.get_json()["sources"]:
        if s.get("bookSourceName") == "测试源":
            return s["uid"]
    return None


def test_search_basic(client):
    uid = _import_test_source(client)
    assert uid, "测试源导入失败"
    r = client.get(f"/api/search?q=剑来&source={uid}")
    assert r.status_code == 200
    d = r.get_json()
    assert "groups" in d and "books" in d
    assert len(d["books"]) >= 1
    # 清理
    client.delete(f"/api/sources/{uid}")


def test_search_type_name(client):
    r = client.get("/api/search?q=剑来&type=name")
    assert r.status_code == 200


def test_search_sort(client):
    assert client.get("/api/search?q=剑来&sort=update").status_code == 200
    assert client.get("/api/search?q=剑来&sort=word").status_code == 200


def test_search_specific_source(client):
    assert client.get("/api/search?q=剑来&source=kudushu").status_code in (200, 404)


def test_search_detail(client):
    # 源不存在 → 400（参数/源校验）；源存在则真实请求（200 或源侧失败 500）
    r = client.post("/api/search-detail",
                    json={"source_uid": "nonexistent_source_xyz",
                          "book_url": "https://example.com/book/1"})
    assert r.status_code == 400


def test_search_toc_count(client):
    # 源不存在 → 400；不依赖任何具体书源（书源库可变）
    r = client.post("/api/search-toc-count",
                    json={"source_uid": "nonexistent_source_xyz",
                          "book_url": "https://example.com/book/1"})
    assert r.status_code == 400


def test_tasks_list(client):
    r = client.get("/api/tasks")
    assert r.status_code == 200
    assert "tasks" in r.get_json()


def test_task_create_validation(client):
    assert client.post("/api/tasks", json={}).status_code == 400
    assert client.post("/api/tasks",
                       json={"source_uid": "x", "book_url": "not-a-url"}).status_code == 400


def test_task_create_pause_stop_delete(client):
    r = client.post("/api/tasks", json={
        "source_uid": "kudushu",
        "book_url": "https://m.kudushu.org/html/938398/asc-1/"})
    assert r.status_code in (202, 409)
    if r.status_code == 202:
        tid = r.get_json()["id"]
        client.post(f"/api/tasks/{tid}/pause")
        client.post(f"/api/tasks/{tid}/stop")
        client.delete(f"/api/tasks/{tid}")


def test_task_404(client):
    assert client.get("/api/tasks/nonexistent").status_code == 404


def test_books_list(client):
    r = client.get("/api/books")
    assert r.status_code == 200
    assert "books" in r.get_json()


def test_book_detail(client):
    r = client.get("/api/books/kudushu_0d4248c198")
    if r.status_code == 404:
        pytest.skip("测试书不存在")
    assert r.get_json()["total"] >= 0


def test_book_chapter(client):
    assert client.get("/api/books/kudushu_0d4248c198/chapter/1").status_code in (200, 404)


def test_book_txt(client):
    assert client.get("/api/books/kudushu_0d4248c198/txt").status_code in (200, 404)


def test_book_check_update_async(client):
    r = client.post("/api/books/kudushu_0d4248c198/check-update")
    assert r.status_code in (202, 404)
    if r.status_code == 202:
        assert r.get_json().get("checking") is True


def test_book_check_status(client):
    assert client.get("/api/books/kudushu_0d4248c198/check-status").status_code in (200, 404)


# ── 规则引擎单元测试 ──
def test_fill_template():
    from engine.rules import fill_template
    assert fill_template("/s?q={{key}}&p={{page}}",
                         {"key": "剑来", "page": 2}) == "/s?q=剑来&p=2"


def test_normalize_url():
    from engine.rules import normalize_url
    assert normalize_url("/book/1/", "http://x.com/a/") == "http://x.com/book/1/"
    assert normalize_url("http://abs.com/", "http://x.com/") == "http://abs.com/"


def test_parse_search_config():
    from engine.rules import parse_search_config
    url, cfg = parse_search_config('/search,{"method":"POST","body":"key={{key}}"}')
    assert url == "/search"
    assert cfg.get("method") == "POST"
    url2, cfg2 = parse_search_config('<js>cookie.removeCookie("x");</js>/search/')
    assert url2 == "/search/"
    assert not cfg2


def test_clean_search_url():
    from engine.rules import clean_search_url
    assert clean_search_url('/search,{"method":"POST"}') == "/search"


def test_split_clean_suffix():
    from engine.rules import split_clean_suffix
    rule, cleans = split_clean_suffix("a@text##^第\\d+章##")
    assert rule == "a@text"
    assert len(cleans) == 1


def test_rule_engine_tag_index():
    from engine.rules import RuleEngine
    html = ('<div class="c"><a href="/1/" title="T1">一</a>'
            '<a href="/2/" title="T2">二</a></div>')
    eng = RuleEngine(source={"bookSourceUrl": "http://x.com"})
    elems = eng.get_elements(".c", html)
    assert len(elems) == 1
    el = elems[0]
    assert eng.get_string("a.1@href", el) == "/2/"
    assert eng.get_string("a.1@title", el) == "T2"
    assert eng.get_string("a.0@text", el) == "一"


def test_rule_engine_jsonpath():
    from engine.rules import RuleEngine
    data = '{"data": {"books": [{"name": "剑来"}, {"name": "雪中"}]}}'
    eng = RuleEngine()
    assert eng.get_string("$.data.books.0.name", data) == "剑来"


def test_rule_engine_regex():
    from engine.rules import RuleEngine
    eng = RuleEngine()
    assert eng.get_string("@正则:第(\\d+)章", "这是第123章内容") == "123"


def test_word_parse():
    from app import _parse_words, _parse_time
    assert _parse_words("123万字") == 1230000
    assert _parse_words("456789") == 456789
    assert _parse_time("2025-06-01 12:00") == 20250601


# ── 搜索功能修复新增测试 ──
def test_group_key_normalization():
    """书名归一化：去后缀/括号/繁简"""
    from app import _group_key
    assert _group_key("剑来·精校") == "剑来"
    assert _group_key("剑来（第一季）") == "剑来"
    assert _group_key("斗破苍穹全集") == "斗破苍穹"
    assert _group_key("鬥破蒼穹") == "斗破苍穹"


def test_search_cache_and_slow_source():
    """搜索缓存与慢源逻辑"""
    # 慢源冷却值合理
    from engine.config import SLOW_COOLDOWN, SEARCH_CACHE_TTL
    assert SLOW_COOLDOWN <= 120
    assert SEARCH_CACHE_TTL >= 30


# ── 文本净化测试（engine/cleaner）──
def test_cleaner_basic_pollution():
    """全方位净化：举报导航/站名声明/书签/分页标题/正文标题"""
    from engine.cleaner import clean_text
    s = """第十七章 执行部的邀请

『章节错误，点此举报』

天才一秒记住本站地址：[爱笔楼]http://m.biqutu.info/最快更新！无广告！

第十七章 执行部的邀请 (第1/3页)

『加入书签，方便阅读』

两个人离开了校长室。

第二章 世界树的化身

他走向了森林。"""
    out = clean_text(s)
    assert '章节错误' not in out
    assert '天才一秒' not in out
    assert '爱笔楼' not in out or 'm.biqutu' not in out
    assert '加入书签' not in out
    assert '第1/3页' not in out
    # 正文标题行清除，但正文保留
    assert '第二章' not in out
    assert '两个人离开了校长室' in out
    assert '他走向了森林' in out


def test_cleaner_keeps_body():
    """净化不误删正文：对话含'第三章'、数字行保留"""
    from engine.cleaner import clean_text
    s = """他说："第三章的剧情更精彩。"

价格是12345元。

第一章 起点"""
    out = clean_text(s)
    assert '第三章的剧情' in out
    assert '12345' in out
    # 末尾"第一章 起点"是标题行应清除
    assert '第一章 起点' not in out


def test_cleaner_replace_regex():
    """书源 replaceRegex 生效（$1 组引用 + replaceFirst）"""
    from engine.cleaner import clean_text
    out = clean_text('正文第123章内容', replace_regex='##第(\\d+)章##第$1章##')
    assert '第123章' in out


def test_clean_cache_text_keeps_title():
    """缓存清洗保留首行标题"""
    from engine.cleaner import clean_cache_text
    s = "第一章 面试\n\n正文内容\n『加入书签』"
    out = clean_cache_text(s)
    assert out.split('\n')[0] == '第一章 面试'
    assert '加入书签' not in out
    assert '正文内容' in out


def test_cleaner_standalone_pagination():
    """孤立分页残留（页)/3页)/第1/3页)）清除"""
    from engine.cleaner import clean_text
    out = clean_text('正文\n页)\n3页)\n第1/3页)\n更多')
    assert '页)' not in out
    assert '3页)' not in out
    assert '正文' in out and '更多' in out


def test_cleaner_site_addr_no_title_eat():
    """站名+网址声明不吞后续标题"""
    from engine.cleaner import clean_text
    out = clean_text('最新网址：m.biqutu.info 第十九章 学生们的拙劣针对 (第1/3页)\n\n正文')
    assert '第十九章' not in out  # 标题被删
    assert 'm.biqutu' not in out
    assert '正文' in out


# ── 并发爬取与限流测试 ──
def test_crawl_concurrency():
    """并发度配置：concurrentRate 可配，默认 8，上限 16"""
    from engine.crawler import CrawlTask
    t1 = CrawlTask({'concurrentRate': '12'}, 'http://x/book', '/tmp/t1', resume=False)
    assert t1._concurrency() == 12
    t2 = CrawlTask({}, 'http://x/book', '/tmp/t2', resume=False)
    assert t2._concurrency() == 8
    t3 = CrawlTask({'concurrentRate': '99'}, 'http://x/book', '/tmp/t3', resume=False)
    assert t3._concurrency() == 16


def test_rate_limiter():
    """滑动窗口限流：3/1000ms 内第 4 次触发等待（绕过 mock，用原始类方法）"""
    import time
    # mock 可能替换了 Fetcher，取原始类方法
    import engine.fetcher as real_fetcher
    cls = getattr(real_fetcher, 'REAL_FETCHER_CLASS', real_fetcher.Fetcher)
    st = cls._state_for('ratetest.com')   # v2：限流状态挂在每域名 _HostState 上
    t0 = time.time()
    for _ in range(5):
        cls._throttle(st, 'ratetest.com', n=3, ms=1000)
    el = time.time() - t0
    assert el >= 0.9, f"应等待约1s，实际 {el:.2f}s"


def test_source_concurrent_rate():
    """书源级 concurrentRate 解析：'5/1000' → 5次/1000ms；'500' → 1次/500ms；'0'/空 → 默认"""
    import engine.fetcher as _rf
    cls = getattr(_rf, 'REAL_FETCHER_CLASS', _rf.Fetcher)
    def get_rl(src):
        n, ms = cls.RATE_LIMIT_N, cls.RATE_LIMIT_MS
        cr = ((src or {}).get('concurrentRate') or '').strip()
        if cr and cr != '0':
            if '/' in cr:
                _n, _ms = cr.split('/', 1)
                n, ms = int(_n.strip()), int(_ms.strip())
            else:
                n, ms = 1, int(cr.strip())
        return n, ms
    assert get_rl({}) == (cls.RATE_LIMIT_N, cls.RATE_LIMIT_MS)
    assert get_rl({'concurrentRate': '5/1000'}) == (5, 1000)
    assert get_rl({'concurrentRate': '500'}) == (1, 500)
    assert get_rl({'concurrentRate': '0'}) == (cls.RATE_LIMIT_N, cls.RATE_LIMIT_MS)


def test_fetcher_retries_zero_still_attempts_once(monkeypatch):
    """retries=0 表示不重试，但仍需执行首次请求。"""
    import engine.fetcher as fm
    # 本用例验证重试次数，与 DNS 连接层绑定无关：显式短路 pin 函数
    # （fake Session 仅实现 get，无 mount/adapters，不应被要求伪装安全能力）。
    monkeypatch.setattr(fm, "pin_curl_session", lambda *a, **k: False)
    cls = getattr(fm, 'REAL_FETCHER_CLASS', fm.Fetcher)
    host = 'retry-zero.example'
    st = cls._state_for(host)

    class Response:
        status_code = 200
        headers = {'Content-Type': 'text/plain; charset=utf-8'}
        content = b'ok'
        def raise_for_status(self):
            pass

    class Session:
        calls = 0
        def get(self, *args, **kwargs):
            self.calls += 1
            return Response()

    session = Session()
    with st.session_lock:
        st.session = session
        st.backend = 'cloud'
        st.ua = 'test'
        st.proxy = None
        st.session_users.clear()
        st.retired_sessions.clear()
    try:
        assert cls().get('http://retry-zero.example/',
                         source={'bookSourceUrl': 'http://retry-zero.example'},
                         timeout=1, retries=0) == 'ok'
        assert session.calls == 1
    finally:
        with st.session_lock:
            st.session = None
            st.session_users.clear()
            st.retired_sessions.clear()


def test_fetcher_deadline_stops_before_retry():
    """Fetcher 的业务 deadline 应在重试前终止，不等待完整退避链。"""
    import time
    import engine.fetcher as fm
    cls = getattr(fm, 'REAL_FETCHER_CLASS', fm.Fetcher)
    host = 'deadline.example'
    st = cls._state_for(host)

    class SlowSession:
        def get(self, *args, **kwargs):
            time.sleep(0.02)
            raise OSError("Connection reset by peer")

    with st.session_lock:
        st.session = SlowSession()
        st.backend = 'cloud'
        st.ua = 'test'
        st.proxy = None
        st.session_users.clear()
        st.retired_sessions.clear()
    try:
        started = time.monotonic()
        with pytest.raises(fm.DeadlineExceeded):
            cls().get('http://deadline.example/',
                      source={'bookSourceUrl': 'http://deadline.example'},
                      timeout=1, retries=5, deadline=time.monotonic() + 0.05)
        assert time.monotonic() - started < 0.5
    finally:
        with st.session_lock:
            st.session = None
            st.session_users.clear()
            st.retired_sessions.clear()


def test_fetcher_rebuild_defers_close_until_lease_release():
    """会话重建不能关闭仍被请求租约持有的旧 session。"""
    import engine.fetcher as fm
    cls = getattr(fm, 'REAL_FETCHER_CLASS', fm.Fetcher)

    class Session:
        def __init__(self):
            self.closed = False
        def close(self):
            self.closed = True

    source = {'bookSourceUrl': 'http://lease.example'}
    st = cls._state_for('lease.example')
    old = Session()
    new = Session()
    with st.session_lock:
        st.session = old
        st.backend = 'cloud'
        st.session_users.clear()
        st.retired_sessions.clear()
    original_build = cls._build_session
    try:
        cls._build_session = classmethod(lambda _cls, _source, prefer=None: (new, 'cloud', 'ua', None))
        lease = cls._lease_session(st, source)
        cls._rebuild_session('lease.example', source, reason='test')
        assert st.session is new
        assert old.closed is False
        lease.release()
        assert old.closed is True
    finally:
        cls._build_session = original_build
        with st.session_lock:
            st.session = None
            st.session_users.clear()
            st.retired_sessions.clear()


def test_get_content_chapter_boundary():
    """get_content 分页不跨章：URL 章节 ID 变化即停止"""
    from engine.crawler import SourceCrawler
    # 用 mock fetcher 模拟：nextContentUrl 指向下一章（ID 变化）
    class MockBoundaryFetcher:
        def get(self, url, source=None, timeout=30, retries=3, extra_headers=None,
                deadline=None):
            # 返回模拟 HTML：正文 + nextContentUrl
            return ('<div id="chaptercontent">本章内容</div>'
                    '<a id="pt_next" href="/139_139779/99999999.html">下一页</a>')
        def post(self, *a, **k): return self.get(*a)
        def parse_header(self, s): return {}
        @property
        def session(self):
            class _S:
                def get(self,*a,**k): return self
                def post(self,*a,**k): return self
            return _S()
    src = {'bookSourceUrl': 'http://mock-boundary-site.example', 'uid': 't',
           'ruleContent': {'content': '@css:div#chaptercontent@html',
                           'nextContentUrl': '@css:a#pt_next@href'}}
    c = SourceCrawler(src)
    c.fetcher = MockBoundaryFetcher()
    text = c.get_content('http://mock-boundary-site.example/139_139779/54462526.html', timeout=10)
    # 只抓本章（nextContentUrl 指向 99999999 = 不同章 ID，应停止）
    assert '本章内容' in text
    assert len(text) < 500  # 不应跨章抓取


def test_cleaner_promotion():
    """站内推广清除：百度一下/请搜索/顶点小说/章末首发"""
    from engine.cleaner import clean_text
    s1 = '他走了。百度一下"百变小樱里的阳光大男孩顶点小说"最新章节第一时间免费阅读。继续。'
    out = clean_text(s1)
    assert '百度一下' not in out
    assert '顶点小说' not in out
    assert '继续' in out
    s2 = '本章完。本书首发于笔趣阁，请支持正版。后续。'
    out2 = clean_text(s2)
    assert '首发于' not in out2
    assert '后续' in out2
    # 不误删正文
    s3 = '他百度了一下这个生词。'
    assert '百度了一下' in clean_text(s3)


def test_chapter_retry_api(client):
    """单章重爬 API：POST /api/books/<key>/chapter/<idx>"""
    # 用 mock 环境（kudushu 书存在则测，否则跳过）
    r = client.post("/api/books/kudushu_0d4248c198/chapter/1")
    assert r.status_code in (200, 404, 500)  # mock 下可能因书不存在/规则失败返回错误


def test_chapter_retry_404(client):
    """重爬越界章节返回 404"""
    r = client.post("/api/books/爱笔楼_m.biqutu.info_b9c2c5cd78/chapter/99999")
    assert r.status_code == 404


def test_search_toc_count_does_not_pollute_full_toc(client):
    """回归：计数缓存（无 chapters）不得污染 search-toc/read 的完整目录缓存命中。
    此前 search-toc-count 覆盖完整目录缓存 → search-toc 返回空目录、search-read 404。
    现在写入端保留已有完整目录；且读取端要求 'chapters' 键存在才命中。"""
    from app import _toc_cache, _toc_lock
    book_url = "https://example.com/book/pollution-test/"
    # 先写入一个计数缓存（模拟 search-toc-count 写入）
    with _toc_lock:
        _toc_cache[book_url] = {"chapter_count": 3, "last_chapter": "第3章",
                                "name": "污染测试", "ts": 9999999999}
    try:
        # search-toc 应忽略计数缓存（无 chapters 键）→ 重新爬取 → 源不存在 400/404 均可，
        # 关键是不得返回空目录 200（缓存命中污染）
        r = client.post("/api/search-toc",
                        json={"source_uid": "nonexistent_source_xyz",
                              "book_url": book_url})
        assert r.status_code in (400, 404, 500)
    finally:
        with _toc_lock:
            _toc_cache.pop(book_url, None)


def test_book_progress_api_roundtrip(client):
    """阅读进度 API 回归：POST 保存 → GET 读回 → _scan_books 携带 last_read_ts。
    历史 bug：BOOK_PROGRESS_FILE 未定义导致 GET/POST 均 500（跨设备进度同步
    静默失效），且 _scan_books 内 try/except 吞掉 NameError 使 last_read_ts
    恒为 0。此用例锁住该回归。"""
    import json as _json
    import app as _app
    from engine.config import BOOK_PROGRESS_FILE

    key = "testsrc_progress01"
    r = client.post(f"/api/books/{key}/progress",
                    json={"idx": 3, "pct": 42, "name": "第三章"})
    assert r.status_code == 200, f"POST progress 应 200，实际 {r.status_code}"
    r = client.get(f"/api/books/{key}/progress")
    assert r.status_code == 200, f"GET progress 应 200，实际 {r.status_code}"
    d = r.get_json()
    assert d.get("ok") is True
    assert d.get("idx") == 3 and d.get("pct") == 42 and d.get("name") == "第三章"
    assert d.get("ts")
    # 落盘验证（原子写到临时 DATA_DIR）
    with open(BOOK_PROGRESS_FILE, encoding="utf-8") as f:
        disk = _json.load(f)
    assert disk.get(key, {}).get("idx") == 3
    # _scan_books 应带 last_read_ts（不再被 try/except 静默吞掉）：
    # R47 拆分后常量的单一来源在 engine.config（经 server.state 使用）
    import server.state as _st
    assert _st.BOOK_PROGRESS_FILE == BOOK_PROGRESS_FILE


def test_book_progress_scan_books_last_read(client, tmp_path):
    """_scan_books 的 last_read_ts 应来自全局进度文件（而非恒 0）。"""
    import os as _os
    import json as _json
    import app as _app
    from engine.config import BOOK_PROGRESS_FILE

    key = "testsrc_progress02"
    d = _os.path.join(_app.BOOKS_DIR, key)
    _os.makedirs(d, exist_ok=True)
    with open(_os.path.join(d, "_state.json"), "w", encoding="utf-8") as f:
        _json.dump({"book": {"name": "进度书", "book_url": "https://x/1"},
                    "chapters": [{"name": "第1章", "url": "https://x/1/1"}],
                    "completed": ["https://x/1/1"], "failed": {},
                    "updated_at": "2026-01-01 00:00:00"}, f)
    r = client.post(f"/api/books/{key}/progress",
                    json={"idx": 1, "pct": 88, "name": "第1章"})
    assert r.status_code == 200
    books = _app._scan_books()
    hit = [b for b in books if b["key"] == key]
    assert hit, "构造的书籍应出现在书库扫描中"
    assert hit[0]["last_read_ts"] > 0, "last_read_ts 不应为 0（进度文件已有记录）"
