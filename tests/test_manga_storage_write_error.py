# -*- coding: utf-8 -*-
"""本地写入失败必须与网络失败区分（离线，不触网）

实测（2026-09-15）：把已下载目录 chmod 500 后再续传，**任务长时间停在 running**——
每张图都按"网络临时失败"重试，用户看到的是"一直在下载"，而真实原因是写不进去。
现在：`atomic_write_image` 的 OSError → StorageWriteError（不重试），
下载管理器立刻把任务置为 error 并给出可读原因。
"""
import builtins
import errno
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.manga import download_manager as manager_module  # noqa: E402
from engine.manga import downloader as downloader_module  # noqa: E402
from engine.manga.download_manager import DownloadManager  # noqa: E402
from engine.manga.downloader import (  # noqa: E402
    ImageDownloader,
    StorageWriteError,
    atomic_write_image,
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


def test_enospc_is_not_retried_and_preserves_previous_image(tmp_path, monkeypatch):
    """磁盘满发生在原子写入阶段时，不得误当网络错误重试或损坏旧页。"""
    root = tmp_path / "downloads"
    dl = ImageDownloader(_FakeAdapter(), str(root), min_interval=0.0, concurrency=1)
    cp = dl.cache_path("comic", "ch1", 0, ".jpg")
    new_image = b"\xff\xd8\xff" + b"replacement-page" * 100

    class _Response:
        status_code = 200
        headers = {"Content-Type": "image/jpeg"}
        content = new_image

    fetch_calls = []
    monkeypatch.setattr(
        "engine.manga.downloader.fetch_image_checked",
        lambda *args, **kwargs: (fetch_calls.append(args[0]) or _Response()),
    )
    def full_disk(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("engine.manga.downloader.atomic_write_image", full_disk)
    with pytest.raises(StorageWriteError, match="存储不可写或空间不足"):
        dl.get("https://example.com/new.jpg", "comic", "ch1", 0)

    assert len(fetch_calls) == 1, "本地 ENOSPC 不应触发网络重试"
    assert not os.path.exists(cp), "首张图片写入失败时不得伪造缓存文件"


def test_enospc_during_atomic_replace_preserves_previous_image(tmp_path, monkeypatch):
    """临时文件写失败时，旧的完整目标页保留且半截文件清理。"""
    target = tmp_path / "page.jpg"
    old_image = b"\xff\xd8\xff" + b"previous-good-page" * 80
    target.write_bytes(old_image)
    real_open = builtins.open

    class _FullDiskWriter:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def write(self, content):
            with real_open(str(target) + ".tmp", "wb") as stream:
                stream.write(content[:24])
            raise OSError(errno.ENOSPC, "No space left on device")

    def fail_temp_write(path, mode="r", *args, **kwargs):
        if os.fspath(path) == str(target) + ".tmp" and mode == "wb":
            return _FullDiskWriter()
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_temp_write)
    with pytest.raises(OSError) as error:
        atomic_write_image(str(target), b"\xff\xd8\xff" + b"new-page" * 100)
    assert error.value.errno == errno.ENOSPC
    assert target.read_bytes() == old_image
    assert not os.path.exists(str(target) + ".tmp")


def test_download_worker_marks_enospc_as_error_and_keeps_completed_page(
        tmp_path, monkeypatch):
    """ENOSPC must be actionable, preserve old pages, and not fake a full chapter."""
    root = tmp_path / "downloads"
    import server.state as manga_state
    monkeypatch.setattr(manga_state, "MANGA_DOWNLOADS_DIR", str(root))
    monkeypatch.setattr(manga_state, "MANGA_CACHE_DIR", str(tmp_path / "cache"))
    source, comic_id, chapter_id = "selftest", "comic", "chapter-1"
    chapter_dir = root / source / comic_id / chapter_id
    chapter_dir.mkdir(parents=True)
    previous_page = b"\xff\xd8\xff" + b"previous-complete-page" * 80
    (chapter_dir / "0000.jpg").write_bytes(previous_page)
    info_path = root / source / comic_id / "_info.json"
    info_path.write_text(json.dumps({
        "title": "存储不足自检",
        "chapters": [{"id": chapter_id, "name": "第1话", "group": ""}],
    }), encoding="utf-8")

    class _Adapter:
        name = "本地故障注入源"

    class _FailingDownloader:
        def __init__(self, *_args, **_kwargs):
            pass

        def get(self, _url, _comic, _chapter, index, **_kwargs):
            if index == 0:
                return previous_page, str(chapter_dir / "0000.jpg")
            raise StorageWriteError(
                "本地写入失败：No space left on device")

    manager = DownloadManager(state_file=str(tmp_path / "tasks.json"))
    manager.configure(
        downloads_root=str(root),
        state_dir=str(tmp_path / "state"),
        library_file=str(tmp_path / "library.json"),
    )
    manager._adapter_pool = {source: _Adapter()}
    key = f"{source}:{comic_id}"
    manager._tasks[key] = {
        "status": "running", "source": source, "comic_id": comic_id,
        "title": "存储不足自检", "cover": "", "chapters": [
            {"id": chapter_id, "name": "第1话", "group": ""},
        ], "images_done": 0, "images_total": 0, "paused": False,
    }
    manager._active = 1
    manager._worker_alive.add(key)
    monkeypatch.setattr(downloader_module, "ImageDownloader", _FailingDownloader)
    monkeypatch.setattr(manager_module, "images_resolver",
                        lambda *_args: ["page-0.jpg", "page-1.jpg"])

    manager._worker(key)

    task = manager.status(key)
    assert task["status"] == "error"
    assert task["stop_kind"] == "error"
    assert "存储空间不足" in task["error"]
    assert "存储管理" in task["error"]
    assert "释放空间或修复权限后点「继续下载」" in task["error"]
    assert task["stop_reason"] == task["error"]
    assert "已下载的内容不受影响" in task["error"]
    assert (chapter_dir / "0000.jpg").read_bytes() == previous_page
    assert manga_state._scan_downloaded_chapters(source, comic_id) == [], (
        "存储不足后只剩部分页的章节不得被扫描成完整已下载"
    )
    assert manager._active == 0


def test_task_snapshot_reserve_preserves_error_status_when_full(tmp_path, monkeypatch):
    """ENOSPC must free reserved blocks and persist an actionable resumable state."""
    state_file = tmp_path / "_tasks.json"
    manager = DownloadManager(state_file=str(state_file))
    manager._tasks["mangadex:comic"] = {
        "status": "running", "source": "mangadex", "comic_id": "comic",
        "title": "满盘恢复", "chapters": ["chapter-1"],
    }
    manager.save()
    reserve = str(state_file) + ".reserve"
    assert os.path.getsize(reserve) == manager_module._TASK_STATE_EMERGENCY_RESERVE_BYTES
    manager._tasks["mangadex:comic"].update(
        status="error", error="设备存储空间不足，可清理后继续",
        stop_reason="设备存储空间不足，可清理后继续")
    real_atomic_write = manager_module.atomic_write
    fail_state_once = {"armed": True}

    def fail_primary_snapshot_once(path, data):
        if str(path) == str(state_file) and fail_state_once["armed"]:
            fail_state_once["armed"] = False
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_atomic_write(path, data)

    monkeypatch.setattr(manager_module, "atomic_write", fail_primary_snapshot_once)
    manager.save()

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["mangadex:comic"]["status"] == "error"
    assert "设备存储空间不足" in saved["mangadex:comic"]["error"]
    recovered = DownloadManager(state_file=str(state_file))
    recovered.load()
    assert recovered.status("mangadex:comic")["status"] == "error"
    assert os.path.getsize(reserve) == manager_module._TASK_STATE_EMERGENCY_RESERVE_BYTES


def test_load_provisions_reserve_for_preexisting_task_snapshot(tmp_path):
    """Upgrading with an older task file should provision reserve on startup."""
    state_file = tmp_path / "legacy-tasks.json"
    state_file.write_text(json.dumps({
        "mangadex:legacy": {"status": "error", "source": "mangadex",
                            "comic_id": "legacy", "error": "retryable"},
    }), encoding="utf-8")
    manager = DownloadManager(state_file=str(state_file))

    manager.load()

    reserve = str(state_file) + ".reserve"
    assert manager.status("mangadex:legacy")["status"] == "error"
    assert os.path.getsize(reserve) == manager_module._TASK_STATE_EMERGENCY_RESERVE_BYTES


def test_enospc_task_survives_restart_and_resumes_only_missing_page(
        tmp_path, monkeypatch):
    """写盘空间恢复后，重启任务应复用已有页并补齐缺页，而非重下整话。"""
    import server.state as manga_state

    root = tmp_path / "downloads"
    monkeypatch.setattr(manga_state, "MANGA_DOWNLOADS_DIR", str(root))
    monkeypatch.setattr(manga_state, "MANGA_CACHE_DIR", str(tmp_path / "cache"))
    source, comic_id, chapter_id = "selftest", "resume-comic", "chapter-1"
    key = f"{source}:{comic_id}"
    info_path = root / source / comic_id / "_info.json"
    page0 = b"\xff\xd8\xff" + b"page-zero" * 800
    page1 = b"\xff\xd8\xff" + b"page-one" * 800
    fetches = []
    info_path.parent.mkdir(parents=True)
    info_path.write_text(json.dumps({
        "title": "恢复自检",
        "chapters": [{"id": chapter_id, "name": "第1话", "group": ""}],
    }), encoding="utf-8")

    class _Adapter:
        name = "恢复自检源"
        concurrent = 1

        def image_headers(self, _url):
            return {}

        def image_url(self, url):
            return url

    class _Response:
        status_code = 200
        headers = {"Content-Type": "image/jpeg"}

        def __init__(self, content):
            self.content = content

    def fetch(url, _headers, timeout=20, **_kwargs):
        fetches.append(url)
        return _Response(page0 if url.endswith("0.jpg") else page1)

    monkeypatch.setattr("engine.manga.downloader.fetch_image_checked", fetch)
    monkeypatch.setattr(manager_module, "images_resolver",
                        lambda *_args: ["https://images.test/0.jpg",
                                        "https://images.test/1.jpg"])
    real_atomic_write = downloader_module.atomic_write_image
    full_disk_once = {"armed": True}

    def fail_second_page_once(path, content):
        if path.endswith(os.path.join(chapter_id, "0001.jpg")) and full_disk_once["armed"]:
            full_disk_once["armed"] = False
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_atomic_write(path, content)

    monkeypatch.setattr(downloader_module, "atomic_write_image", fail_second_page_once)

    manager = DownloadManager(state_file=str(tmp_path / "tasks.json"))
    manager.configure(
        downloads_root=str(root), state_dir=str(tmp_path / "state"),
        library_file=str(tmp_path / "library.json"),
    )
    manager._adapter_pool = {source: _Adapter()}
    manager._tasks[key] = {
        "status": "running", "source": source, "comic_id": comic_id,
        "title": "恢复自检", "cover": "", "chapters": [chapter_id],
        "images_done": 0, "images_total": 0, "paused": False,
    }
    manager._active = 1
    manager._worker_alive.add(key)

    manager._worker(key)

    assert manager.status(key)["status"] == "error"
    assert manager.status(key)["images_done"] == 1
    assert (root / source / comic_id / chapter_id / "0000.jpg").read_bytes() == page0
    assert not (root / source / comic_id / chapter_id / "0001.jpg").exists()
    assert json.loads(info_path.read_text(encoding="utf-8"))["chapters"][0]["id"] == chapter_id

    # Recreate the manager as a cold process would, recover its persisted task,
    # and suppress scheduling so the test can invoke the real worker deterministically.
    recovered = DownloadManager(state_file=str(tmp_path / "tasks.json"))
    recovered.configure(
        downloads_root=str(root), state_dir=str(tmp_path / "state"),
        library_file=str(tmp_path / "library.json"),
    )
    recovered.load()
    recovered._adapter_pool = {source: _Adapter()}
    monkeypatch.setattr(recovered, "_kick_workers", lambda: None)
    assert recovered.resume_result(key) == "resumed"
    with recovered._lock:
        recovered._tasks[key]["status"] = "running"
        recovered._active = 1
        recovered._worker_alive.add(key)

    recovered._worker(key)

    task = recovered.status(key)
    assert task["status"] == "done"
    assert task["images_done"] == 2
    assert (root / source / comic_id / chapter_id / "0000.jpg").read_bytes() == page0
    assert (root / source / comic_id / chapter_id / "0001.jpg").read_bytes() == page1
    assert fetches.count("https://images.test/0.jpg") == 1, "已落盘的第 0 页不得重复回源"
    assert fetches.count("https://images.test/1.jpg") == 2, "恢复只应补取缺失的第 1 页"
    assert manga_state._scan_downloaded_chapters(source, comic_id) == [chapter_id]


def test_download_speed_sampler_is_process_local_and_cleared(tmp_path):
    """Throughput sampling state must not leak into task JSON or survive workers."""
    manager = DownloadManager(state_file=str(tmp_path / "tasks.json"))
    manager._tasks["selftest:comic"] = {
        "status": "running", "images_done": 0, "images_total": 4,
    }
    manager._speed_samples["selftest:comic"] = {"at": 100.0, "done": 0}

    manager.save()

    saved = json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))
    assert "_speed_sample" not in saved["selftest:comic"]
    assert "speed_samples" not in saved["selftest:comic"]
    manager._speed_samples.pop("selftest:comic", None)
    assert "selftest:comic" not in manager._speed_samples


