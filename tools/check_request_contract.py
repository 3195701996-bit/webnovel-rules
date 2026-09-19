"""客户端**写请求 body** 的键 ↔ 服务端**真正读**的键（静态核对，不需要起服务）。

为什么需要：`tools/check_client_server_contract.py` 守的是**响应方向**（服务端发的键，
客户端会不会读到）。反方向一直没有工具，而它同样会静默坏：

  · 客户端 POST `{"index": N}` 而服务端读 `body.get("idx")` → 用户输入被**悄悄丢掉**，
    功能表现为"点了没反应/进度不保存"，两边都不报错；
  · 服务端读 `{"source_uid":"..."}` 而客户端发 `{"source":"..."}` → 建任务失败或建错源；
  · 客户端多发一个服务端根本不读的键 → 维护者以为改这个键有用。

做法（纯静态、可回归、无需夹具）：
  1. 扫 `server/*.py`，按 `@bp.route(...)` 分组，抽出该处理函数从 request JSON 里读的键
     （`x = request.get_json(...)` 之后的 `x.get("k")` / `x["k"]`，以及内联
     `request.get_json(...).get("k")`）；
  2. 扫 `android/.../mobile/*.kt`，抽出每个 `gateway.httpPost/httpDelete/httpPut` 调用点的
     **规范化路径**与 **body 的键**；body 支持四种形态：内联 `JSONObject()` 链、
     转义 JSON 字面量（`"{\"dry_run\":false}"`）、本文件的 `val body = ...` 变量、
     `EngineData.<fn>()` 助手（进 `EngineData.kt` 解析）；
  3. 双向核对：
     · 客户端发了、服务端不读 → **问题**（用户输入被静默丢弃）；
     · 服务端读了、客户端从不发 → 必须在 `REQUEST_OPTIONAL` 登记**且写明理由**，
       否则 **问题**（否则工具会退化成橡皮图章）；
  4. 解析不了的 body（新写法）**计为未解析并判失败** —— 宁可逼着补解析，
     也不许"看不懂就跳过"把覆盖面悄悄缩小。

用法：
  PYTHONDONTWRITEBYTECODE=1 ./venv/bin/python tools/check_request_contract.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT_DIR = os.path.join(ROOT, "android", "app", "src", "main", "java",
                          "com", "webnovel", "mobile")
SERVER_DIR = os.path.join(ROOT, "server")

# ── 服务端"读了但客户端按设计不发"的键：每条必须写明**为什么**────────────────
# 键格式： "<规范化路径> <键>"；"*" 表示该段是变量
REQUEST_OPTIONAL = {
    "/api/tasks source": "服务端兼容别名：老网页端发 source，客户端统一发 source_uid（服务端先读 source_uid）",
    "/api/tasks url": "服务端兼容别名：老网页端发 url，客户端发 book_url",
    "/api/sources/bulk-enabled uids": "网页端按 uid 列表批量操作；安卓端只用 enabled+filter（按筛选条件批量）",
    "/api/sources/verify uids": "网页端按 uid 列表验证；安卓端用 limit+skip_* 走队列",
    "/api/sources/verify include_disabled": "网页端可含已停用源；安卓端只验证启用源",
    "/api/sources/verify keyword": "网页端按关键词筛源；安卓端不筛",
    "/api/sources/verify offset": "网页端分页偏移；安卓端只发 limit",
    "/api/net/proxy timeout": "网页端可指定探测超时；安卓端用服务端默认",
    "/api/storage/clear comic_id": "网页端清**单个作品**的漫画临时缓存（source+comic_id）；"
                                   "安卓端那个按钮的文案就是「清理「<源>」的临时缓存」，只发 source",
    "/api/storage/clear files": "网页端按文件清单精简清理；安卓端只用 scope（按分类清理）",
    "/api/storage/clear dir": "同上（网页端单目录清理用）",
    "/api/manga/*/*/download source": "服务端从 URL 路径取 source；body 里的 source 只有网页端会带",
    "/api/manga/*/*/download comic_id": "同一原因：URL 路径已含 comic_id",
    "/api/manga/*/*/download idx": "整本下载不传起始话；按范围下载才带（客户端另有带 idx 的调用点）",
    "/api/manga/*/*/download pos": "同上（范围下载的起始页）",
    "/api/manga/*/*/download chapter_id": "同上（按话下载用 chapter_id/chapter_label）",
    "/api/manga/*/*/download chapter_label": "同上",
    "/api/manga/*/*/download new_chapters": "网页端「只下新增话」用；安卓端用 check-updates-download",
    "/api/sources/removed/restore dir": "网页端按**指定批次**恢复；安卓端只发 latest（恢复最近一批）",
    "/api/sources/removed/restore files": "网页端按**文件名**逐个恢复；安卓端不挑文件",
    "/api/sources/verify skip_tested_days": "网页端可跳过「最近测过」的源（tested 与 verified 是两个概念）；"
                                            "安卓端只用 skip_verified_days",
    "/api/manga/sources/verify keys": "网页端按源 key 列表验证；安卓端用 limit 走队列",
    "/api/manga/sources/verify keyword": "网页端按关键词筛源；安卓端不筛",
    "/api/manga/sources/verify offset": "网页端分页偏移；安卓端只发 limit",
    "/api/net/proxy/test timeout": "网页端可指定探测超时；安卓端用服务端默认",
    "/api/books/*/progress name": "客户端会发 name（章名）；此处登记为可选是因为存在不带 name 的调用点",
}

# 客户端调用点里路径段是变量、无法静态确定时，登记它可能命中的具体端点。
# 每条同样必须写明理由（否则"对不上"会被这里吞掉）。
DYNAMIC_PATH_TARGETS = {
    "/api/tasks/*/*": ["/api/tasks/*/pause", "/api/tasks/*/resume",
                       "/api/tasks/*/stop"],
}


def strip_comments(text):
    """去 // 与 /* */ 注释（保留字符串字面量），避免注释里的字样被当代码。"""
    out, i, n = [], 0, len(text)
    while i < n:
        if text.startswith('"""', i):
            j = text.find('"""', i + 3)
            j = n if j < 0 else j + 3
            out.append(text[i:j]); i = j; continue
        if text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1]); i = j + 1; continue
        if text.startswith("//", i):
            j = text.find("\n", i); i = n if j < 0 else j; continue
        if text.startswith("/*", i):
            j = text.find("*/", i); i = n if j < 0 else j + 2; continue
        out.append(text[i]); i += 1
    return "".join(out)


