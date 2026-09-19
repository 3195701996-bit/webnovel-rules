# -*- coding: utf-8 -*-
"""拷贝漫画网页通道（`engine/manga/copy_web.py`）与 CopyManga 的通道调度（全离线）。

## 为什么全离线

真实源站请求是**稀缺资源**（APP 通道已有 210 风控，网页通道被 Cloudflare 保护）。
这里的每一类断言都用**本文件自己生成的密文**与**自写的最小 HTML/JSON 片段**，
不依赖 /tmp 样本、不依赖网络：fixture 一旦被源站改动影响，测试就会从
"验证实现"变成"验证网络"。

密文用**正向加密**生成（`_enc()`），因此 `_aes_decrypt_str` 的解密正确性
是被独立验证的，而不是"加密解密同源、一起错也一起过"。
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.manga.copy_web as cw          # noqa: E402
import engine.manga.copymanga as cm         # noqa: E402

KEY = "op0zzpvv.nmn.00p"          # 站点当前 ccz/cct（16 字符）；测试里只是样本值
IV = "0123456789abcdef"


# ── 正向加密（本文件的独立实现，用来验证解密）────────────────────────────
def _enc(plain, key=KEY, iv=IV):
    """AES-128-CBC + PKCS7 加密 → 拷贝漫画密文串格式：IV(16 明文) + hex(密文)"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sympad
    p = sympad.PKCS7(128).padder()
    data = p.update(plain.encode("utf-8")) + p.finalize()
    e = Cipher(algorithms.AES(key.encode("utf-8")), modes.CBC(iv.encode("utf-8")))
    enc = e.encryptor()
    ct = enc.update(data) + enc.finalize()
    return iv + ct.hex()


# ── 假响应 / 路由 ────────────────────────────────────────────────────────
class _R:
    def __init__(self, status=200, text="", headers=None):
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def clean_caches():
    """清 copy_web 的进程内缓存（域/密钥/详情页），并在测试后复原"""
    cw.clear_cache()
    yield
    cw.clear_cache()


@pytest.fixture
def base_ready(monkeypatch, clean_caches):
    """域已选定（跳过探测请求），所有请求都按 URL 路由到假响应"""
    monkeypatch.setattr(cw, "_BASE", {"base": cw.WEB_DOMAINS[0], "ts": time.time()})
    monkeypatch.setattr(cw, "_NEG", {"ts": 0.0, "msg": ""}, raising=False)
    calls = []

    def _install(routes):
        """routes: [(substring, response_or_callable)]；未匹配的 URL 直接失败。

        匹配**最长的那个子串**（最具体的路由优先），因此 `/comic/x` 与
        `/comic/x/chapter/u1` 同时注册时不会互相遮蔽。
        """
        def _fake_get(url, headers=None, timeout=None, retries=cw.MAX_RETRIES,
                      budget=None):        # budget：0.74.2 新增的总预算参数

            calls.append((url, dict(headers or {})))
            if url.rstrip("/") in [b.rstrip("/") for b in cw._domain_pool()]:
                # 换域重试会重新探测根域：给一个"看起来是拷贝漫画"的首页
                return _R(200, "<title>拷貝漫畫 拷贝漫画</title>")
            best = None
            for sub, resp in routes:
                if sub in url and (best is None or len(sub) > len(best[0])):
                    best = (sub, resp)
            if best is None:
                raise AssertionError(f"未预期的请求: {url}")
            return best[1]() if callable(best[1]) else best[1]
        monkeypatch.setattr(cw, "_web_get", _fake_get)
        return calls
    return _install


# ── 1. AES 解密 ──────────────────────────────────────────────────────────
def test_decrypt_from_own_ciphertext():
    plain = '{"build":{"path_word":"abc"}}'
    assert cw._aes_decrypt_str(_enc(plain), KEY) == plain


def test_decrypt_takes_iv_from_first_16_chars():
    """前 16 字符是**明文 IV**（不是 hex），且必须真的被当作 IV 使用"""
    plain = "A" * 40                              # 跨 3 个块
    a, b = _enc(plain, iv="aaaaaaaaaaaaaaaa"), _enc(plain, iv="bbbbbbbbbbbbbbbb")
    assert a[:16] == "aaaaaaaaaaaaaaaa" and b[:16] == "bbbbbbbbbbbbbbbb"
    assert a[16:] != b[16:]                       # CBC 链：换 IV 整段密文都变
    assert cw._aes_decrypt_str(a, KEY) == plain
    assert cw._aes_decrypt_str(b, KEY) == plain
    # 篡改前 16 字符（IV 前缀）：只有第一个明文块被破坏，说明 IV 确实取自前缀
    tampered = "zzzzzzzzzzzzzzzz" + a[16:]
    out = cw._aes_decrypt_str(tampered, KEY)
    assert out != plain
    assert out[16:] == plain[16:], "CBC 解密：错 IV 只破坏第一个明文块"


def test_decrypt_removes_pkcs7_padding_and_hex_parses():
    """整块明文（16 的整数倍）会整块填充；解密后不得残留 \\x0c 之类填充字节"""
    plain = "x" * 32
    assert cw._aes_decrypt_str(_enc(plain), KEY) == plain
    # 中文字节不是 16 的整数倍 → 部分填充块
    assert cw._aes_decrypt_str(_enc("繁體"), KEY) == "繁體"


def test_decrypt_strips_surrounding_whitespace():
    assert cw._aes_decrypt_str(_enc("  {\"a\":1}  "), KEY) == '{"a":1}'


