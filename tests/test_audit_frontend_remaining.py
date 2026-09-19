# -*- coding: utf-8 -*-
"""前端剩余健壮性修复的 pytest 包装（沿用 C01/B03/C02 的分层验证方式）。

被测对象：tests/js/audit_frontend_remaining.test.js —— 用 node:vm 从模板/脚本
**原样抽取真实函数**执行的离线回归（与线上同一份代码），覆盖：
  1. sources.html   loadSources / selectedUids / batchToggle / srcAutoRefresh
  2. manga_detail.html toggleFav
  3. manga_reader.html renderScroll / _mountImg / showPage / renderChapter
  4. library.html   mangaPollLoop / mangaShowResult / mangaPollThenFinish
  5. netip.js       poll（在途去重 + finally 清理 abort 定时器）

本包装只做两件事：
  - node --check：JS 测试脚本自身语法可解析；
  - 子进程（带 timeout）运行完整断言，要求 returncode==0 且输出含完成标记
    （防"进程被杀 / 提前退出仍算通过"的假成功）。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS_TEST = ROOT / "tests" / "js" / "audit_frontend_remaining.test.js"

# 完成标记：与测试脚本末尾 console.log 完全一致，防止无输出/中断被判通过
DONE_MARKER = "audit_frontend_remaining.test.js: 全部断言通过"

# 测试脚本内置 5s watchdog；子进程级再给更宽裕的硬超时兜底
RUN_TIMEOUT_S = 60


def _node_available() -> bool:
    return shutil.which("node") is not None


@pytest.mark.skipif(not _node_available(), reason="node 不可用，跳过可执行断言")
def test_audit_frontend_remaining_node_check():
    """JS 测试脚本自身必须语法可解析。"""
    assert JS_TEST.is_file(), f"缺少测试脚本：{JS_TEST}"
    r = subprocess.run(
        ["node", "--check", str(JS_TEST)],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"测试脚本语法错误:\n{r.stderr}"


@pytest.mark.skipif(not _node_available(), reason="node 不可用，跳过可执行断言")
def test_audit_frontend_remaining_runs_green():
    """完整执行 node 行为测试：必须全绿且打印完成标记。"""
    assert JS_TEST.is_file(), f"缺少测试脚本：{JS_TEST}"
    try:
        r = subprocess.run(
            ["node", str(JS_TEST)],
            capture_output=True, text=True, timeout=RUN_TIMEOUT_S,
            cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"前端健壮性测试超时（>{RUN_TIMEOUT_S}s）——疑似存在未 resolve 的 Promise"
        )
    assert r.returncode == 0, (
        f"前端健壮性断言失败 (exit={r.returncode}):\n"
        f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    )
    assert DONE_MARKER in r.stdout, (
        f"未检测到完成标记，疑似假通过：\n{'-' * 40}\n{r.stdout}\n{r.stderr}"
    )
