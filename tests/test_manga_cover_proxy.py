# -*- coding: utf-8 -*-
"""漫画封面代理（GET /api/manga/cover）契约回归（离线：不触网）

为什么需要这个端点（真实故障，不是想象）：
  - 搜索/浏览出来的漫画**还没进书库**，per-comic 封面接口只查书库记录 → 404；
  - 禁漫图床校验 Referer：实测无 Referer 403、带 Referer 200（48777B），
    所以 App 直连源站封面地址也不行。
因此由服务端带适配器自己的 image_headers 取一次；这里把口径钉住：
只允许 http(s)、未知源 404、SSRF 直接拒绝（不绕过）、非图片/过大一律 502、
不落服务端缓存（交给客户端 HTTP 缓存）。
"""
import io
import os
import time
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.urlsec import SSRFBlocked  # noqa: E402

JPEG = b"\xff\xd8\xff" + b"\x00" * 2048          # 魔数正确、长度足够


class _FakeResp:
    def __init__(self, status=200, content=JPEG):
        self.status_code = status
        self.content = content


class _FakeAdapter:
    """记录 image_headers 被问到的 URL（禁漫就是靠 Referer 才拿得到图）"""

    def __init__(self, headers=None, boom=False):
        self.seen = []
        self._headers = headers if headers is not None else {
            "User-Agent": "UA", "Referer": "https://localhost/"}
        self._boom = boom

    def image_headers(self, url):
        self.seen.append(url)
        if self._boom:
            raise RuntimeError("取防盗链头炸了")
        return dict(self._headers)


@pytest.fixture(autouse=True)
def _isolate_cover_cache(monkeypatch, tmp_path):
    """每个用例一个空的封面代理缓存目录。

    0.68.0 起该端点带**有界磁盘缓存**（翻页/回封面不再重复回源）；本文件的历史
    用例都复用同一个 URL，若不隔离就会出现"上一条用例把图缓存住了，这一条拿不到
    502/不再回源"的假失败（实测踩到 4 条）。缓存目录是模块级变量、调用时才读，
    所以这里直接改它即可。"""
    import server.manga_api as ma
    monkeypatch.setattr(ma, "_COVER_PROXY_DIR", str(tmp_path / "covers"), raising=False)
    # 并发闸/单飞状态也要清干净，避免用例之间互相影响
    monkeypatch.setattr(ma, "_cover_proxy_flight", {}, raising=False)


@pytest.fixture
def client():
    import app
    return app.app.test_client()


def _patch_adapter(monkeypatch, adapter):
    """直接替掉路由实际调用的那两个函数。

    不要 patch engine.manga.manager.get_adapter：别的测试会把假适配器注册进
    全局注册表，套件整体跑时 `_manga_read_adapter` 可能先命中那些残留，
    于是本用例单独跑通过、整体跑失败（实测踩到，正是"测试之间互相污染"）。
    """
    import server.manga_api as ma
    monkeypatch.setattr(ma, "_manga_read_adapter", lambda k: adapter)
    monkeypatch.setattr(ma, "_manga_adapter", lambda k: adapter)


def _patch_fetch(monkeypatch, resp=None, exc=None, calls=None):
    import engine.manga.downloader as dl

    def fake(url, headers, timeout=20, **kw):
        if calls is not None:
            calls.append((url, dict(headers)))
        if exc is not None:
            raise exc
        return resp

    monkeypatch.setattr(dl, "fetch_image_checked", fake, raising=True)


def test_missing_url_400(client):
    r = client.get("/api/manga/cover?source=jm")
    assert r.status_code == 400
    assert "http" in r.get_json()["error"]


def test_non_http_url_400(client):
    r = client.get("/api/manga/cover?source=jm&url=file:///etc/passwd")
    assert r.status_code == 400


def test_unknown_source_404(client, monkeypatch):
    _patch_adapter(monkeypatch, None)
    r = client.get("/api/manga/cover?source=不存在&url=https://cdn.example.com/a.jpg")
    assert r.status_code == 404


