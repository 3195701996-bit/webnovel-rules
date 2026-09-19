# -*- coding: utf-8 -*-
"""C05 小说预取缓存修复的轻量验证（沿用 A04/C03 的两层验证方式，不引入 JS 框架）。

验证方式：
1. 文本断言：缓存实现抽取为 static/js/chapter_cache.js（pending Map 在途去重 /
   Map 插入序 LRU / 完成入库时循环约束容量 / 代际失效）；reader.html 的 goto 与
   preloadChapter 改走共享缓存，旧的缺陷模式（无 pending、Object.keys 假 LRU、
   先查容量后写入）不复存在；A05/C03 的延迟进度提交与请求守卫不回退。
2. node --check：新组件与 reader.html 内联脚本语法均可解析。
3. node 可执行断言（均由本文件 parametrize 调用，断言“全部断言通过”完成标记 +
   子进程超时，防异步未跑完静默 exit 0）：
   - tests/js/chapter_cache.test.js：在途去重 / 失败重试 / 真 LRU / 乱序容量 / 版本失效
   - tests/js/search_seq.test.js：搜索代际——被取代搜索的旧读流响应不得污染新搜索状态
   - tests/js/reader_fetch.test.js：章节请求 cache:'no-cache' + 非 2xx 抛错
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CACHE_JS = (ROOT / "static" / "js" / "chapter_cache.js").read_text(encoding="utf-8")
READER = (ROOT / "templates" / "reader.html").read_text(encoding="utf-8")


def _goto_body():
    m = re.search(
        r"async function goto\(idx\) \{(?P<body>.*?)\n\}\n\n// 渲染章节",
        READER, re.S,
    )
    assert m, "goto() 未找到"
    return m.group("body")


# ═══════════ 缓存实现本身 ═══════════

class TestChapterCacheImpl:
    def test_pending_map_inflight_dedup(self):
        assert "var pending = new Map();" in CACHE_JS
        # 在途共享：pending 命中直接返回同一 Promise
        assert re.search(r"if \(pending\.has\(idx\)\) return pending\.get\(idx\);", CACHE_JS)

    def test_failure_deletes_pending_allows_retry(self):
        # 失败/取消路径删除 pending（拒绝分支里同一性守卫删除后 rethrow）
        assert re.search(
            r"function \(err\) \{\s*if \(pending\.get\(idx\) === p\) pending\.delete\(idx\);[\s\S]*?throw err;",
            CACHE_JS,
        )

    def test_capacity_enforced_on_put_loop(self):
        # 淘汰发生在完成入库时（put），while 循环约束容量——乱序返回不超限
        assert "while (cache.size > capacity) cache.delete(cache.keys().next().value);" in CACHE_JS

    def test_true_lru_by_map_insertion_order(self):
        assert "var cache = new Map();" in CACHE_JS
        # 命中重排到最新：get 与 peek 均 delete+set
        assert len(re.findall(r"cache\.delete\(idx\);\s*cache\.set\(idx,", CACHE_JS)) >= 2

    def test_invalidation_semantics(self):
        # 单章失效：连在途登记一起作废（重爬前发出的旧请求不得再入库/回传）
        assert "function invalidate(idx) { cache.delete(idx); pending.delete(idx); }" in CACHE_JS
        # invalidateAll：清缓存 + 清 pending（旧代在途不再被新 get 共享）+ 代际 bump
        assert re.search(
            r"function invalidateAll\(\) \{\s*cache\.clear\(\);\s*pending\.clear\(\);\s*gen\+\+;\s*\}",
            CACHE_JS,
        )
        # 跨代在途 resolve：不回传旧响应，以新代重新 get（重发/共享新代在途）
        assert "if (g !== gen) return get(idx);" in CACHE_JS
        # 单章 invalidate 后旧请求才返回：pending 身份守卫作废，改以当前代际重取
        assert "if (pending.get(idx) !== p) return get(idx);" in CACHE_JS
        # 同代且合规才入库
        assert "if (shouldCache(ch)) put(idx, ch);" in CACHE_JS

    def test_umd_dual_export(self):
        assert "module.exports = factory()" in CACHE_JS
        assert "root.ChapterCache = factory()" in CACHE_JS


# ═══════════ reader.html 接入 ═══════════

class TestReaderIntegration:
    def test_includes_cache_module(self):
        assert '<script src="/static/js/chapter_cache.js"></script>' in READER
        assert "ChapterCache.makeChapterCache({" in READER

    def test_goto_shares_inflight_via_cache(self):
        body = _goto_body()
        # 点击阅读经共享缓存取章（与在途预取复用同一 Promise）
        assert "await window._chCache.get(idx)" in body
        # goto 内不再直接发起章节 fetch（请求唯一出口在缓存层 fetchChapter）
        assert "await fetch('/api/books/' + KEY + '/chapter/'" not in body

    def test_preload_via_cache_prefetch(self):
        m = re.search(r"function preloadChapter\(idx\) \{(?P<body>.*?)\n\}", READER, re.S)
        assert m, "preloadChapter 未找到"
        assert "window._chCache.prefetch(idx);" in m.group("body")

    def test_old_defective_patterns_gone(self):
        # 旧缺陷：Object.keys 整数键升序假 LRU / 无 pending 的裸 fetch 预取
        assert "Object.keys(window._chCache)" not in READER
        assert "delete window._chCache[keys[0]]" not in READER
        assert "if (!window._chCache) window._chCache = {};" not in READER

    def test_invalidation_on_recrawl(self):
        # 单章重爬成功：该章缓存失效 + init 重入整书失效（正文版本变化）
        assert re.search(
            r"toast\('✓ 本章重爬成功[\s\S]*?window\._chCache\.invalidate\(idx\);[\s\S]*?await init\(\);",
            READER,
        )
        assert re.search(
            r"async function init\(\) \{[\s\S]*?window\._chCache\.invalidateAll\(\);",
            READER,
        )

    def test_c03_guards_compatible(self):
        # A05/C03 不回退：缓存命中与网络分支都是 校验→提交→渲染→存进度；
        # 请求序号守卫与 hasOld 保留旧正文逻辑仍在
        body = _goto_body()
        assert "const seq = ++_gotoSeq;" in body
        assert body.count("if (seq !== _gotoSeq) return;") >= 2
        assert "const hasOld = currentIdx >= 1;" in body
        cache_pos = body.index("window._chCache && window._chCache[idx]")
        net_pos = body.index("await window._chCache.get(idx)")
        commit_pos = body.index("currentIdx = idx;          // C03: 新章正文就绪")
        assert cache_pos < net_pos < commit_pos


# ═══════════ node --check：组件与 reader 内联脚本语法 ═══════════

@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用，跳过语法检查")
@pytest.mark.parametrize("name,source", [
    ("chapter_cache.js", CACHE_JS),
    ("reader.html", READER),
])
def test_node_check(name, source, tmp_path):
    scripts = [source] if name.endswith(".js") else \
        re.findall(r"<script>([\s\S]*?)</script>", source)
    assert scripts, f"{name} 未抽取到脚本"
    for i, code in enumerate(scripts):
        # Jinja 占位替换为合法 JS 字面量后再做语法检查
        code = code.replace("{{ book_key | tojson }}", "'bk-test'")
        f = tmp_path / f"{name.replace('.', '_')}_{i}.js"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{name} 第 {i} 段脚本语法错误:\n{r.stderr}"


# ═══════════ node 可执行断言：deferred 模拟网络驱动线上缓存 ═══════════

@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用，跳过可执行断言")
@pytest.mark.parametrize("script_name", [
    "chapter_cache.test.js",   # 缓存：在途去重/失败重试/真 LRU/版本失效
    "search_seq.test.js",      # 搜索代际：旧搜索读流 await 后不得污染新搜索状态
    "reader_fetch.test.js",    # 阅读器章节请求：cache:'no-cache' + 非 2xx 抛错
])
def test_chapter_cache_js_semantics(script_name):
    script = ROOT / "tests" / "js" / script_name
    r = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"{script_name} node 单测失败:\n{r.stdout}\n{r.stderr}"
    # 明确完成标记：node 内测试自带 watchdog（未跑完主流程会 exit 1）；此处再断言
    # 完成标记，防止“存在未 resolve 的 Promise → 事件循环空转 exit 0”的假通过。
    assert "全部断言通过" in r.stdout, \
        f"{script_name} 未打印完成标记，异步主流程疑似未跑完：\n{r.stdout}\n{r.stderr}"
