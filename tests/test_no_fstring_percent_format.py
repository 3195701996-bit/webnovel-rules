# -*- coding: utf-8 -*-
"""R78 回归：禁止"f-string 再做 % 格式化"（真实事故写法）。

事故（2026-09-18，漫画回归实测抓到）：
    raise WebUnreachable(f"网页通道超时（预算 %.0fs 用尽）: {url[:100]}"
                         % TOTAL_BUDGET)
f-string 会**先**把 `{url}` 插进来，URL 里的百分号编码（`%E6%B5%B7`…）于是被
后面的 `%` 当成转换符 → 实测异常文案变成
`网页通道超时（预算 9s 用尽）: not enough arguments for format string`，
**真实原因（超时）被一句 Python 报错盖掉**，排查时只能靠猜。

判据：任何 `f"..." % x` / `f'...' % x`（AST 里 `BinOp(JoinedStr, Mod, ...)`）
都判失败——f-string 不会留下 `%` 占位符，这种写法必然是笔误。
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"venv", "build", ".git", "__pycache__", "node_modules", ".gradle"}


def _py_files():
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def test_no_fstring_percent_formatting():
    bad = []
    for path in _py_files():
        rel = os.path.relpath(path, ROOT)
        if rel.startswith("tests" + os.sep + os.path.basename(__file__)):
            continue                     # 本文件自身的说明文字不算
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod)
                    and isinstance(node.left, ast.JoinedStr)):
                bad.append(f"{rel}:{node.lineno}")
    assert not bad, (
        "f-string 又被拿去 % 格式化了（URL 里的 % 会让它报 "
        "'not enough arguments for format string'）：" + "、".join(bad))


def test_the_actual_fixed_site_still_works():
    """原事故点的行为：超时文案必须带上**真实原因与预算**，且 URL 里的 % 不炸"""
    from engine.manga.copy_web import WebUnreachable, TOTAL_BUDGET     # noqa: F401
    import engine.manga.copy_web as cw
    # 直接构造同款文案（与源码同一写法），确认含预算数字且不抛 TypeError
    url = "https://www.copy4000.com/api/kb/web/searchci/comics?q=%E6%B5%B7%E8%B4%BC%E7%8E%8B"
    msg = f"网页通道超时（预算 {TOTAL_BUDGET:.0f}s 用尽）: {url[:100]}"
    assert "预算" in msg and "9s" in msg and "%E6" in msg
    try:
        _ = cw.WebUnreachable(msg)
    except TypeError as e:                                       # pragma: no cover
        raise AssertionError(f"构造异常文案不应抛 TypeError: {e}")


def test_budget_exhausted_message_is_truthful(monkeypatch):
    """真实路径：预算耗尽的报错必须说明**预算用尽**，且 URL 带 % 编码也不炸。

    这条是事故点的行为级复现：旧写法在这里会抛
    `not enough arguments for format string`（TypeError），把原因盖掉。
    """
    import io
    import engine.manga.copy_web as cw

    src = io.open(cw.__file__, encoding="utf-8").read()
    i = src.index("def _web_get(")
    j = src.index("\ndef ", i + 10)          # 只取这个函数体，别串到别的函数
    body = src[i:j]
    code = "\n".join(ln for ln in body.split("\n") if not ln.lstrip().startswith("#"))
    assert "% TOTAL_BUDGET" not in code, "又回到 f-string + % 的写法了"
    assert "预算 {TOTAL_BUDGET:.0f}s 用尽" in code, "预算文案应是 f-string 内插"

    class _Exhausted:
        def exhausted(self):
            return True

        def slice_timeout(self, base=None):        # pragma: no cover
            return base

    with_ = "https://www.copy4000.com/api/kb/web/searchci/comics" \
            "?q=%E6%B5%B7%E8%B4%BC%E7%8E%8B&limit=21"
    try:
        cw._web_get(with_, budget=_Exhausted())
        raise AssertionError("预算耗尽应抛 WebUnreachable")
    except cw.WebUnreachable as e:
        msg = str(e)
        assert "预算" in msg and "用尽" in msg, msg
        assert "not enough arguments" not in msg, f"文案被格式化错误盖掉: {msg}"
        assert "%E6%B5" in msg, "URL 里的百分号编码应原样保留（不参与格式化）"
