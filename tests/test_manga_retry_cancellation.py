"""A user pause during network failure must stop further image retries."""
import io
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from engine.manga import downloader as module


class Adapter:
    key = "retry-cancellation"
    concurrent = 1

    def __init__(self):
        self.notes = []

    def image_headers(self, url):
        return {}

    def image_url(self, url):
        return url

    def note_image_result(self, url, seconds, ok):
        self.notes.append(ok)


def image_response():
    stream = io.BytesIO()
    Image.new("RGB", (1200, 1800), (40, 80, 120)).save(stream, format="JPEG")
    return SimpleNamespace(status_code=200, headers={"Content-Type": "image/jpeg"},
                           content=stream.getvalue())


@pytest.mark.parametrize("successful_response", [False, True])
def test_cancellation_during_transport_never_retries_or_writes_and_can_resume(
        tmp_path, monkeypatch, successful_response):
    adapter = Adapter()
    downloader = module.ImageDownloader(adapter, str(tmp_path), min_interval=0)
    cancelled = threading.Event()
    calls = []
    response = image_response()

    def transport(*args, **kwargs):
        calls.append(args[0])
        cancelled.set()
        if successful_response:
            return response
        raise ConnectionError("offline fixture")

    monkeypatch.setattr(module, "fetch_image_checked", transport)
    with pytest.raises(module.ImageRequestCancelled):
        downloader.get("https://fixture.example/page.jpg", "comic", "chapter", 0,
                       cancel_check=cancelled.is_set)
    assert len(calls) == 1
    assert not list(tmp_path.rglob("*.jpg"))
    assert adapter.notes == [], "User cancellation is not evidence of a bad source"
    assert downloader._flight == {}, "Cancellation must release the single-flight identity"

    cancelled.clear()
    monkeypatch.setattr(module, "fetch_image_checked", lambda *a, **kw: response)
    content, path = downloader.get("https://fixture.example/page.jpg", "comic", "chapter", 0,
                                   cancel_check=cancelled.is_set)
    assert content == response.content
    with Image.open(path) as page:
        page.verify()
    assert adapter.notes == [True]


def test_user_pause_interrupts_retry_backoff_without_another_network_attempt(tmp_path, monkeypatch):
    downloader = module.ImageDownloader(Adapter(), str(tmp_path), min_interval=0)
    cancelled = threading.Event()
    backoff = threading.Event()
    calls, errors = [], []
    original_wait = module._wait_image_retry

    def fail(*args, **kwargs):
        calls.append(args[0])
        raise ConnectionError("offline fixture")

    def wait(seconds, check):
        backoff.set()
        original_wait(seconds, check)

    def run():
        try:
            downloader.get("https://fixture.example/page.jpg", "comic", "chapter", 0,
                           cancel_check=cancelled.is_set)
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(module, "fetch_image_checked", fail)
    monkeypatch.setattr(module, "_wait_image_retry", wait)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert backoff.wait(2)
        cancelled.set()
        worker.join(1)
        assert not worker.is_alive(), "Pause must not wait for the entire 1.5s backoff"
        assert len(calls) == 1
        assert len(errors) == 1 and isinstance(errors[0], module.ImageRequestCancelled)
        assert not list(tmp_path.rglob("*.jpg"))
    finally:
        cancelled.set()
        worker.join(3)


def test_uncancelled_requests_keep_existing_three_attempt_policy(tmp_path, monkeypatch):
    downloader = module.ImageDownloader(Adapter(), str(tmp_path), min_interval=0)
    calls, sleeps = [], []

    def fail(*args, **kwargs):
        calls.append(args[0])
        raise ConnectionError("offline fixture")

    monkeypatch.setattr(module, "fetch_image_checked", fail)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    with pytest.raises(module.MangaError, match="offline fixture"):
        downloader.get("https://fixture.example/page.jpg", "comic", "chapter", 0)
    assert len(calls) == 3
    assert sleeps == [1.5, 3.0]
