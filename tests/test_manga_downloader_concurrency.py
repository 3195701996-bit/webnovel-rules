# -*- coding: utf-8 -*-
"""图片下载器必须尊重调用方配置的并发上限（纯离线）。"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.manga.downloader import ImageDownloader  # noqa: E402
from engine.manga.downloader import _SourceRequestGate  # noqa: E402
from engine.manga.downloader import _source_request_gate  # noqa: E402


class _Adapter:
    key = "concurrency-test"
    concurrent = 4


@pytest.mark.parametrize(("concurrency", "second_may_enter"), [(1, False), (2, True)])
def test_explicit_concurrency_is_an_actual_inflight_limit(
        tmp_path, concurrency, second_may_enter):
    downloader = ImageDownloader(_Adapter(), str(tmp_path),
                                 concurrency=concurrency, min_interval=0)
    lock = threading.Lock()
    release = threading.Event()
    entered = [threading.Event(), threading.Event()]
    active = 0
    peak = 0

    def fake_download(url, path, timeout, chapter_id, orig_url):
        nonlocal active, peak
        slot = 0 if url.endswith("1.jpg") else 1
        with lock:
            active += 1
            peak = max(peak, active)
            entered[slot].set()
        try:
            assert release.wait(3), "test did not release in-flight requests"
            return b"image", path
        finally:
            with lock:
                active -= 1

    downloader._download = fake_download
    errors = []

    def fetch(index):
        try:
            downloader.get(f"https://example.test/{index}.jpg", "comic",
                           f"chapter-{index}", index)
        except Exception as exc:  # surfaced below in the test thread
            errors.append(exc)

    threads = [threading.Thread(target=fetch, args=(i,)) for i in (1, 2)]
    try:
        threads[0].start()
        assert entered[0].wait(1), "first request never entered downloader"
        threads[1].start()
        observed_second = entered[1].wait(0.2)
        assert observed_second is second_may_enter
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(2)

    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert peak == concurrency


def test_source_gate_is_shared_across_independent_downloaders():
    gate = _SourceRequestGate(capacity=2)
    lock = threading.Lock()
    release = threading.Event()
    entered = [threading.Event() for _ in range(3)]
    active = 0
    peak = 0

    def request(slot):
        nonlocal active, peak
        assert gate.acquire(), "request never acquired source gate"
        try:
            with lock:
                active += 1
                peak = max(peak, active)
                entered[slot].set()
            assert release.wait(3), "test did not release source gate"
        finally:
            with lock:
                active -= 1
            gate.release()

    threads = [threading.Thread(target=request, args=(i,)) for i in range(3)]
    try:
        for thread in threads:
            thread.start()
        assert entered[0].wait(1)
        assert entered[1].wait(1)
        assert not entered[2].wait(0.15), "source gate capacity was exceeded"
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(2)

    assert not any(thread.is_alive() for thread in threads)
    assert peak == 2


def test_copymanga_transport_aliases_share_one_source_gate():
    assert _source_request_gate("copymanga") is \
        _source_request_gate("copymanga_web")


def test_source_request_spacing_is_shared_across_budget_users(monkeypatch):
    monkeypatch.setenv("WR_MANGA_DL_INTERVAL", "0.04")
    gate = _SourceRequestGate(capacity=2, min_interval=0.04)
    started = []

    def request():
        assert gate.acquire()
        started.append(time.monotonic())
        gate.release()

    first = threading.Thread(target=request)
    second = threading.Thread(target=request)
    first.start()
    first.join(1)
    second.start()
    second.join(1)

    assert not first.is_alive() and not second.is_alive()
    assert len(started) == 2
    assert started[1] - started[0] >= 0.035


def test_source_budget_can_be_released_after_cancelled_waiter():
    gate = _SourceRequestGate(capacity=1)
    assert gate.acquire()
    cancelled = threading.Event()
    exited = threading.Event()

    def wait_for_budget():
        try:
            gate.acquire(cancel_check=cancelled.is_set)
        finally:
            exited.set()

    waiter = threading.Thread(target=wait_for_budget)
    waiter.start()
    cancelled.set()
    assert exited.wait(1), "cancelled waiter did not exit promptly"
    gate.release()
    waiter.join(1)
    assert not waiter.is_alive()
    assert gate.acquire()
    gate.release()


def test_independent_comic_downloaders_share_one_source_connection_cap(
        tmp_path, monkeypatch):
    """Per-comic cache/downloaders must not multiply one source's socket budget."""
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    import engine.manga.downloader as dl_mod

    class SharedAdapter:
        key = "cross-comic-source-gate-test"
        concurrent = 12

        def image_headers(self, _url):
            return {}

        def image_url(self, url):
            return url

        def on_image_failed(self, _url):
            return None

    lock = threading.Lock()
    active = 0
    peak = 0
    payload = b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 600

    def fetch(*_args, **_kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.025)
        with lock:
            active -= 1
        return SimpleNamespace(status_code=200,
                               headers={"Content-Type": "image/webp"},
                               content=payload)

    monkeypatch.setattr(dl_mod, "fetch_image_checked", fetch)
    adapter = SharedAdapter()
    downloaders = [ImageDownloader(adapter, str(tmp_path / f"comic-{i}"),
                                   concurrency=12, min_interval=0)
                   for i in range(3)]

    def get_one(i):
        owner = i % len(downloaders)
        return downloaders[owner].get(
            f"https://cdn.example/{i}.webp", f"comic-{owner}", "chapter", i)

    with ThreadPoolExecutor(max_workers=15) as pool:
        results = list(pool.map(get_one, range(15)))
    assert len(results) == 15
    assert 1 < peak <= 8, f"same-source aggregate concurrency escaped cap: {peak}"


