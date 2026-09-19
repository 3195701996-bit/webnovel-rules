# -*- coding: utf-8 -*-
"""A04/C03 前端正确性修复的轻量验证（项目无 JS 测试基建，不引入框架）。

验证方式两层：
1. 文本断言：确认关键守卫在模板源码中存在且相对顺序正确（代码审查的机器化）。
2. 可执行断言（有 node 时）：把 reader.html 中抽出的纯函数 pickEntryChapter
   原样抠出交给 node 执行，验证入口章优先级语义：显式参数 > 续读历史 > 首个已下载章。
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NOVEL_DETAIL = (ROOT / "templates" / "novel_detail.html").read_text(encoding="utf-8")
READER = (ROOT / "templates" / "reader.html").read_text(encoding="utf-8")


# ═══════════ A04：换源竞态 generation 守卫 ═══════════

class TestA04SourceRaceGuard:
    def test_generation_counter_and_abort_controller(self):
        assert "let _srcGen = 0;" in NOVEL_DETAIL
        assert "let _srcAbort = null;" in NOVEL_DETAIL
        assert "_srcAbort.abort()" in NOVEL_DETAIL
        assert "new AbortController()" in NOVEL_DETAIL
        # 换源统一入口：下拉切换与首次加载都走 selectSource
        assert "sel.onchange = () => selectSource(" in NOVEL_DETAIL
        assert "selectSource(SEARCH_SOURCES[0])" in NOVEL_DETAIL

    def test_generation_checked_in_all_async_paths(self):
        # 详情/目录成功路径 + 两个下载按钮闭包 = 至少 4 处序号守卫
        guards = re.findall(r"if \(gen !== _srcGen\) return;", NOVEL_DETAIL)
        assert len(guards) >= 4, f"序号守卫不足：{len(guards)}"
        # 错误提示路径：AbortError 或过期序号都不落地（目录 + 详情各一处）
        abort_guards = re.findall(r"e\.name === 'AbortError' \|\| gen !== _srcGen", NOVEL_DETAIL)
        assert len(abort_guards) >= 2, f"AbortError 守卫不足：{len(abort_guards)}"

    def test_fetch_carries_abort_signal(self):
        # search-detail / search-toc 两个请求都挂 signal
        assert re.search(r"/api/search-detail'[^\n]*signal", NOVEL_DETAIL)
        assert re.search(r"/api/search-toc'[^\n]*signal", NOVEL_DETAIL)

    def test_source_specific_fields_cleared_on_switch(self):
        # selectSource 内清理上一源专有字段（简介/封面/统计/按钮绑定）
        m = re.search(r"function selectSource\(src\) \{(?P<body>.*?)\n\}", NOVEL_DETAIL, re.S)
        assert m, "selectSource 未定义"
        body = m.group("body")
        assert "$('#intro').textContent = '';" in body
        assert "$('#intro').style.display = 'none';" in body
        assert "$('#cover').innerHTML = '📖';" in body
        assert "$('#info-sec').innerHTML = '';" in body
        assert "$('#dl-btn').onclick = null;" in body

    def test_intro_not_inherited_when_new_source_lacks_it(self):
        # 详情成功分支：新源无简介时显式隐藏，不沿用旧简介
        assert re.search(
            r"if \(d\.intro\) \{[^}]*\}\s*else \{[^}]*intro[^}]*display = 'none'",
            NOVEL_DETAIL,
        )


# ═══════════ C03：章节参数 + 进度提交时机 ═══════════

def _goto_body():
    m = re.search(
        r"async function goto\(idx\) \{(?P<body>.*?)\n\}\n\n// 渲染章节",
        READER, re.S,
    )
    assert m, "goto() 未找到"
    return m.group("body")


class TestC03ChapterParam:
    def test_detail_chapter_link_carries_ch_param(self):
        # 书库模式已下载章节链接携带章节参数
        assert re.search(
            r"/reader/' \+ encodeURIComponent\(KEY\) \+ '\?ch=' \+ c\.index",
            NOVEL_DETAIL,
        )

    def test_reader_parses_ch_param(self):
        assert "new URLSearchParams(location.search).get('ch')" in READER
        assert "_chParamPending" in READER

    def test_entry_priority_param_before_history(self):
        assert "function pickEntryChapter(chParam, savedIdx, total, firstDl)" in READER
        # init 内显式参数分支出现在 progress 历史请求之前
        param_branch = READER.index("if (_chParamPending >= 1 && _chParamPending <= BOOK.total)")
        history_fetch = READER.index("fetch('/api/books/' + encodeURIComponent(KEY) + '/progress')")
        assert param_branch < history_fetch


class TestC03DeferredProgressCommit:
    def test_no_commit_before_fetch_in_goto(self):
        body = _goto_body()
        # 旧缺陷模式：一切章立刻 currentIdx = idx + savePos()——必须不复存在
        assert not re.search(r"\+\+_gotoSeq;\s*currentIdx = idx;\s*savePos\(\);", body)
        # goto 顶部（缓存命中分支之前）不得有任何提交
        head = body[:body.index("window._chCache")]
        assert "currentIdx = idx" not in head
        assert "savePos()" not in head

    def test_commit_only_after_content_ready(self):
        body = _goto_body()
        # 网络分支（C05 起请求经共享缓存 window._chCache.get 发起，预取/阅读在途去重）：
        # downloaded/content 校验 → 提交 displayedIdx → 渲染 → savePos
        net = body[body.index("window._chCache.get(idx)"):]
        order = [
            net.index("if (!ch.downloaded || typeof ch.content !== 'string')"),
            net.index("currentIdx = idx;          // C03: 新章正文就绪"),
            net.index("renderChapter(ch);"),
            net.index("savePos();                 // C03: 渲染成功后才保存进度"),
        ]
        assert order == sorted(order), "提交/渲染/存进度顺序错误"
        # 缓存命中分支同样先提交后渲染再存
        cache_pos = body.index("window._chCache && window._chCache[idx]")
        cache_block = body[cache_pos:body.index("const hasOld")]
        cache_order = [
            cache_block.index("currentIdx = idx"),
            cache_block.index("renderChapter(window._chCache[idx]);"),
            cache_block.index("savePos();"),
        ]
        assert cache_order == sorted(cache_order), "缓存分支提交顺序错误"

    def test_failure_keeps_old_content_and_position(self):
        body = _goto_body()
        assert "const hasOld = currentIdx >= 1;" in body
        # 未下载与异常两个分支都有 hasOld 保留旧正文逻辑
        assert body.count("if (hasOld)") >= 2
        assert "章节加载失败（已保留当前章节）" in body


# ═══════════ 可执行断言：node 运行抠出的纯函数 ═══════════

@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用，跳过可执行断言")
def test_pick_entry_chapter_semantics_with_node():
    m = re.search(
        r"(function pickEntryChapter\(chParam, savedIdx, total, firstDl\) \{.*?\n\})",
        READER, re.S,
    )
    assert m, "pickEntryChapter 未找到"
    script = m.group(1) + """
const cases = [
  [[5, 3, 100, 1], 5],    // 显式参数 > 历史
  [[0, 3, 100, 1], 3],    // 无参数 → 历史
  [[0, 0, 100, 7], 7],    // 无历史 → 首个已下载
  [[0, 0, 100, 0], 1],    // 全空 → 兜底第 1 章
  [[200, 3, 100, 1], 3],  // 参数越界 → 回退历史
  [[5, 0, 3, 1], 1],      // 参数越界且无历史 → 首个已下载
];
for (const [args, want] of cases) {
  const got = pickEntryChapter(...args);
  if (got !== want) { console.error(`FAIL ${JSON.stringify(args)}: got ${got}, want ${want}`); process.exit(1); }
}
console.log('pickEntryChapter: all cases pass');
"""
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