def call_args(src, open_paren):
    """顶层逗号切分实参（跳过字符串/嵌套括号）。

    从 `open_paren + 1` 开始扫、**depth 从 1 起**：开括号本身不能进第一个实参，
    而 depth 必须体现"我们已经在这层括号里面"，否则嵌套调用的右括号（如
    `mangaPath(a, b, "/x")` 的那个 `)`）会被当成实参列表的结束，逗号也跟着错位。
    （第一版从 `open_paren` 起把 `(` 塞进首个实参 —— 调用点那边习惯性丢掉第一个
    参数（端口）所以没暴露，形参解析一用 args[0] 就撞上。）
    """
    depth, args, cur, i = 1, [], [], open_paren + 1
    while i < len(src):
        c = src[i]
        if c == '"':
            j = i + 1
            while j < len(src) and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            cur.append(src[i:j + 1]); i = j + 1; continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                args.append("".join(cur))
                return [a.strip() for a in args]
        elif c == "," and depth == 1:
            args.append("".join(cur)); cur = []; i += 1; continue
        cur.append(c); i += 1
    return None


# ── 服务端 ─────────────────────────────────────────────────────────────

ROUTE_RE = re.compile(r'@bp\.route\(\s*"([^"]+)"\s*(?:,\s*methods=\[([^\]]*)\])?\s*\)')
_SERVER_SRC = {}


def _server_file(fn):
    if fn not in _SERVER_SRC:
        with open(os.path.join(SERVER_DIR, fn), encoding="utf-8") as f:
            _SERVER_SRC[fn] = f.read()
    return _SERVER_SRC[fn]


