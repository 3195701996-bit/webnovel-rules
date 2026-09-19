# -*- coding: utf-8 -*-
"""R78 回归：阅读器"离开前必须落进度"的源码级守卫（两侧对称，不依赖设备）。

两起真缺陷，同一类：
- 漫画阅读器（2026-09-17 前）只有顶栏「← 返回」保存进度，用手势/返回键退出就丢进度
  —— 当时用户的原话是"刚读完退出再进又让我从第一话读"；
- **小说阅读器到 2026-09-18 仍是如此**（漫画修了、小说没对齐），而且它连"切后台/锁屏"
  也不保存：滚动保存是 ChapterBody 里的 1.2s 防抖，连续滚动会把每一次待写都取消掉，
  恰好在这时退出，丢掉的正是"刚读到哪"。

行为级验证要上真机（Compose 手势 + 进程冻结），所以这里用**源码不变式**锁住两侧：
每个阅读器都必须在**自己**的函数体里
  ① 注册 `BackHandler`，且**先 saveProgress 再 onBack**（顺序反了等于没保存就退出）；
  ② 注册生命周期观察者，在 `ON_STOP` 时保存。
用例自带"变异自检"：把源码改坏后断言必须失败 —— 不会失败的守卫等于没有守卫。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KT_DIR = os.path.join(ROOT, "android", "app", "src", "main", "java", "com", "webnovel", "mobile")


def _read(name):
    with open(os.path.join(KT_DIR, name), encoding="utf-8") as f:
        return f.read()


def _strip_comments(code):
    """去掉 // 与 /* */ 注释，但保留字符串字面量（模板串整体当字符串，含 ${}）。

    注释里提到 BackHandler 不算数 —— 否则"写句注释假装修了"就能骗过守卫。
    """
    out = []
    i, n = 0, len(code)
    while i < n:
        ch = code[i]
        if code.startswith('"""', i):
            j = code.find('"""', i + 3)
            j = n if j < 0 else j + 3
            out.append(code[i:j]); i = j; continue
        if ch == '"':
            j = i + 1
            while j < n and code[j] != '"':
                j += 2 if code[j] == "\\" else 1
            out.append(code[i:j + 1]); i = j + 1; continue
        if ch == "'":
            j = i + 1
            while j < n and code[j] != "'":
                j += 2 if code[j] == "\\" else 1
            out.append(code[i:j + 1]); i = j + 1; continue
        if code.startswith("//", i):
            j = code.find("\n", i)
            i = n if j < 0 else j
            continue
        if code.startswith("/*", i):
            j = code.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        out.append(ch); i += 1
    return "".join(out)


def _body_from(code, start):
    """从 `start` 处的 `{` 起做括号配对，返回花括号内的文本（跳过字符串）；找不到返回 None。"""
    i = code.find("{", start)
    if i < 0:
        return None
    depth, j = 0, i
    while j < len(code):
        ch = code[j]
        if ch == '"':
            j += 1
            while j < len(code) and code[j] != '"':
                j += 2 if code[j] == "\\" else 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return code[i + 1:j]
        j += 1
    return None


def _function_body(code, signature):
    """顶层函数体（顶层 fun 都顶格写，签名唯一）。"""
    m = re.search(r"^" + re.escape(signature), code, re.M)
    assert m, f"源码里找不到顶层函数 {signature}（被重命名/移动了？守卫需要同步更新）"
    body = _body_from(code, m.end())
    assert body is not None, f"{signature} 的函数体括号不配对"
    return body