def test_success_uses_source_headers(client, monkeypatch):
    ad = _FakeAdapter()
    _patch_adapter(monkeypatch, ad)
    calls = []
    _patch_fetch(monkeypatch, resp=_FakeResp(), calls=calls)
    r = client.get("/api/manga/cover?source=jm" +
                   "&url=https%3A%2F%2Fcdn.example.com%2Fa.jpg")
    assert r.status_code == 200, r.data[:200]
    assert r.headers["Content-Type"].startswith("image/jpeg")
    assert r.headers["Cache-Control"].startswith("public, max-age=")
    assert calls and calls[0][1].get("Referer") == "https://localhost/"   # 防盗链头带上
    assert ad.seen == ["https://cdn.example.com/a.jpg"]


def test_ssrf_blocked_is_502_not_bypass(client, monkeypatch):
    _patch_adapter(monkeypatch, _FakeAdapter())
    _patch_fetch(monkeypatch, exc=SSRFBlocked("内网地址"))
    r = client.get("/api/manga/cover?source=jm&url=http%3A%2F%2F127.0.0.1%2Fa.jpg")
    assert r.status_code == 502
    assert r.get_json()["error"]


def test_source_error_502_readable(client, monkeypatch):
    _patch_adapter(monkeypatch, _FakeAdapter())
    _patch_fetch(monkeypatch, resp=_FakeResp(status=403, content=b"forbidden"))
    r = client.get("/api/manga/cover?source=jm&url=https%3A%2F%2Fcdn.example.com%2Fa.jpg")
    assert r.status_code == 502
    assert "403" in r.get_json()["error"]


def test_html_error_page_is_not_an_image(client, monkeypatch):
    """源站风控常返回 200 + HTML：不能当图片返回（更不能当封面显示）"""
    _patch_adapter(monkeypatch, _FakeAdapter())
    _patch_fetch(monkeypatch, resp=_FakeResp(content=b"<html>" + b"x" * 4096))
    r = client.get("/api/manga/cover?source=jm&url=https%3A%2F%2Fcdn.example.com%2Fa.jpg")
    assert r.status_code == 502
    assert "不是图片" in r.get_json()["error"]


def test_tiny_body_502(client, monkeypatch):
    _patch_adapter(monkeypatch, _FakeAdapter())
    _patch_fetch(monkeypatch, resp=_FakeResp(content=b"\xff\xd8\xff" + b"x" * 10))
    r = client.get("/api/manga/cover?source=jm&url=https%3A%2F%2Fcdn.example.com%2Fa.jpg")
    assert r.status_code == 502


def test_image_headers_failure_still_fetches(client, monkeypatch):
    """image_headers 抛异常不能把接口带崩（改用默认 UA 继续取图）"""
    _patch_adapter(monkeypatch, _FakeAdapter(boom=True))
    calls = []
    _patch_fetch(monkeypatch, resp=_FakeResp(), calls=calls)
    r = client.get("/api/manga/cover?source=jm&url=https%3A%2F%2Fcdn.example.com%2Fa.jpg")
    assert r.status_code == 200
    assert calls[0][1].get("User-Agent")


def test_library_cover_url_falls_back_to_detail_cache(tmp_path, monkeypatch):
    """/api/manga/<源>/<id>/cover 也要能用详情缓存里的封面地址。

    否则"搜索进详情"的漫画（还没加入书库）永远显示无封面。"""
    import server.manga_api as ma
    cover = "https://cdn.example.com/cover.jpg"
    info = os.path.join(str(tmp_path), "_cache", "jm", "123", "_info_full.json")
    os.makedirs(os.path.dirname(info), exist_ok=True)
    with io.open(info, "w", encoding="utf-8") as f:
        f.write('{"ts": 1, "data": {"cover": "%s"}}' % cover)
    monkeypatch.setattr(ma, "MANGA_DIR", str(tmp_path))
    monkeypatch.setattr(ma, "MANGA_LIBRARY_FILE", os.path.join(str(tmp_path), "none.json"))
    assert ma._library_cover_url("jm", "123") == cover
    assert ma._library_cover_url("jm", "999") == ""


# ── 0.68.0 新契约：缓存命中不再回源 / 并发闸不堵死引擎 / 同 URL 单飞 ──────────