def _def_body(text, fname):
    """取 `def <fname>(...)` 的函数体（按缩进，Python 用缩进定界）。"""
    m = re.search(r'\ndef\s+' + re.escape(fname) + r'\s*\(', text)
    if not m:
        return None
    lines = text[m.end():].split("\n")
    body, started = [], False
    for ln in lines:
        if not started:
            if ln.strip().endswith(":") or ln.rstrip().endswith(":"):
                started = True
                continue
            if ":" in ln:                      # 单行签名 `def f(a):`
                started = True
                rest = ln.split(":", 1)[1]
                if rest.strip():
                    body.append(rest)
                continue
            continue
        if ln.strip() and not ln.startswith((" ", "\t")):
            break
        body.append(ln)
    return "\n".join(body)


def _keys_in_text(text, param=None):
    """抽一段函数体里从 request JSON 读的键。

    `param`：形参名（跨函数时用）—— `storage.clear(target)` 这类**转发**很常见：
    处理函数把 body 交给下层函数，键在下层读。只看处理函数体会误报
    "客户端发的键服务端不读"，所以必须跟着走一层（见 `_delegated_keys`）。
    """
    keys = set()
    vars_ = set()
    for vm in re.finditer(r'(\w+)\s*=\s*request\.get_json\([^)]*\)(?:\s*or\s*\{)?',
                          text):
        vars_.add(vm.group(1))
        seg = text[vm.end():]
        keys |= set(re.findall(re.escape(vm.group(1)) + r'\.get\(\s*"([^"]+)"', seg))
        keys |= set(re.findall(re.escape(vm.group(1)) + r'\[\s*"([^"]+)"\s*\]', seg))
    keys |= set(re.findall(r'request\.get_json\([^)]*\)[^.\n]*\.get\(\s*"([^"]+)"', text))
    if param:                                  # 跨函数：形参当成"已解析的 body"
        vars_.add(param)
        keys |= set(re.findall(re.escape(param) + r'\.get\(\s*"([^"]+)"', text))
        keys |= set(re.findall(re.escape(param) + r'\[\s*"([^"]+)"\s*\]', text))
    return keys, vars_


def _delegated_keys(text, body_vars, depth=2):
    """处理函数把 body 交给下层函数时，跟着走进下层取键（浅层，depth≤2）。"""
    keys = set()
    if depth <= 0:
        return keys
    for var in list(body_vars):
        for m in re.finditer(r'(?:(\w+)\.)?(\w+)\s*\(\s*' + re.escape(var) + r'\s*[,)]',
                             text):
            modname, fname = m.group(1), m.group(2)
            for fn in sorted(os.listdir(SERVER_DIR)):
                if not fn.endswith(".py"):
                    continue
                if modname and fn != modname + ".py":
                    continue
                src = _server_file(fn)
                sub = _def_body(src, fname)
                if sub is None:
                    continue
                # 下层函数的形参名（实参传的是第一个位置参数 → 取第一个形参）
                sig = re.search(r'\ndef\s+' + re.escape(fname) + r'\s*\(([^)]*)\)', src)
                params = [p.strip().split("=")[0].strip()
                          for p in (sig.group(1).split(",") if sig else []) if p.strip()]
                param = params[0] if params else None
                k2, _v2 = _keys_in_text(sub, param=param)
                keys |= k2
                keys |= _delegated_keys(sub, {param} if param else set(), depth - 1)
                break
    return keys


