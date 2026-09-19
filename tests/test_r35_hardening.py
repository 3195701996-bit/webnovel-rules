# -*- coding: utf-8 -*-
"""R35 安全加固回归（离线：不触网、不写用户数据）

覆盖三项改动——均属"回归即静默失效"的高危区：
  1. engine/urlsec       SSRF 单一实现源
  2. source_mgr.import_sources / _search_one   书源导入 SSRF
  3. app._host_allowed / _same_origin_ok       CSRF + DNS rebinding
  4. main() 条件默认 host
"""
import os
import sys

import pytest

HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HUB)


@pytest.fixture(scope="module")
def app_mod():
    import app
    return app


@pytest.fixture(scope="module")
def client(app_mod):
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


# ══════════════════════════════════════════════
#  1. urlsec 单一实现源
# ══════════════════════════════════════════════

def test_urlsec_is_single_source_of_truth(app_mod):
    """app 的 SSRF 函数必须就是 engine.urlsec 的同一对象，杜绝逻辑漂移"""
    from engine import urlsec
    assert app_mod._url_is_public is urlsec.url_is_public
    assert app_mod._safe_target_url is urlsec.safe_target_url

    from engine import source_mgr
    assert source_mgr.url_is_public is urlsec.url_is_public


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
    "http://100.64.0.1/",                         # CGNAT
    "http://224.0.0.1/",                          # 组播
    "file:///etc/passwd",
    "ftp://example.com/x",
    "",
    None,
])
def test_urlsec_blocks_internal(url):
    from engine.urlsec import url_is_public
    assert url_is_public(url) is False


@pytest.mark.parametrize("url", [
    "https://www.example.com/book/1",
    "http://example.org:8080/x",
])
def test_urlsec_allows_public(url):
    from engine.urlsec import url_is_public
    assert url_is_public(url) is True


# ══════════════════════════════════════════════
#  2. 书源导入 SSRF
# ══════════════════════════════════════════════

@pytest.mark.parametrize("bad_url", [
    "http://127.0.0.1:8766/api/books",
    "http://192.168.1.1/admin",
    "http://169.254.169.254/latest/meta-data/",
    "http://localhost:9200/_cat/indices",
    "http://[::1]:6379/",
    "file:///etc/passwd",
])
def test_import_rejects_internal_source_url(bad_url, tmp_path, monkeypatch):
    """导入内网书源必须在落盘与联网校验之前被拒"""
    from engine import source_mgr

    # 隔离写入目录，确保测试不污染真实 sources/
    monkeypatch.setattr(source_mgr, "SOURCES_DIR", str(tmp_path))

    def _boom(*a, **k):
        raise AssertionError("validate_source 不应被调用——校验必须前置拦截")

    monkeypatch.setattr(source_mgr, "validate_source", _boom)

    imported, results = source_mgr.import_sources(
        [{"bookSourceName": "evil", "bookSourceUrl": bad_url}], validate=True)

    assert imported == 0
    assert results[0]["ok"] is False
    assert "公网" in results[0]["error"]
    assert list(tmp_path.iterdir()) == []          # 未落盘


def test_import_accepts_public_source_url(tmp_path, monkeypatch):
    """正常公网书源不受影响"""
    from engine import source_mgr
    monkeypatch.setattr(source_mgr, "SOURCES_DIR", str(tmp_path))
    monkeypatch.setattr(source_mgr, "validate_source",
                        lambda src, timeout=25: {"ok": True, "error": "",
                                                 "tested_at": "now"})

    imported, results = source_mgr.import_sources(
        [{"bookSourceName": "好源", "bookSourceUrl": "https://example.com"}],
        validate=True)

    assert imported == 1
    assert results[0]["ok"] is True
    assert len(list(tmp_path.iterdir())) == 1      # 已落盘


def test_search_one_blocks_absolute_internal_search_url():
    """searchUrl 为绝对内网地址时，不得绕过 bookSourceUrl 的公网校验"""
    from engine import source_mgr

    class _NeverFetcher:
        def get(self, *a, **k):
            raise AssertionError("不应发起请求")

        def post(self, *a, **k):
            raise AssertionError("不应发起请求")

    src = {"bookSourceName": "混合", "bookSourceUrl": "https://example.com"}
    html, err = source_mgr._search_one(
        _NeverFetcher(), None, src,
        "http://169.254.169.254/latest/meta-data/", {}, "剑来", 5)

    assert html is None
    assert "拒绝" in err


def test_search_one_blocks_relative_url_on_internal_base():
    """相对 searchUrl 拼接到内网 base 后同样被拦截"""
    from engine import source_mgr

    class _NeverFetcher:
        def get(self, *a, **k):
            raise AssertionError("不应发起请求")

    src = {"bookSourceName": "内网", "bookSourceUrl": "http://192.168.1.10"}
    html, err = source_mgr._search_one(
        _NeverFetcher(), None, src, "/search?q={{key}}", {}, "剑来", 5)

    assert html is None
    assert "拒绝" in err