def test_decrypt_rejects_bad_ciphertext():
    with pytest.raises(cw.WebError):
        cw._aes_decrypt_str("short", KEY)                     # 太短
    with pytest.raises(cw.WebError):
        cw._aes_decrypt_str("0" * 16 + "zzzz", KEY)           # 非法 hex
    with pytest.raises(cw.WebError):
        cw._aes_decrypt_str("0" * 16 + "abcd", KEY)           # hex 长度非块对齐
    with pytest.raises(cw.WebError):
        cw._aes_decrypt_str(_enc("hello"), "short-key")        # 密钥长度非法
    # 密钥不匹配 → PKCS7 去填充失败（不得静默返回乱码）
    with pytest.raises(cw.WebError):
        cw._aes_decrypt_str(_enc("hello" * 8, key="zzzzzzzzzzzzzzzz"), KEY)


# ── 2. 详情页 HTML 解析 ─────────────────────────────────────────────────
DETAIL_HTML = """<!DOCTYPE html><html lang="zh-hant"><head><meta charset="UTF-8">
<title>神明與初戀-神明與初戀漫畫-連載中-愛情漫画-在线阅读 - 拷貝漫畫 拷贝漫画</title>
</head><body>
<div class="comicParticulars-title-left"><div class="comicParticulars-left-img loadingIcon">
<img class="lazyload" data-src="https://ss.mangafunb.fun/s/shenmingyuchulian/cover/1780773566.jpg.328x422.jpg">
</div></div>
<div class="comicParticulars-title-right"><ul>
<li><h6 title="神明與初戀">神明與初戀</h6></li>
<li><span>別名：</span><p class="comicParticulars-right-txt"> 神明与初恋 </p></li>
<li><span>作者：</span><span class="comicParticulars-right-txt">
<a href="/author/wadakoma/comics" target="_blank">和田こま</a></span></li>
<li><span>熱度：</span><p class="comicParticulars-right-txt">3.3W</p></li>
<li><span class="comicParticulars-sigezi">最後更新：</span>
<span class="comicParticulars-right-txt">2026-09-01</span></li>
<li><span>狀態：</span><span class="comicParticulars-right-txt">連載中</span></li>
<li><span>題材：</span><span class="comicParticulars-left-theme-all comicParticulars-tag">
<a href="/comics?theme=aiqing" target="_blank">#愛情</a>
<a href="/comics?theme=shengui" target="_blank">#神鬼</a></span></li>
<li><a href="/comic/shenmingyuchulian/chapter/b0d030f5-61dc-11f1-914c-fa163e02432f"
 class="btn comicParticulars-botton">開始閱讀</a></li>
</ul></div>
<span id="dnt" style="display:none;" value="3"></span>
<div style="display:flex;"><p class="intro" style="flex:1;">
「做我的新娘 為我誕下子嗣」祭因為擁有看見非人之物的體質而慘遭未婚夫退婚。</p></div>
<script>var ccz = '%s';</script>
</body></html>""" % KEY


def test_detail_parses_documented_selectors(base_ready, monkeypatch):
    base_ready([("/comic/shenmingyuchulian", _R(200, DETAIL_HTML))])
    d = cw.web_detail("shenmingyuchulian")
    assert d.id == "shenmingyuchulian"
    assert d.title == "神明與初戀"
    assert d.author == "和田こま"
    assert d.tags == ["愛情", "神鬼"]                    # 去掉 '#' 前缀
    assert "看見非人之物" in d.description
    assert d.cover.endswith(".328x422.jpg") and "/cover/" in d.cover
    assert d.update_time == "2026-09-01" and d.views == "3.3W"
    assert d.sub_title == "拷贝漫画"
    assert d.url.endswith("/comic/shenmingyuchulian")
    assert d.chapters == []                              # 章节由 web_chapters 单独拉
    # 繁体**原样返回**：engine/manga/ 现有适配器都不做简繁转换，
    # 这里也不新增转换行为（要转换应由上层统一处理）
    assert "與" in d.title


def test_page_key_reads_ccz_and_dnt(base_ready):
    base_ready([("/comic/shenmingyuchulian", _R(200, DETAIL_HTML))])
    assert cw.page_key("shenmingyuchulian") == (KEY, "3")


def test_page_key_second_call_hits_cache(base_ready):
    calls = base_ready([("/comic/shenmingyuchulian", _R(200, DETAIL_HTML))])
    cw.page_key("shenmingyuchulian")
    cw.page_key("shenmingyuchulian")
    assert len(calls) == 1, "密钥缓存必须避免重复取详情页"


def test_detail_missing_inline_key_raises(base_ready):
    html = DETAIL_HTML.replace("var ccz = '%s';" % KEY, "")
    base_ready([("/comic/x", _R(200, html))])
    with pytest.raises(cw.WebError) as ei:
        cw.page_key("x")
    assert "ccz" in str(ei.value) and "页面结构" in str(ei.value)


def test_detail_missing_title_raises(base_ready):
    base_ready([("/comic/x", _R(200, "<html><body>%s</body></html>" % ("x" * 300)))])
    with pytest.raises(cw.WebError) as ei:
        cw.web_detail("x")
    assert "页面结构可能已变更" in str(ei.value)


def test_empty_shell_page_raises(base_ready):
    """200 但内容为空壳（裸域 copy4000.com 的症状）必须报错，不得当成"这本书没数据" """
    base_ready([("/comic/x", _R(200, ""))])
    with pytest.raises(cw.WebError) as ei:
        cw.web_detail("x")
    assert "空壳" in str(ei.value) or "内容异常" in str(ei.value)


