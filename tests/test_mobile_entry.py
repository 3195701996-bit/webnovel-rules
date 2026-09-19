# -*- coding: utf-8 -*-
"""阶段 C：手机入口承载**真实业务**的端到端验证（子进程，模拟 Android 形态）

验证点（设计 §5/§5.3/§11 阶段 C）：
  1. 环境注入顺序正确：WR_DATA_DIR/WR_DEFER_INIT/WR_PROFILE 在导入 app 之前设置，
     因此导入 app 不产生副作用、业务按 mobile profile 起；
  2. 手机入口服务的是**真实 app**（同一套 templates/static/API），不是第二套实现；
  3. 会话凭据覆盖全部路径：无凭据 401（含页面与 API），带 Cookie 或 X-Mobile-Token 放行；
     仅宿主就绪探测 /__mobile/health 免凭据；
  4. 能力台账在真实运行时下如实标注（无 curl_cffi/playwright 时 jm 降级、
     网页通道不可用）；
  5. stop() 关服务、端口释放、凭据文件清除。

子进程同时阻断 curl_cffi 与 playwright，尽量贴近真实 Android 运行时。
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture()
def mobile_run(tmp_path):
    """在子进程里起手机入口（真实 app + 受控会话），返回其自检结果 JSON"""
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
    data_dir = tmp_path / "files" / "runtime"
    cache_dir = tmp_path / "cache"
    code = (
        "import block_optional\n"
        "import json, os, sys, time, urllib.request, urllib.error\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "from server import mobile_entry as ME\n"
        "rt = ME.get_runtime()\n"
        f"rt.initialize(data_dir={str(data_dir)!r}, cache_dir={str(cache_dir)!r},\n"
        "              token='tok-phase-c', port=0)\n"
        "st = rt.start()\n"
        "assert st['state'] == 'ready', st\n"
        "port = st['port']\n"
        "def get(path, token=None):\n"
        "    url = 'http://127.0.0.1:%d%s' % (port, path)\n"
        "    req = urllib.request.Request(url)\n"
        "    if token:\n"
        "        req.add_header('X-Mobile-Token', token)\n"
        "    try:\n"
        "        with urllib.request.urlopen(req, timeout=20) as r:\n"
        "            return r.status, r.read().decode('utf-8', 'replace')\n"
        "    except urllib.error.HTTPError as e:\n"
        "        return e.code, e.read().decode('utf-8', 'replace')\n"
        "out = {'state': st['state'], 'port': port}\n"
        "out['no_token_health'] = get('/api/health')[0]\n"
        "out['no_token_page'] = get('/')[0]\n"
        # 技术指南 §9.2 端点表：图片与导出路径同样必须在认证内（不能只保护 API/页面）
        "out['no_token_image'] = get('/api/manga/jm/1/chapter/2/img/0')[0]\n"
        "out['no_token_export'] = get('/api/books/x/txt')[0]\n"
        "out['no_token_storage'] = get('/api/storage')[0]\n"
        "out['probe_health'] = get('/__mobile/health')[0]\n"
        "code_h, body_h = get('/api/health', 'tok-phase-c')\n"
        "h = json.loads(body_h) if code_h == 200 else {}\n"
        "out['health_code'] = code_h\n"
        "out['health_ok'] = h.get('ok')\n"
        "out['runtime_state'] = (h.get('runtime') or {}).get('state')\n"
        "out['profile'] = (h.get('runtime') or {}).get('profile')\n"
        "out['workers'] = (h.get('runtime') or {}).get('workers')\n"
        "out['missing'] = (h.get('capabilities') or {}).get('missing')\n"
        "code_p, body_p = get('/', 'tok-phase-c')\n"
        "out['page_code'] = code_p\n"
        "out['page_has_title'] = ('webnovel_rules' in body_p or '书源' in body_p)\n"
        "code_s, body_s = get('/api/manga/sources', 'tok-phase-c')\n"
        "src = json.loads(body_s) if code_s == 200 else {}\n"
        "by = {x['key']: x for x in (src.get('sources') or [])}\n"
        "out['sources_code'] = code_s\n"
        "out['jm_status'] = (by.get('jm') or {}).get('status')\n"
        "out['web_ok'] = src.get('web_channel_available')\n"
        "out['cookie_auth'] = None\n"
        "req = urllib.request.Request('http://127.0.0.1:%d/api/health' % port)\n"
        "req.add_header('Cookie', 'mobile_session=tok-phase-c')\n"
        "try:\n"
        "    with urllib.request.urlopen(req, timeout=20) as r:\n"
        "        out['cookie_auth'] = r.status\n"
        "except urllib.error.HTTPError as e:\n"
        "    out['cookie_auth'] = e.code\n"
        "rt.stop('test')\n"
        "out['stopped_state'] = rt.status()['state']\n"
        "try:\n"
        "    urllib.request.urlopen('http://127.0.0.1:%d/api/health' % port, timeout=3)\n"
        "    out['port_closed'] = False\n"
        "except Exception:\n"
        "    out['port_closed'] = True\n"
        "out['session_file_gone'] = not os.path.exists(\n"
        "    os.path.join(rt.paths['config'], 'session.json'))\n"
        "out['data_dirs'] = sorted(os.listdir(rt.paths['data'])[:3]) or ['empty']\n"
        "print('MOBILE_TEST ' + json.dumps(out, ensure_ascii=False))\n"
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("WR_DISABLE_BACKGROUND", "WR_PROFILE", "WR_DEFER_INIT",
                        "WR_DATA_DIR", "WR_SOURCES_DIR")}
    env["PYTHONPATH"] = str(tmp_path)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       timeout=300, cwd=ROOT, env=env)
    if r.returncode != 0:
        pytest.fail(f"子进程失败:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    line = [l for l in r.stdout.splitlines() if l.startswith("MOBILE_TEST ")]
    assert line, f"未拿到结果: {r.stdout[-1500:]}"
    return json.loads(line[-1][len("MOBILE_TEST "):])


def test_mobile_entry_serves_real_app_with_session_auth(mobile_run):
    d = mobile_run
    assert d["state"] == "ready", d
    # 会话凭据：无凭据一律 401（页面与 API 都算），仅宿主探测端点免凭据
    assert d["no_token_health"] == 401
    assert d["no_token_page"] == 401
    # 图片 / 导出 / 存储 三类路径也必须 401（指南 §9.2 的端点表逐项对账）
    assert d["no_token_image"] == 401, "本地图片路径不能免认证直出"
    assert d["no_token_export"] == 401, "导出路径不能免认证"
    assert d["no_token_storage"] == 401, "存储明细接口也不能免认证"
    assert d["probe_health"] == 200
    assert d["cookie_auth"] == 200, "Cookie 形式凭据也应放行"
    # 真实业务：health/页面/源列表都来自真实 app
    assert d["health_code"] == 200 and d["health_ok"] is True
    assert d["page_code"] == 200 and d["page_has_title"], "应返回真实首页"
    assert d["sources_code"] == 200
    # mobile profile 生效：只有本地工作线程
    assert d["profile"] == "mobile"
    assert d["workers"] == ["manga-stats-verify", "task-watchdog"], d["workers"]
    assert d["runtime_state"] == "ready"


def test_mobile_entry_reports_honest_capabilities(mobile_run):
    d = mobile_run
    assert "curl_cffi" in d["missing"] and "playwright" in d["missing"], d
    assert d["jm_status"] == "degraded", f"缺 curl_cffi 时 jm 应降级: {d}"
    assert d["web_ok"] is False, "无 playwright 时不得声称网页通道可用"


def test_mobile_entry_stop_releases_everything(mobile_run):
    d = mobile_run
    assert d["stopped_state"] == "stopped"
    assert d["port_closed"] is True, "停止后端口必须关闭"
    assert d["session_file_gone"] is True, "停止后凭据文件必须清除"
