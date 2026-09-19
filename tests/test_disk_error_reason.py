# -*- coding: utf-8 -*-
"""R78 回归：存储不足必须给出**可行动原因**（发布门槛项）。

实测缺口（2026-09-18）：手机上磁盘写满时，小说任务以
"任务异常：OSError（详细错误见任务日志/服务端日志）"结束、漫画任务以
"下载过程异常中断，可重新启动续传"结束——**都没说清是存储问题**，
用户只会以为"又坏了"，清理磁盘前反复重试同样失败。

判据：
  1. errno 归因准确（ENOSPC/EDQUOT/EROFS/EACCES 各给对应说明）；
  2. 异常链/文本兜底也能识别（不同绑定可能不带 errno）；
  3. **非磁盘错误一律返回空**（不能把网络超时说成"磁盘满"）；
  4. 文案脱敏（只有固定中文句子，不带路径/URL/异常原文）；
  5. 两条后端（小说任务 / 漫画 worker）都接了这根线。
"""
import errno
import io
import os

import pytest

from engine.app_utils import disk_error_text


def test_enospc_is_actionable():
    txt = disk_error_text(OSError(errno.ENOSPC, "No space left on device"))
    assert "存储空间不足" in txt
    assert "存储管理" in txt and "继续" in txt, f"要能照做: {txt}"


@pytest.mark.parametrize("code,word", [
    ("EDQUOT", "配额"),
    ("EROFS", "只读"),
    ("EACCES", "权限"),
])
def test_other_disk_errnos(code, word):
    num = getattr(errno, code, None)
    if num is None:
        pytest.skip(f"平台无 {code}")
    assert word in disk_error_text(OSError(num, "x"))


def test_text_fallback_without_errno():
    """异常链里只有文本（无 errno）时也要认出来"""
    inner = Exception("write failed: No space left on device")
    outer = RuntimeError("保存章节失败")
    outer.__cause__ = inner
    assert "存储空间不足" in disk_error_text(outer)


@pytest.mark.parametrize("exc", [
    TimeoutError("timed out"),
    ConnectionError("connection reset by peer"),
    ValueError("boom"),
    None,
])
def test_non_disk_errors_return_empty(exc):
    assert disk_error_text(exc) == "", "非磁盘错误不得归因成存储问题"


def test_message_is_desensitized():
    """客户端可见文案不得带路径/异常原文"""
    txt = disk_error_text(OSError(errno.ENOSPC, "/data/user/0/com.x/files No space left"))
    assert "/data/" not in txt and "com.x" not in txt, txt


def test_both_backends_are_wired():
    """小说任务与漫画 worker 都必须调用归因（否则又是笼统文案）"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    novel = io.open(os.path.join(root, "server", "state.py"), encoding="utf-8").read()
    i = novel.index("def _run_task")
    assert "disk_error_text" in novel[i:i + 6000], "小说任务未接磁盘归因"
    manga = io.open(os.path.join(root, "engine", "manga", "download_manager.py"),
                    encoding="utf-8").read()
    assert manga.count("disk_error_text") >= 2, "漫画 worker 的两处异常兜底都要接"
    for src in (novel[i:i + 6000], manga):
        assert "存储空间不足" not in src, "文案应集中在 app_utils，不散落在调用点"


# ── 章节级失败也要说清"为什么"（只回"重爬失败"用户没法照做）──────────────
def test_novel_chapter_retry_reason_is_classified():
    """章节重爬失败要带归因：磁盘类给存储说明，网络类给网络说明"""
    import server.novel_api as na
    src = io.open(na.__file__, encoding="utf-8").read()
    i = src.index("def api_book_chapter_retry")
    j = src.index("@bp.route", i)          # 函数边界：下一个路由之前
    body = src[i:j]
    assert "disk_error_text" in body, "章节重爬未接磁盘归因"
    assert "classify" in body and "reason_for" in body, "章节重爬未接网络归因"
    assert "重爬失败：" in body, "要拼成'重爬失败：<原因>'"


def test_manga_chapter_image_failure_is_classified():
    import server.manga_api as ma
    src = io.open(ma.__file__, encoding="utf-8").read()
    assert "def _img_fail_hint(" in src, "缺可行动原因辅助函数"
    assert src.count("_img_fail_hint(e)") >= 2, "两个图片失败点都要接"
    assert "章节图片获取失败：" in src, "要拼成'章节图片获取失败：<原因>'"


def test_classified_reason_is_still_desensitized():
    """归因后的文案仍是固定中文——不得把异常原文/路径带出去"""
    from engine.neterr import classify, reason_for
    from engine.app_utils import disk_error_text
    for exc in (OSError(errno.ENOSPC, "/data/user/0/com.webnovel.mobile/x"),
                OSError(errno.ENETUNREACH, "Network is unreachable"),
                ConnectionError("Failed to connect: /10.0.0.5:8080 "
                                "temporary failure in name resolution")):
        why = disk_error_text(exc)
        if not why:
            kind = classify(exc)
            why = reason_for(kind) if kind else ""
        assert why, f"应能归因: {exc}"
        assert "/data/" not in why and "10.0.0.5" not in why and "OSError" not in why, why