def test_body_meta_charset_wins_over_iso8859(base_ready):
    """UTF-8 中文页面：解码不得因 requests 的 ISO-8859-1 缺省而乱码"""
    raw = DETAIL_HTML.encode("utf-8")
    r = _R(200, "")
    r.content, r.text = raw, raw.decode("utf-8")
    base_ready([("/comic/shenmingyuchulian", r)])
    assert cw.web_detail("shenmingyuchulian").title == "神明與初戀"


# ── 3. 搜索解析 ──────────────────────────────────────────────────────────
SEARCH_JSON = json.dumps({
    "code": 200, "message": "请求成功",
    "results": {
        "list": [
            {"name": "劍姬", "alias": "劍姬,剑姬", "path_word": "jianji",
             "cover": "https://sj.mangafunb.fun/j/jianji/cover/1650986966.jpg.328x422.jpg",
             "author": [{"name": "びっけ", "alias": "びっけ", "path_word": "bikke"}],
             "popular": 806, "theme": [], "parodies": [], "females": [], "males": []},
            {"comic": {"name": "火與劍", "path_word": "hyj", "cover": "c2",
                       "author": {"name": "佐藤将"},
                       "theme": [{"name": "愛情"}]}},
            {"name": "无 path_word 的脏数据", "author": []},
        ], "total": 2495, "limit": 2, "offset": 0}}, ensure_ascii=False)


def test_search_parses_canned_json(base_ready):
    base_ready([("/api/kb/web/searchci/comics", _R(200, SEARCH_JSON))])
    comics, total = cw.web_search("劍", limit=2, offset=0)
    assert total == 2495
    assert [c.id for c in comics] == ["jianji", "hyj"]      # 脏数据被跳过
    assert comics[0].title == "劍姬" and comics[0].author == "びっけ"
    assert comics[0].cover.endswith(".jpg")
    assert comics[0].total == 2495 and comics[0].source_key == "copymanga"
    assert comics[1].author == "佐藤将" and comics[1].tags == ["愛情"]


def test_search_request_url_shape(base_ready):
    calls = base_ready([("/api/kb/web/searchci/comics", _R(200, SEARCH_JSON))])
    cw.web_search("姐 姐", limit=30, offset=60)
    url = calls[0][0]
    assert "/api/kb/web/searchci/comics?" in url
    assert "limit=30" in url and "offset=60" in url
    assert "q=%E5%A7%90%20%E5%A7%90" in url                # 关键词已 URL 编码
    assert "platform=2" in url                              # 站点搜索页同款参数


def test_search_results_missing_raises(base_ready):
    """`results` 缺失 = 结构变了，不能当成"没搜到" """
    base_ready([("/api/kb/web/searchci/comics", _R(200, '{"code":200,"results":{}}'))])
    with pytest.raises(cw.WebError) as ei:
        cw.web_search("x")
    assert "结构" in str(ei.value) or "变更" in str(ei.value)


def test_search_legit_empty_list_is_not_an_error(base_ready):
    """合法的"搜不到"（有 list、空数组）照实返回空，不抛异常"""
    body = '{"code":200,"results":{"list":[],"total":0,"limit":30,"offset":0}}'
    base_ready([("/api/kb/web/searchci/comics", _R(200, body))])
    assert cw.web_search("不存在的关键词") == ([], 0)


def test_search_code_not_200_raises(base_ready):
    base_ready([("/api/kb/web/searchci/comics",
                 _R(200, '{"code":500,"message":"请求失败","results":null}'))])
    with pytest.raises(cw.WebError) as ei:
        cw.web_search("x")
    assert "请求失败" in str(ei.value)


def test_search_non_json_raises(base_ready):
    base_ready([("/api/kb/web/searchci/comics", _R(200, "<html>登录</html>"))])
    with pytest.raises(cw.WebError):
        cw.web_search("x")


# ── 4. 章节分组映射 ──────────────────────────────────────────────────────
def _chapters_body(payload):
    return json.dumps({"code": 200, "message": "请求成功",
                       "results": _enc(json.dumps(payload, ensure_ascii=False))},
                      ensure_ascii=False)


CHAPTERS = {
    "build": {"path_word": "smycl",
              "type": [{"id": 1, "name": "話"}, {"id": 2, "name": "卷"},
                       {"id": 3, "name": "番外篇"}]},
    "groups": {
        "default": {"path_word": "default", "count": 2, "name": "默認", "chapters": [
            {"type": 1, "name": "第1話", "id": "11111111-1111-1111-1111-111111111111"},
            {"type": 1, "name": "第2話", "id": "22222222-2222-2222-2222-222222222222"}]},
        "fanwai": {"path_word": "fanwai", "count": 1, "name": "", "chapters": [
            {"type": 3, "name": "番外篇", "id": "33333333-3333-3333-3333-333333333333"}]},
    },
}


def test_chapters_group_mapping_and_order(base_ready):
    base_ready([("/comic/smycl", _R(200, DETAIL_HTML)),
                ("/comicdetail/smycl/chapters", _R(200, _chapters_body(CHAPTERS)))])
    chs = cw.web_chapters("smycl")
    assert [c.id for c in chs] == ["11111111-1111-1111-1111-111111111111",
                                   "22222222-2222-2222-2222-222222222222",
                                   "33333333-3333-3333-3333-333333333333"]
    assert [c.name for c in chs] == ["第1話", "第2話", "番外篇"]
    assert [c.group for c in chs] == ["默認", "默認", "番外篇"]   # 组名空→build.type 映射
    assert all("/chapter/" in c.url for c in chs)
    # **不反转**：站点 JS 逐项顺序渲染（离线核对过 comic_detail_pass*.js）
    assert chs[0].name == "第1話"


