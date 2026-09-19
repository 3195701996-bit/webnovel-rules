# -*- coding: utf-8 -*-
"""本地写入失败必须与网络失败区分（离线，不触网）

实测（2026-09-15）：把已下载目录 chmod 500 后再续传，**任务长时间停在 running**——
每张图都按"网络临时失败"重试，用户看到的是"一直在下载"，而真实原因是写不进去。
现在：`atomic_write_image` 的 OSError → StorageWriteError（不重试），
下载管理器立刻把任务置为 error 并给出可读原因。
"""
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.manga.downloader import (  # noqa: E402
    ImageDownloader, StorageWriteError,
)


class _FakeAdapter:
    """最小适配器：图片头/URL 直通，不改写"""

    key = "fake"

    def image_headers(self, url):
        return {}

    def image_url(self, url):
        return url


def test_unwritable_dir_raises_storage_write_error(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    (root / "comic" / "ch1").mkdir(parents=True)
    ad = _FakeAdapter()
    dl = ImageDownloader(ad, str(root), min_interval=0.0, concurrency=1)

    jpeg = b"\xff\xd8\xff" + b"x" * 4096

    class _Resp:
        status_code = 200
        headers = {"Content-Type": "image/jpeg"}
        content = jpeg

    # 让网络层"成功返回"，失败只在落盘这一步
    monkeypatch.setattr("engine.manga.downloader.fetch_image_checked",
                        lambda url, headers, timeout=20, **kw: _Resp())

    # 目录必须取自下载器自己的 cache_path（实测它的结构是 <root>/<章节>/，
    # 不含 comic 层——手写路径会 chmod 错目录，测试变成假通过）
    import pathlib
    cp = dl.cache_path("comic", "ch1", 0, ".jpg")
    d = pathlib.Path(cp).parent
    os.chmod(d, 0o500)          # 不可写
    try:
        with pytest.raises(StorageWriteError) as ei:
            dl.get("https://example.com/1.jpg", "comic", "ch1", 0)
        assert "本地写入失败" in str(ei.value)
    finally:
        os.chmod(d, 0o700)

    # 不该留下半截文件
    assert not list(d.glob("*.tmp")), "写入失败不得留下 .tmp 残留文件"


def test_writable_dir_still_works(tmp_path, monkeypatch):
    """反向对照：目录可写时照常成功（别把正常路径改坏）"""
    root = tmp_path / "downloads"
    (root / "comic" / "ch1").mkdir(parents=True)
    ad = _FakeAdapter()
    dl = ImageDownloader(ad, str(root), min_interval=0.0, concurrency=1)
    jpeg = b"\xff\xd8\xff" + b"x" * 4096

    class _Resp:
        status_code = 200
        headers = {"Content-Type": "image/jpeg"}
        content = jpeg

    monkeypatch.setattr("engine.manga.downloader.fetch_image_checked",
                        lambda url, headers, timeout=20, **kw: _Resp())
    data, cp = dl.get("https://example.com/1.jpg", "comic", "ch1", 0)
    assert data == jpeg and os.path.exists(cp)


def test_progress_is_persisted_while_running():
    """下载过程中进度要**定期落盘**：系统强杀后恢复出来的进度不能是 0

    2026-09-15 设备实测：强杀时本地已有 40 张，但 _tasks.json 里
    images_done/images_total 仍是 0/0（进度只在章末落盘，单章任务永远等不到）。
    任务虽然能装载、能续传，但用户看到的是"0 张"，与磁盘事实不符。
    """
    import json
    import os
    import tempfile
    from engine.manga.download_manager import DownloadManager

    d = tempfile.mkdtemp()
    sf = os.path.join(d, "_tasks.json")
    m = DownloadManager(state_file=sf)
    key = "mangadex:progress"
    with m._lock:
        m._tasks[key] = {"status": "running", "source": "mangadex",
                         "comic_id": "progress", "images_done": 0,
                         "images_total": 83, "chapters": [], "paused": False}
    # 第一次调用立刻落盘（节流窗口内第二次不写）
    m._save_throttled(min_interval=5.0)
    assert os.path.exists(sf), "进度必须落盘"
    with m._lock:
        m._tasks[key]["images_done"] = 7
    m._save_throttled(min_interval=0.0)          # 关掉节流，验证写入内容
    data = json.load(open(sf, encoding="utf-8"))
    assert data[key]["images_done"] == 7
    assert data[key]["images_total"] == 83
