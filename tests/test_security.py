# -*- coding: utf-8 -*-
"""安全加固回归（离线：不触网、不写用户数据）

覆盖此前零测试的防护代码——这些是"回归即静默失效"的高危区：
  - source_mgr._safe_uid / _source_path  路径穿越
  - app._url_is_public / _safe_target_url  SSRF
  - app._safe_seg  路径段校验
  - Fetcher._looks_blocked / _on_block  反爬降级
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.source_mgr import SOURCES_DIR, _safe_uid, _source_path  # noqa: E402


# ── 书源 UID 路径穿越 ──────────────────────────────────────────

@pytest.mark.parametrize("uid", [
    "../evil",
    "../../etc/passwd",
    "a/../../b",
    "sub/dir",
    "back\\slash",
    "..",
    ".hidden",
    "",
    "x" * 81,
])
def test_safe_uid_rejects_traversal(uid):
    assert _safe_uid(uid) is None


@pytest.mark.parametrize("uid", ["src_abc123", "书源-1", "a.b_c-d", "全本小说"])
def test_safe_uid_accepts_normal(uid):
    assert _safe_uid(uid) == uid


def test_safe_uid_rejects_non_string():
    assert _safe_uid(None) is None
    assert _safe_uid(123) is None


def test_source_path_raises_on_illegal_uid():
    with pytest.raises(ValueError):
        _source_path("../evil")


def test_source_path_stays_inside_sources_dir():
    p = os.path.realpath(_source_path("legit_uid"))
    assert p.startswith(os.path.realpath(SOURCES_DIR) + os.sep)
    assert p.endswith("legit_uid.json")


# ── SSRF 防护 ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app_mod():
    import app
    return app


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8766/api/cache/clean",
    "http://localhost/admin",
    "http://sub.localhost/x",
    "http://192.168.1.1/",
    "http://10.0.0.5/",
    "http://172.16.0.1/",
    "http://169.254.169.254/latest/meta-data/",   # 云元数据
    "http://[::1]/",
    "http://0.0.0.0/",
    "http://100.64.0.1/",                          # CGNAT
    "http://224.0.0.1/",                           # 组播
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://example.com/x",
    "",
])
def test_url_is_public_blocks_internal(app_mod, url):
    assert app_mod._url_is_public(url) is False


@pytest.mark.parametrize("url", [
    "https://www.example.com/book/1",
    "http://example.org:8080/x",
    "https://api.copy2000.online/api/v3/search",
])
def test_url_is_public_allows_public(app_mod, url):
    assert app_mod._url_is_public(url) is True


def test_safe_target_url_raises_on_internal(app_mod):
    with pytest.raises(ValueError):
        app_mod._safe_target_url("http://127.0.0.1/x", "书籍URL")


def test_safe_target_url_returns_public(app_mod):
    u = "https://example.com/a"
    assert app_mod._safe_target_url(u, "书籍URL") == u


def test_search_detail_rejects_internal_url(app_mod, monkeypatch):
    """/api/search-detail 曾遗漏 SSRF 校验（第二轮评审 P0-3）

    必须注入有效书源：否则 400 来自"参数不完整"分支，
    即使删掉 SSRF 校验用例也会假绿（变异测试已验证过这一点）。
    """
    import server.novel_api as NA  # R47: 实现已拆入 novel_api，打桩随实现走
    monkeypatch.setattr(NA, "get_by_uid",
                        lambda uid: {"bookSourceUrl": "https://example.com",
                                     "bookSourceName": "t"})

    called = {"n": 0}

    class _Boom:
        def __init__(self, *a, **kw):
            called["n"] += 1

        def get_book(self, *a, **kw):        # pragma: no cover
            raise AssertionError("内网 URL 不应到达抓取层")

    monkeypatch.setattr(NA, "SourceCrawler", _Boom)

    app_mod.app.config["TESTING"] = True
    c = app_mod.app.test_client()
    r = c.post("/api/search-detail",
               json={"source_uid": "u1", "book_url": "http://127.0.0.1:8766/x"})
    assert r.status_code == 400
    assert "非法" in r.get_json().get("error", "")
    assert called["n"] == 0          # 校验必须发生在构造爬虫之前


def test_search_detail_allows_public_url(app_mod, monkeypatch):
    """对照组：公网 URL 必须放行到抓取层，避免上面的用例靠"全拒绝"假绿"""
    import server.novel_api as NA  # R47: 实现已拆入 novel_api，打桩随实现走
    monkeypatch.setattr(NA, "get_by_uid",
                        lambda uid: {"bookSourceUrl": "https://example.com",
                                     "bookSourceName": "t"})
    seen = {}

    class _Stub:
        def __init__(self, src):
            pass

        def get_book(self, url, fast=False, **kw):
            seen["url"] = url
            return type("B", (), {"name": "n", "last_chapter": "", "kind": "",
                                  "update_time": "", "word_count": "",
                                  "intro": "", "cover": ""})()

    monkeypatch.setattr(NA, "SourceCrawler", _Stub)
    app_mod.app.config["TESTING"] = True
    c = app_mod.app.test_client()
    r = c.post("/api/search-detail",
               json={"source_uid": "u1", "book_url": "https://example.com/b/1"})
    assert r.status_code == 200
    assert seen["url"] == "https://example.com/b/1"


# ── 路径段校验 ─────────────────────────────────────────────────

@pytest.mark.parametrize("seg", ["..", "a/b", "a\\b", "", "x" * 129])
def test_safe_seg_rejects(app_mod, seg):
    from werkzeug.exceptions import HTTPException
    with pytest.raises(HTTPException):
        app_mod._safe_seg(seg, "测试")


def test_safe_seg_accepts_normal(app_mod):
    assert app_mod._safe_seg("chapter-001", "章节") == "chapter-001"


def test_chapter_id_validated_before_path_join(app_mod, monkeypatch):
    """chapter_id 参与 os.makedirs 路径拼接，必须先过 _safe_seg。

    直接调用视图函数：走 HTTP 时 '..' 会被 URL 路由层归一化，
    测不到应用层校验（会靠 404 假绿）。
    """
    from werkzeug.exceptions import HTTPException

    def _boom(*a, **kw):                      # pragma: no cover
        raise AssertionError("非法 chapter_id 不应到达文件系统")

    monkeypatch.setattr(app_mod.os, "makedirs", _boom)

    with app_mod.app.test_request_context(
            "/api/manga/copymanga/cid/chapter/x/proxy",
            query_string={"u": "https://example.com/a.webp"}):
        with pytest.raises(HTTPException) as ei:
            app_mod.api_manga_proxy("copymanga", "cid", "../../etc")
    assert ei.value.code == 404      # _safe_seg 既有契约：非法路径段 → 404


# ── Fetcher 反爬降级 ───────────────────────────────────────────

@pytest.fixture(scope="module")
def fetcher_cls():
    import engine.fetcher as fm
    return getattr(fm, "REAL_FETCHER_CLASS", fm.Fetcher)


def test_looks_blocked_detects_challenge(fetcher_cls):
    assert fetcher_cls._looks_blocked(
        "<html><body>Checking your browser before accessing</body></html>")


def test_looks_blocked_ignores_long_article(fetcher_cls):
    """正文里出现挑战词不应误判（长度阈值保护）"""
    body = "第一章 " + ("正文内容" * 3000) + " 验证码"
    assert fetcher_cls._looks_blocked(body) is False


def test_looks_blocked_on_empty(fetcher_cls):
    assert fetcher_cls._looks_blocked("") is False


def test_on_block_5xx_is_gentler_than_4xx(fetcher_cls):
    """5xx 多为源站瞬时故障，惩罚必须弱于 403 类真封锁"""
    st5 = fetcher_cls._state_for("gentle.example")
    st4 = fetcher_cls._state_for("harsh.example")
    st5.interval_mul = st4.interval_mul = 1.0
    fetcher_cls._on_block(st5, 503)
    fetcher_cls._on_block(st4, 403)
    assert st5.interval_mul < st4.interval_mul
    assert st4.block_streak == 1


def test_on_block_respects_cap(fetcher_cls):
    st = fetcher_cls._state_for("cap.example")
    st.interval_mul = 1.0
    for _ in range(30):
        fetcher_cls._on_block(st, 403)
    assert st.interval_mul <= fetcher_cls.INTERVAL_MUL_MAX