def test_chapters_uses_dnts_header(base_ready):
    calls = base_ready([("/comic/smycl", _R(200, DETAIL_HTML)),
                        ("/comicdetail/smycl/chapters", _R(200, _chapters_body(CHAPTERS)))])
    cw.web_chapters("smycl")
    assert calls[1][1].get("dnts") == "3", "反爬头必须取自详情页 #dnt"
    assert calls[1][0].startswith(cw.WEB_DOMAINS[0])


def test_chapters_empty_results_raises(base_ready):
    base_ready([("/comic/smycl", _R(200, DETAIL_HTML)),
                ("/comicdetail/smycl/chapters",
                 _R(200, '{"code":200,"message":"请求成功","results":""}'))])
    with pytest.raises(cw.WebError) as ei:
        cw.web_chapters("smycl")
    assert "results 为空" in str(ei.value)


def test_chapters_bad_ciphertext_raises(base_ready):
    base_ready([("/comic/smycl", _R(200, DETAIL_HTML)),
                ("/comicdetail/smycl/chapters",
                 _R(200, '{"code":200,"results":"0000000000000000zzzz"}'))])
    with pytest.raises(cw.WebError):
        cw.web_chapters("smycl")


def test_chapters_zero_chapters_raises(base_ready):
    """0 章不得当成功（否则上层显示成"这部漫画没有章节"）"""
    payload = {"build": {"path_word": "x"},
               "groups": {"default": {"path_word": "default", "count": 0,
                                      "name": "默認", "chapters": []}}}
    base_ready([("/comic/smycl", _R(200, DETAIL_HTML)),
                ("/comicdetail/smycl/chapters", _R(200, _chapters_body(payload)))])
    with pytest.raises(cw.WebError) as ei:
        cw.web_chapters("smycl")
    assert "0 章" in str(ei.value)


def test_chapters_missing_groups_raises(base_ready):
    base_ready([("/comic/smycl", _R(200, DETAIL_HTML)),
                ("/comicdetail/smycl/chapters",
                 _R(200, _chapters_body({"build": {"path_word": "x"}})))])
    with pytest.raises(cw.WebError) as ei:
        cw.web_chapters("smycl")
    assert "groups" in str(ei.value)


# ── 5. contentKey → 图片列表 ────────────────────────────────────────────
IMGS = [{"url": "https://sj.mangafunb.fun/j/x/1/001.jpg"},
        {"url": "https://s3.mangafunb.fun/static/ads/ad.jpg"},
        {"url": "https://sj.mangafunb.fun/j/x/1/002.jpg"}]


def _reader_html(imgs, cct=KEY, key_override=None):
    ck = key_override if key_override is not None else _enc(
        json.dumps(imgs, ensure_ascii=False))
    return ("<html><head><meta charset='UTF-8'><title>第1話</title></head><body>"
            "<ul class='comicContent-list'></ul>"
            "<script>var cct = '%s';\n  var contentKey = '%s';</script>"
            "</body></html>" % (cct, ck))


def test_images_from_content_key(base_ready):
    base_ready([("/comic/smycl", _R(200, DETAIL_HTML)),
                ("/comic/smycl/chapter/u1", _R(200, _reader_html(IMGS)))])
    imgs = cw.web_images("smycl", "u1")
    assert imgs == ["https://sj.mangafunb.fun/j/x/1/001.jpg",
                    "https://sj.mangafunb.fun/j/x/1/002.jpg"]   # 广告图被过滤，顺序不变


def test_images_sends_dnts_and_uses_cct(base_ready):
    """图片密钥用章节页的 cct（与 ccz 实测同值，但必须各自取自页面）"""
    other = "abcdefghijklmnop"
    calls = base_ready([("/comic/smycl", _R(200, DETAIL_HTML)),
                        ("/comic/smycl/chapter/u1",
                         _R(200, _reader_html(IMGS, cct=other, key_override=_enc(
                             json.dumps(IMGS), key=other))))])
    assert len(cw.web_images("smycl", "u1")) == 2
    assert calls[1][1].get("dnts") == "3"


def test_images_filter_never_empties_list():
    """过滤只排除站内静态图；若过滤会清空列表，则退回未过滤（CDN 换域名时不静默变 0 图）"""
    only_static = ["https://s3.mangafunb.fun/static/a.jpg"]
    assert cw._filter_images(only_static) == only_static
    assert cw._filter_images([]) == []


def test_images_accepts_plain_string_items(base_ready):
    payload = ["https://sj.mangafunb.fun/j/x/1/001.jpg"]
    base_ready([("/comic/smycl", _R(200, DETAIL_HTML)),
                ("/comic/smycl/chapter/u1", _R(200, _reader_html(payload)))])
    assert cw.web_images("smycl", "u1") == payload


def test_images_missing_cct_raises(base_ready):
    html = _reader_html(IMGS).replace("var cct = '%s'" % KEY, "var cct = ''")
    base_ready([("/comic/smycl", _R(200, DETAIL_HTML)),
                ("/comic/smycl/chapter/u1", _R(200, html))])
    with pytest.raises(cw.WebError) as ei:
        cw.web_images("smycl", "u1")
    assert "cct" in str(ei.value)


