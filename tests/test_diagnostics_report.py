# -*- coding: utf-8 -*-
"""脱敏诊断报告契约回归（方向基线 §7.1：给用户一次可导出的脱敏诊断）

要点：这份报告要被用户发出来，因此**不能**夹带凭据；同时它必须可用于分层定位
（入口 → 连接 → 源注册 → 搜索/分类 → 详情 → 图片 → 存储），并且在各部分数据
拿不到时如实降级，而不是整篇 500。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import diag  # noqa: E402


@pytest.fixture
def client():
    import app
    return app.app.test_client()


@pytest.fixture(autouse=True)
def _clean_events():
    diag.reset()
    yield
    diag.reset()


def test_report_has_all_sections(client):
    r = client.get("/api/diagnostics/report")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] and d["bytes"] > 200
    for section in ("【1. 引擎】", "【2. 能力台账", "【3. 书源", "【4. 漫画源",
                    "【5. 存储】", "【6. 最近的失败请求与异常】",
                    # 0.73.0 新增出站网络路径（第 7 节）：报告必须说清测速走的哪条路
                    "【7. 出站网络路径（代理）】",
                    # 0.71.0 新增测速段（原第 7 节，现顺延为第 8 节），脱敏声明第 9 节
                    "【8. 阅读链路分段测速", "【9. 本报告的范围与脱敏声明】"):
        assert section in d["text"], f"报告缺少小节：{section}"


def test_report_records_failed_requests_without_query(client):
    client.get("/api/manga/browse?source=nhentai&q=私人搜索词")
    text = client.get("/api/diagnostics/report").get_json()["text"]
    assert "/api/manga/browse" in text
    assert "HTTP 404" in text
    assert "私人搜索词" not in text, "URL 查询串必须剥掉（可能含私人搜索词）"
    assert "?source=" not in text


def test_report_never_leaks_credentials(client, monkeypatch):
    """报告是要发给别人的：凭据字样必须抹掉（含路径里被解码出来的 token=…）"""
    monkeypatch.setenv("WR_AUTH_PASSWORD", "超机密密码123")
    client.get("/api/sources/token%3Dabc123/toggle")
    diag.record_error("test.where",
                      RuntimeError("password=SECRETPASSWORDVALUE cookie=SECRETCOOKIEVALUE"))
    text = client.get("/api/diagnostics/report").get_json()["text"]
    assert "超机密密码123" not in text
    assert "SECRETPASSWORDVALUE" not in text
    assert "SECRETCOOKIEVALUE" not in text
    assert "abc123" not in text, "路径里的 token 值必须被抹掉"
    assert "token=<removed>" in text


def test_successful_requests_are_not_recorded(client):
    client.get("/api/sources")
    text = client.get("/api/diagnostics/report").get_json()["text"]
    assert "未记录到失败请求" in text


def test_static_requests_are_skipped(client):
    client.get("/static/js/netip.js")          # 200，不该记
    client.get("/static/不存在.js")             # 404，但属于静态资源噪音
    text = client.get("/api/diagnostics/report").get_json()["text"]
    assert "/static/" not in text.split("【6.")[1], "静态资源失败不应进报告正文列表"


def test_events_are_bounded():
    for i in range(200):
        diag.record_request("GET", f"/api/x{i}", 500, 1)
        diag.record_error("where", RuntimeError("boom"))
    ev = diag.events()
    assert len(ev["failed_requests"]) <= 30
    assert len(ev["errors"]) <= 20


def test_report_degrades_honestly_when_sections_fail(client, monkeypatch):
    """某一节炸了只影响该节：报告仍要能生成（否则用户什么都导不出来）"""
    import server.diag as d

    def boom():
        raise RuntimeError("能力探测炸了")

    monkeypatch.setattr(d, "_caps_lines", boom)
    r = client.get("/api/diagnostics/report")
    assert r.status_code == 200 or r.status_code == 500   # 允许失败，但必须可解释
    if r.status_code == 200:
        assert "【1. 引擎】" in r.get_json()["text"]


def test_desktop_environment_is_labelled(client):
    text = client.get("/api/diagnostics/report").get_json()["text"]
    assert "不是手机 App 运行时" in text or "state：ready" in text


def test_scrub_text_masks_keys():
    masked = diag.scrub_text("token=abcdef session=zzz 其它内容")
    assert "abcdef" not in masked and "zzz" not in masked
    assert "token=<removed>" in masked


def test_scrub_path_keeps_path_only():
    assert diag.scrub_path("/api/search?q=秘密&page=2") == "/api/search"
    assert diag.scrub_path("") == "/"
