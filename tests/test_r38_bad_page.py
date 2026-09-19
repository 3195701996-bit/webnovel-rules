# -*- coding: utf-8 -*-
"""R38: 源站坏页/占位图识别——不重试、不误导任务状态"""
import pytest


def test_bad_image_error_class():
    """BadImageError 存在且继承 MangaError（永久失败类）"""
    from engine.manga.downloader import BadImageError
    from engine.manga.base import MangaError
    assert issubclass(BadImageError, MangaError)


def test_downloader_no_retry_on_bad_image(monkeypatch, tmp_path):
    """坏页不重试:直接抛 BadImageError,不进入重试循环"""
    from engine.manga.downloader import ImageDownloader, BadImageError
    import engine.manga.downloader as dl_mod

    class FakeAdapter:
        concurrent = 2
        name = "fake"
        def image_headers(self, u): return {}
        def image_url(self, u): return u
        def on_image_failed(self, u): return None

    calls = {"n": 0}

    class FakeResp:
        status_code = 200
        headers = {"Content-Type": "image/webp"}
        content = b"RIFF" + b"\x00" * 60  # 64B < 500B 占位

    class FakeSession:
        def get(self, *a, **k):
            calls["n"] += 1
            return FakeResp()

    import threading
    # 本用例验证"坏页不重试"，与 DNS 连接层绑定无关：显式短路 pin 函数
    # （fake Session 仅实现 get，无 mount/adapters，不应被要求伪装安全能力）。
    monkeypatch.setattr(dl_mod, "pin_curl_session", lambda *a, **k: False)
    monkeypatch.setattr(dl_mod, "_session_pool", [FakeSession()])
    monkeypatch.setattr(dl_mod, "_session_pool_locks", [threading.Lock()])
    monkeypatch.setattr(dl_mod, "_SESSION_POOL_SIZE", 1)
    dl = ImageDownloader(FakeAdapter(), str(tmp_path), min_interval=0.0)
    with pytest.raises(BadImageError):
        dl._download("http://x/y.webp", str(tmp_path / "bad.webp"), 10)
    assert calls["n"] == 1, "坏页只请求一次,不应重试"