def test_images_empty_content_key_raises_after_retry(base_ready):
    """contentKey 为空 = **源站偶发空壳**：换域重试一次；仍为空才明确报因。

    2026-09-17 实测两次遇到空壳，紧接着重跑就正常（24 张）——所以这里不能一次
    就判死刑（那正是早期把网页通道误判为"不可用"的原因），但也不能无限重试。
    """
    html = _reader_html(IMGS, key_override="")
    calls = base_ready([("/comic/smycl", _R(200, DETAIL_HTML)),
                        ("/comic/smycl/chapter/u1", _R(200, html))])
    with pytest.raises(cw.WebError) as ei:
        cw.web_images("smycl", "u1")
    assert "contentKey" in str(ei.value)
    assert "重试后仍为空" in str(ei.value), str(ei.value)
    # 恰好两次（一次原始 + 一次换域重试），不多不少
    chap_calls = [c for c in calls if "/chapter/u1" in c[0]]
    assert len(chap_calls) == 2, chap_calls


def test_images_empty_shell_then_success(base_ready):
    """第一次空壳、第二次正常 → **必须成功**（把偶发抖动当故障是错的）"""
    seq = {"n": 0}

    def _flaky():
        seq["n"] += 1
        return _R(200, _reader_html(IMGS, key_override="") if seq["n"] == 1
                  else _reader_html(IMGS))
    calls = base_ready([("/comic/smycl", _R(200, DETAIL_HTML)),
                        ("/comic/smycl/chapter/u1", _flaky)])
    urls = cw.web_images("smycl", "u1")
    assert urls, "空壳后重试成功却仍报错 = 把偶发抖动当成不可用"
    assert len([c for c in calls if "/chapter/u1" in c[0]]) == 2


# 实测到的空壳形态：结构完全正确、groups 存在，但 chapters 为空、count=0
EMPTY_CHAPTERS = {
    "build": {"path_word": "smycl", "type": [{"id": 1, "name": "話"}]},
    "groups": {"default": {"path_word": "default", "count": 0,
                           "name": "默認", "chapters": []}},
}


def test_chapters_empty_shell_retries_once(base_ready):
    """章节列表空壳（解密成功但 0 章）→ 换域重试一次；仍为空才报"0 章"。"""
    empty = _chapters_body(EMPTY_CHAPTERS)
    base_ready([("/comic/smycl", _R(200, DETAIL_HTML)),
                ("/comicdetail/smycl/chapters", _R(200, empty))])
    with pytest.raises(cw.WebError) as ei:
        cw.web_chapters("smycl")
    assert "0 章" in str(ei.value), str(ei.value)


def test_chapters_empty_shell_then_success(base_ready):
    """空壳一次后恢复 → 必须成功（源站偶发抖动）"""
    seq = {"n": 0}

    def _flaky():
        seq["n"] += 1
        return _R(200, _chapters_body(EMPTY_CHAPTERS) if seq["n"] == 1
                  else _chapters_body(CHAPTERS))
    base_ready([("/comic/smycl", _R(200, DETAIL_HTML)),
                ("/comicdetail/smycl/chapters", _flaky)])
    chs = cw.web_chapters("smycl")
    assert chs, "空壳后重试成功却仍报错"


def test_images_empty_uuid_raises(base_ready):
    base_ready([])
    with pytest.raises(cw.WebError):
        cw.web_images("smycl", "")


def test_images_decrypt_to_empty_raises(base_ready):
    base_ready([("/comic/smycl", _R(200, DETAIL_HTML)),
                ("/comic/smycl/chapter/u1", _R(200, _reader_html([])))])
    with pytest.raises(cw.WebError):
        cw.web_images("smycl", "u1")


# ── 6. HTTP 层与域池 ─────────────────────────────────────────────────────
def test_http_500_raises_after_limited_retries(monkeypatch):
    n = []

    class _Sess:
        proxies = {}

        def get(self, *a, **k):
            n.append(1)
            return _R(500, "boom")

    monkeypatch.setattr(cw, "_session", lambda: _Sess())
    with pytest.raises(cw.WebError) as ei:
        cw._web_get("https://x/y", retries=1)
    assert "HTTP 500" in str(ei.value)
    assert len(n) == 2, "有限重试：retries=1 → 共 2 次"


def test_http_403_stops_immediately(monkeypatch):
    """403/Cloudflare → 立刻停止（换域硬撞只会让 IP 被标记更深）"""
    n = []

    class _Sess:
        proxies = {}

        def get(self, *a, **k):
            n.append(1)
            return _R(403, "Just a moment...")

    monkeypatch.setattr(cw, "_session", lambda: _Sess())
    with pytest.raises(cw.WebBlocked) as ei:
        cw._web_get("https://x/y", retries=2)
    assert "403" in str(ei.value) and len(n) == 1


def test_network_error_retried_then_raises(monkeypatch):
    n = []

    class _Sess:
        proxies = {}

        def get(self, *a, **k):
            n.append(1)
            raise OSError("connection reset by peer")

    monkeypatch.setattr(cw, "_session", lambda: _Sess())
    with pytest.raises(cw.WebError) as ei:
        cw._web_get("https://x/y", retries=1)
    assert "connection reset" in str(ei.value) and len(n) == 2