def test_second_request_is_served_from_cache_without_refetch(client, monkeypatch):
    """同一封面第二次请求必须走缓存（翻页回来不再回源、也不占引擎线程）。"""
    _patch_adapter(monkeypatch, _FakeAdapter())
    calls = []
    _patch_fetch(monkeypatch, resp=_FakeResp(), calls=calls)
    u = "/api/manga/cover?source=jm&url=https%3A%2F%2Fcdn.example.com%2Fcached.jpg"
    r1 = client.get(u)
    assert r1.status_code == 200
    n1 = len(calls)
    r2 = client.get(u)
    assert r2.status_code == 200
    assert r2.data == r1.data, "第二次应该是同一份字节（来自缓存）"
    assert len(calls) == n1, f"第二次不得再回源（实际又取了 {len(calls) - n1} 次）"


def test_gate_busy_returns_503_instead_of_blocking(client, monkeypatch):
    """回源闸被占满时必须**立刻让路**（503 且说明原因），而不是把引擎线程堵死。

    用户实测的"从第 2 页回第 1 页全面卡死"就是旧实现把 8 个 waitress 线程全堵在
    慢回源上造成的。"""
    import server.manga_api as ma
    _patch_adapter(monkeypatch, _FakeAdapter())
    _patch_fetch(monkeypatch, resp=_FakeResp())
    # 手动占满闸（3 个）
    acquired = [ma._COVER_PROXY_SEM.acquire(blocking=False) for _ in range(3)]
    assert all(acquired), "用例前提：闸容量应为 3"
    try:
        r = client.get("/api/manga/cover?source=jm&url=https%3A%2F%2Fcdn.example.com%2Fbusy.jpg")
        assert r.status_code == 503, r.data[:200]
        assert "忙" in r.get_json()["error"] or "排队" in r.get_json()["error"]
    finally:
        for _ in range(3):
            ma._COVER_PROXY_SEM.release()


def test_cache_is_bounded_by_file_count(client, monkeypatch, tmp_path):
    """缓存有界：超过上限按 mtime 淘汰最旧的（不能无界增长）。"""
    import server.manga_api as ma
    _patch_adapter(monkeypatch, _FakeAdapter())
    _patch_fetch(monkeypatch, resp=_FakeResp())
    monkeypatch.setattr(ma, "_COVER_PROXY_MAX_FILES", 3, raising=False)
    for i in range(5):
        client.get(f"/api/manga/cover?source=jm&url=https%3A%2F%2Fcdn.example.com%2Fb{i}.jpg")
    files = [n for n in os.listdir(str(tmp_path / "covers")) if n.endswith(".img")]
    assert len(files) <= 3, f"缓存文件数应被限制在 3，实际 {len(files)}"


def test_warm_proxy_covers_prefetches_and_is_bounded(client, monkeypatch, tmp_path):
    # 后台预热受 WR_BG_SEARCH_WARM 控制（测试环境默认 WR_DISABLE_BACKGROUND=1）：
    # 本用例测的正是"后台预热是否真的发生"，所以显式打开这个开关。
    monkeypatch.setenv("WR_BG_SEARCH_WARM", "1")
    """搜索返回后会**后台预热**本页封面（用户反馈"切页封面加载慢"）。

    这里验证预热函数本身：会把封面写进代理缓存、重复 URL 去重、且**有上限**。
    """
    import server.manga_api as ma
    _patch_adapter(monkeypatch, _FakeAdapter())
    calls = []
    _patch_fetch(monkeypatch, resp=_FakeResp(), calls=calls)
    urls = [f"https://cdn.example.com/w{i}.jpg" for i in range(20)]
    covers_dir = tmp_path / "covers"
    covers_dir.mkdir(exist_ok=True)          # 后台线程创建之前先建好，便于轮询
    ma._warm_proxy_covers("jm", urls)

    def _files():
        return [n for n in os.listdir(str(covers_dir)) if n.endswith(".img")]

    # 等后台线程把闸内的活干完（最多 3 并发，给足时间）
    for _ in range(60):
        if len(_files()) >= ma._COVER_WARM_PROXY_MAX or len(calls) >= ma._COVER_WARM_PROXY_MAX:
            break
        time.sleep(0.1)
    files = _files()
    assert len(files) <= ma._COVER_WARM_PROXY_MAX, f"预热必须有上限，实际 {len(files)}"
    assert len(calls) <= ma._COVER_WARM_PROXY_MAX, f"不得超额回源，实际 {len(calls)}"
    assert len(files) >= 1, "至少要预热到一张（后台线程）"
