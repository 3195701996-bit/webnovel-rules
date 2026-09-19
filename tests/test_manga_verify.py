# -*- coding: utf-8 -*-
"""漫画源功能级验证（engine/manga/verify.py）单元测试。

覆盖诊断 §6：依赖在 ≠ 业务成功；未验证不得标成通过；缺依赖的源如实标 unsupported。
"""
import os

import pytest

from engine.manga import verify as mv


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(mv, "VERIFIED_FILE", os.path.join(str(tmp_path), "manga_verified.json"))
    mv.reset_for_tests()
    yield
    mv.reset_for_tests()


class _Comic:
    def __init__(self, cid, title="标题"):
        self.id, self.title, self.author = cid, title, ""
        self.cover = self.url = ""
        self.tags, self.total = [], 0


class _Chapter:
    def __init__(self, cid, name="第1话"):
        self.id, self.name, self.group, self.url = cid, name, "", ""


class _Details:
    def __init__(self, chapters):
        self.id, self.title, self.chapters = "c1", "标题", chapters
        self.cover = self.description = self.author = ""
        self.tags, self.recommend = [], []


class _Adapter:
    name = "假源"

    def __init__(self, comics=None, chapters=None, images=None, boom=""):
        self._comics = comics if comics is not None else [_Comic("c1")]
        self._chapters = chapters if chapters is not None else [_Chapter("ch1")]
        self._images = images if images is not None else ["https://img.example.com/1.jpg"]
        self._boom = boom

    def search(self, keyword, page=1):
        if self._boom == "search":
            raise RuntimeError("search boom")
        return self._comics

    def comic_info(self, comic_id):
        if self._boom == "detail":
            raise RuntimeError("detail boom")
        return _Details(self._chapters)

    def chapters(self, comic_id):
        return self._chapters

    def images(self, comic_id, chapter_id):
        if self._boom == "images":
            raise RuntimeError("images boom")
        return self._images

    def image_headers(self, url):
        return {}

    def image_url(self, url):
        return url


_DEFAULT_ADAPTER = object()


@pytest.fixture
def fake(monkeypatch):
    """装上假适配器、假能力台账、假取图（全部走 monkeypatch，测试之间不泄漏）。"""
    def _install(adapter=_DEFAULT_ADAPTER, ledger_status="supported", ledger_reason="",
                 image_bytes=5000, registered=None, image_status=200, image_data=None):
        import engine.manga.manager as mgr
        import server.capabilities as caps
        import engine.manga.downloader as dl
        chosen = _Adapter() if adapter is _DEFAULT_ADAPTER else adapter
        monkeypatch.setattr(mgr, "get_adapter", lambda key, state_dir=None: chosen)
        monkeypatch.setattr(caps, "source_status",
                            lambda key, caps=None: {"status": ledger_status,
                                                    "reason": ledger_reason,
                                                    "verified": False, "transport": {}})
        class _Resp:
            def __init__(self, code, data):
                self.status_code, self.content = code, data

        monkeypatch.setattr(dl, "fetch_image_checked",
                            lambda url, headers, timeout=20, **kw:
                            _Resp(image_status, image_data if image_data is not None
                                  else (b"\xff\xd8\xff" + b"x" * max(0, image_bytes - 3))))
        monkeypatch.setattr(mv, "registered_keys", lambda: registered or ["mangadex"])
    return _install


# ── 依赖级判定优先 ───────────────────────────────────────────

def test_dependency_missing_is_unsupported(fake):
    """copymanga_web 在手机上缺 Playwright：如实标 unsupported，不偷偷换通道。"""
    fake(ledger_status="unsupported", ledger_reason="缺少 playwright，无法搜索/取详情")
    r = mv.verify_one("copymanga_web")
    assert r["status"] == "unsupported"
    assert "playwright" in r["reason"]
    assert r["stages"] == {}                     # 没有假装跑过
    assert r["ledger"]["status"] == "unsupported"


def test_unregistered_adapter_is_unsupported(fake):
    """未注册适配器（如 baozi）→ unsupported，并说明是"未注册"，不假装跑过。"""
    fake(adapter=None)
    r = mv.verify_one("baozi")
    assert r["status"] == "unsupported"
    assert "未注册" in r["reason"]
    assert r["stages"] == {}


# ── 阶段与归类 ───────────────────────────────────────────────

def test_verified_requires_real_image_bytes(fake):
    fake(image_bytes=5000)
    r = mv.verify_one("mangadex", keyword="巨人")
    assert r["status"] == "verified"
    assert r["stages"]["search"]["count"] == 1
    assert r["stages"]["detail"]["count"] == 1
    assert r["stages"]["images"]["bytes"] == 5000
    assert r["stages"]["images"]["count"] == 1


