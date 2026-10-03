# -*- coding: utf-8 -*-
"""漫画下载：**用户选的话不能被"整卷规则"吃掉**（0.74.12 实测缺陷）。

## 缺陷

worker 里有一条"纯整卷合集不下载"的规则（名字整名匹配"第N卷/Vol.N"），
它的前提是"APP 的 chapter2 接口对整卷返回 null"。实测《巨人》(jurenmeiman)
的 5 章**全都叫"第01卷…第05卷"**，于是：

- **整本下载**：5 章全被滤掉 → total=0 → 任务直接 `error`；
- **用户明确选了某一卷**：也被滤掉 → 同样 error，用户完全无从理解；
- 报的还是**错误的归因**："源可能正在风控或该漫画已下架"——
  其实是我们自己按规则跳过的（指南明令禁止这种误报）。

修法（两处）：
1. **用户明确选的话不做排除**（他点了就是要下）；
2. 滤完一章不剩时，**不再谎称风控/下架**，而是说清真因并给出下一步。

## 本文件锁死这两条（用假适配器 + 假图片下载器，全离线）
"""
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.manga.base import Chapter, ComicDetails  # noqa: E402


class _VolAdapter(object):
    """章节名全为"第0N卷"的源（复刻 jurenmeiman 的真实形态）"""
    key = "demovol"
    name = "演示卷源"
    concurrent = 2

    def __init__(self, *a, **k):
        pass

    def _chs(self):
        return [Chapter(id="v1", name="第01卷", group="默認"),
                Chapter(id="v2", name="第02卷", group="默認")]

    def comic_info(self, cid):
        return ComicDetails(id=cid, title="卷型漫画", cover="", author="作者",
                            chapters=self._chs())

    def chapters(self, cid):
        return self._chs()

    def fetch_chapters(self, cid):
        return self._chs()

    def images(self, cid, chid):
        return ["https://img.example/%s/1.jpg" % chid]

    def image_headers(self, url):
        return {}

    def image_url(self, url):
        return url


class _FakeImageDownloader(object):
    """假图片下载器：写一个合法 JPEG 头的小文件，返回 (bytes, path)"""

    def __init__(self, adapter, cache_root, *a, **k):
        self.cache_root = cache_root

    def get(self, image_url, comic_id, chapter_id, idx, timeout=20, **kwargs):
        d = os.path.join(self.cache_root, chapter_id)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "%04d.webp" % idx)
        data = b"\xff\xd8\xff\xe0" + b"x" * 2000
        with open(p, "wb") as f:
            f.write(data)
        return data, p


# ── 决策函数（政策本身，纯函数、全离线）──────────────────────────────
def _ch(name, cid="x"):
    return {"id": cid, "name": name}


def test_user_selection_wins_over_volume_rule():
    """**用户明确选了话 → 不剔除**（旧行为：选了也被滤掉 → total=0 → 任务 error）"""
    from engine.manga.download_manager import filter_volume_only
    chs = [_ch("第01卷", "v1"), _ch("第02卷", "v2")]
    kept, note = filter_volume_only(chs, sel_chapters=["v1"])
    assert [c["id"] for c in kept] == ["v1", "v2"], kept
    assert note == ""


def test_all_volumes_are_kept_not_emptied():
    """整本都是整卷条目 → **一章都不能少**（否则该作品完全下载不了）"""
    from engine.manga.download_manager import filter_volume_only
    chs = [_ch("第01卷", "v1"), _ch("第二卷", "v2"), _ch("Vol.3", "v3")]
    kept, note = filter_volume_only(chs, sel_chapters=None)
    assert len(kept) == 3, kept
    assert note == ""
    assert "风控" not in note and "下架" not in note, (
        "绝不能把'我们自己跳过'说成'源在风控/已下架'：%s" % note)


def test_mixed_list_keeps_volumes_before_single_chapters_for_download():
    """卷式前段与单话后段混合时，下载必须覆盖两种阅读单元。"""
    from engine.manga.download_manager import filter_volume_only
    chs = [_ch("第01卷", "v1"), _ch("第1话 开端", "c1"), _ch("01卷番外", "e1")]
    kept, note = filter_volume_only(chs, sel_chapters=None)
    ids = [c["id"] for c in kept]
    assert ids == ["v1", "c1", "e1"], "卷和单话均须下载：%s" % ids
    assert note == ""