def server_routes():
    """{规范化路径: (键集合, 原文路径)}"""
    out = {}
    for fn in sorted(os.listdir(SERVER_DIR)):
        if not fn.endswith(".py"):
            continue
        text_all = _server_file(fn)
        lines = text_all.split("\n")
        for i, line in enumerate(lines):
            m = ROUTE_RE.search(line)
            if not m:
                continue
            path, methods = m.group(1), (m.group(2) or "GET")
            if "POST" not in methods and "PUT" not in methods and "DELETE" not in methods:
                continue
            # 处理函数 = 装饰器之后第一个 def（可能隔着一个 def 名带参数多行）
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith("def "):
                j += 1
            if j >= len(lines):
                continue
            body = []
            k = j + 1
            while k < len(lines):
                ln = lines[k]
                if ln.strip() and not ln.startswith((" ", "\t", ")")):
                    if ln.lstrip().startswith(("def ", "@", "class ")):
                        break
                    if not ln.startswith((" ", "\t")):
                        break
                body.append(ln); k += 1
            text = "\n".join(body)
            keys, body_vars = _keys_in_text(text)
            keys |= _delegated_keys(text, body_vars)
            out[_norm(path)] = (keys, path)
    return out


def _norm(path):
    """路径规范：所有 <var> / 变量段 → *"""
    p = re.sub(r'<[^>]*>', "*", path)
    p = re.sub(r'\$\{[^}]*\}', "*", p)
    p = p.split("?")[0]
    return "/".join("*" if seg in ("*", "") and seg == "*" else seg for seg in p.split("/"))


# ── 客户端 ─────────────────────────────────────────────────────────────

PUT_RE = re.compile(r'\.\s*put\(\s*"([^"]+)"')
ESC_JSON_RE = re.compile(r'"((?:\{|\[).*?(?:\}|\]))"', re.DOTALL)


def _keys_from_expr(expr, files, seen=None, same_file=None, before=None, depth=0):
    """从一段表达式里抽 body 的键；返回 (键集合, 是否解析成功)。

    `same_file` = 调用点所在文件的正文：局部变量**必须先在本文件里找**。
    第一版在所有文件里搜 `val body`，于是撞上别的文件里同名的 body，
    把本来能解析的判成"解析不了"（覆盖面悄悄缩小）。
    """
    seen = seen or set()
    if depth > 8:
        # 解析链路上的自我保护：`doClear(body)` 里的 body 与形参同名时会互相引用，
        # 没有上限就是无限递归（真撞过 RecursionError）。
        return set(), False
    if expr is None:
        return set(), True                       # 真的没有 body
    e = expr.strip()
    if e in ("", '""', "null"):
        return set(), True                       # 显式空 body
    if e.endswith(".toString()"):                # JSONObject()…toString()
        e = e[: -len(".toString()")].strip()
    if "JSONObject(" in e:
        keys = set(PUT_RE.findall(e))
        # JSONObject(mapOf("a" to 1)) 这类：退化为找 "k" to
        keys |= set(re.findall(r'"([^"]+)"\s+to\s', e))
        return keys, True
    m = ESC_JSON_RE.search(e)                    # "{\"dry_run\":false}"
    if m and "\\\"" in e:
        inner = m.group(1).replace('\\"', '"')
        return set(re.findall(r'"([^"]+)"\s*:', inner)), True
    m = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)', e)
    if m:                                        # 局部变量 / 函数形参 / 解构形参
        var = m.group(1)
        if var in seen:
            return set(), False
        seen.add(var)
        # **只在调用点所在文件里解析**：Kotlin 的局部变量、函数形参、解构形参
        # 都是文件作用域。早先在所有文件里搜 `val body`，会撞上别的文件里同名的
        # body —— 那不是"解析不了"，而是更糟的"解析成了别的东西"（核对的是错的键集）。
        if not same_file:
            return set(), False
        hit = _resolve_local(same_file, var, before)
        if hit is not None:
            return _keys_from_expr(hit, files, seen, same_file, before, depth + 1)
        # 形参：去调用点取实参（如 `doClear(body: JSONObject)` ← doClear(JSONObject()…)）
        # 注意**不能**用 before 过滤：调用点通常在被调函数**之后**（httpPost 写在函数体里，
        # 调用点在界面上），带过滤会把它们全排掉，等于这条调用点没核对。
        #
        # 策略必须**逐个试、取第一个成功的**，不能在第一个策略上就判死：
        # `doClear(body)` 传进来的实参恰好也叫 body，形参策略会一直自我指涉直到
        # 深度耗尽 —— 若因此直接返回失败，后面真正能解开的"解构形参"就永远没机会。
        for hits in (_resolve_param(same_file, var),
                     _resolve_destructured(same_file, var)):
            if not hits:
                continue
            keys = set()
            for h in hits:
                # 实参在**调用者作用域**里，同名的局部变量是完全不同的东西
                # （`doClear(body)` 里的 body 不是被调函数那个 body），
                # 所以递归必须用干净的 seen，否则一撞名字就被判成"解析不了"。
                k, ok = _keys_from_expr(h, files, set(), same_file, None, depth + 1)
                if not ok:
                    keys = None
                    break
                keys |= k
            if keys is not None:
                return keys, True
        return set(), False
    m = re.fullmatch(r'EngineData\.([A-Za-z_][A-Za-z0-9_]*)\([^)]*\)', e, re.DOTALL)
    if m:                                        # EngineData 助手
        with open(os.path.join(CLIENT_DIR, "EngineData.kt"), encoding="utf-8") as f:
            kt = strip_comments(f.read())
        mm = re.search(r'\n    fun ' + re.escape(m.group(1)) + r'\(', kt)
        if not mm:
            return set(), False
        nxt = kt.find("\n    fun ", mm.end())
        seg = kt[mm.end():nxt if nxt > 0 else len(kt)]
        if "JSONObject(" in seg:
            return set(PUT_RE.findall(seg)), True
        return set(), False
    return set(), False