def test_direct_checked_image_requests_share_source_gate(monkeypatch):
    """Covers cover/repair/self-check GETs that don't go through ImageDownloader."""
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    import engine.manga.downloader as dl_mod

    monkeypatch.setenv("WR_MANGA_DL_INTERVAL", "0")
    lock = threading.Lock()
    release = threading.Event()
    entered = threading.Event()
    active = 0
    peak = 0

    def fake_fetch(*_args, **_kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active >= 8:
                entered.set()
        assert release.wait(3), "test did not release direct source requests"
        with lock:
            active -= 1
        return SimpleNamespace(status_code=200, content=b"ok")

    monkeypatch.setattr(dl_mod, "_fetch_image_checked_unlimited", fake_fetch)
    source = "direct-shared-gate-test"
    sources = [source] * 6 + [source] * 6
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(dl_mod.fetch_image_checked, "https://cdn.test/i",
                               {}, 1, None, source=src)
                   for src in sources]
        assert entered.wait(1), "direct requests never filled the shared gate"
        time.sleep(0.05)
        assert peak == 8
        release.set()
        responses = [future.result(timeout=2) for future in futures]
    assert len(responses) == 12
    assert peak == 8


def test_source_gate_admits_interactive_waiter_before_prefetch():
    gate = _SourceRequestGate(capacity=1)
    assert gate.acquire("interactive")
    order = []

    def waiter(priority):
        assert gate.acquire(priority)
        order.append(priority)
        gate.release()

    prefetch = threading.Thread(target=waiter, args=("prefetch",))
    prefetch.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with gate._condition:
            if gate._waiting["prefetch"]:
                break
        time.sleep(0.005)
    interactive = threading.Thread(target=waiter, args=("interactive",))
    interactive.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with gate._condition:
            if gate._waiting["interactive"]:
                break
        time.sleep(0.005)
    gate.release()
    interactive.join(1)
    prefetch.join(1)
    assert not interactive.is_alive() and not prefetch.is_alive()
    assert order == ["interactive", "prefetch"]


def test_source_gate_cancels_queued_prefetch_without_leaking_slot():
    gate = _SourceRequestGate(capacity=1)
    assert gate.acquire("interactive")
    cancelled = threading.Event()
    result = []
    waiter = threading.Thread(target=lambda: result.append(
        gate.acquire("prefetch", cancelled.is_set)))
    waiter.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with gate._condition:
            if gate._waiting["prefetch"]:
                break
        time.sleep(0.005)
    cancelled.set()
    waiter.join(1)
    gate.release()
    assert result == [False]
    assert gate.acquire("interactive")
    gate.release()
