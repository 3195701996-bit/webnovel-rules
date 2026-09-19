# -*- coding: utf-8 -*-
"""C02 任务页/漫画页 DOM 稳定更新的轻量验证（沿用 C01/B03 的两层验证方式）。

验证方式：
1. 文本断言：
   - tasks.html 不再有"每轮轮询整表 innerHTML 替换"模式——表仅在不存在时
     创建一次，行按任务 ID upsert，ops 容器仅按操作签名（类型+状态）重建，
     rowSig 未变时整行零 DOM 写入；
   - 加载态 aria-busy、克制的 aria-live（仅已有行状态迁移播报一句）；
   - 行级 busy 守卫：任务有未完成操作时禁止重复提交，节点不重建不丢焦点；
   - 焦点捕获/恢复逻辑存在；
   - manga.html 流式状态栏取消按钮节点稳定（复用 index.html P2-3 模式），
     逐源进度只更文案，aria-busy 表达流式加载态，终态才播报。
2. node 可执行断言（有 node 时）：抠出纯函数 _taskRowParts，验证
   "同数据同签名（→ 零 DOM 写入）、进度变化改 rowSig 但不动 opsSig
   （→ 按钮不重建、焦点保留）、状态变化改 opsSig（→ 才重建 ops）"。
3. node --check：两个模板的内联脚本语法均可解析。
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TASKS = (ROOT / "templates" / "tasks.html").read_text(encoding="utf-8")
MANGA = (ROOT / "templates" / "manga.html").read_text(encoding="utf-8")


# ═══════════ tasks.html：行级 upsert，无整表替换 ═══════════

class TestTasksRowUpsert:
    def test_table_created_once_not_rebuilt_per_poll(self):
        # 表只在不存在时创建；每轮轮询不再 createElement('table') + 整表替换
        assert "let tbl = box.querySelector('table.task-table');" in TASKS
        assert "if (!tbl) {" in TASKS
        assert TASKS.count("document.createElement('table')") == 1
        assert TASKS.count("box.appendChild(tbl)") == 1
        # 旧模式：循环里向新表 tbody 塞全新行——必须不复存在
        assert "tbl.querySelector('tbody').appendChild(tr);" not in TASKS

    def test_rows_keyed_by_task_id(self):
        assert "tr.dataset.taskId = id;" in TASKS
        assert "function upsertTaskRow(tbody, rows, t) {" in TASKS

    def test_no_change_no_dom_write(self):
        # rowSig 快速通道：签名一致直接返回，零 DOM 写入
        assert "tr.dataset.rowSig === parts.rowSig) return false;" in TASKS
        assert "无变化不改 DOM" in TASKS

    def test_ops_rebuilt_only_on_ops_signature_change(self):
        # 操作按钮生命周期稳定：仅 opsSig（类型+状态）变化才重建 ops 容器
        assert "tr.dataset.opsSig !== parts.opsSig" in TASKS
        assert "tr.querySelector('.ops').innerHTML = parts.ops;" in TASKS
        # 行内容签名聚合四格，ops 签名独立
        assert "rowSig: JSON.stringify([nameHtml, stKey, progHtml, opsSig])" in TASKS

    def test_disappeared_rows_removed_and_order_reconciled(self):
        assert "if (!keep.has(tr.dataset.taskId)) tr.remove();" in TASKS
        assert "tbody.insertBefore(tr, cursor)" in TASKS

    def test_focus_capture_and_restore(self):
        # 键盘聚焦暂停按钮后轮询刷新焦点仍在该按钮的支撑逻辑
        assert "function captureFocus(tbody)" in TASKS
        assert "function restoreFocus(tbody, saved)" in TASKS
        assert "document.activeElement" in TASKS
        assert "btn.focus()" in TASKS
        # 焦点未丢时不干预
        assert "焦点未丢，不干预" in TASKS

    def test_busy_guard_blocks_duplicate_submit(self):
        # 任务有未完成操作时禁止重复提交；节点稳定，焦点不丢
        assert re.search(r"if \(tr && tr\.dataset\.busy === '1'\) return;", TASKS)
        assert "tr.dataset.busy = '1';" in TASKS

    def test_aria_busy_for_loading(self):
        assert 'id="tasks" aria-busy="true"' in TASKS
        assert "box.setAttribute('aria-busy', 'true')" in TASKS
        assert "box.removeAttribute('aria-busy')" in TASKS

    def test_restrained_aria_live(self):
        # 专用 live 区，仅已有行状态迁移时播报一句；不给整表挂 live
        assert 'id="tasks-live" aria-live="polite"' in TASKS
        assert "function announceTask(" in TASKS
        assert re.search(
            r"tr\.dataset\.status && tr\.dataset\.status !== t\.status\)\s*\{\s*announceTask\(",
            TASKS)
        # 整表不挂 live：aria-live 属性全文件仅出现在专用播报区
        assert TASKS.count('aria-live="polite"') == 1

    def test_fetch_failure_preserves_existing_table(self):
        # 已有表格时轮询失败仅 toast，不清空表格（不丢焦点）
        assert re.search(
            r"if \(box\.querySelector\('\.task-table'\)\) \{\s*toast\('⚠ 任务刷新失败",
            TASKS)

    def test_c01_b03_contracts_preserved(self):
        assert "await requestJSON('/api/tasks')" in TASKS
        assert "createPoller(loadTasks" in TASKS
        assert "tasksPoller.kick()" in TASKS
        # C01 操作按钮仍走 requestJSON + button 恢复
        assert "box.querySelectorAll('.pause-btn')" in TASKS
        assert "/pause', {method: 'POST', button: b}" in TASKS
        assert "/stop', {method: 'POST', button: b}" in TASKS


# ═══════════ manga.html：流式状态栏稳定子节点 ═══════════

class TestMangaStableStatusBar:
    def test_status_nodes_mounted_once(self):
        assert "function _mangaStatusNodes(st) {" in MANGA
        # 仅当节点被外部 textContent 清空（isConnected=false）才重建
        assert "!st._nodes.btn.isConnected" in MANGA
        assert MANGA.count("st.innerHTML = ''") == 1
        # 取消按钮只 createElement 一次、abort 处理器稳定绑定
        assert "btn.id = 'btn-cancel-manga-search';" in MANGA
        assert "btn.onclick = () => { if (window._mangaSearchCtrl)" in MANGA

    def test_no_unconditional_status_innerhtml_rebuild(self):
        # 旧模式：每次进度 tick 整段 innerHTML 重建（含取消按钮）——必须不复存在
        assert "st.innerHTML = '<span class=\"spinner\"></span>'" not in MANGA
        # 进度更新只写 textContent
        assert "n.prog.textContent = (done != null" in MANGA
        assert "n.q.textContent = '🔍 ' + q;" in MANGA

    def test_per_source_progress_still_updates(self):
        assert re.search(r"if \(!silent\) setSearchStatus\(st, q, d\.done, d\.total",
                         MANGA)

    def test_aria_busy_during_streaming(self):
        assert "st.setAttribute('aria-busy', 'true')" in MANGA
        assert re.search(r"finally \{\s*clearTimeout\(timeout\);\s*"
                         r"ctrl\.abort\(\);\s*if \(myGen === SEARCH\.gen\) "
                         r"st\.removeAttribute\('aria-busy'\);", MANGA)

    def test_restrained_aria_live_terminal_only(self):
        assert "function announceManga(" in MANGA
        assert "aria-live', 'polite'" in MANGA
        # 完成/空结果/取消(有结果)/取消(无结果)/失败 各播报一句
        assert MANGA.count("announceManga(") >= 5


# ═══════════ node 可执行断言：行签名逻辑 ═══════════

def _extract_row_parts():
    # fmtEta + _taskRowParts 均为纯函数，原样抠出交给 node 验证签名语义
    m = re.search(r"(function fmtEta\(sec\) \{[\s\S]*?)\n\n// C02: 克制的状态播报",
                  TASKS)
    assert m, "fmtEta/_taskRowParts 未找到"
    return m.group(1)


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node 不可用，跳过可执行断言")
def test_row_signature_semantics_with_node(tmp_path):
    script = """