def _resolve_param(src, var, before=None):
    """变量是某个**函数形参**时，返回各调用点上对应位置的实参表达式。

    为什么需要：`doClear(body: JSONObject)` 里 body 的键在**调用点**构造，
    只看被调函数就会判成"解析不了"，于是这条调用点等于没核对。
    """
    out = []
    for fm in re.finditer(r'\bfun\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(', src):
        args = call_args(src, fm.end() - 1)
        if not args:
            continue
        idx = next((i for i, a in enumerate(args)
                    if re.match(r'\s*' + re.escape(var) + r'\s*:', a)), None)
        if idx is None:
            continue
        fname = fm.group(1)
        decl_at = fm.start() + src[fm.start():].index(fname)
        for cm in re.finditer(r'(?<![A-Za-z0-9_.])' + re.escape(fname) + r'\s*\(', src):
            if cm.start() == decl_at:            # 跳过声明本身
                continue
            if before is not None and cm.start() > before:
                continue
            cargs = call_args(src, cm.end() - 1)
            if cargs and idx < len(cargs):
                out.append(cargs[idx])
    return out


def _capture_stmt(src, start):
    """从 `start` 起捕获一条语句：括号未配平、或后续行是更深缩进的续行、或以
    `.`/`to`/`+`/`?:` 开头时继续；否则结束。

    比"只看第一个换行"稳：Kotlin 的流式链与 `a to b` 都会换行写，
    只看换行会把表达式截断成半截，键抽成空集（"通过"但没核对）。
    """
    line_start = src.rfind("\n", 0, start) + 1
    base_indent = len(src[line_start:start]) - len(src[line_start:start].lstrip())
    out, depth, i = [], 0, start
    while i < len(src):
        c = src[i]
        if c == '"':
            j = i + 1
            while j < len(src) and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1]); i = j + 1; continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "\n" and depth <= 0:
            m = re.match(r'([ \t]*)([^\n]*)', src[i + 1:])
            indent_str, nxt = m.group(1), m.group(2)
            # 缩进必须从**含缩进**的那一组取：第一版取了剥掉缩进的 group，
            # nxt_indent 恒为 0，"续行缩进更深"这条判据永远不成立
            # （于是 `… +` / `… to` 的跨行表达式被截断成半截）。
            cont = (nxt.startswith((".", "to ", "+ ", "?:", "?."))
                    or (nxt.strip() and len(indent_str) > base_indent))
            if not cont:
                break
        out.append(c); i += 1
    return "".join(out)


