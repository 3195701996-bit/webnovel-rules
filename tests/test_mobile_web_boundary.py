# -*- coding: utf-8 -*-
"""手机网页边界回归（方向基线 P0-C / §3.2、§6.3）

背景：手机 App 打开本机页面时，桌面用的"局域网地址发现"脚本会把 127.0.0.1
判成"已失效地址"，页面上挂着红字提示（用户截图里那条）。修法是两条：

  1. 手机打开的网页带 `?mobile=1` → **不加载** netip.js（App 侧固定加这个参数）；
  2. 共享脚本自身也要对回环免疫（浏览器直接访问 127.0.0.1 时同样不该误报）。
     行为级验证在 tests/js/netip_loopback.test.js（node:vm 跑真实文件）。

桌面功能不能因此回归：不带参数时仍要加载它。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NETIP = "static/js/netip.js"


@pytest.fixture
def client():
    import app
    return app.app.test_client()


def test_desktop_page_keeps_netip(client):
    """桌面网页继续加载局域网地址发现（桌面换网提示是既有功能，不能修没）"""
    r = client.get("/")
    assert r.status_code == 200
    assert NETIP in r.get_data(as_text=True)


def test_mobile_page_skips_netip(client):
    r = client.get("/?mobile=1")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert NETIP not in body, "带 mobile=1 的页面不得加载桌面局域网地址脚本"
    assert "进入我的书库" in body, "跳过脚本不应影响页面本身"


def test_mobile_param_does_not_break_other_pages(client):
    """mobile=1 只是展示开关，不该让其它页面 404/500"""
    for path in ("/?mobile=1", "/manga?mobile=1", "/library?mobile=1"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} 应正常返回"


def test_netip_script_guards_loopback():
    """脚本层也要挡住回环：/api/net-ips 明确排除 loopback，
    因此"当前数字 IP 不在列表里"这条判定对 127.0.0.1 是**必然误报**。"""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), NETIP)
    src = open(p, encoding="utf-8").read()
    assert "curIsLoopback" in src, "netip 缺少回环判断"
    assert "127.0.0.1" in src and "localhost" in src
    # 回环时直接隐藏地址栏（本机访问不需要"换成新地址"）
    assert "b.style.display = 'none'" in src
