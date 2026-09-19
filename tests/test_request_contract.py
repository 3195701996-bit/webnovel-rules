# -*- coding: utf-8 -*-
"""R78 回归：客户端**写请求 body** 的键 ↔ 服务端**真正读**的键（工具守卫）。

`tools/check_request_contract.py` 补的是一个**一直没人守的方向**：
此前所有核对都是"服务端发的键，客户端会不会读到"（响应方向）。反方向同样会静默坏，
而且坏起来更隐蔽 —— 用户点了按钮，什么都没发生，两边都不报错：

  · 客户端发 `{"latest": true}`、服务端读 `dir`/`files` → 弹窗承诺"恢复**最近一次**清理
    的源文件"，实际 `batch=None` 落到"遍历所有批次"，把用户**故意删掉**的源也带回来
    并解除墓碑（本文件写下的当天就是靠这个工具抓到的，见 `test_removed_restore_latest.py`）；
  · 这类漂移不会抛异常：服务端安静地用默认值，界面安静地显示"成功"。

守卫分三层：
  1. **真源跑绿**：工具对真实代码必须 0 问题、0 未解析（未解析=覆盖面漏洞，同样算失败）；
  2. **单元级**：四种 body 写法（内联链 / 转义 JSON 字面量 / 局部变量 / 形参+解构形参）
     都必须能抽出键 —— 抽不出来就会"看起来在核对，其实一个键都没核对"；
  3. **检测力**：用合成数据验证"客户端发 index、服务端读 idx"这类漂移**真的会被报出来**。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import check_request_contract as T  # noqa: E402


def test_tool_is_green_on_real_sources():
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools",
                                                     "check_request_contract.py")],
                       cwd=ROOT, capture_output=True, text=True, timeout=600, check=False,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                            "WR_TEST": "1", "WR_DISABLE_BACKGROUND": "1"})
    tail = "\n".join(r.stdout.strip().split("\n")[-8:])
    assert r.returncode == 0, f"发现请求键漂移：\n{tail}\n\n{r.stderr[-800:]}"
    assert "发现问题 0 个" in r.stdout, tail
    assert "未解析 body 0 个" in r.stdout, (
        "有 body 解析不了 → 覆盖面的漏洞（宁可逼着补解析，也不许悄悄跳过）：\n" + tail)


# ── 单元级：四种 body 写法都要抽得出来 ────────────────────────────────

def _kt(text):
    """把一段 Kotlin 当"某文件正文"，按调用点方式解析 body 键。"""
    src = T.strip_comments(text)
    m = __import__("re").search(r"\.httpPost\(", src)
    args = T.call_args(src, m.end() - 1)
    body = args[2] if len(args) > 2 else None      # 没有第三个实参 = 没有 body
    return T._keys_from_expr(body, [src], same_file=src, before=m.start())


def test_inline_chain_multiline():
    assert _kt('x.httpPost(p, "/api/x", JSONObject().put("a", 1)\n'
               '    .put("b", 2).toString())') == ({"a", "b"}, True)


def test_escaped_json_literal():
    assert _kt('x.httpPost(p, "/api/x", "{\\"dry_run\\":false}")') == ({"dry_run"}, True)


def test_local_val_chain_uses_nearest_declaration():
    """同名变量出现两次时必须取**调用点之前最近**的那次（否则核对的键是别人的）。"""
    code = ('val body = JSONObject().put("first", 1).toString()\n'
            'x.httpPost(p, "/api/x", body)\n'
            'val body = JSONObject().put("second", 2).toString()\n'
            'x.httpPost(p, "/api/y", body)\n')
    src = T.strip_comments(code)
    first = src.index("/api/x")
    second = src.index("/api/y")
    k1, _ = T._keys_from_expr("body", [src], same_file=src, before=first)
    k2, _ = T._keys_from_expr("body", [src], same_file=src, before=second)
    assert k1 == {"first"} and k2 == {"second"}, (k1, k2)


def test_function_param_resolved_from_call_site():
    code = ('suspend fun doClear(body: JSONObject) {\n'
            '    x.httpPost(p, "/api/x", body.toString())\n'
            '}\n'
            'fun ui() { scope.launch { doClear(JSONObject().put("scope", "regen")) } }\n')
    assert _kt(code) == ({"scope"}, True)


def test_destructured_lambda_param_resolved_from_pair():
    """`confirm?.let { (text, body) -> … }` 的键在 `confirm = … to …` 的赋值处。"""
    code = ('var confirm by remember { mutableStateOf<Pair<String, JSONObject>?>(null) }\n'
            'suspend fun doClear(body: JSONObject) {\n'
            '    x.httpPost(p, "/api/x", body.toString())\n'
            '}\n'
            'fun ui() {\n'
            '    confirm = "清理缓存？" +\n'
            '        "已下载内容不受影响。" to\n'
            '        JSONObject().put("scope", "regen")\n'
            '    confirm?.let { (text, body) -> scope.launch { doClear(body) } }\n'
            '}\n')
    assert _kt(code) == ({"scope"}, True)


def test_empty_and_missing_body_are_not_unresolved():
    assert _kt('x.httpPost(p, "/api/x")') == (set(), True)
    assert _kt('x.httpPost(p, "/api/x", "")') == (set(), True)


# ── 检测力：漂移必须被报出来 ──────────────────────────────────────────

def test_compare_flags_client_key_server_never_reads():
    routes = {"/api/x": ({"idx", "name"}, "/api/x")}
    calls = [("A.kt", 1, "httpPost", "/api/x", '"/api/x"', {"index"}, True, "")]
    problems, unresolved, _ = T.compare(routes, calls, optional={})
    assert problems and "index" in problems[0], problems
    assert not unresolved


def test_compare_flags_server_key_client_never_sends():
    routes = {"/api/x": ({"idx"}, "/api/x")}
    calls = [("A.kt", 1, "httpPost", "/api/x", '"/api/x"', {"name"}, True, "")]
    problems, _, _ = T.compare(routes, calls, optional={})
    kinds = " ".join(problems)
    assert "name" in kinds and "idx" in kinds, problems  # 两个方向都要报


def test_compare_accepts_registered_server_only_key():
    routes = {"/api/x": ({"idx", "uids"}, "/api/x")}
    calls = [("A.kt", 1, "httpPost", "/api/x", '"/api/x"', {"idx"}, True, "")]
    problems, _, _ = T.compare(routes, calls, optional={"/api/x uids": "网页端专用"})
    assert not problems, problems


def test_compare_rejects_stale_registration():
    """登记表里留着已不存在的路径/键 → 必须报（否则登记表会烂在手里）。"""
    routes = {"/api/x": ({"idx"}, "/api/x")}
    calls = [("A.kt", 1, "httpPost", "/api/x", '"/api/x"', {"idx"}, True, "")]
    problems, _, _ = T.compare(routes, calls, optional={"/api/gone key": "旧条目"})
    assert any("登记表过期" in p for p in problems), problems


def test_unresolved_body_counts_as_failure():
    """解析不了 body 也算失败：否则"看不懂就跳过"会把覆盖面悄悄缩小。"""
    calls = [("A.kt", 1, "httpPost", "/api/x", '"/api/x"', set(), False, "weirdBody()")]
    _problems, unresolved, _ = T.compare({"/api/x": (set(), "/api/x")}, calls, optional={})
    assert len(unresolved) == 1, unresolved


# ── 服务端侧：转发（处理函数把 body 交给下层函数）也要跟进去 ──────────────

def test_server_keys_follow_delegation_into_other_module():
    routes = T.server_routes()
    keys = routes["/api/storage/clear"][0]
    # 键在 server/storage.py::clear(target) 里读，不在路由处理函数里
    assert {"scope", "source", "comic_id"} <= keys, keys


def test_server_keys_cover_tasks_and_progress():
    routes = T.server_routes()
    assert {"book_url", "source_uid"} <= routes["/api/tasks"][0]
    assert {"idx", "pct", "name"} <= routes["/api/books/*/progress"][0]