def test_request_goes_through_netproxy(monkeypatch):
    """出站代理的唯一事实来源是 engine.netproxy（不得自己读环境变量）"""
    seen = {}

    class _Sess:
        proxies = {}

        def get(self, url, headers=None, timeout=None, proxies=None, **k):
            seen["proxies"] = proxies
            return _R(200, "ok")

    monkeypatch.setattr(cw, "_session", lambda: _Sess())
    cw.reset_session()
    monkeypatch.setattr(cw, "_proxy", lambda: "http://127.0.0.1:7890")
    cw._web_get("https://x/y")
    assert seen["proxies"] == {"http": "http://127.0.0.1:7890",
                              "https": "http://127.0.0.1:7890"}


def test_domain_pool_all_dead_raises(monkeypatch, clean_caches):
    def _dead(url, **k):
        raise cw.WebError("connect timeout")
    monkeypatch.setattr(cw, "_web_get", _dead)
    with pytest.raises(cw.WebError) as ei:
        cw._web_base(force=True)
    assert "域池全部失败" in str(ei.value)


def test_get_path_rotates_domain_then_reports(monkeypatch, clean_caches):
    """域池轮换：第一个域请求失败后换第二个域，全挂才报错（不返回第一个域硬撞）"""
    tried = []
    site = "<html><title>拷貝漫畫 拷贝漫画</title></html>"

    def _fake(url, **k):
        tried.append(url)
        if url.rstrip("/") in cw.WEB_DOMAINS:
            return _R(200, site)                       # 首页探测都通过
        raise cw.WebError("connect timeout")           # 业务路径都失败
    monkeypatch.setattr(cw, "_web_get", _fake)
    with pytest.raises(cw.WebError) as ei:
        cw._get_path("/comic/x")
    assert "所有域均失败" in str(ei.value)
    path_calls = [u for u in tried if u.endswith("/comic/x")]
    assert len(path_calls) == len(cw._domain_pool()), tried
    assert path_calls[0].startswith(cw.WEB_DOMAINS[0])
    assert path_calls[1].startswith(cw.WEB_DOMAINS[1])
    assert all("//www." in u for u in path_calls)       # 绝不含裸域


def test_naked_domain_not_in_pool():
    """裸域 copy4000.com 对匿名 HTTP 是空壳——绝不能进域池（放最前更不行）"""
    assert cw.WEB_DOMAINS[0] == "https://www.copy4000.com"
    assert not any(d.rstrip("/") == "https://copy4000.com" for d in cw.WEB_DOMAINS)
    assert all(d.startswith("https://www.") for d in cw.WEB_DOMAINS)


def test_domain_pool_env_override(monkeypatch):
    monkeypatch.setenv("WR_COPY_WEB_DOMAINS", "a.example,b.example/")
    assert cw._domain_pool() == ["https://a.example", "https://b.example"]


def test_web_base_probe_rejects_empty_shell(monkeypatch, clean_caches):
    """探测必须否掉空壳域：只有真站点（含"拷贝/拷貝"）才被选中"""
    def _fake(url, **k):
        if "www.copy4000.com" in url:
            return _R(200, "<html><body>nothing here</body></html>")
        return _R(200, "<html><title>拷貝漫畫 拷贝漫画</title></html>")
    monkeypatch.setattr(cw, "_web_get", _fake)
    assert cw._web_base(force=True) == cw.WEB_DOMAINS[1]


# ── 7. 适配器：网页优先 + 回落 APP（210 逻辑不变）───────────────────────
@pytest.fixture
def adapter(monkeypatch):
    """离线的 CopyManga 实例（不碰 APP 域名发现、不碰官网版本探测）"""
    monkeypatch.setattr(cm, "_fetch_latest_version", lambda: "3.0.9")
    monkeypatch.setattr(cm.CopyManga, "_refresh_api", lambda self: None)
    monkeypatch.delenv("WR_COPY_TRANSPORT", raising=False)
    return cm.CopyManga()


def test_transport_mode_defaults_and_env(monkeypatch):
    monkeypatch.delenv("WR_COPY_TRANSPORT", raising=False)
    assert cm._transport_mode() == "auto"
    monkeypatch.setenv("WR_COPY_TRANSPORT", "WEB")
    assert cm._transport_mode() == "web"
    monkeypatch.setenv("WR_COPY_TRANSPORT", "banana")     # 非法值不生效
    assert cm._transport_mode() == "auto"


def test_search_prefers_web_channel(adapter, monkeypatch):
    called = []
    monkeypatch.setattr(cw, "web_search",
                        lambda kw, **k: ([cm.Comic(id="jianji", title="劍姬")], 2495))
    monkeypatch.setattr(cm.CopyManga, "_app_search",
                        lambda self, kw, pg=1: called.append("app") or [])
    out = adapter.search("劍姬")
    assert [c.id for c in out] == ["jianji"] and called == []


def test_search_falls_back_to_app_then_210_logic_unchanged(adapter, monkeypatch):
    def _boom(*a, **k):
        raise cw.WebError("域池全部失败")
    monkeypatch.setattr(cw, "web_search", _boom)
    seen = []

    def _app(self, kw, pg=1):
        seen.append((kw, pg))
        return [cm.Comic(id="app1", title="来自 APP")]
    monkeypatch.setattr(cm.CopyManga, "_app_search", _app)
    out = adapter.search("劍姬", page=3)
    assert [c.id for c in out] == ["app1"] and seen == [("劍姬", 3)]

    # 210 状态机（降级/冷却/敏感期判定）在回落路径上必须保持原样
    assert adapter.in_cooldown() is False
    adapter._mark_210()
    assert adapter._rg_state == "normal" and adapter.in_cooldown() is True
    adapter._mark_210()
    assert adapter._rg_state == "degraded"                 # 第 2 次 → 降级
    adapter._mark_210()
    adapter._mark_210()
    assert adapter._rg_state == "cooldown"                 # 第 4 次 → 冷却
    with pytest.raises(cm.MangaError) as ei:
        adapter._rate_wait()
    assert "冷却" in str(ei.value)