# ══════════════════════════════════════════════
#  3. Host 白名单（DNS rebinding）
# ══════════════════════════════════════════════

@pytest.mark.parametrize("host", [
    "localhost", "localhost:8766",
    "127.0.0.1", "127.0.0.1:8766",
    "192.168.1.20:8766", "10.0.0.3:8766", "172.16.5.1:8766",
    "[::1]:8766", "[fd00::1]:8766",
    "macbook.local:8766",
])
def test_host_allowed(app_mod, host):
    assert app_mod._host_allowed(host) is True


@pytest.mark.parametrize("host", [
    "evil.com", "evil.com:8766",
    "rebind.attacker.net:8766",
    "8.8.8.8:8766",
    "",
])
def test_host_rejected(app_mod, host):
    assert app_mod._host_allowed(host) is False


def test_host_allowlist_env_is_honored(app_mod, monkeypatch):
    """内网穿透域名可经 WR_ALLOWED_HOSTS 放行"""
    monkeypatch.setattr(app_mod, "ALLOWED_HOSTS", frozenset({"tunnel.example.com"}))
    assert app_mod._host_allowed("tunnel.example.com:8766") is True
    assert app_mod._host_allowed("other.example.com:8766") is False


def test_request_with_foreign_host_is_403(client):
    r = client.get("/api/sources", headers={"Host": "evil.com"})
    assert r.status_code == 403
    assert r.get_json()["code"] == "HOST_NOT_ALLOWED"


def test_request_with_lan_host_ok(client):
    r = client.get("/api/sources", headers={"Host": "192.168.1.50:8766"})
    assert r.status_code == 200


# ══════════════════════════════════════════════
#  4. CSRF：跨站写操作
# ══════════════════════════════════════════════

CSRF_WRITE_ROUTES = [
    ("POST", "/api/cache/clean"),
    ("POST", "/api/tasks"),
    ("POST", "/api/manga/download/pause-all"),
    ("POST", "/api/manga/history"),
    ("POST", "/api/manga/favorites"),
    ("POST", "/api/sources/import"),
    ("DELETE", "/api/sources/whatever"),
    ("DELETE", "/api/books/whatever"),
    ("DELETE", "/api/manga/library/jm/123"),
]


@pytest.mark.parametrize("method,path", CSRF_WRITE_ROUTES)
def test_cross_site_origin_blocked(client, method, path):
    r = client.open(path, method=method,
                    headers={"Origin": "https://evil.com"}, json={})
    assert r.status_code == 403, f"{method} {path} 未拦截跨站请求"
    assert r.get_json()["code"] == "CSRF_BLOCKED"


@pytest.mark.parametrize("method,path", CSRF_WRITE_ROUTES)
def test_cross_site_referer_blocked(client, method, path):
    r = client.open(path, method=method,
                    headers={"Referer": "https://evil.com/page"}, json={})
    assert r.status_code == 403
    assert r.get_json()["code"] == "CSRF_BLOCKED"


def test_null_origin_blocked(client):
    """sandbox iframe / file:// 发出的请求 Origin 为 null，必须拒绝"""
    r = client.post("/api/cache/clean", headers={"Origin": "null"}, json={})
    assert r.status_code == 403
    assert r.get_json()["code"] == "CSRF_BLOCKED"


