"""Real queue/cache/persistence pipeline, with only image transport replaced."""
import io
import json
import time
import threading

import pytest

from PIL import Image

from engine.manga.base import Chapter, ComicDetails
from engine.manga import download_manager as module
from engine.manga.downloader import ImageDownloader, atomic_write_image


def _idle(manager, key, expected):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with manager._lock:
            if manager._tasks[key]["status"] == expected and key not in manager._worker_alive:
                return
        time.sleep(0.01)
    raise AssertionError(manager.status(key))


def test_paused_download_survives_reload_and_fetches_only_missing_or_corrupt_pages(
        tmp_path, monkeypatch):
    monkeypatch.setenv("WR_MANGA_DL_CONCURRENCY", "1")
    monkeypatch.setattr(module, "images_resolver", None)
    monkeypatch.setattr(module, "library_change_hook", None)
    image = io.BytesIO()
    Image.new("RGB", (1200, 1800), (40, 80, 120)).save(image, format="JPEG")
    content = image.getvalue()
    assert len(content) > 4096

    class Adapter:
        key = "resume-pipeline"
        name = "离线续传测试源"
        concurrent = 1

        def comic_info(self, cid):
            return ComicDetails(id=cid, title="续传测试", cover="", author="",
                                chapters=[Chapter(id="ch1", name="第1话")])

        def images(self, cid, chapter):
            return [f"https://fixture.example/{i}.jpg" for i in range(3)]

    state_file = tmp_path / "tasks.json"

    def manager():
        result = module.DownloadManager(state_file=str(state_file))
        result.configure(downloads_root=str(tmp_path / "downloads"),
                         state_dir=str(tmp_path / "state"),
                         cache_root=str(tmp_path / "cache"),
                         library_file=str(tmp_path / "library.json"))
        result._adapter_pool = {Adapter.key: Adapter()}
        return result

    original = manager()
    key = Adapter.key + ":comic"
    requests = []

    def transport(self, url, path, timeout, chapter_id, orig_url):
        requests.append(int(url.rsplit("/", 1)[-1].split(".")[0]))
        if len(requests) == 1:
            assert original.pause(key)
        atomic_write_image(path, content)
        return content, path

    monkeypatch.setattr(ImageDownloader, "_download", transport)
    original.start(Adapter.key, "comic", "续传测试", chapters=["ch1"])
    _idle(original, key, "paused")
    directory = tmp_path / "downloads" / Adapter.key / "comic" / "ch1"
    first = directory / "0000.jpg"
    old_bytes, old_mtime = first.read_bytes(), first.stat().st_mtime_ns
    assert requests == [0]
    assert json.loads(state_file.read_text())[key]["status"] == "paused"
    # A partial/corrupt page must not be reused on recovery.
    (directory / "0001.jpg").write_bytes(b"incomplete-image")

    recovered = manager()
    recovered.load()
    assert recovered.status(key)["status"] == "paused"
    assert recovered.resume_result(key) == "resumed"
    _idle(recovered, key, "done")
    assert requests == [0, 1, 2]
    assert first.read_bytes() == old_bytes and first.stat().st_mtime_ns == old_mtime
    for index in range(3):
        with Image.open(directory / f"{index:04d}.jpg") as page:
            page.verify()
    manifest = json.loads((directory.parent / "_info.json").read_text())
    assert manifest["chapters"][0]["download_page_count"] == 3
    assert recovered.status(key)["images_done"] == 3
    assert recovered._active == 0


@pytest.mark.parametrize("restart", [False, True])
def test_running_download_accepts_new_identity_once_and_preserves_it_after_restart(
        tmp_path, monkeypatch, restart):
    monkeypatch.setenv("WR_MANGA_DL_CONCURRENCY", "1")
    monkeypatch.setattr(module, "images_resolver", None)
    monkeypatch.setattr(module, "library_change_hook", None)
    image = io.BytesIO()
    Image.new("RGB", (1200, 1800), (70, 80, 90)).save(image, format="JPEG")
    content = image.getvalue()
    entered, release = threading.Event(), threading.Event()
    requests, published = [], []
    state_file = tmp_path / "tasks.json"
    key = "append-pipeline:comic"

    class Adapter:
        key = "append-pipeline"
        name = "追加下载测试"
        concurrent = 1

        def comic_info(self, cid):
            return ComicDetails(id=cid, title="追加测试", cover="", author="", chapters=[
                Chapter(id="ch41", name="第41话"), Chapter(id="ch42", name="第42话")])

        def images(self, cid, chapter):
            return [f"https://fixture.example/{chapter}/0.jpg"]

    def manager():
        result = module.DownloadManager(state_file=str(state_file))
        result.configure(downloads_root=str(tmp_path / "downloads"),
                         state_dir=str(tmp_path / "state"),
                         library_file=str(tmp_path / "library.json"))
        result._adapter_pool = {Adapter.key: Adapter()}
        mark = result._mark_done

        def observe(*args, **kwargs):
            mark(*args, **kwargs)
            published.append(result.status(key)["status"])

        result._mark_done = observe
        return result

    def transport(self, url, path, timeout, chapter_id, orig_url):
        requests.append(chapter_id)
        if len(requests) == 1:
            entered.set()
            assert release.wait(5)
        atomic_write_image(path, content)
        return content, path

    monkeypatch.setattr(ImageDownloader, "_download", transport)
    original = manager()
    original.start(Adapter.key, "comic", "追加测试", chapters=["ch41"])
    try:
        assert entered.wait(5)
        assert original.start(Adapter.key, "comic", "追加测试", chapters=["ch42"])[1] == "running"
        assert original.start(Adapter.key, "comic", "追加测试", chapters=["ch42"])[1] == "running"
        assert original.status(key)["chapters"] == ["ch41"]
        assert original.status(key)["pending_chapters"] == ["ch42"]
        assert json.loads(state_file.read_text())[key]["pending_chapters"] == ["ch42"]
        assert original._active == 1
        if restart:
            assert original.pause(key)
    finally:
        release.set()
    if restart:
        _idle(original, key, "paused")
        recovered = manager()
        recovered.load()
        assert recovered.status(key)["pending_chapters"] == ["ch42"]
        assert recovered.resume_result(key) == "resumed"
        _idle(recovered, key, "done")
    else:
        _idle(original, key, "done")
        assert published == ["queued", "done"], "追加批次前不可发布最终完成"
    assert requests == ["ch41", "ch42"], "追加或重启均不得重复回源旧页"
    for chapter in requests:
        path = tmp_path / "downloads" / Adapter.key / "comic" / chapter / "0000.jpg"
        with Image.open(path) as page:
            page.verify()
