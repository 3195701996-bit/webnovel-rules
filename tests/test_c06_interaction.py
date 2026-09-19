# -*- coding: utf-8 -*-
"""C06 交互一致性 + perf(covers) 封面懒加载验收（沿用 C01 文本断言 + node --check 方式）。

验收点：
1. 搜索四态（index.html + server/novel_api.py）：搜索中 / 部分完成 / 源失败 / 真正无结果；
   源失败不得描述为"搜不到书"；服务端 finished 事件携带 errors 字段。
2. novel_detail.html 删除文案与软删除语义一致（"不可恢复"消失，不承诺恢复入口）。
3. sources.html 开关：button + role="switch" + aria-checked，键盘 Enter/Space（原生 click）。
4. ETA 修复（engine/manga/download_manager.py）：章末归零模式消失；速率未知记 None；
   前端未知显示"估算中"，进度分母未知不用假百分比。
5. reader.html 切章失败保持局部 toast + 保留旧正文（C03 不回退）。
6. common.js requestJSON：401 → 跳 /login。
7. 封面懒加载：index/library/novel_detail/manga_detail 的 <img> 带 loading="lazy" 与
   decoding="async"，不改 src 与样式。
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
NOVEL_DETAIL = (ROOT / "templates" / "novel_detail.html").read_text(encoding="utf-8")
SOURCES = (ROOT / "templates" / "sources.html").read_text(encoding="utf-8")
TASKS = (ROOT / "templates" / "tasks.html").read_text(encoding="utf-8")
TASK = (ROOT / "templates" / "task.html").read_text(encoding="utf-8")
MANGA_DL = (ROOT / "templates" / "manga_download.html").read_text(encoding="utf-8")
READER = (ROOT / "templates" / "reader.html").read_text(encoding="utf-8")
LIBRARY = (ROOT / "templates" / "library.html").read_text(encoding="utf-8")
MANGA_DETAIL = (ROOT / "templates" / "manga_detail.html").read_text(encoding="utf-8")
COMMON_JS = (ROOT / "static" / "js" / "common.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
NOVEL_API = (ROOT / "server" / "novel_api.py").read_text(encoding="utf-8")
DL_MANAGER = (ROOT / "engine" / "manga" / "download_manager.py").read_text(encoding="utf-8")


# ═══════════ 1. 搜索四态 ═══════════

class TestSearchStates:
    def test_state_searching(self):
        # 搜索中：进行中态存在
        assert "正在搜索…" in INDEX

    def test_state_partial(self):
        # 部分完成：有结果但部分源失败/超时
        assert "结果可能不完整" in INDEX
        assert re.search(r"errCount > 0[\s\S]*?结果可能不完整", INDEX)

    def test_state_source_failure(self):
        # 源失败：独立文案与渲染函数，不得描述为"搜不到书"
        assert "function renderSearchSourceFail(" in INDEX
        fn = INDEX[INDEX.index("function renderSearchSourceFail("):]
        fn = fn[:fn.index("\n}")]
        assert "书源连接失败，未能完成搜索" in fn
        assert "不代表没有这本书" in fn
        assert "没有找到相关小说" not in fn

    def test_state_truly_empty(self):
        # 真正无结果：仅在无源失败时走"没有找到相关小说"
        assert "renderSearchEmpty(q)" in INDEX
        m = re.search(r"if \(!_rowIndex\.size\) \{([\s\S]*?)\} else if \(errCount > 0\)", INDEX)
        assert m, "空结果分支应先判定 errCount"
        seg = m.group(1)
        assert "errCount > 0" in seg and "renderSearchSourceFail" in seg
        assert "renderSearchEmpty(q);   // 真正无结果" in seg

    def test_server_finished_event_carries_errors(self):
        # 服务端 finished 事件携带 errors 字段（源名 -> 原因）
        assert '"errors": _errors' in NOVEL_API
        assert "_note_src_error" in NOVEL_API
        # 空结果不算源失败（慢源台账 "空结果 Xs" 被排除）
        assert 'reason.startswith("空结果")' in NOVEL_API


# ═══════════ 2. 详情危险操作区文案 ═══════════

class TestNovelDetailDangerZone:
    def test_unrecoverable_wording_gone(self):
        assert "不可恢复" not in NOVEL_DETAIL

    def test_soft_delete_accurate_wording(self):
        # 与 server/novel_api.py api_book_delete 软删除语义一致：移入回收站，
        # 不承诺尚未提供的界面恢复入口
        assert "回收站" in NOVEL_DETAIL
        assert "data/trash/" in NOVEL_DETAIL
        assert "暂无界面恢复入口" in NOVEL_DETAIL

    def test_secondary_actions_still_present(self):
        # 下载/检查更新为次要动作存在，删除按钮仍是 danger
        assert 'id="check-btn"' in NOVEL_DETAIL
        assert 'id="del-btn"' in NOVEL_DETAIL
        assert "op-btn danger" in NOVEL_DETAIL


# ═══════════ 3. 书源开关可访问性 ═══════════

class TestSourceSwitchA11y:
    def test_switch_is_button_with_switch_role(self):
        assert 'role="switch"' in SOURCES
        assert re.search(r'<button type="button" role="switch" aria-checked=', SOURCES)

    def test_aria_checked_reflects_state(self):
        assert "aria-checked=\"${s.enabled ? 'true' : 'false'}\"" in SOURCES

    def test_no_clickable_div_switch(self):
        # 旧的纯 div.switch 不复存在
        assert not re.search(r'<div class="switch ', SOURCES)

    def test_keyboard_operable_via_native_button(self):
        # button 原生 Enter/Space 触发 click；处理函数仍绑定 onclick
        assert re.search(r"querySelectorAll\('\.switch'\)[\s\S]*?\.onclick", SOURCES)

    def test_visual_style_preserved(self):
        # 沿用 .switch 样式（仅补 padding:0 抵消 UA 默认 + 焦点可见环）
        assert ".switch {" in STYLE_CSS
        assert ".switch.on" in STYLE_CSS
        assert re.search(r"\.switch \{[^}]*padding: 0;", STYLE_CSS)


# ═══════════ 4. ETA 修复与任务状态文案 ═══════════

class TestEtaFix:
    def test_chapter_end_eta_zeroing_gone(self):
        # 旧缺陷：每章结束把刚算出的 eta 归零
        assert not re.search(r't\["images_done"\] = images_done\s*\n\s*t\["eta"\] = 0',
                             DL_MANAGER)

    def test_unknown_rate_marks_eta_none(self):
        assert 't["eta"] = int(rem / rate) if rate > 0 else None' in DL_MANAGER

    def test_frontend_unknown_eta_shows_estimating(self):
        # 前端：ETA 未知显示"估算中"
        assert "估算中" in TASKS
        assert re.search(r"p\.eta === null \|\| p\.eta === undefined[\s\S]*?估算中", TASKS)

    def test_no_fake_percent_when_denominator_unknown(self):
        # 进度分母未知时不用假百分比
        assert "denomKnown" in TASKS
        assert "总量估算中…" in TASKS


class TestTaskStatusLabels:
    def test_manga_status_distinguished(self):
        # 排队/运行/停止中/暂停/部分失败/完成 六态区分
        assert "排队中" in TASKS
        assert "下载中" in TASKS
        assert "停止中…" in TASKS          # cancel ≠ 已停止
        assert "已暂停" in TASKS
        assert "部分失败（可续传）" in TASKS  # error = 部分失败
        assert "已完成" in TASKS

    def test_manga_cancel_not_labeled_stopped(self):
        m = re.search(r"const stMap2 = isManga \? \{([^}]*)\}", TASKS)
        assert m
        assert "cancel:['⏹ 停止中…'" in m.group(1)

    def test_task_detail_partial_failure(self):
        # task.html：完成但有失败章 → 部分失败
        assert "部分失败" in TASK
        assert re.search(r"st === 'done' && \(p\.failed \|\| 0\) > 0", TASK)

    def test_manga_download_page_statuses(self):
        assert "停止中…" in MANGA_DL
        assert "部分失败" in MANGA_DL
        assert "排队中" in MANGA_DL


# ═══════════ 5. 阅读错误局部提示（C03 不回退）═══════════

class TestReaderLocalError:
    def test_chapter_switch_failure_keeps_old_content(self):
        assert "已保留当前章节" in READER
        assert re.search(r"if \(hasOld\) \{[\s\S]*?toast\('章节加载失败（已保留当前章节）",
                         READER)

    def test_undownloaded_chapter_keeps_old_content(self):
        assert re.search(r"if \(hasOld\) \{[\s\S]*?toast\('第 ' \+ idx \+ ' 章尚未下载", READER)


# ═══════════ 6. 全局 401 处理 ═══════════

class TestGlobal401:
    def test_requestjson_redirects_to_login_on_401(self):
        m = re.search(r"if \(!r\.ok\) \{([\s\S]*?)throw he;", COMMON_JS)
        assert m
        seg = m.group(1)
        assert "r.status === 401" in seg
        assert "location.href = '/login'" in seg

    def test_401_still_throws_http_error(self):
        # 跳转后仍抛 HttpError，调用方后续逻辑中止
        assert "HttpError" in COMMON_JS
        assert re.search(r"401[\s\S]{0,200}he\.status = r\.status", COMMON_JS)


# ═══════════ 7. 封面懒加载 ═══════════

class TestCoverLazyLoading:
    def _assert_lazy(self, html, name):
        imgs = re.findall(r"<img [^>]*src=", html)
        assert imgs, f"{name} 未找到封面 img"
        for tag in imgs:
            assert 'loading="lazy"' in tag, f"{name} 封面缺 loading=lazy: {tag[:80]}"
            assert 'decoding="async"' in tag, f"{name} 封面缺 decoding=async: {tag[:80]}"

    def test_index_search_results(self):
        m = re.search(r'const cover = g\.cover \? `<img ([^`]*)>`', INDEX)
        assert m
        assert 'loading="lazy"' in m.group(1) and 'decoding="async"' in m.group(1)

    def test_library(self):
        # 封面地址：cover_view（服务器封面接口，本地优先/离线可看）优先，
        # 回退到原源站 URL
        m = re.search(r'<img src="\$\{esc\(c\.cover_view \|\| c\.cover[^>]*>', LIBRARY)
        assert m and 'loading="lazy"' in m.group(0) and 'decoding="async"' in m.group(0)

    def test_novel_detail(self):
        m = re.search(r"cv\.innerHTML = '<img src=\"' \+ esc\(d\.cover\) \+ '\" ([^']*)'", NOVEL_DETAIL)
        assert m and 'loading="lazy"' in m.group(1) and 'decoding="async"' in m.group(1)

    def test_manga_detail(self):
        # 主封面 + 相关推荐封面
        main = re.search(r'<img id="cover"[^>]*>', MANGA_DETAIL)
        assert main and 'loading="lazy"' in main.group(0)
        rel = re.search(r'it\.innerHTML = `<img ([^`]*)>`', MANGA_DETAIL)
        assert rel and 'loading="lazy"' in rel.group(1) and 'decoding="async"' in rel.group(1)

    def test_src_and_style_unchanged(self):
        # lazy 只加属性：src 表达式与样式串保持既有语义
        # （书库卡片 src 改为 cover_view 优先——本地封面接口，见
        #   tests/test_manga_cover_offline.py；索引页仍是源站 cover）
        assert 'src="${esc(c.cover_view || c.cover || \'\')}"' in LIBRARY
        assert 'aspect-ratio:3/4' in LIBRARY
        assert 'src="${esc(g.cover)}"' in INDEX


# ═══════════ node --check：全部改动脚本 ═══════════

def _inline_scripts(html):
    return re.findall(r"<script>([\s\S]*?)</script>", html)


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用，跳过语法检查")
@pytest.mark.parametrize("name,relpath", [
    ("common.js", "static/js/common.js"),
    ("index.html", "templates/index.html"),
    ("novel_detail.html", "templates/novel_detail.html"),
    ("sources.html", "templates/sources.html"),
    ("tasks.html", "templates/tasks.html"),
    ("manga_download.html", "templates/manga_download.html"),
    ("library.html", "templates/library.html"),
    ("manga_detail.html", "templates/manga_detail.html"),
])
def test_changed_scripts_node_check(name, relpath, tmp_path):
    source = (ROOT / relpath).read_text(encoding="utf-8")
    scripts = [source] if name.endswith(".js") else _inline_scripts(source)
    assert scripts, f"{name} 未抽取到脚本"
    for i, code in enumerate(scripts):
        # Jinja 占位（{{ ... }}）替换为 null 以便纯 JS 语法检查
        code = re.sub(r"\{\{[^}]*\}\}", "null", code)
        f = tmp_path / f"{name.replace('.', '_')}_{i}.js"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{name} 第 {i} 段脚本语法错误: {r.stderr[:500]}"