def test_same_origin_write_allowed(client):
    """同源写操作必须放行（功能不能被防护打断）"""
    r = client.post("/api/cache/clean",
                    headers={"Origin": "http://localhost"},
                    json={"scope": "cache"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_same_origin_referer_allowed(client):
    r = client.post("/api/cache/clean",
                    headers={"Referer": "http://localhost/library"},
                    json={"scope": "cache"})
    assert r.status_code == 200


def test_no_origin_header_allowed(client):
    """curl 等非浏览器客户端不带 Origin，不得误杀"""
    r = client.post("/api/cache/clean", json={"scope": "cache"})
    assert r.status_code == 200


def test_cross_site_get_not_blocked(client):
    """GET 是安全方法，跨站读不经 CSRF 分支（真正的防线是鉴权）"""
    r = client.get("/api/sources", headers={"Origin": "https://evil.com"})
    assert r.status_code == 200


def test_lan_origin_matches_lan_host(client):
    """局域网 IP 访问时，同源判定基于实际 Host 而非硬编码 localhost"""
    r = client.post("/api/cache/clean",
                    headers={"Host": "192.168.1.50:8766",
                             "Origin": "http://192.168.1.50:8766"},
                    json={"scope": "cache"})
    assert r.status_code == 200


def test_lan_host_with_foreign_origin_blocked(client):
    r = client.post("/api/cache/clean",
                    headers={"Host": "192.168.1.50:8766",
                             "Origin": "http://192.168.1.99:8766"},
                    json={"scope": "cache"})
    assert r.status_code == 403


# ══════════════════════════════════════════════
#  5. 鉴权与防护的叠加行为
# ══════════════════════════════════════════════

@pytest.fixture
def auth_on(app_mod, monkeypatch):
    """临时启用鉴权（不落盘、不改环境）"""
    import hashlib
    token = hashlib.sha256(("wr" + "pw123").encode()).hexdigest()
    monkeypatch.setattr(app_mod, "_auth_enabled", True)
    monkeypatch.setattr(app_mod, "_auth_token", token)
    return token


def test_auth_blocks_unauthenticated_api(client, auth_on):
    r = client.get("/api/sources")
    assert r.status_code == 401
    assert r.get_json()["code"] == "UNAUTHORIZED"


def test_auth_allows_with_cookie(client, auth_on):
    client.set_cookie("wr_auth", auth_on, domain="localhost")
    r = client.get("/api/sources")
    assert r.status_code == 200
    client.delete_cookie("wr_auth", domain="localhost")


def test_csrf_checked_before_auth(client, auth_on):
    """跨站写请求即使未登录也应返回 CSRF_BLOCKED，防护顺序不可颠倒"""
    r = client.post("/api/cache/clean",
                    headers={"Origin": "https://evil.com"}, json={})
    assert r.status_code == 403
    assert r.get_json()["code"] == "CSRF_BLOCKED"


def test_host_checked_before_auth(client, auth_on):
    r = client.get("/api/sources", headers={"Host": "evil.com"})
    assert r.status_code == 403
    assert r.get_json()["code"] == "HOST_NOT_ALLOWED"


def test_static_bypasses_all_guards(client, auth_on):
    """静态资源在鉴权开启时仍放行（登录页 CSS/JS 依赖它）"""
    r = client.get("/static/css/style.css", headers={"Host": "evil.com"})
    assert r.status_code in (200, 304, 404)   # 404 亦可：证明未被 403 拦截


def test_login_page_reachable_when_auth_on(client, auth_on):
    r = client.get("/login")
    assert r.status_code == 200


# ══════════════════════════════════════════════
#  6. 条件默认 host
# ══════════════════════════════════════════════

def _run_main_capture_host(app_mod, monkeypatch, argv, auth_enabled):
    """执行 main() 但拦截 app.run，返回实际绑定的 host"""
    captured = {}

    def _fake_run(host=None, port=None, **kw):
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(app_mod, "_auth_enabled", auth_enabled)
    monkeypatch.setattr(app_mod.app, "run", _fake_run)
    monkeypatch.setattr(app_mod._manga_dl, "load", lambda: None)
    monkeypatch.setattr(sys, "argv", ["app.py"] + argv)
    app_mod.main()
    return captured


def test_default_host_is_lan(app_mod, monkeypatch):
    """R36b: 默认即开放局域网(家庭+手机App主场景)，与鉴权无关"""
    got = _run_main_capture_host(app_mod, monkeypatch, [], auth_enabled=False)
    assert got["host"] == "0.0.0.0"
    got2 = _run_main_capture_host(app_mod, monkeypatch, [], auth_enabled=True)
    assert got2["host"] == "0.0.0.0"


def test_explicit_host_overrides_without_auth(app_mod, monkeypatch):
    """显式 --host 0.0.0.0 即使无鉴权也必须被尊重（不阻断用户）"""
    got = _run_main_capture_host(app_mod, monkeypatch,
                                 ["--host", "0.0.0.0"], auth_enabled=False)
    assert got["host"] == "0.0.0.0"


def test_explicit_loopback_with_auth(app_mod, monkeypatch):
    got = _run_main_capture_host(app_mod, monkeypatch,
                                 ["--host", "127.0.0.1"], auth_enabled=True)
    assert got["host"] == "127.0.0.1"


def test_port_still_honored(app_mod, monkeypatch):
    got = _run_main_capture_host(app_mod, monkeypatch,
                                 ["--port", "9999"], auth_enabled=False)
    assert got["port"] == 9999


def test_no_auth_default_open_lan_with_warning(app_mod, monkeypatch, capsys):
    """R36b: 默认开放局域网(家庭+手机App主场景)，无鉴权时打印醒目警告"""
    got = _run_main_capture_host(app_mod, monkeypatch, [], auth_enabled=False)
    assert got["host"] == "0.0.0.0"
    out = capsys.readouterr().out
    assert "未启用访问鉴权" in out
    assert "WR_AUTH_PASSWORD" in out


def test_explicit_open_without_auth_warns_loudly(app_mod, monkeypatch, capsys):
    _run_main_capture_host(app_mod, monkeypatch,
                           ["--host", "0.0.0.0"], auth_enabled=False)
    out = capsys.readouterr().out
    assert "未启用访问鉴权" in out
