"""Download-worker failures must be actionable without exporting credentials."""
import pytest


@pytest.mark.parametrize("startup_failure", [True, False])
def test_worker_reports_startup_and_execution_failures(tmp_path, monkeypatch, startup_failure):
    from engine.manga import download_manager as module
    from engine.manga import downloader
    from server import diag

    secret = "https://private.example/image?token=private-token-value"

    class Adapter:
        name = "test"

        def comic_info(self, _comic):
            raise RuntimeError(secret)

    class Downloader:
        def __init__(self, *_args, **_kwargs):
            if startup_failure:
                raise RuntimeError(secret)

    manager = module.DownloadManager(state_file=str(tmp_path / "tasks.json"))
    manager.configure(downloads_root=str(tmp_path / "downloads"),
                      state_dir=str(tmp_path / "state"),
                      library_file=str(tmp_path / "library.json"))
    manager._adapter_pool = {"fixture": Adapter()}
    key = "fixture:comic"
    manager._tasks[key] = {"source": "fixture", "comic_id": "comic", "title": "fixture",
                           "status": "running", "chapters": ["chapter"], "paused": False}
    manager._active = 1
    manager._worker_alive.add(key)
    events = []

    def sink(where, exc, msg=None):
        events.append((where, type(exc).__name__, msg))
        diag.record_error(where, exc, msg=msg)

    monkeypatch.setattr(module, "diagnostic_error_hook", sink)
    monkeypatch.setattr(downloader, "ImageDownloader", Downloader)
    diag.reset()
    try:
        manager._worker(key)
        assert manager.status(key)["status"] == "error"
        assert manager._active == 0
        assert len(events) == 1
        where, kind, locations = events[0]
        assert where == "manga.download.worker"
        assert kind in ("RuntimeError", "MangaError")
        assert "_worker:" in locations
        recorded = repr(diag.events())
        assert "manga.download.worker" in recorded
        assert "private.example" not in recorded
        assert "private-token-value" not in recorded
    finally:
        diag.reset()


def test_broken_diagnostic_sink_cannot_change_task_failure_handling(monkeypatch):
    from engine.manga import download_manager as module

    def broken(*_args, **_kwargs):
        raise RuntimeError("sink failure")

    monkeypatch.setattr(module, "diagnostic_error_hook", broken)
    module._report_worker_error(ValueError("original failure"))