def test_download_speed_sampler_calculates_live_rate_and_eta():
    from engine.manga.download_manager import _sample_image_speed

    task = {"images_done": 8, "speed": 0, "eta": None}
    sample = {"at": 10.0, "done": 2}

    assert _sample_image_speed(task, sample, images_total=20, now=12.0)
    assert task["speed"] == 3.0
    assert task["eta"] == 4
    assert sample == {"at": 12.0, "done": 8}

    assert not _sample_image_speed(task, sample, images_total=20, now=12.5)
    assert task["speed"] == 3.0


def test_progress_is_persisted_while_running():
    """下载过程中进度要**定期落盘**：系统强杀后恢复出来的进度不能是 0

    2026-09-15 设备实测：强杀时本地已有 40 张，但 _tasks.json 里
    images_done/images_total 仍是 0/0（进度只在章末落盘，单章任务永远等不到）。
    任务虽然能装载、能续传，但用户看到的是"0 张"，与磁盘事实不符。
    """
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


def test_running_task_exposes_exact_current_chapter_id(tmp_path, monkeypatch):
    """详情 UI 可区分任务当前话与同一任务中仍排队的其他话。"""
    import threading
    import time

    import server.state as manga_state

    root = tmp_path / "downloads"
    source, comic_id, chapter_id = "selftest", "current-chapter", "chapter-7"
    key = f"{source}:{comic_id}"
    chapter_root = root / source / comic_id
    chapter_root.mkdir(parents=True)
    info_path = chapter_root / "_info.json"
    info_path.write_text(json.dumps({"title": "当前话状态", "cover": "fixture-cover",
                                     "chapters": [{
        "id": chapter_id, "name": "第7话", "group": "",
    }]}), encoding="utf-8")
    monkeypatch.setattr(manga_state, "MANGA_DOWNLOADS_DIR", str(root))
    monkeypatch.setattr(manga_state, "MANGA_CACHE_DIR", str(tmp_path / "cache"))

    class _Adapter:
        name = "状态自检源"
        concurrent = 1

    entered_resolver = threading.Event()
    release_resolver = threading.Event()

    class _BlockingDownloader:
        def __init__(self, _adapter, cache_root, **_kwargs):
            self.cache_root = cache_root

        def get(self, _url, comic, chapter, index, **_kwargs):
            folder = os.path.join(self.cache_root, chapter)
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"{index:04d}.jpg")
            data = b"\xff\xd8\xff" + b"page" * 1600
            with open(path, "wb") as stream:
                stream.write(data)
            return data, path

    def resolve(_adapter, _source, _comic, _chapter):
        entered_resolver.set()
        assert release_resolver.wait(5), "图片列表桩应能被测试释放"
        return ["page-0"]

    monkeypatch.setattr(downloader_module, "ImageDownloader", _BlockingDownloader)
    monkeypatch.setattr(manager_module, "images_resolver", resolve)
    manager = DownloadManager(state_file=str(tmp_path / "tasks.json"))
    manager.configure(
        downloads_root=str(root),
        state_dir=str(tmp_path / "state"),
        library_file=str(tmp_path / "library.json"),
    )
    manager._adapter_pool = {source: _Adapter()}
    manager._tasks[key] = {
        "status": "running", "source": source, "comic_id": comic_id,
        "title": "当前话状态", "cover": "", "chapters": [
            {"id": chapter_id, "name": "第7话", "group": ""},
        ], "images_done": 0, "images_total": 0, "paused": False,
    }
    manager._active = 1
    manager._worker_alive.add(key)
    worker = threading.Thread(target=manager._worker, args=(key,), daemon=True)
    worker.start()
    try:
        assert entered_resolver.wait(5), "下载 worker 应进入章节图片清单阶段"
        deadline = time.monotonic() + 5
        while (manager.status(key).get("current_chapter_id") != chapter_id
               and time.monotonic() < deadline):
            time.sleep(0.01)
        assert manager.status(key).get("current_chapter_id") == chapter_id
    finally:
        release_resolver.set()
        worker.join(10)
    assert not worker.is_alive(), "下载 worker 应在测试释放后退出"
    assert manager.status(key)["status"] == "done"
    assert manager.status(key).get("current_chapter_id", "") == ""
