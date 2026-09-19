# -*- coding: utf-8 -*-
"""C01 前端统一请求错误处理的轻量验证（沿用 A04/C03 的两层验证方式，不引入 JS 框架）。

验证方式：
1. 文本断言：requestJSON 辅助存在且语义完整（超时/非 2xx/非 JSON/取消/按钮恢复），
   三个模板均改用 requestJSON，旧的缺陷模式（catch 后仍提示成功、盲吞错误、
   固定 setTimeout 后整页 reload）不复存在。
2. node --check：三个模板的内联脚本与 common.js 语法均可解析。
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMMON_JS = (ROOT / "static" / "js" / "common.js").read_text(encoding="utf-8")
TASKS = (ROOT / "templates" / "tasks.html").read_text(encoding="utf-8")
MANGA_DL = (ROOT / "templates" / "manga_download.html").read_text(encoding="utf-8")
NOVEL_DETAIL = (ROOT / "templates" / "novel_detail.html").read_text(encoding="utf-8")


# ═══════════ requestJSON 辅助本身 ═══════════

class TestRequestJSONHelper:
    def test_defined_and_exported(self):
        assert "async function requestJSON(url, opts)" in COMMON_JS
        assert "window.requestJSON = requestJSON;" in COMMON_JS

    def test_timeout_via_abort_controller(self):
        assert "new AbortController()" in COMMON_JS
        assert re.search(r"setTimeout\(\(\) => \{\s*timedOut = true;\s*ctrl\.abort\(\);", COMMON_JS)
        assert "TimeoutError" in COMMON_JS

    def test_external_signal_cancellation_passthrough(self):
        # 外部 signal 接入同一 controller，主动取消透传 AbortError（不包装）
        assert "ext.addEventListener('abort', onExtAbort" in COMMON_JS
        assert re.search(r"if \(timedOut\) \{[\s\S]*?\}\s*throw e;\s*// 外部主动取消", COMMON_JS)

    def test_non_2xx_and_non_json_rejected(self):
        # 非 2xx → HttpError（携带 status/data）；2xx 但非 JSON → ParseError
        assert "HttpError" in COMMON_JS
        assert re.search(r"if \(!r\.ok\) \{[\s\S]*?he\.status = r\.status", COMMON_JS)
        assert "ParseError" in COMMON_JS
        assert "非 JSON 响应" in COMMON_JS
        # 网络层失败 → NetworkError
        assert "NetworkError" in COMMON_JS

    def test_button_always_restored_in_finally(self):
        m = re.search(r"async function requestJSON[\s\S]*?\n  \}\n", COMMON_JS)
        assert m, "requestJSON 未找到"
        body = m.group(0)
        assert re.search(r"finally \{[\s\S]*?btn\.disabled = false;", body), \
            "按钮未在 finally 中恢复"

    def test_body_auto_json_stringify(self):
        assert "JSON.stringify(opts.body)" in COMMON_JS
        assert "init.headers['Content-Type'] = 'application/json';" in COMMON_JS


# ═══════════ tasks.html ═══════════

class TestTasksTemplate:
    def test_uses_requestjson_for_all_operations(self):
        # 列表加载 + 暂停/停止/启动/删除/批量暂停/批量启动
        assert "await requestJSON('/api/tasks')" in TASKS
        for frag in [
            "/api/manga/download/pause?source=",
            "/pause', {method: 'POST', button: b}",
            "/stop', {method: 'POST', button: b}",
            "/resume', {method: 'POST', button: b}",
            "{method: 'DELETE', button: b}",
            "/api/manga/download/pause-all",
            "/api/manga/download/resume-all",
        ]:
            assert frag in TASKS, f"tasks.html 缺少 requestJSON 改造点: {frag}"

    def test_no_blind_error_swallow(self):
        # 旧缺陷：.catch(() => {}) 吞掉错误后仍提示成功——必须不复存在
        assert ".catch(() => {})" not in TASKS

    def test_no_raw_fetch_in_operation_handlers(self):
        # 操作按钮区内不应残留裸 fetch（loadTasks 区之外）
        ops_zone = TASKS[TASKS.index("box.querySelectorAll('.pause-btn')"):]
        assert "await fetch(" not in ops_zone

    def test_failure_shows_error_not_fake_success(self):
        # 每个操作 catch 后提示失败并 return，不再落入成功 toast
        assert TASKS.count("toast('⚠ ") >= 5

    def test_no_permanent_disabled_button(self):
        # 旧缺陷：b.disabled = true 后失败路径不再恢复——禁用/恢复统一由 requestJSON 负责
        assert not re.search(r"b\.disabled = true;\s*\n\s*if \(b\.dataset", TASKS)


# ═══════════ manga_download.html ═══════════

class TestMangaDownloadTemplate:
    def test_uses_requestjson(self):
        assert re.search(r"await requestJSON\(`/api/manga/\$\{SOURCE\}/", MANGA_DL)
        assert re.search(r"method: 'POST', body: \{title: TITLE, chapters: chapters\}", MANGA_DL)
        assert re.search(r"await requestJSON\(`/api/manga/download/status", MANGA_DL)

    def test_post_failure_recovers_button_no_fake_success(self):
        # POST 失败：状态栏显示可恢复错误 + 重试入口 + 按钮恢复；不进入轮询假成功
        m = re.search(r"\} catch \(e\) \{\s*\$\('#status'\)\.textContent = '⚠️ 下载请求失败: "
                      r"' \+ e\.message;[\s\S]*?btn\.disabled = false;\s*return;", MANGA_DL)
        assert m, "下载 POST 缺少可恢复失败分支"

    def test_accepted_then_server_confirmed(self):
        # 长任务：POST 后只提示"请求已接受"，终态由轮询确认
        assert "请求已接受" in MANGA_DL
        post_pos = MANGA_DL.index("请求已接受")
        poll_pos = MANGA_DL.index("async function poll()")
        assert post_pos < poll_pos

    def test_poll_has_unified_error_recovery(self):
        # 轮询单次失败不清进度/不停轮询；连续失败才停止并给恢复入口
        assert "let pollErrors = 0;" in MANGA_DL
        assert "pollErrors++" in MANGA_DL
        assert "if (pollErrors >= 10)" in MANGA_DL
        assert "function resumePoll()" in MANGA_DL

    def test_a02_guard_preserved(self):
        # A02 不回退：单话定位失败仍停止提交、不扩大为整本
        assert "不会自动下载整本" in MANGA_DL
        assert "chapters = [CH_ID];" in MANGA_DL


# ═══════════ novel_detail.html ═══════════

class TestNovelDetailTemplate:
    def test_uses_requestjson(self):
        assert "await requestJSON('/api/books/' + encodeURIComponent(KEY))" in NOVEL_DETAIL
        assert "/check-update',\n      {method: 'POST', timeout: 20000}" in NOVEL_DETAIL
        assert "/download-missing" in NOVEL_DETAIL
        assert re.search(r"requestJSON\('/api/books/' \+ encodeURIComponent\(KEY\), "
                         r"\{method: 'DELETE'", NOVEL_DETAIL)

    def test_check_update_polls_check_status_terminal(self):
        # C01 核心：轮询 check-status 直至终态，reload 只发生在终态确认后
        assert "async function checkUpdate(key)" in NOVEL_DETAIL
        assert "/check-status'" in NOVEL_DETAIL
        assert "if (sd.status === 'checking') continue;" in NOVEL_DETAIL
        fn = NOVEL_DETAIL[NOVEL_DETAIL.index("async function checkUpdate(key)"):]
        fn = fn[:fn.index("\n}\n")]
        reload_pos = fn.index("location.reload();")
        terminal_pos = fn.index("if (sd.error)")
        assert terminal_pos < reload_pos, "reload 必须发生在终态判定之后"

    def test_no_fixed_delay_reload(self):
        # 旧缺陷模式：固定 setTimeout 后整页 reload——必须不复存在
        assert not re.search(r"setTimeout\(\s*\(\)\s*=>\s*location\.reload\(\)", NOVEL_DETAIL)

    def test_check_button_recoverable(self):
        fn = NOVEL_DETAIL[NOVEL_DETAIL.index("async function checkUpdate(key)"):]
        fn = fn[:fn.index("\n}\n")]
        assert "const restore = () => { btn.disabled = false;" in fn
        assert fn.count("restore()") >= 3, "启动失败/轮询失败/超时/终态路径都要恢复按钮"

    def test_a04_guards_preserved(self):
        # A04 换源竞态守卫不回退
        assert "let _srcGen = 0;" in NOVEL_DETAIL
        assert NOVEL_DETAIL.count("if (gen !== _srcGen) return;") >= 4
        assert NOVEL_DETAIL.count("e.name === 'AbortError' || gen !== _srcGen") >= 2

    def test_c03_chapter_link_preserved(self):
        # C03 ?ch= 链接不回退
        assert re.search(r"/reader/' \+ encodeURIComponent\(KEY\) \+ '\?ch=' \+ c\.index",
                         NOVEL_DETAIL)


# ═══════════ node --check：内联脚本语法 ═══════════

def _inline_scripts(html):
    # 抽取不带 src 的 <script> 块内容
    return re.findall(r"<script>([\s\S]*?)</script>", html)


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用，跳过语法检查")
@pytest.mark.parametrize("name,source", [
    ("common.js", COMMON_JS),
    ("tasks.html", TASKS),
    ("manga_download.html", MANGA_DL),
    ("novel_detail.html", NOVEL_DETAIL),
])
def test_inline_scripts_node_check(name, source, tmp_path):
    scripts = [source] if name.endswith(".js") else _inline_scripts(source)
    assert scripts, f"{name} 未抽取到脚本"
    for i, code in enumerate(scripts):
        f = tmp_path / f"{name.replace('.', '_')}_{i}.js"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{name} 第 {i} 段脚本语法错误:\n{r.stderr}"
