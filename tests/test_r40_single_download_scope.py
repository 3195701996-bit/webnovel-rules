# -*- coding: utf-8 -*-
"""A02 回归（离线静态断言）：单话下载定位失败不得擅自扩大为整本下载。

原实现（templates/manga_download.html startDl）：单话模式按名称找不到
章节时仅提示"改为下载全部"，随后仍以空 chapters 提交——后端语义为空
列表 = 整本下载，用户只要单话却整本全下。

修复契约：
1. 入口（manga_reader.html downloadCurrent）尽量传稳定 chapter_id（chid）；
2. 已选单话只提交该 ID（有 chid 时 chapters=[CH_ID]，不再按名称定位）；
3. 名称定位失败（兼容旧链接）就停止提交，提供重试/返回目录，
   绝不自动扩大范围；整本下载由用户另行选择。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DL_HTML = os.path.join(_HUB, "templates", "manga_download.html")
_READER_HTML = os.path.join(_HUB, "templates", "manga_reader.html")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_no_auto_expand_to_full_book():
    html = _read(_DL_HTML)
    assert "改为下载全部" not in html, "定位失败的扩大下载提示必须移除"


def test_entry_passes_stable_chapter_id():
    """阅读页入口在跳转下载页时携带 chid（稳定章节 id）"""
    html = _read(_READER_HTML)
    m = re.search(r"function downloadCurrent\(\).*?location\.href\s*=\s*([^;]+);",
                  html, re.S)
    assert m, "downloadCurrent 入口必须存在"
    assert "chid=" in m.group(1), "入口必须传稳定 chapter_id（chid 参数）"


def test_single_mode_submits_only_selected_id():
    """有 chid 时直接 chapters=[CH_ID]：已选单话只提交该 ID"""
    html = _read(_DL_HTML)
    assert "params.get('chid')" in html
    assert "chapters = [CH_ID]" in html


def test_locate_failure_stops_submission():
    """定位失败的分支必须在提交 fetch 之前 return，且给出重试/返回目录"""
    html = _read(_DL_HTML)
    m = re.search(r"if \(!chapters\.length\) \{(?P<body>.*?)\n    \}", html, re.S)
    assert m, "必须存在定位失败的拦截分支"
    body = m.group("body")
    assert "return;" in body, "定位失败必须停止提交（return）"
    assert "重试" in body and "history.back()" in body, \
        "必须提供重试或返回目录"
    # 拦截分支必须出现在下载提交 fetch 之前
    guard_pos = m.start()
    submit_pos = html.index("/download`")
    assert guard_pos < submit_pos, "拦截分支必须先于提交请求执行"


def test_whole_book_still_user_choice():
    """非 single 模式（整本下载页入口不带 mode）仍可提交空 chapters——
    整本下载保留为用户显式选择的路径，只是不再由失败兜底触发"""
    html = _read(_DL_HTML)
    m = re.search(r"if \(MODE === 'single'\) \{.*?\n  \}\n", html, re.S)
    assert m, "single 模式守卫必须存在"
    # 守卫之外（整本模式）直接走提交逻辑：提交 fetch 在守卫之后
    assert html.index("/download`") > m.end()
