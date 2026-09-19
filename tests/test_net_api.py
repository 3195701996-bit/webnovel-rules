# -*- coding: utf-8 -*-
"""出站代理接口（0.73.0）：GET/POST /api/net/proxy、POST /api/net/proxy/test。

用户背景：真机诊断显示"API 快、图片/部分源域不通"，且包子漫画同一 URL
直连两条传输都失败、走代理都成功 —— 也就是说"每源降级"的主因是**网络可达性**，
所以这一层必须可信：
  · 回显的是**真实生效值**（环境变量优先于配置文件），不是用户填了什么；
  · 非法地址**显式 400**，绝不静默回落直连；
  · 探测如实分列 直连/经代理，并给出"代理到底有没有用"的结论；
  · 代理串里的凭据不进诊断报告。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.netproxy as NP            # noqa: E402
from server import net_api              # noqa: E402


@pytest.fixture
def client():
    import app
    return app.app.test_client()


@pytest.fixture(autouse=True)
def _proxy_env(tmp_path, monkeypatch):
    """每个用例独立的代理配置文件 + 干净环境（netproxy 有模块级缓存）。"""
    cfg = tmp_path / "runtime_config" / "proxy.txt"
    monkeypatch.setenv("WR_PROXY_FILE", str(cfg))
    monkeypatch.delenv("WR_PROXY", raising=False)
    NP._cached = None
    NP._env_pushed = None
    NP._HEALTH.update({"value": None, "ts": 0.0, "ok": True, "err": "",
                       "fell_back": False})
    yield cfg
    os.environ.pop("WR_PROXY", None)
    NP._cached = None
    NP._env_pushed = None


# --------------------------------------------------------------- 读 / 写状态

def test_state_defaults_to_direct(client):
    r = client.get("/api/net/proxy")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True
    assert d["data"]["proxied"] is False
    assert d["data"]["source"] == "direct"
    assert d["data"]["proxy"] == ""


def test_set_then_read_back(client, _proxy_env):
    r = client.post("/api/net/proxy", json={"proxy": "127.0.0.1:7897"})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d["ok"] is True
    assert d["data"]["proxy"] == "http://127.0.0.1:7897"
    assert d["data"]["proxied"] is True
    # 设置页自己写的值会被推入环境变量以求"立刻生效"，来源必须仍报"本机设置"
    # （报成 env 会让用户以为被别人的环境变量覆盖了——实测就误导过一次排查）
    assert d["data"]["source"] == "set"
    # 落盘：重启引擎后仍生效（这正是"配了就该一直有效"的要求）
    assert _proxy_env.read_text(encoding="utf-8").strip() == "http://127.0.0.1:7897"

    d2 = client.get("/api/net/proxy").get_json()["data"]
    assert d2["proxy"] == "http://127.0.0.1:7897"
    assert d2["source"] == "set"


def test_env_wins_over_file_and_is_reported_as_source(client, monkeypatch, _proxy_env):
    """读取优先级：环境变量 > 配置文件（用户自己设的环境变量必须压过旧配置）。"""
    _proxy_env.parent.mkdir(parents=True, exist_ok=True)
    _proxy_env.write_text("127.0.0.1:7897\n", encoding="utf-8")
    NP._cached = None
    NP._env_pushed = None
    d = client.get("/api/net/proxy").get_json()["data"]
    assert d["proxy"] == "http://127.0.0.1:7897"
    assert d["source"] == "file"

    monkeypatch.setenv("WR_PROXY", "socks5://127.0.0.1:1080")
    d = client.get("/api/net/proxy").get_json()["data"]
    assert d["proxy"] == "socks5://127.0.0.1:1080"
    assert d["source"] == "env", "必须说清实际生效的是环境变量"


def test_saving_replaces_preexisting_env(client, monkeypatch):
    """用户在设置页显式保存 = 他的意图，新值必须生效（来源报"本机设置"）。"""
    monkeypatch.setenv("WR_PROXY", "socks5://127.0.0.1:1080")
    d = client.post("/api/net/proxy", json={"proxy": "127.0.0.1:7897"}).get_json()["data"]
    assert d["proxy"] == "http://127.0.0.1:7897"
    assert d["source"] == "set"


def test_source_reports_file_when_only_config_exists(_proxy_env):
    """重启后只剩配置文件（环境变量没了）：来源应报 file，仍照常生效。"""
    _proxy_env.parent.mkdir(parents=True, exist_ok=True)
    _proxy_env.write_text("127.0.0.1:7897\n", encoding="utf-8")
    NP._cached = None
    NP._env_pushed = None
    assert NP.current_proxy() == "http://127.0.0.1:7897"
    assert NP.source() == "file"


def test_invalid_value_is_rejected_loudly(client, _proxy_env):
    r = client.post("/api/net/proxy", json={"proxy": "ftp://127.0.0.1:21"})
    assert r.status_code == 400
    d = r.get_json()
    assert d["ok"] is False
    assert "无法识别" in d["error"]
    assert not _proxy_env.exists(), "非法值不得落盘"
    assert client.get("/api/net/proxy").get_json()["data"]["proxied"] is False


def test_empty_clears_proxy(client, _proxy_env):
    client.post("/api/net/proxy", json={"proxy": "127.0.0.1:7897"})
    r = client.post("/api/net/proxy", json={"proxy": ""})
    assert r.status_code == 200
    d = r.get_json()
    assert d["data"]["proxied"] is False
    assert d["data"]["proxy"] == ""
    assert "直连" in d["note"]


def test_set_reports_reset_sessions(client, monkeypatch):
    """切换代理必须重置各通道会话，否则"改了代理没生效"。"""
    calls = []
    monkeypatch.setattr(NP, "reset_channel_sessions", lambda: calls.append(1) or 2)
    client.post("/api/net/proxy", json={"proxy": "socks5://127.0.0.1:1080"})
    assert calls, "设置代理必须调用 reset_channel_sessions"


# ------------------------------------------------------------------- 探测

def test_probe_compares_direct_and_proxied(client, monkeypatch):
    """探测必须两列并排：代理没带来可达域时要看得出来。"""
    calls = []

    def _fake(url, proxy, timeout):
        calls.append(proxy)
        if proxy:                       # 经代理：全部可达
            return (True, 200, 120, "")
        return (False, 0, 3000, "ConnectionError: 连接被重置")

    monkeypatch.setattr(net_api, "_probe_one", _fake)
    monkeypatch.setattr(net_api, "PROBE_TARGETS", (("甲站", "https://a.example.com/"),
                                                   ("乙站", "https://b.example.com/")))
    client.post("/api/net/proxy", json={"proxy": "127.0.0.1:7897"})
    r = client.post("/api/net/proxy/test", json={})
    assert r.status_code == 200
    d = r.get_json()
    assert set(d["data"]["modes"]) == {"direct", "proxied"}
    rows = {(x["mode"], x["target"]): x for x in d["data"]["rows"]}
    assert rows[("direct", "甲站")]["ok"] is False
    assert rows[("proxied", "甲站")]["ok"] is True
    assert "连接被重置" in d["text"]
    assert "代理确实扩展了可达域" in d["text"], d["text"]
    assert any(p == "http://127.0.0.1:7897" for p in calls)


def test_probe_without_proxy_only_direct(client, monkeypatch):
    monkeypatch.setattr(net_api, "_probe_one",
                        lambda url, proxy, timeout: (True, 200, 10, ""))
    monkeypatch.setattr(net_api, "PROBE_TARGETS", (("甲站", "https://a.example.com/"),))
    d = client.post("/api/net/proxy/test", json={}).get_json()
    assert d["data"]["modes"] == ["direct"]
    assert "未配置" in d["text"]


def test_probe_says_when_proxy_is_not_better(client, monkeypatch):
    """代理更差时必须直说（多一跳只会更慢），不替代理说好话。"""
    monkeypatch.setattr(net_api, "_probe_one",
                        lambda url, proxy, timeout: (bool(proxy) is False, 200, 50, ""))
    monkeypatch.setattr(net_api, "PROBE_TARGETS", (("甲站", "https://a.example.com/"),))
    client.post("/api/net/proxy", json={"proxy": "127.0.0.1:7897"})
    d = client.post("/api/net/proxy/test", json={}).get_json()
    assert "代理反而更差" in d["text"], d["text"]


def test_probe_accepts_candidate_before_saving(client, monkeypatch):
    """测试按钮要能"先测再存"：候选值不落盘、不改生效值。"""
    seen = {}

    def _fake(url, proxy, timeout):
        seen["p"] = proxy
        return (True, 200, 1, "")

    monkeypatch.setattr(net_api, "_probe_one", _fake)
    monkeypatch.setattr(net_api, "PROBE_TARGETS", (("甲站", "https://a.example.com/"),))
    d = client.post("/api/net/proxy/test", json={"proxy": "socks5://127.0.0.1:1080"}).get_json()
    assert seen["p"] == "socks5://127.0.0.1:1080"
    assert d["data"]["proxy"] == "socks5://127.0.0.1:1080"
    assert client.get("/api/net/proxy").get_json()["data"]["proxied"] is False


def test_probe_rejects_invalid_candidate(client):
    r = client.post("/api/net/proxy/test", json={"proxy": "ftp://127.0.0.1:21"})
    assert r.status_code == 400
    assert "无法识别" in r.get_json()["error"]


def test_state_includes_proxy_health(client, monkeypatch, _proxy_env):
    """状态里必须带"代理是否真的可用"：界面据此显示"已临时直连"。

    只报"经代理"而不报"连不上"，正是上一版让用户看到"所有源都超时"却
    找不到原因的界面缺口。
    """
    monkeypatch.setenv("WR_PROXY_HEALTHCHECK", "1")
    NP._HEALTH.update({"value": None, "ts": 0.0, "ok": True, "err": "",
                       "fell_back": False})
    d = client.post("/api/net/proxy", json={"proxy": "127.0.0.1:1"}).get_json()
    assert d["ok"] is True
    h = d["data"]["health"]
    assert h["configured"].endswith("127.0.0.1:1")
    assert h["ok"] is False and h["fell_back"] is True
    assert "连不上" in d["note"], d["note"]
    # 生效值为空（临时直连），来源报 direct —— 界面不能继续说"经代理"
    assert d["data"]["proxied"] is False
    assert d["data"]["source"] == "direct"

    st = client.get("/api/net/proxy").get_json()["data"]
    assert st["health"]["fell_back"] is True


# --------------------------------------------------- 写操作的最小权限边界

def test_write_rejected_from_lan(client):
    """代理改动等于把所有抓取流量交给某个中间人：局域网客户端不得改。"""
    r = client.post("/api/net/proxy", json={"proxy": "127.0.0.1:7897"},
                    environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert r.status_code == 403
    assert "只允许从本机发起" in r.get_json()["error"]
    assert client.get("/api/net/proxy").get_json()["data"]["proxied"] is False

    r2 = client.post("/api/net/proxy/test", json={},
                     environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert r2.status_code == 403


def test_write_allowed_from_lan_when_explicitly_enabled(client, monkeypatch):
    monkeypatch.setenv("WR_ALLOW_PROXY_API", "1")
    r = client.post("/api/net/proxy", json={"proxy": "127.0.0.1:7897"},
                    environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert r.status_code == 200


def test_read_state_is_allowed_but_credentials_redacted_from_lan(client):
    """只读状态不拦（便于排查），但**凭据只回显给本机**。"""
    client.post("/api/net/proxy", json={"proxy": "socks5://u:p@127.0.0.1:1080"})
    local = client.get("/api/net/proxy").get_json()["data"]
    assert local["proxy"] == "socks5://u:p@127.0.0.1:1080", "本机要能看到自己填的原值"

    r = client.get("/api/net/proxy", environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert r.status_code == 200
    lan = r.get_json()["data"]
    assert lan["proxy"] == "socks5://127.0.0.1:1080"
    assert "u:p" not in r.get_data(as_text=True)


# --------------------------------------------------------- 报告 / 脱敏

def test_report_states_direct_and_proxied(client, monkeypatch):
    # 只验证"网络路径"这一节：把第 8 节的真实测速换掉（它要发真网络请求、
    # 且有独立测试覆盖）——这里的关注点是代理状态与脱敏。
    from server import diag
    monkeypatch.setattr(diag, "latency_lines", lambda *a, **k: ["  （本用例未测速）"])
    text = client.get("/api/diagnostics/report").get_json()["text"]
    assert "【7. 出站网络路径（代理）】" in text
    assert "未配置（全部直连）" in text

    client.post("/api/net/proxy", json={"proxy": "socks5://u:p@127.0.0.1:1080"})
    text = client.get("/api/diagnostics/report").get_json()["text"]
    assert "socks5://127.0.0.1:1080" in text
    assert "u:p" not in text, "报告的代理串必须脱敏（报告会被发出来）"


def test_redact_proxy():
    assert NP.redact_proxy("socks5://u:p@h:1080") == "socks5://h:1080"
    assert NP.redact_proxy("http://h:7897") == "http://h:7897"
    assert NP.redact_proxy("") == ""
    assert NP.redact_proxy(None) == ""