def test_worker_downloads_volume_and_episode_from_mixed_catalog(
        tmp_path, monkeypatch):
    """目录含卷和单话时，二者都应真正经过 worker 并写入缓存。"""
    from engine.manga import download_manager as manager_module
    from engine.manga import downloader as downloader_module
    import server.state as manga_state

    source, comic_id = "demovol", "mixed-comic"
    root = tmp_path / "downloads"
    monkeypatch.setattr(manga_state, "MANGA_DOWNLOADS_DIR", str(root))
    monkeypatch.setattr(manga_state, "MANGA_CACHE_DIR", str(tmp_path / "cache"))

    class _MixedAdapter(_VolAdapter):
        def _chs(self):
            return [Chapter(id="v1", name="第01卷", group="默認"),
                    Chapter(id="c1", name="第1话 开端", group="默認")]

        def comic_info(self, cid):
            return ComicDetails(id=cid, title="卷话混合漫画", cover="",
                                chapters=self._chs())

    manager = manager_module.DownloadManager(
        state_file=str(tmp_path / "tasks.json"))
    manager.configure(
        downloads_root=str(root), state_dir=str(tmp_path / "state"),
        library_file=str(tmp_path / "library.json"))
    manager._adapter_pool = {source: _MixedAdapter()}
    key = f"{source}:{comic_id}"
    manager._tasks[key] = {
        "status": "running", "source": source, "comic_id": comic_id,
        "title": "卷话混合漫画", "cover": "", "chapters": None,
        "images_done": 0, "images_total": 0, "paused": False,
    }
    manager._active = 1
    manager._worker_alive.add(key)
    requested_ids = []

    class _RecordingDownloader(_FakeImageDownloader):
        def __init__(self, adapter, cache_root, *args, **kwargs):
            super().__init__(adapter, cache_root, *args, **kwargs)
            self._requested_root = cache_root

        def get(self, image_url, comic_id, chapter_id, idx, **kwargs):
            requested_ids.append(chapter_id)
            return super().get(image_url, comic_id, chapter_id, idx, **kwargs)

    monkeypatch.setattr(downloader_module, "ImageDownloader",
                        _RecordingDownloader)
    monkeypatch.setattr(manager_module, "images_resolver",
                        lambda _adapter, _source, _comic, chapter:
                        [f"https://img.example/{chapter}/1.jpg"])

    manager._worker(key)

    task = manager.status(key)
    assert task["status"] == "done"
    assert task["total"] == 2
    assert task["images_done"] == 2
    assert sorted(requested_ids) == ["c1", "v1"]
    cache_root = root / source / comic_id
    assert (cache_root / "v1" / "0000.webp").is_file()
    assert (cache_root / "c1" / "0000.webp").is_file()
    with (cache_root / "_info.json").open(encoding="utf-8") as stream:
        manifest = json.load(stream)
    assert {row["id"]: row["download_page_count"]
            for row in manifest["chapters"]} == {"v1": 1, "c1": 1}
    assert set(manga_state._scan_downloaded_chapters(source, comic_id)) == {
        "v1", "c1"}


def test_worker_rechecks_and_fills_only_missing_pages_across_image_formats(
        tmp_path, monkeypatch):
    """Failed-chapter recovery must key on valid page indices, not file count."""
    from engine.manga import download_manager as manager_module
    from engine.manga import downloader as downloader_module

    source, comic_id = "demovol", "mixed-format-resume-comic"
    root = tmp_path / "downloads"

    class _MixedImageAdapter(_VolAdapter):
        def images(self, _cid, _chapter):
            return [
                "https://img.example/0.avif",
                "https://img.example/1.jpg",
                "https://img.example/2.gif",
            ]

    requested = []

    class _InterruptedDownloader:
        def __init__(self, _adapter, cache_root, *args, **kwargs):
            self.cache_root = cache_root

        def get(self, image_url, _comic, chapter_id, idx, **kwargs):
            requested.append(idx)
            if idx == 1 and requested.count(1) == 1:
                raise RuntimeError("simulated first-attempt network failure")
            extension = os.path.splitext(image_url)[1]
            signatures = {
                ".avif": b"\x00\x00\x00\x18ftypavif" + b"x" * 12,
                ".jpg": b"\xff\xd8\xff" + b"x" * 20,
                ".gif": b"GIF89a" + b"x" * 20,
            }
            directory = os.path.join(self.cache_root, chapter_id)
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, f"{idx:04d}{extension}")
            with open(path, "wb") as image:
                image.write(signatures[extension])
            return signatures[extension], path

    manager = manager_module.DownloadManager(
        state_file=str(tmp_path / "tasks.json"))
    manager.configure(
        downloads_root=str(root), state_dir=str(tmp_path / "state"),
        library_file=str(tmp_path / "library.json"))
    manager._adapter_pool = {source: _MixedImageAdapter()}
    monkeypatch.setattr(downloader_module, "ImageDownloader", _InterruptedDownloader)
    monkeypatch.setattr(manager_module, "images_resolver", None)
    key = f"{source}:{comic_id}"
    manager._tasks[key] = {
        "status": "running", "source": source, "comic_id": comic_id,
        "title": "混合格式续传漫画", "cover": "", "chapters": [
            {"id": "v1", "name": "第01卷", "group": "默认"}],
        "images_done": 0, "images_total": 0, "paused": False,
    }
    manager._active = 1
    manager._worker_alive.add(key)

    manager._worker(key)

    assert manager.status(key)["status"] == "done"
    assert requested == [0, 1, 2, 1], "只应复用已验证页并请求唯一缺页"
    chapter_dir = root / source / comic_id / "v1"
    assert sorted(path.name for path in chapter_dir.iterdir()) == [
        "0000.avif", "0001.jpg", "0002.gif"]