'use strict';
const esc = s => String(s == null ? '' : s);
%s
function assert(c, m) { if (!c) { console.error('FAIL: ' + m); process.exit(1); } }

const base = {id: 'n1', type: 'novel', status: 'running', book_key: 'bk',
              progress: {completed: 5, total: 100, book_name: '书'}};
const clone = JSON.parse(JSON.stringify(base));

// 1) 同数据 → 同 rowSig：轮询拿到无变化数据时整行零 DOM 写入
assert(_taskRowParts(base).rowSig === _taskRowParts(clone).rowSig,
       '同数据应产生同签名（无变化不改 DOM）');

// 2) 进度变化 → rowSig 变、opsSig 不变：进度刷新只更进度格，
//    操作按钮不重建，键盘焦点稳定留在按钮上
const prog = JSON.parse(JSON.stringify(base));
prog.progress.completed = 6;
assert(_taskRowParts(prog).rowSig !== _taskRowParts(base).rowSig,
       '进度变化应改行签名');
assert(_taskRowParts(prog).opsSig === _taskRowParts(base).opsSig,
       '进度变化不应改操作签名（按钮不重建）');

// 3) 状态变化 → opsSig 变：running→paused 按钮集合才重建
const paused = JSON.parse(JSON.stringify(base));
paused.status = 'paused';
assert(_taskRowParts(paused).opsSig !== _taskRowParts(base).opsSig,
       '状态变化应改操作签名');

// 4) 小说/漫画类型差异也体现在 opsSig
const manga = {id: 'manga_1', type: 'manga', status: 'running',
               book_key: 'c1', source_uid: 's', title: '漫', progress: {done: 1, total: 3}};
assert(_taskRowParts(manga).opsSig === 'm:running', '漫画 opsSig 应带类型前缀');
console.log('ok');
""" % _extract_row_parts()
    f = tmp_path / "c02_row_sig.js"
    f.write_text(script, encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"行签名语义断言失败:\n{r.stderr}"
    assert "ok" in r.stdout


# ═══════════ node --check：内联脚本语法 ═══════════

def _inline_scripts(html):
    return re.findall(r"<script>([\s\S]*?)</script>", html)


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用，跳过语法检查")
@pytest.mark.parametrize("name,source", [
    ("tasks.html", TASKS),
    ("manga.html", MANGA),
])
def test_inline_scripts_node_check(name, source, tmp_path):
    scripts = _inline_scripts(source)
    assert scripts, f"{name} 未抽取到脚本"
    for i, code in enumerate(scripts):
        f = tmp_path / f"{name.replace('.', '_')}_{i}.js"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{name} 第 {i} 段脚本语法错误:\n{r.stderr}"
