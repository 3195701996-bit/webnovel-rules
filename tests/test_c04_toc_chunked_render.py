# -*- coding: utf-8 -*-
"""C04 目录分段渲染组件化的轻量验证（沿用 A04/C03 的两层验证方式，不引入 JS 框架）。

验证方式：
1. 文本断言：共享组件 static/js/toc_render.js 存在且语义完整（分段常量/纯函数/
   UMD 双导出）；reader.html 与 novel_detail.html 均复用组件、旧的一次性全量
   挂载模式不复存在；既有守卫（A04 换源序号、C03 ?ch= 链接、目录搜索作用于
   全量数组）不回退。
2. node --check：新组件与两个模板的内联脚本语法均可解析。
3. node 可执行断言：tests/js/toc_render.test.js 用假 DOM 驱动线上同一份组件，
   验证 2000 章长目录首屏挂载有界、搜索能找到未挂载章节、locate/扩段语义。
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOC_JS = (ROOT / "static" / "js" / "toc_render.js").read_text(encoding="utf-8")
NOVEL_DETAIL = (ROOT / "templates" / "novel_detail.html").read_text(encoding="utf-8")
READER = (ROOT / "templates" / "reader.html").read_text(encoding="utf-8")


# ═══════════ 组件本身 ═══════════

class TestTocRenderComponent:
    def test_chunk_constant_and_pure_functions(self):
        assert re.search(r"var TOC_CHUNK = 200;", TOC_JS), "分段常量 TOC_CHUNK=200"
        assert "function chunkWindow(targetIdx, total, chunk)" in TOC_JS
        assert "function filterChapters(chapters, kw)" in TOC_JS
        assert "function makeTocView(opts)" in TOC_JS

    def test_umd_dual_export(self):
        # 浏览器 window.TocRender / node module.exports（node 单测跑线上同一份代码）
        assert "module.exports = factory()" in TOC_JS
        assert "root.TocRender = factory()" in TOC_JS

    def test_search_filters_full_array_not_mounted_dom(self):
        # 搜索作用于全量目录数组（chapters.filter），未挂载章节同样可被找到
        m = re.search(r"function filterChapters[\s\S]*?\n  \}", TOC_JS)
        assert m and "chapters.filter" in m.group(0)

    def test_bounded_mount_by_design(self):
        # 未过滤态只渲染 [from, to] 区间；过滤态按 chunk 步长增量挂载
        assert "for (var i = from; i <= to; i++)" in TOC_JS
        assert "var end = Math.min(hitMounted + chunk, hits.length);" in TOC_JS


# ═══════════ reader.html ═══════════

class TestReaderTemplate:
    def test_includes_shared_component(self):
        assert '<script src="/static/js/toc_render.js"></script>' in READER
        assert "TocRender.makeTocView({" in READER

    def test_no_inline_chunk_rendering_leftover(self):
        # 旧的内联分段实现（_tocFrom/_tocTo/makeExpandBtn）不复存在
        assert "_tocFrom" not in READER
        assert "_tocTo" not in READER
        assert "makeExpandBtn" not in READER

    def test_locate_and_expand_via_component(self):
        # 翻章跨段定位走组件 locate；过滤态不干预的守卫保留
        assert re.search(r"function ensureTocVisible\(idx\) \{[\s\S]*?tocView\.locate\(idx\)", READER)
        assert "(window._tocFilter || '').trim()" in READER

    def test_toc_search_input_wired_to_full_array(self):
        assert 'id="toc-search"' in READER
        assert re.search(
            r"tocSearch\.oninput = \(\) => \{\s*window\._tocFilter = tocSearch\.value;\s*"
            r"tocView\.setFilter\(tocSearch\.value\);\s*tocView\.render\(\);",
            READER,
        )


# ═══════════ novel_detail.html ═══════════

class TestNovelDetailTemplate:
    def test_includes_shared_component_and_search_box(self):
        assert '<script src="/static/js/toc_render.js"></script>' in NOVEL_DETAIL
        assert 'id="chap-search"' in NOVEL_DETAIL
        # 书库模式 + 搜索模式两处目录都走组件
        assert NOVEL_DETAIL.count("TocRender.makeTocView({") >= 2

    def test_no_full_mount_foreach_leftover(self):
        # 旧缺陷模式：chs.forEach 一次性挂载全部章节——必须不复存在
        assert "chs.forEach" not in NOVEL_DETAIL

    def test_c03_chapter_link_semantics_preserved(self):
        # 点击跳章仍是 ?ch=N 链接（组件 makeItem 内语义不变）
        assert re.search(
            r"/reader/' \+ encodeURIComponent\(KEY\) \+ '\?ch=' \+ c\.index",
            NOVEL_DETAIL,
        )

    def test_locate_current_chapter_via_progress(self):
        # 首屏核心信息先展示；当前章定位由异步 progress 请求完成（不阻塞）
        assert "detailToc.locate(idx)" in NOVEL_DETAIL
        assert "/progress'" in NOVEL_DETAIL

    def test_a04_guards_preserved(self):
        assert "let _srcGen = 0;" in NOVEL_DETAIL
        assert NOVEL_DETAIL.count("if (gen !== _srcGen) return;") >= 4


# ═══════════ node --check：组件与模板内联脚本语法 ═══════════

def _inline_scripts(html):
    return re.findall(r"<script>([\s\S]*?)</script>", html)


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用，跳过语法检查")
@pytest.mark.parametrize("name,source,jinja_sub", [
    ("toc_render.js", TOC_JS, False),
    ("novel_detail.html", NOVEL_DETAIL, False),
    ("reader.html", READER, True),
])
def test_node_check(name, source, jinja_sub, tmp_path):
    scripts = [source] if name.endswith(".js") else _inline_scripts(source)
    assert scripts, f"{name} 未抽取到脚本"
    for i, code in enumerate(scripts):
        if jinja_sub:
            # Jinja 占位替换为合法 JS 字面量后再做语法检查
            code = code.replace("{{ book_key | tojson }}", "'bk-test'")
        f = tmp_path / f"{name.replace('.', '_')}_{i}.js"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{name} 第 {i} 段脚本语法错误:\n{r.stderr}"


# ═══════════ node 可执行断言：假 DOM 驱动线上组件 ═══════════

@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用，跳过可执行断言")
def test_toc_render_js_semantics():
    script = ROOT / "tests" / "js" / "toc_render.test.js"
    r = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"node 单测失败:\n{r.stdout}\n{r.stderr}"