def test_worker_publishes_done_only_after_library_index_is_written(
        tmp_path, monkeypatch):
    """Completion polling must not outrun publication of the library index."""
    from engine.manga import download_manager as manager_module
    from engine.manga import downloader as downloader_module

    source, comic_id = "demovol", "completion-order-comic"
    root = tmp_path / "downloads"
    manager = manager_module.DownloadManager(
        state_file=str(tmp_path / "tasks.json"))
    manager.configure(
        downloads_root=str(root), state_dir=str(tmp_path / "state"),
        library_file=str(tmp_path / "library.json"))
    manager._adapter_pool = {source: _VolAdapter()}
    monkeypatch.setattr(downloader_module, "ImageDownloader", _FakeImageDownloader)
    monkeypatch.setattr(manager_module, "images_resolver",
                        lambda _adapter, _source, _comic, chapter:
                        [f"https://img.example/{chapter}/1.jpg"])
    key = f"{source}:{comic_id}"
    manager._tasks[key] = {
        "status": "running", "source": source, "comic_id": comic_id,
        "title": "完成顺序漫画", "cover": "", "chapters": [
            {"id": "v1", "name": "第01卷", "group": "默认"}],
        "images_done": 0, "images_total": 0, "paused": False,
    }
    manager._active = 1
    manager._worker_alive.add(key)

    entering_write = threading.Event()
    release_write = threading.Event()
    real_write_library = manager._write_library

    def blocked_write_library(*args, **kwargs):
        entering_write.set()
        assert release_write.wait(timeout=5), "test did not release library write"
        return real_write_library(*args, **kwargs)

    monkeypatch.setattr(manager, "_write_library", blocked_write_library)
    worker = threading.Thread(target=manager._worker, args=(key,), daemon=True)
    worker.start()
    try:
        assert entering_write.wait(timeout=5), (
            "worker never reached library publication")
        assert manager.status(key)["status"] == "running"
        assert not (tmp_path / "library.json").exists()
    finally:
        release_write.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert manager.status(key)["status"] == "done"
    library = json.loads((tmp_path / "library.json").read_text(encoding="utf-8"))
    assert any(row["source"] == source and row["comic_id"] == comic_id
               for row in library)


def test_incremental_volume_worker_preserves_existing_page_count(
        tmp_path, monkeypatch):
    """Refreshing the catalog for a new volume must retain old completeness data."""
    from engine.manga import download_manager as manager_module
    from engine.manga import downloader as downloader_module

    source, comic_id = "demovol", "incremental-comic"
    root = tmp_path / "downloads"

    class _IncrementalAdapter(_VolAdapter):
        def _chs(self):
            return [Chapter(id="v1", name="第01卷", group="默认"),
                    Chapter(id="v2", name="第02卷", group="默认")]

    manager = manager_module.DownloadManager(
        state_file=str(tmp_path / "tasks.json"))
    manager.configure(
        downloads_root=str(root), state_dir=str(tmp_path / "state"),
        library_file=str(tmp_path / "library.json"))
    manager._adapter_pool = {source: _IncrementalAdapter()}
    monkeypatch.setattr(downloader_module, "ImageDownloader", _FakeImageDownloader)
    monkeypatch.setattr(manager_module, "images_resolver",
                        lambda _adapter, _source, _comic, chapter:
                        [f"https://img.example/{chapter}/1.jpg"])
    key = f"{source}:{comic_id}"

    for chapter_id, chapter_name in (("v1", "第01卷"), ("v2", "第02卷")):
        manager._tasks[key] = {
            "status": "running", "source": source, "comic_id": comic_id,
            "title": "增量卷漫画", "cover": "", "chapters": [
                {"id": chapter_id, "name": chapter_name, "group": "默认"}],
            "images_done": 0, "images_total": 0, "paused": False,
        }
        manager._active = 1
        manager._worker_alive.add(key)
        manager._worker(key)
        assert manager.status(key)["status"] == "done"

    manifest_path = root / source / comic_id / "_info.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert {row["id"]: row.get("download_page_count")
            for row in manifest["chapters"]} == {"v1": 1, "v2": 1}


def test_empty_list_is_untouched():
    from engine.manga.download_manager import filter_volume_only
    kept, note = filter_volume_only([], sel_chapters=None)
    assert kept == [] and note == ""


def test_volume_rule_still_matches_its_own_cases():
    """判据本身不变（R36 已有的用例继续管着它）"""
    from engine.manga.download_manager import _is_volume_only
    assert _is_volume_only("第01卷") is True
    assert _is_volume_only("第一卷") is True
    assert _is_volume_only("01卷番外") is False
    assert _is_volume_only("第1话 开端") is False