def _split_pair_top(s):
    """Kotlin `a to b`：返回最后一个**字符串外**的 `to` 之后的部分（Pair 的第二项）。

    必须是词边界上的独立 `to`，且**后面可以紧跟换行**——真实代码几乎都写成
    `"…" to\n    JSONObject().put(…)`；第一版只认 `" to "`（带尾空格），
    于是这种最平的写法全部漏掉。
    """
    in_str, pos = False, None
    i = 0
    while i < len(s):
        if s[i] == '"':
            in_str = not in_str
            i += 1
            continue
        if (not in_str and s.startswith("to", i)
                and i > 0 and s[i - 1] in " \t\n"
                and (i + 2 >= len(s) or s[i + 2] in " \t\n")):
            pos = i
            i += 2
            continue
        i += 1
    return s[pos + 2:].lstrip() if pos is not None else None


def _resolve_destructured(src, var, before=None):
    """解构 lambda 形参：`confirm?.let { (text, body) -> … }` 里的 body。

    这类写法里键在**接收者的赋值**处构造（`confirm = "…" to JSONObject().put(…)`），
    不追过去就只能判"解析不了"，那条调用点等于没核对。
    """
    out = []
    for dm in re.finditer(r'\(\s*[A-Za-z_]\w*\s*,\s*' + re.escape(var) + r'\s*\)\s*->', src):
        head = src[max(0, dm.start() - 200):dm.start()]
        rm = None
        for r in re.finditer(r'(\w+)\s*\??\s*\.\s*(?:let|also|forEach|map|onSuccess)\b',
                             head):
            rm = r
        if not rm:
            continue
        recv = rm.group(1)
        for am in re.finditer(r'\b' + re.escape(recv) + r'\s*=\s*', src):
            if before is not None and am.start() > before:
                continue
            stmt = _capture_stmt(src, am.end())
            tail = _split_pair_top(stmt)
            if tail:
                out.append(tail.strip())
    return out


def _resolve_local(src, var, before=None):
    """在同一个 .kt 里找 `val <var> = <表达式>`，取到该**语句**结束。

    · `before`：调用点在文件里的偏移量。同名变量在一个文件里可能出现多次
      （SearchScreens.kt 里就有两处 `val body`），必须取**调用点之前最近的一次声明**，
      否则会把另一个调用点的键算到这条调用上——工具照样"通过"，但核对的是错的。
    · 结束判据：括号配平到 0，且**下一非空行不以 `.` 开头**（Kotlin 流式链会换行写
      `.put(...)`）。第一版只看"第一个换行处 depth==0"，于是多行链被截断、键抽成空集
      —— 覆盖面看起来是满的，实际一个键都没核对。
    """
    pat = re.compile(r'\b(?:val|var)\s+' + re.escape(var)
                     + r'\s*(?::\s*[A-Za-z_][\w<>?.]*\s*)?=')
    hits = [h for h in pat.finditer(src) if before is None or h.start() < before]
    if not hits:
        return None
    return _capture_stmt(src, hits[-1].end())


def client_calls():
    """[(文件, 行号, 方法, 规范化路径, 原始路径, 键集合, 已解析?, 原文)]"""
    files = {}
    for fn in sorted(os.listdir(CLIENT_DIR)):
        if fn.endswith(".kt"):
            with open(os.path.join(CLIENT_DIR, fn), encoding="utf-8") as f:
                files[fn] = strip_comments(f.read())
    rows = []
    for fn, src in files.items():
        for m in re.finditer(r'\.(httpPost|httpDelete|httpPut)\s*\(', src):
            args = call_args(src, m.end() - 1)
            if not args or len(args) < 2:
                continue
            rest = args[1:]
            raw_path = rest[0]
            body = rest[1] if len(rest) > 1 else None
            path = _client_path(raw_path)
            keys, ok = _keys_from_expr(body, list(files.values()), same_file=src,
                                       before=m.start())
            rows.append((fn, src[:m.start()].count("\n") + 1, m.group(1), path,
                         raw_path, keys, ok, (body or "").strip()))
    return rows


