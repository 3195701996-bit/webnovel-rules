# -*- coding: utf-8 -*-
"""A03 前端合并纯函数（static/js/group_merge.js）的 node 离线单测封装。

group_merge.js 是浏览器端"新增/更新"协议实际运行的同一份代码（UMD 双导出），
node 单测在 tests/js/group_merge.test.js。本用例负责在 pytest 套件里拉起它；
无 node 运行时的环境按任务约定跳过（此时以 Python 侧服务端协议回归 +
代码审查兜底，见 test_a03_stream_updates.py 与该测试文件头部注释）。
"""
import os
import shutil
import subprocess

import pytest

HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_group_merge_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("无 node 运行时：JS 合并逻辑单测跳过"
                    "（逻辑审查见 tests/js/group_merge.test.js 注释）")
    script = os.path.join(HUB, "tests", "js", "group_merge.test.js")
    r = subprocess.run([node, script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"node 单测失败:\n{r.stdout}\n{r.stderr}"