def test_response_object_is_handled_not_crashed(fake):
    """fetch_image_checked 返回的是响应对象（不是 bytes）——早先按 bytes 处理会 TypeError，
    把本来能读的源误判成失败（实测在 MangaDex 上踩到）。"""
    fake(image_bytes=9000)
    r = mv.verify_one("mangadex")
    assert r["status"] == "verified"
    assert r["stages"]["images"]["bytes"] == 9000
    assert r["stages"]["images"]["format"] == "jpeg"
    assert r["stages"]["images"]["http"] == 200


def test_non_200_image_is_partial(fake):
    """图片请求 4xx/5xx：字节再多也不算通过。"""
    fake(image_bytes=9000, image_status=403)
    r = mv.verify_one("mangadex")
    assert r["status"] == "partial"
    assert "取图" in r["reason"] and "403" in r["reason"]


def test_html_body_masquerading_as_image_is_partial(fake):
    """风控 HTML 页字节够大但不是图片：必须判失败（按文件头识别）。"""
    fake(image_data=b"<html>" + b"x" * 9000)
    r = mv.verify_one("mangadex")
    assert r["status"] == "partial"
    assert "不是图片" in r["reason"]


def test_sniff_image_formats():
    assert mv.sniff_image(b"\xff\xd8\xff\x00") == "jpeg"
    assert mv.sniff_image(b"\x89PNG\r\n\x1a\n") == "png"
    assert mv.sniff_image(b"RIFF" + b"\x00" * 4 + b"WEBP") == "webp"
    assert mv.sniff_image(b"GIF89a...") == "gif"
    assert mv.sniff_image(b"<html>") == ""
    assert mv.sniff_image(b"") == ""


def test_tiny_image_is_partial_not_verified(fake):
    """图片列表非空但取到的字节过小：不能算"能看"（与本地直出的 1000 字节门槛一致）。"""
    fake(image_bytes=10)
    r = mv.verify_one("mangadex")
    assert r["status"] == "partial"
    assert "取图" in r["reason"] or "过小" in r["reason"]


def test_empty_search_is_failed(fake):
    fake(adapter=_Adapter(comics=[]))
    r = mv.verify_one("mangadex", keyword="不存在")
    assert r["status"] == "failed"
    assert r["stages"]["search"]["ok"] is False
    assert "搜索" in r["reason"]


def test_detail_failure_is_partial(fake):
    fake(adapter=_Adapter(chapters=[]))
    r = mv.verify_one("mangadex")
    assert r["status"] == "partial"
    assert "详情/目录" in r["reason"]


def test_images_failure_is_partial(fake):
    fake(adapter=_Adapter(images=[]))
    r = mv.verify_one("mangadex")
    assert r["status"] == "partial"
    assert "取图" in r["reason"]


def test_exception_in_stage_becomes_failure_not_crash(fake):
    fake(adapter=_Adapter(boom="images"))
    r = mv.verify_one("mangadex")
    assert r["status"] == "partial" and r["stages"]["images"]["ok"] is False
    fake(adapter=_Adapter(boom="search"))
    r2 = mv.verify_one("mangadex")
    assert r2["status"] == "failed" and "boom" in r2["stages"]["search"]["detail"]


# ── 汇总、合并与分批 ────────────────────────────────────────

def test_summary_pass_rate_uses_registered_total(fake):
    mv.save_merged({"tested_at": "t", "items": [
        {"key": "a", "status": "verified", "stages": {}},
        {"key": "b", "status": "failed", "reason": "x", "stages": {}},
    ]})
    fake(registered=["a", "b", "c"])
    p = mv.results_payload()
    assert p["summary"]["registered_total"] == 3
    assert p["summary"]["verified"] == 1
    assert p["summary"]["never_attempted"] == 1      # c 从未验证，必须暴露
    assert p["summary"]["pass_rate"] == 33           # 1/3


def test_save_merges_by_key(fake):
    fake(registered=["a", "b"])
    mv.save_merged({"tested_at": "t1", "items": [{"key": "a", "status": "failed", "stages": {}}]})
    mv.save_merged({"tested_at": "t2", "items": [{"key": "b", "status": "verified", "stages": {}}]})
    got = {i["key"]: i["status"] for i in mv.results_payload()["items"]}
    assert got == {"a": "failed", "b": "verified"}    # 第二批没有冲掉第一批


def test_start_runs_and_persists(fake, monkeypatch):
    fake(registered=["mangadex"])
    started = mv.start(keyword="巨人", skip_verified_days=0)
    assert started["started"] is True
    for _ in range(100):
        if mv.status()["status"] != "running":
            break
        import time as _t
        _t.sleep(0.05)
    assert mv.status()["status"] == "done"
    items = mv.results_payload()["items"]
    assert len(items) == 1 and items[0]["status"] == "verified"
    assert mv.load_results()["items"][0]["key"] == "mangadex"