def _client_path(expr):
    e = expr.strip()
    if "bookPath(" in e:
        suf = re.findall(r'bookPath\([^,]+,\s*"([^"]*)"', e)
        return _norm("/api/books/<key>" + (suf[0] if suf else ""))
    if "mangaPath(" in e:
        suf = re.findall(r'mangaPath\([^,]+,[^,]+,\s*"([^"]*)"', e)
        return _norm("/api/manga/<s>/<c>" + (suf[0] if suf else ""))
    lits = re.findall(r'"([^"]*)"', e)
    path = "".join(lits) if lits else e
    path = re.sub(r'\$\{[^}]*\}', "*", path)
    return _norm(path)


def compare(routes, calls, optional=None):
    """双向核对，返回 (problems, unresolved, sent_by_path)。

    抽成函数是为了**可测**：守卫用例要能用合成数据验证"客户端发 index、服务端读 idx"
    这类漂移**真的会被报出来**（不会失败的核对等于没有核对）。
    """
    optional = REQUEST_OPTIONAL if optional is None else optional
    problems, unresolved = [], []
    sent_by_path = {}
    for fn, line, method, path, raw, keys, ok, body in calls:
        if not ok:
            unresolved.append(f"{fn}:{line} {method} {raw[:60]} → body 解析不了：{body[:60]}")
            continue
        sent_by_path.setdefault(path, set()).update(keys)

    # ① 客户端发了、服务端不读
    for fn, line, method, path, raw, keys, ok, body in calls:
        if not ok:
            continue
        targets = DYNAMIC_PATH_TARGETS.get(path)
        if targets:
            read = set()
            for t in targets:
                read |= routes.get(t, (set(), t))[0]
            label = " ∪ ".join(targets)
        else:
            if path not in routes:
                if keys:
                    problems.append(f"{fn}:{line} {method} {path} 不在服务端路由表里"
                                    f"（但客户端发了 {sorted(keys)}）")
                continue
            read, _ = routes[path]
            label = path
        extra = sorted(keys - read)
        if extra:
            problems.append(f"{fn}:{line} {method} {path} 发了服务端不读的键 {extra}"
                            f"（服务端读：{sorted(read)}）")
            print(f"  ✗ {fn}:{line} {path} 多发 {extra}")
        elif keys:
            print(f"  ✓ {fn}:{line} {method} {label} {sorted(keys)}")

    # ② 服务端读了、客户端从不发
    print("-" * 78)
    print("反向核对（服务端读、客户端从不发的键）：")
    for path, (keys, raw) in sorted(routes.items()):
        sent = sent_by_path.get(path, set())
        if not sent:
            continue
        never = sorted(k for k in keys if k not in sent)
        for k in never:
            tag = "登记" if f"{path} {k}" in optional else "**未登记**"
            print(f"  · {raw:52s} {k:18s} {tag}")
            if f"{path} {k}" not in optional:
                problems.append(f"{raw} 读 {k}，但客户端所有调用点都不发（且未在 "
                                f"REQUEST_OPTIONAL 登记理由）")

    for k in optional:
        if not any(k.startswith(p + " ") for p in routes):
            problems.append(f"REQUEST_OPTIONAL 登记了不存在的路径/键：{k}（登记表过期了？）")
    return problems, unresolved, sent_by_path


def main():
    routes = server_routes()
    calls = client_calls()
    problems, unresolved, sent_by_path = compare(routes, calls)

    print("-" * 78)
    print(f"写接口 {len([p for p in sent_by_path if sent_by_path[p]])} 个 · "
          f"调用点 {len(calls)} 个 · 未解析 body {len(unresolved)} 个 · "
          f"发现问题 {len(problems)} 个")
    for u in unresolved:
        print("  ◇ " + u)
    for p in problems:
        print("  ⚠ " + p)
    # 未解析 = 覆盖面的漏洞，同样判失败（宁可逼着补解析，也不许悄悄缩小覆盖）
    return 1 if (problems or unresolved) else 0


if __name__ == "__main__":
    sys.exit(main())
