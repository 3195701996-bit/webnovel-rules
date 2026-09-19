# -*- coding: utf-8 -*-
"""A01 回归残留修复（离线）：.repair_old 崩溃残留不得被识别为"章节"

A01 覆盖重下的原子切换：旧章节目录改名 `<chapter>.repair_old`（与章节
目录同级）→ 暂存改名正式 → 删备份。进程在两次 rename 间崩溃会残留
`<chapter_id>.repair_old/`，而 _scan_downloaded_chapters 与
_manga_count_comic 把"含图片的子目录"当章节 → 幽灵章节、章节数 +1。

修复契约（双保险）：
1. 备份目录移到 MANGA_STATE_DIR/_repair_staging/... 下（staging + ".old"，
   同文件系统 rename 仍原子），不再出现在漫画目录树内；
2. 两处扫描跳过 .repair_old 后缀目录（兼容历史残留）；
3. 扫描与计数口径一致：残留目录不进章节列表、不进章节数。
"""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x11" * 2000

SOURCE = "copymanga"
COMIC = "a01_residue_comic"


@pytest.fixture()
def media_tree():
    """造一部漫画：1 个正常章节 + 1 个 .repair_old 崩溃残留（含图片）"""
    from engine.config import MANGA_DOWNLOADS_DIR
    base = os.path.join(MANGA_DOWNLOADS_DIR, SOURCE, COMIC)
    shutil.rmtree(base, ignore_errors=True)
    for sub in ("ch001", "ch002.repair_old"):
        d = os.path.join(base, sub)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "0000.webp"), "wb") as f:
            f.write(_WEBP)
    yield base
    shutil.rmtree(base, ignore_errors=True)


def test_scan_downloaded_chapters_skips_repair_old(media_tree):
    from server.state import _scan_downloaded_chapters
    got = _scan_downloaded_chapters(SOURCE, COMIC)
    assert got == ["ch001"], f"残留 .repair_old 被误识别为章节: {got}"


def test_manga_count_comic_skips_repair_old(media_tree):
    from server.state import _manga_count_comic
    images, chapters = _manga_count_comic(SOURCE, COMIC)
    assert chapters == 1, f"章节数被残留目录抬高: {chapters}"
    assert images == 1, "备份图片不得计入有效图片数"


def test_bak_dir_moved_out_of_media_tree():
    """双保险①：覆盖修复的备份目录在 _repair_staging 下，不与章节同级"""
    import inspect
    import server.manga_api as mapi
    src = inspect.getsource(mapi._repair_overwrite_staged)
    assert '_bak = staging + ".old"' in src, \
        "备份目录应移到 _repair_staging 下（staging + .old）"
    assert '_dir + ".repair_old"' not in src, \
        "备份目录不得再与章节目录同级"
    # 修复入口清理历史崩溃残留（staging 与 .old 备份都清）
    assert 'staging + ".old"' in src