def test_start_skips_recently_verified(fake):
    import time as _t
    fake(registered=["a", "b"])
    mv.save_merged({"tested_at": "now", "items": [
        {"key": "a", "status": "verified", "stages": {},
         "tested_at": _t.strftime("%Y-%m-%d %H:%M:%S")},
    ]})
    mv.start(skip_verified_days=7)
    for _ in range(100):
        if mv.status()["status"] != "running":
            break
        _t.sleep(0.05)
    items = {i["key"]: i["status"] for i in mv.results_payload()["items"]}
    assert items["b"] == "verified"
    assert items["a"] == "skipped"


# ── HTML 解码/解析助手（本轮新增，包子漫画实测失败后补上）──────────

def test_decode_html_honours_declared_and_sniffed_charset():
    from engine.manga.base import decode_html
    assert decode_html("中文".encode("utf-8")) == "中文"
    assert decode_html("中文".encode("gbk"), "gbk") == "中文"
    # 没给声明时按 meta charset 嗅探
    assert decode_html(b'<meta charset="gb2312">' + "中文".encode("gb2312")).endswith("中文")
    # BOM 优先于一切
    assert decode_html("中文".encode("utf-16")) == "中文"
    assert decode_html(b"") == ""


def test_html_fromstring_safe_strips_declaration_and_nuls():
    """源站带 XML 声明（或含 NUL）时 lxml 会直接报 encoding not supported——
    实测包子漫画就是这个，助手必须先剥声明/去 NUL 再解析。"""
    from lxml import html as lhtml
    from engine.manga.base import html_fromstring_safe
    doc = html_fromstring_safe(
        lhtml, '<?xml version="1.0" encoding="USC-4"?><html><body><p>ok</p></body></html>')
    assert doc.cssselect("p")[0].text == "ok"
    doc2 = html_fromstring_safe(lhtml, "\ufeff\x00<html><body><p>y</p></body></html>")
    assert doc2.cssselect("p")[0].text == "y"


# ── 需要执行 JS 的源（包子漫画实测：章节列表改为前端渲染）────────────

def test_js_required_source_is_unsupported_not_partial(fake, monkeypatch):
    """站点把目录改成前端渲染时，必须标 unsupported 并说清原因，
    不能含糊报"目录为空"（更不能偷偷换通道）。"""
    from engine.manga.base import JsRequiredError

    class _JsAdapter(_Adapter):
        def comic_info(self, comic_id):
            raise JsRequiredError("目录由网页脚本渲染（服务端 HTML 只有空章节提示）")

    fake(adapter=_JsAdapter())
    r = mv.verify_one("baozi")
    assert r["status"] == "unsupported"
    assert "网页脚本渲染" in r["reason"]
    assert r["stages"]["search"]["ok"] is True      # 搜索阶段证据保留
    assert r["stages"]["detail"]["ok"] is False


def test_baozi_detects_js_rendered_chapters(monkeypatch):
    """包子漫画适配器：页面只有 empty_chapters_tips 时必须抛 JsRequiredError。"""
    from engine.manga import baozi
    from engine.manga.base import JsRequiredError
    page = ('<html><body><h1 class="comics-detail__title">巨人</h1>'
            '<div class="l-content empty_chapters_tips">章节加载中</div></body></html>')
    monkeypatch.setattr(baozi, "_fetch", lambda url, timeout=15: page)
    ad = baozi.Baozi()
    try:
        ad.comic_info("juren-darkhorsecomics")
        raise AssertionError("应当抛出 JsRequiredError")
    except JsRequiredError as e:
        assert "网页脚本渲染" in str(e)


def test_baozi_still_parses_classic_markup(monkeypatch):
    """站点若仍是老结构（chapter_slot 链接），必须照旧能解析出章节。"""
    from engine.manga import baozi
    page = ('<html><body><h1 class="comics-detail__title">某漫画</h1>'
            '<div class="comics-chapters">'
            '<a href="/comic/chapter/x/0_2.html?chapter_slot=2"><div><span>第2话</span></div></a>'
            '<a href="/comic/chapter/x/0_1.html?chapter_slot=1"><div><span>第1话</span></div></a>'
            '</div></body></html>')
    monkeypatch.setattr(baozi, "_fetch", lambda url, timeout=15: page)
    d = baozi.Baozi().comic_info("x")
    assert [c.name for c in d.chapters] == ["第1话", "第2话"]   # 页面倒序 → 输出正序