def test_transport_app_never_touches_web(adapter, monkeypatch):
    monkeypatch.setenv("WR_COPY_TRANSPORT", "app")
    monkeypatch.setattr(cw, "web_search",
                        lambda *a, **k: pytest.fail("transport=app 不得走网页通道"))
    monkeypatch.setattr(cm.CopyManga, "_app_search",
                        lambda self, kw, pg=1: [cm.Comic(id="app1", title="APP")])
    assert [c.id for c in adapter.search("x")] == ["app1"]


def test_transport_web_does_not_fall_back(adapter, monkeypatch):
    """显式要求网页通道时不得偷偷回落 APP（APP 请求正是 210 的来源）"""
    monkeypatch.setenv("WR_COPY_TRANSPORT", "web")
    monkeypatch.setattr(cw, "web_search",
                        lambda *a, **k: (_ for _ in ()).throw(cw.WebError("403")))
    monkeypatch.setattr(cm.CopyManga, "_app_search",
                        lambda *a, **k: pytest.fail("transport=web 不得回落 APP"))
    with pytest.raises(cm.MangaError) as ei:
        adapter.search("x")
    assert "网页通道失败" in str(ei.value)


def test_comic_info_web_attaches_chapters(adapter, monkeypatch):
    monkeypatch.setattr(cw, "web_detail",
                        lambda cid: cm.ComicDetails(id=cid, title="神明與初戀"))
    monkeypatch.setattr(cw, "web_chapters",
                        lambda cid: [cm.Chapter(id="u1", name="第1話", group="默認")])
    monkeypatch.setattr(cm.CopyManga, "_app_comic_info",
                        lambda *a, **k: pytest.fail("网页通道成功时不得回落 APP"))
    d = adapter.comic_info("smycl")
    assert d.title == "神明與初戀" and d.sub_title == "拷贝漫画"
    assert [c.id for c in d.chapters] == ["u1"]


def test_comic_info_falls_back_on_web_failure(adapter, monkeypatch):
    monkeypatch.setattr(cw, "web_detail",
                        lambda cid: (_ for _ in ()).throw(cw.WebError("结构变更")))
    monkeypatch.setattr(cm.CopyManga, "_app_comic_info",
                        lambda self, cid: cm.ComicDetails(id=cid, title="APP"))
    assert adapter.comic_info("x").title == "APP"


def test_fetch_chapters_prefers_web_and_chapters_alias(adapter, monkeypatch):
    monkeypatch.setattr(cw, "web_chapters",
                        lambda cid: [cm.Chapter(id="u9", name="第9話")])
    monkeypatch.setattr(cm.CopyManga, "_app_fetch_chapters",
                        lambda *a, **k: pytest.fail("不得回落 APP"))
    assert [c.id for c in adapter.fetch_chapters("smycl")] == ["u9"]
    assert [c.id for c in adapter.chapters("smycl")] == ["u9"]   # 基类别名同通道


def test_images_prefers_web_uuid_channel(adapter, monkeypatch):
    seen = []

    def _wi(cid, cht):
        seen.append((cid, cht))
        return ["https://sj.mangafunb.fun/j/x/1/001.jpg"]
    monkeypatch.setattr(cw, "web_images", _wi)
    monkeypatch.setattr(cm.CopyManga, "_app_images",
                        lambda *a, **k: pytest.fail("不得回落 APP"))
    out = adapter.images("smycl", "11111111-1111-1111-1111-111111111111")
    assert out and seen == [("smycl", "11111111-1111-1111-1111-111111111111")]


def test_unreachable_is_bounded_by_budget(monkeypatch):
    """域池全不可达时**必须有界**：预算是硬约束。

    背景（0.74.1 真机事故）：网页通道失败后回落到 APP 通道，而 APP 通道是
    "7 域 × 重试 3 次 × 15 秒"的长链——单源搜索能挂十几分钟，App 的 30 秒客户端
    超时先炸，用户看到的是"**所有源都超时**"。修法一是给网页通道设总预算、
    二是"不可达"不再回落 APP。本用例锁死第一条。
    """
    import time as _t
    monkeypatch.setenv("WR_COPY_WEB_BUDGET", "1")
    monkeypatch.setattr(cw, "_BASE", {"base": "", "ts": 0.0})
    monkeypatch.setattr(cw, "_NEG", {"ts": 0.0, "msg": ""}, raising=False)
    monkeypatch.setattr(cw, "_domain_pool", lambda: ["https://a.invalid", "https://b.invalid"])

    calls = []

    def _slow(url, headers=None, timeout=None, retries=0, budget=None):
        calls.append(url)
        _t.sleep(0.4)                     # 每次请求都慢，但远小于真实 15 秒超时
        raise cw.WebUnreachable("ConnectTimeout: 模拟不可达")
    monkeypatch.setattr(cw, "_web_get", _slow)

    t0 = _t.time()
    with pytest.raises(cw.WebUnreachable):
        cw.web_search("剑来")
    dt = _t.time() - t0
    assert dt < 4, f"域池不可达必须限时收尾，实际 {dt:.1f}s（调用 {len(calls)} 次）"
    assert len(calls) <= 4, f"不该无限轮换域：{calls}"


