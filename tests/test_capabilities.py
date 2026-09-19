# -*- coding: utf-8 -*-
"""能力台账回归（阶段 C：逐项 supported/degraded/unsupported + 原因）

设计依据：
- §11 阶段 C 退出条件：必须列出每个适配器的 supported/degraded/unsupported 及原因，
  "不能用首页能打开代替业务通过"；
- §4 禁止捷径：不得用同名 shim 冒充 curl-cffi，Playwright 第一版显式不支持且不静默换通道；
- B2：通道映射不得因依赖缺失而"静默换通道还假装原配置可用"。

覆盖：
  1. 依赖探测与降级判定（缺 curl_cffi → 图片不可用=degraded；缺 API 依赖=unsupported）；
  2. 源列表**能力感知**：网页通道不可用时不再隐藏 APP 通道，且逐源带状态与原因；
     网页通道可用时行为与改造前一致（桌面不变）；
  3. 子进程真实模拟"Android 无 curl_cffi"：import app 正常、health 给出缺失清单、
     源列表把依赖图片能力的源标为 degraded 而不是假装可用。
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import capabilities as cap            # noqa: E402

ALL_OK = {k: {"available": True, "detail": ""} for k in
          ("curl_cffi", "requests", "lxml", "pillow", "cryptography",
           "playwright", "playwright_browser")}


def _caps(**missing):
    c = {k: dict(v) for k, v in ALL_OK.items()}
    for k in missing:
        c[k] = {"available": False, "detail": f"模拟缺少 {k}"}
    return c


# ── 1. 判定矩阵 ──────────────────────────────────────────────
def test_all_available_is_supported():
    for k in ("jm", "copymanga", "copymanga_web", "mangadex", "nhentai", "komiic"):
        st = cap.source_status(k, _caps())
        assert st["status"] == "supported", (k, st)


def test_missing_image_dep_is_degraded_with_transport_reason():
    """缺 curl_cffi 时图片走 requests 降级 → degraded，且说明实际通道"""
    st = cap.source_status("jm", _caps(curl_cffi=""))
    assert st["status"] == "degraded", st
    assert "curl_cffi" in st["reason"] and "requests" in st["reason"], st["reason"]
    assert st["transport"]["image"] == "requests_fallback", st
    assert st["verified"] is False, "依赖级判定不等于功能验证"
    # curl_cffi 可用时运输通道应标为 curl_cffi
    assert cap.source_status("jm", _caps())["transport"]["image"] == "curl_cffi"


def test_copymanga_not_degraded_by_missing_curl_cffi():
    """拷贝漫画：**缺 curl_cffi 不再算降级**（0.74.0 起，有实测依据）。

    依据（2026-09-17 实测）：主通道是**纯 HTTP 网页通道**——搜索、
    详情（服务端渲染 HTML）、章节（AES-128-CBC，密钥取自页面内联脚本）、
    正文图片直链（真的下到 257KB JPEG）全部无需任何 TLS 指纹伪装。
    因此 image_soft 只是"可选加速"：缺它仍标 supported，且**不许**再写
    "已降级为 requests/可能被拒"（那会让用户在手机上怀疑一个能用的源）。

    注意这不等于"已验证"：本函数只做依赖级判定，`verified` 恒为 False，
    真正的可用性由「实测」一栏（verify.py 的四段结果）回答。
    """
    st = cap.source_status("copymanga", _caps(curl_cffi=""))
    assert st["status"] == "supported", st
    assert st["reason"] == "", st
    assert "降级" not in (st.get("note") or "")
    assert "requests" in st["transport"]["image"], st
    assert st["verified"] is False, "依赖级判定不等于功能验证"
    # 依赖缺一不可：真缺 requests/cryptography/lxml 时仍必须判不可用
    st3 = cap.source_status("copymanga", _caps(requests=""))
    assert st3["status"] == "unsupported", st3
    # 网页版（Playwright 渲染）仍依赖 playwright → 缺则不支持（这一条不变）
    st2 = cap.source_status("copymanga_web", _caps(playwright="", playwright_browser=""))
    assert st2["status"] == "unsupported", st2
    assert "playwright" in st2["reason"]


def test_other_sources_still_degraded_without_curl_cffi():
    """除已实测的源外，缺 curl_cffi 仍按"降级"如实标注（不扩大豁免范围）。"""
    for k in ("jm", "baozi", "nhentai", "mangadex"):
        st = cap.source_status(k, _caps(curl_cffi=""))
        assert st["status"] == "degraded", (k, st)
        assert "requests" in st["reason"], (k, st["reason"])


def test_unknown_source_is_pending_not_supported():
    """阶段 C 诊断第六节：未登记适配器不得默认 supported，应为 pending"""
    st = cap.source_status("no_such_source", _caps())
    assert st["status"] == "pending", st
    assert st["verified"] is False
    assert "功能验证" in st["reason"]


def test_summary_lists_missing_without_paths():
    s = cap.summary(_caps(curl_cffi="", playwright=""))
    assert "curl_cffi" in s["missing"] and "playwright" in s["missing"]
    assert s["deps"]["requests"] == "ok"
    txt = json.dumps(s, ensure_ascii=False)
    assert "/Users/" not in txt and "data/" not in txt, \
        "能力摘要不得泄露数据目录/凭据（设计 §5.3）"


# ── 2. 源列表能力感知（桌面行为不变 / 移动端如实标注）─────────
def _sources_payload(monkeypatch, caps):
    import app as APP
    from server import manga_api as MAP
    monkeypatch.setattr(cap, "probe", lambda force=False: caps)
    APP.app.config["TESTING"] = True
    r = APP.app.test_client().get("/api/manga/sources")
    assert r.status_code == 200
    return r.get_json()


def test_web_channel_available_keeps_desktop_behavior(monkeypatch):
    d = _sources_payload(monkeypatch, _caps())
    by = {s["key"]: s for s in d["sources"]}
    assert "copymanga" not in by, "网页通道可用时应与改造前一致（隐藏 APP 通道 key）"
    assert "copymanga_web" in by
    assert d["web_channel_available"] is True
    # 已登记能力需求的源在桌面（依赖齐全）应为 supported
    for k in ("copymanga_web", "jm", "mangadex", "nhentai"):
        assert by[k]["status"] == "supported", by[k]
    # 已注册的包子漫画也必须有依赖声明；依赖满足不等于已通过功能实测
    assert by["baozi"]["status"] == "supported", by["baozi"]
    assert by["baozi"]["verified"] is False
    assert by["jm"]["transport"]["image"] == "curl_cffi", by["jm"]


def test_web_channel_unavailable_reveals_app_channel_with_status(monkeypatch):
    """移动端第一版：无 Playwright → 不能藏起 APP 通道，也不能假装网页端可用"""
    d = _sources_payload(monkeypatch, _caps(playwright="", playwright_browser=""))
    by = {s["key"]: s for s in d["sources"]}
    assert d["web_channel_available"] is False
    assert "copymanga" in by, "网页通道不可用时应如实列出 APP 通道"
    assert by["copymanga_web"]["status"] == "unsupported", by["copymanga_web"]
    assert by["copymanga_web"]["reason"], "必须给出原因"
    # 只缺 playwright 时 APP 通道（copymanga）依赖 curl_cffi —— 它是**真的可用**，
    # 如实标 supported；这才是"能力台账"的意义（既不高报也不低报）。
    assert by["copymanga"]["status"] == "supported", by["copymanga"]


def test_no_curl_cffi_marks_image_dependent_sources_degraded(monkeypatch):
    d = _sources_payload(monkeypatch, _caps(curl_cffi=""))
    by = {s["key"]: s for s in d["sources"]}
    assert by["jm"]["status"] == "degraded", by["jm"]
    assert "curl_cffi" in by["jm"]["reason"] and "requests" in by["jm"]["reason"]
    assert d["capabilities"]["missing"] == ["curl_cffi"]


# ── 3. 子进程真实模拟 Android（无 curl_cffi）─────────────────
def test_android_like_runtime_reports_capabilities(tmp_path):
    # 真实 Android 环境：curl_cffi 无可用 wheel（阶段 A 结论），playwright 也不存在
    # （桌面专属，见设计 §4）。两个都阻断才是可信的移动端模拟——只阻断一个会得到
    # "网页通道仍可用"的桌面态结论。
    blocker = tmp_path / "no_curl_cffi.py"
    blocker.write_text(
        "import sys\n"
        "BLOCKED = ('curl_cffi', 'playwright')\n"
        "class B:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        for b in BLOCKED:\n"
        "            if name == b or name.startswith(b + '.'):\n"
        "                raise ImportError('模拟 Android：无 ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, B())\n", encoding="utf-8")
    code = (
        "import no_curl_cffi\n"
        "import sys, json\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "import app\n"
        "app._RUNTIME.start(wsgi_app=None, host='127.0.0.1', port=0,\n"
        "                   start_workers=False, profile='mobile')\n"
        "c = app.app.test_client()\n"
        "h = c.get('/api/health').get_json()\n"
        "s = c.get('/api/manga/sources').get_json()\n"
        "print(json.dumps({'ok': h['ok'], 'missing': h['capabilities']['missing'],\n"
        "  'profile': h['runtime']['profile'],\n"
        "  'workers': h['runtime']['workers'],\n"
        "  'jm': [x['status'] for x in s['sources'] if x['key'] == 'jm'],\n"
        "  'web_ok': s['web_channel_available']}))\n"
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("WR_DISABLE_BACKGROUND", "WR_PROFILE")}
    env["WR_PROFILE"] = "mobile"
    env["PYTHONPATH"] = str(tmp_path)          # 让阻断器模块可被导入
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       timeout=180, cwd=ROOT, env=env)
    assert r.returncode == 0, r.stderr[-1200:]
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert d["ok"] is True
    assert "curl_cffi" in d["missing"], d
    assert d["profile"] == "mobile"
    assert d["workers"] == [], "start_workers=False 时不得起线程"
    assert d["jm"] == ["degraded"], f"jm 缺 curl_cffi 应显式降级: {d}"
    assert d["web_ok"] is False, "无 playwright 时网页通道必须判为不可用"
    assert "playwright" in d["missing"], d


# ── 已知条件必须真的露到界面上（0.74.10）──────────────────────────────
# 缺陷：`conditional` 写得很细（源站迁域/挑战门、本机不可达、需代理），
# 但**从来没有任何界面读过它** —— 手机上只显示笼统的"缺 curl_cffi"，
# 用户看不出该去做什么。这些用例保证它一路走到 API。

def test_source_status_carries_conditional():
    st = cap.source_status("baozi", _caps(curl_cffi=""))
    assert st["conditional"], "source_status 必须把已知条件带出来"
    assert "挑战" in st["conditional"] or "迁域" in st["conditional"], st


def test_combined_reason_puts_condition_first_and_keeps_dependency():
    st = {"conditional": "源站已迁域并启用 JS 挑战门", "reason": "图片通道缺少 curl_cffi"}
    txt = cap.combined_reason(st)
    assert txt.startswith("源站已迁域"), "具体条件要排前面（那才是可行动的信息）"
    assert "curl_cffi" in txt, "依赖原因不能丢"
    assert cap.combined_reason({"conditional": "只有条件"}) == "只有条件"
    assert cap.combined_reason({"reason": "只有依赖"}) == "只有依赖"
    assert cap.combined_reason({}) == ""


def test_manga_sources_api_exposes_conditional(monkeypatch):
    """**App 走的就是这个接口**：源列表的说明里必须出现已知条件。

    之前这里直接用 `_st["reason"]`，把 conditional 丢掉了——
    于是"源站已迁域并启用挑战门"这类实测结论永远到不了用户眼前。
    """
    import server.manga_api as ma
    _real = cap.probe

    def _phone():
        c = _real()
        for k in ("playwright", "playwright_browser", "curl_cffi"):
            if k in c:
                c[k] = {"available": False, "detail": "模拟手机"}
        return c
    monkeypatch.setattr(cap, "probe", _phone)
    import app
    rows = {r["key"]: r for r in
            (app.app.test_client().get("/api/manga/sources").get_json() or {}).get("sources", [])}
    baozi = rows.get("baozi") or {}
    assert "挑战" in (baozi.get("reason") or ""), baozi.get("reason")
    nh = rows.get("nhentai") or {}
    assert "不可达" in (nh.get("reason") or "") or "超时" in (nh.get("reason") or ""), nh.get("reason")