def _check_reader(code, signature, where):
    """返回该阅读器缺失/写错的不变式列表（空 = 全过）。"""
    bad = []
    body = _function_body(code, signature)

    # ① BackHandler：先保存、再退出
    m = re.search(r"BackHandler\s*\{", body)
    if not m:
        bad.append(f"{where}：没有注册 BackHandler —— 用手势/返回键退出不会保存进度")
    else:
        lam = _body_from(body, m.end() - 1)
        if lam is None:
            bad.append(f"{where}：BackHandler 的 lambda 括号不配对")
        else:
            i_save = lam.find("saveProgress(")
            i_back = lam.find("onBack()")
            if i_save < 0:
                bad.append(f"{where}：BackHandler 里没有 saveProgress —— 退出前不落进度")
            if i_back < 0:
                bad.append(f"{where}：BackHandler 里没有 onBack() —— 返回会失效")
            if i_save >= 0 and i_back >= 0 and i_save > i_back:
                bad.append(f"{where}：BackHandler 里 onBack() 排在 saveProgress 之前"
                           "（先退出，协程可能随组合一起被取消）")

    # ② 切后台/锁屏（ON_STOP）也要保存
    if "ON_STOP" not in body:
        bad.append(f"{where}：没有在 ON_STOP（切后台/锁屏）时保存进度")
    elif "saveProgress(" not in body[body.find("ON_STOP"):]:
        bad.append(f"{where}：ON_STOP 分支里没有 saveProgress")
    return bad


def test_both_readers_flush_progress_before_leaving():
    novel = _strip_comments(_read("ReaderScreens.kt"))
    manga = _strip_comments(_read("MangaScreens.kt"))
    bad = (_check_reader(novel, "fun NovelReaderScreen(", "小说阅读器 NovelReaderScreen")
           + _check_reader(manga, "fun MangaReaderScreen(", "漫画阅读器 MangaReaderScreen"))
    assert not bad, "阅读器退出前落进度的不变式被破坏：\n- " + "\n- ".join(bad)


# ── 变异自检：守卫必须会失败 ─────────────────────────────────────────

def test_guard_detects_missing_backhandler():
    """把小说阅读器的 BackHandler 整段删掉 —— 守卫必须报出来。"""
    code = _strip_comments(_read("ReaderScreens.kt"))
    m = re.search(r"BackHandler\s*\{", code)
    assert m, "夹具前提：源码里本来有 BackHandler"
    lam_start = code.rfind("\n", 0, m.start()) + 1
    lam_end = m.end() + len(_body_from(code, m.end() - 1) or "") + 1
    mutated = code[:lam_start] + code[lam_end:]
    bad = _check_reader(mutated, "fun NovelReaderScreen(", "小说阅读器")
    assert any("BackHandler" in b for b in bad), f"守卫没抓到删除 BackHandler：{bad}"


def test_guard_detects_save_after_navigate():
    """把顺序改成"先 onBack 再 saveProgress" —— 守卫必须报出来。"""
    code = _strip_comments(_read("ReaderScreens.kt"))
    lam = _body_from(code, re.search(r"BackHandler\s*\{", code).end() - 1)
    assert lam and "saveProgress(" in lam and "onBack()" in lam
    # 位置互换式变异（不依赖具体写法：限时等待/单行都适配）——
    # 把第一个 saveProgress(...) 调用与第一个 onBack() 的先后对调
    m_save = re.search(r"saveProgress\([^)]*\)", lam)
    m_back = re.search(r"onBack\(\)", lam)
    assert m_save and m_back and m_save.start() < m_back.start(), \
        "夹具前提：正文里确实是『先保存再退出』的写法"
    swapped_lam = (lam[:m_save.start()] + "onBack()"
                   + lam[m_save.end():m_back.start()]
                   + m_save.group(0) + lam[m_back.end():])
    swapped = code.replace(lam, swapped_lam, 1)
    assert swapped != code
    bad = _check_reader(swapped, "fun NovelReaderScreen(", "小说阅读器")
    assert any("onBack() 排在" in b for b in bad), f"守卫没抓到顺序颠倒：{bad}"


def test_guard_detects_missing_on_stop_save():
    """删掉 ON_STOP 保存 —— 守卫必须报出来。"""
    code = _strip_comments(_read("MangaScreens.kt"))
    assert "ON_STOP" in code
    mutated = code.replace("ON_STOP", "ON_PAUSE")
    bad = _check_reader(mutated, "fun MangaReaderScreen(", "漫画阅读器")
    assert any("ON_STOP" in b for b in bad), f"守卫没抓到切后台不保存：{bad}"
