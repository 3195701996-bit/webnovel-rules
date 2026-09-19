# -*- coding: utf-8 -*-
"""0.70.0 回归（离线）：拷贝漫画"能否移植到 Android"的三处结论性改动。

背景（2026-09-17 实测，见 `03-验证结果/拷贝漫画移植研究-2026-09-17.md`）：

1. **"必须 curl_cffi 的 Chrome TLS 指纹"这个前提不成立**：
   走本地代理、用**纯 requests（无任何指纹伪装）**访问
   `/api/v3/search/comic` → HTTP 200 正常 JSON。
   此前的失败是"域名过期（api.mangacopy.com → 现为 copy4000.com 系）+ APP 通道
   210 风控"，与 TLS 指纹无关。于是 Android（无 curl_cffi wheel）也能走同一通道：
   `_cp_request()` 在无 curl_cffi 时降级到 requests（每线程会话 + 连接池）。

2. **每请求轮换设备指纹正是"破解版客户端"的特征**：源站 210 原文点名
   "您或您身邊的人曾經下載過破解版本的拷貝漫畫…等待1小時"。
   默认改为进程内**稳定指纹**（`WR_COPY_ROTATE_FP=1` 才轮换）。

3. **210 原文必须透出**：用户要能看懂"是源站限制、约 1 小时自动解除"，
   而不是一句"请求失败"。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.manga.copymanga as cm  # noqa: E402


def test_transport_falls_back_to_requests_without_curl(monkeypatch):
    """没有 curl_cffi（Android）时必须能用 requests，且**复用会话**（keep-alive）。"""
    monkeypatch.setattr(cm, "_CP_HAS_CURL", False, raising=False)
    monkeypatch.setattr(cm, "_cp_local", type("L", (), {})(), raising=False)

    seen = []

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {}

    class _Sess:
        proxies = {}

        def mount(self, *a, **k):        # requests.Session 的接口
            return None

        def get(self, url, headers=None, timeout=None, proxies=None):
            seen.append((id(self), url, dict(headers or {})))
            return _Resp()

    import requests
    monkeypatch.setattr(requests, "Session", lambda: _Sess(), raising=False)

    r1 = cm._cp_request("https://api.example/x", headers={"A": "1"}, timeout=5)
    r2 = cm._cp_request("https://api.example/y", headers={"A": "1"}, timeout=5)
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(seen) == 2
    assert seen[0][0] == seen[1][0], "两次请求必须复用同一个会话（否则每请求重付握手）"
    assert seen[0][2]["A"] == "1"


def test_fingerprint_is_stable_by_default(monkeypatch):
    """默认不再每请求轮换指纹（210 原文把'频繁换指纹'指向破解版客户端）。"""
    monkeypatch.delenv("WR_COPY_ROTATE_FP", raising=False)
    ad = cm.CopyManga()
    assert ad._should_rotate_fp() is False
    first = ad.headers
    second = ad.headers
    assert first.get("deviceinfo") and first.get("deviceinfo") == second.get("deviceinfo")
    # 显式开启时才轮换
    monkeypatch.setenv("WR_COPY_ROTATE_FP", "1")
    assert ad._should_rotate_fp() is True


def test_210_message_is_surfaced_to_caller(monkeypatch):
    """210（破解版标记）原文要带给用户，并说明约 1 小时后自动解除。"""
    ad = cm.CopyManga()
    ad._throttle = False                      # 阅读通道：快速失败，不做重试链
    msg = ("請到官網更新最新APP，您或您身邊的人曾經下載過破解版本的拷貝漫畫，"
           "請下載安裝正版之後等待1小時，限制會自動解除。")

    class _Resp:
        status_code = 210

        def json(self):
            return {"message": msg}

        text = msg

    class _Sess:
        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(cm, "_get_session", lambda: _Sess(), raising=False)
    with pytest.raises(cm.MangaError) as ei:
        ad._get("https://api.example/api/v3/comic2/x?platform=3")
    err = str(ei.value)
    assert "破解" in err, err
    assert "1 小时" in err or "1小時" in err, err
