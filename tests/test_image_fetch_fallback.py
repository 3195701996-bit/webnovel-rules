# -*- coding: utf-8 -*-
"""图片通道 requests 降级回归（Android 无 curl_cffi 时的唯一合法替代）

设计 §4：若原生 curl-cffi 不通过验收，**显式**走 requests/cloudscraper 降级路径，
并在能力清单里说明受影响源；**禁止**创建同名 curl_cffi shim 冒充依赖、
禁止用 verify=False / 关 DNS 防护 / 自动跟随未校验重定向换取"能联网"。

因此本文件验证降级路径与 curl 路径**同一套安全契约**：
  1. 每跳（含首跳）都过 url_is_public_resolved；
  2. 每跳都尝试受控 IP 绑定（pin），绑定不了就 PinUnavailable，绝不静默不绑定；
  3. 关闭自动重定向，手动逐跳跟随且逐跳校验（跳数上限）；
  4. TLS 校验保持默认（不做关闭开关）；
  5. 成功路径返回与 curl 响应对调用方同构的对象（status_code/headers/content）。
"""
import http.server
import json
import os
import subprocess
import sys
import threading
import urllib.parse

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.manga import downloader as DL          # noqa: E402
from engine.urlsec import SSRFBlocked, PinUnavailable   # noqa: E402

IMG = b"\xff\xd8\xff\xe0" + b"P" * 2048            # 最小 JPEG 头 + 填充


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/img.jpg":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(IMG)))
            self.end_headers()
            self.wfile.write(IMG)
        elif u.path == "/hop":
            self.send_response(302)
            self.send_header("Location", "/img.jpg")
            self.end_headers()
        elif u.path == "/to-private":
            self.send_response(302)
            self.send_header("Location", "http://10.0.0.1/secret.jpg")
            self.end_headers()
        elif u.path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture()
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture()
def fallback(monkeypatch):
    """强制走 requests 降级路径；并把 SSRF 判定替换为"仅允许 127.0.0.1"。"""
    pins = []

    def _public(u):
        host = urllib.parse.urlparse(u).hostname or ""
        if host in ("127.0.0.1", "localhost"):
            return True, ""
        return False, f"非公网地址({host})"

    monkeypatch.setattr(DL, "_curl_engine_available", lambda: False)
    monkeypatch.setattr(DL, "url_is_public_resolved", _public)
    monkeypatch.setattr(DL, "pin_requests_session",
                        lambda sess, url, proxy=None: (pins.append(url), True)[1])
    return {"pins": pins}


def test_fallback_fetches_image_and_pins_every_hop(server, fallback):
    r = DL.fetch_image_checked(f"{server}/hop", {"User-Agent": "t"}, timeout=5)
    assert r.status_code == 200
    assert r.content == IMG
    assert r.headers.get("Content-Type") == "image/jpeg"
    # 首跳 + 重定向后那一跳都必须尝试绑定（不是只绑首跳）
    assert len(fallback["pins"]) == 2, fallback["pins"]
    assert fallback["pins"][0].endswith("/hop")
    assert fallback["pins"][1].endswith("/img.jpg")


def test_fallback_blocks_redirect_to_private(server, fallback):
    """302 跳内网必须被拒（降级路径不得因为"能连上"就放过）"""
    with pytest.raises(SSRFBlocked) as e:
        DL.fetch_image_checked(f"{server}/to-private", {}, timeout=5)
    assert "第1跳重定向" in str(e.value)


def test_fallback_blocks_private_target_first_hop(fallback):
    with pytest.raises(SSRFBlocked):
        DL.fetch_image_checked("http://10.0.0.1/x.jpg", {}, timeout=5)


def test_fallback_redirect_loop_is_bounded(server, fallback):
    with pytest.raises(SSRFBlocked) as e:
        DL.fetch_image_checked(f"{server}/loop", {}, timeout=5)
    assert "重定向次数超过上限" in str(e.value)


def test_fallback_refuses_when_pin_unavailable(server, monkeypatch):
    """绑定不了（如经代理）→ 明确 PinUnavailable，绝不静默未绑定直连"""
    monkeypatch.setattr(DL, "_curl_engine_available", lambda: False)
    monkeypatch.setattr(DL, "url_is_public_resolved", lambda u: (True, ""))
    monkeypatch.setattr(DL, "pin_requests_session",
                        lambda sess, url, proxy=None: False)
    with pytest.raises(PinUnavailable):
        DL.fetch_image_checked(f"{server}/img.jpg", {}, timeout=5)


def test_fallback_session_disables_env_proxy():
    """降级会话必须 trust_env=False：绑定语义要求直连，不能悄悄走环境/系统代理"""
    sess = DL._requests_session()
    assert sess.trust_env is False
    assert DL._requests_session() is sess, "同线程应复用同一会话（保持 keep-alive）"


def test_curl_engine_still_preferred_when_available(server, monkeypatch):
    """curl 可用时必须优先走 curl（桌面行为不变）"""
    called = {"req": 0}
    monkeypatch.setattr(DL, "_curl_engine_available", lambda: True)
    monkeypatch.setattr(DL, "_fetch_image_checked_requests",
                        lambda *a, **k: called.__setitem__("req", called["req"] + 1))
    monkeypatch.setattr(DL, "url_is_public_resolved", lambda u: (True, ""))
    monkeypatch.setattr(DL, "pin_curl_session", lambda sess, url, proxy=None: True)

    class _Resp:
        status_code = 200
        headers = {}
        content = IMG

    class _Sess:
        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(DL, "_acquire_session", lambda timeout=None: (_Sess(), threading.Lock()))
    monkeypatch.setattr(DL, "_release_session", lambda lock: None)
    r = DL.fetch_image_checked(f"{server}/img.jpg", {}, timeout=5)
    assert r.status_code == 200 and called["req"] == 0, "curl 可用时不得走降级路径"


def test_android_shape_subprocess_fetches_image_without_curl(server, tmp_path):
    """真 Android 形态：拦截 curl_cffi/playwright 后，图片通道仍能取回图片。

    这条是阶段 C 的关键证据——阶段 A 已证 curl_cffi 在 Android 无可用 wheel，
    若没有降级路径，所有漫画源的"在线阅读/下载图片"都不可用（能力台账里的
    degraded/unsupported 就是这么来的）。
    """
    blocker = tmp_path / "block_optional.py"
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
        "import block_optional\n"
        "import sys, json\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "import engine.urlsec as US\n"
        "import engine.manga.downloader as DL\n"
        "US.url_is_public_resolved = lambda u: (True, '')\n"
        "DL.url_is_public_resolved = lambda u: (True, '')\n"
        "DL.pin_requests_session = lambda sess, url, proxy=None: True\n"
        "from engine.manga.downloader import fetch_image_checked\n"
        f"r = fetch_image_checked({server + '/img.jpg'!r}, {{}}, timeout=10)\n"
        "print(json.dumps({'engine_curl': DL._curl_engine_available(),\n"
        "                  'status': r.status_code, 'bytes': len(r.content),\n"
        "                  'is_jpeg': r.content[:2] == b'\\xff\\xd8'}))\n"
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("WR_DISABLE_BACKGROUND", "WR_PROFILE")}
    env["PYTHONPATH"] = str(tmp_path)
    env["WR_PROFILE"] = "mobile"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       timeout=180, cwd=ROOT, env=env)
    assert r.returncode == 0, r.stderr[-1200:]
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert d["engine_curl"] is False, "本用例必须在无 curl_cffi 的前提下有意义"
    assert d["status"] == 200 and d["is_jpeg"], d
    assert d["bytes"] == len(IMG)