def test_negative_cache_stops_repeat_probing(monkeypatch):
    """整池失败后的短时间负缓存：连点两次搜索不该白等两个预算。"""
    monkeypatch.setenv("WR_COPY_WEB_BUDGET", "1")
    monkeypatch.setattr(cw, "_BASE", {"base": "", "ts": 0.0})
    monkeypatch.setattr(cw, "_NEG", {"ts": 0.0, "msg": ""}, raising=False)
    monkeypatch.setattr(cw, "_domain_pool", lambda: ["https://a.invalid"])
    n = []

    def _fail(url, headers=None, timeout=None, retries=0, budget=None):
        n.append(url)
        raise cw.WebUnreachable("ConnectTimeout: 模拟不可达")
    monkeypatch.setattr(cw, "_web_get", _fail)

    with pytest.raises(cw.WebUnreachable):
        cw.web_search("剑来")
    assert n, "第一次应当真的去探过"
    first = len(n)
    with pytest.raises(cw.WebUnreachable) as ei:
        cw.web_search("斗破苍穹")
    assert len(n) == first, "负缓存生效时不该再发探测请求"
    assert "不再重试" in str(ei.value)


def test_unreachable_does_not_fall_back_to_app(adapter, monkeypatch):
    """**网络不可达**时不得回落 APP 通道（这是真机事故的直接成因）。"""
    monkeypatch.setattr(cw, "web_detail",
                        lambda cid: (_ for _ in ()).throw(
                            cw.WebUnreachable("域池全部失败: ConnectTimeout")))
    monkeypatch.setattr(cm.CopyManga, "_app_comic_info",
                        lambda *a, **k: pytest.fail("不可达时不得回落 APP（会挂十几分钟）"))
    with pytest.raises(cm.MangaError) as ei:
        adapter.comic_info("x")
    assert "连不上拷贝漫画站点" in str(ei.value)
    assert "www.copy4000.com" in str(ei.value), "错误信息要给出可行动目标"


def test_web_channel_does_not_touch_app_version_or_domain(adapter, monkeypatch):
    """网页通道**不得**触发 APP 侧的"官网版本发现 / API 域刷新"。

    手机实测：构造适配器时就去官网解析版本号 + 刷 API 域，会让首次搜索白等 1~2 秒
    且多发两个请求——而网页通道根本不需要 APP 版本与 API 域名。
    所以这两件事必须**懒取**，只在真正要发 APP 请求时才发生。
    """
    called = []
    monkeypatch.setattr(cm, "_fetch_latest_version",
                        lambda: (called.append("version"), "9.9.9")[1])
    monkeypatch.setattr(cm.CopyManga, "_refresh_api",
                        lambda self: called.append("domain"))
    monkeypatch.setattr(cw, "web_search",
                        lambda *a, **k: ([cm.Comic(id="pw", title="T")], 1))
    monkeypatch.setenv("WR_COPY_TRANSPORT", "web")

    ad = cm.CopyManga()                       # 构造本身不该打网络
    assert called == [], f"构造期不得请求官网/域名：{called}"
    ad.search("剑来")
    assert called == [], f"网页通道不得触发 APP 侧请求：{called}"

    # 对照：APP 通道**必须**照旧懒取（用到了才取，且只取一次）
    monkeypatch.setattr(cm.CopyManga, "_app_search",
                        lambda self, kw, pg=1: [])
    cm.CopyManga()._app_search("x")
    ad2 = cm.CopyManga()
    ad2._ensure_api()
    ad2._ensure_api()
    assert called.count("domain") == 1, f"API 域刷新应每次实例一次：{called}"


def test_blocked_does_not_fall_back_to_app(adapter, monkeypatch):
    """被 403/Cloudflare 拦截时**不得**回落 APP：这个 IP 已经被盯上，
    再敲 APP 接口等于把 R59「不再出现 APP 请求特征」的约定破坏掉，只会更重。"""
    monkeypatch.setattr(cw, "web_detail",
                        lambda cid: (_ for _ in ()).throw(cw.WebBlocked("HTTP 403")))
    monkeypatch.setattr(cm.CopyManga, "_app_comic_info",
                        lambda *a, **k: pytest.fail("被拦截时不得回落 APP"))
    with pytest.raises(cm.MangaError) as ei:
        adapter.comic_info("x")
    assert "拦截" in str(ei.value)


def test_images_falls_back_to_app(adapter, monkeypatch):
    monkeypatch.setattr(cw, "web_images",
                        lambda *a, **k: (_ for _ in ()).throw(cw.WebError("contentKey 为空")))
    monkeypatch.setattr(cm.CopyManga, "_app_images",
                        lambda self, cid, cht: ["https://app.example/1.jpg"])
    assert adapter.images("x", "u1") == ["https://app.example/1.jpg"]


def test_app_path_entry_points_unchanged(adapter, monkeypatch):
    """重命名只发生在内部：`_app_*` 是原实现，公开方法仍是四个入口"""
    for name in ("_app_search", "_app_comic_info", "_app_fetch_chapters", "_app_images"):
        assert callable(getattr(cm.CopyManga, name))
    # 210 快速失败语义（阅读通道）仍在 _get 里
    import inspect
    src = inspect.getsource(cm.CopyManga._get)
    assert "210" in src and "cooldown" in src
