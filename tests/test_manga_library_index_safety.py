# -*- coding: utf-8 -*-
"""书库索引损坏时保护已下载媒体和旧漫画缓存。"""
import json
import time
import uuid
from types import SimpleNamespace

import pytest

from engine.manga.download_manager import DownloadManager, MangaLibraryWriteError


def _writer(path):
    return DownloadManager().configure(library_file=str(path))


def _write_one(manager):
    manager._write_library(
        SimpleNamespace(name="测试源"), "source:comic-new", "新作品", "",
        total=3, images=20,
    )


def test_corrupt_library_without_valid_backup_is_preserved_and_rejected(tmp_path):
    path = tmp_path / "_library.json"
    raw = b'{"partial":\xff'
    path.write_bytes(raw)

    with pytest.raises(MangaLibraryWriteError, match="没有可用备份"):
        _write_one(_writer(path))

    assert path.read_bytes() == raw
    copies = list(tmp_path.glob("_library.json.corrupt*"))
    assert len(copies) == 1
    assert copies[0].read_bytes() == raw
    assert not path.with_suffix(".json.bak").exists()


def test_corrupt_library_recovers_from_valid_backup_before_appending(tmp_path):
    path = tmp_path / "_library.json"
    old = [{"source": "source", "comic_id": "old", "title": "旧作品",
            "status": "done", "images": 12, "chapters": 2}]
    backup = path.with_suffix(".json.bak")
    backup.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    raw = b"truncated library"
    path.write_bytes(raw)

    _write_one(_writer(path))

    records = json.loads(path.read_text(encoding="utf-8"))
    assert [(x["source"], x["comic_id"]) for x in records] == [
        ("source", "old"), ("source", "comic-new")]
    assert json.loads(backup.read_text(encoding="utf-8")) == old
    assert (tmp_path / "_library.json.corrupt").read_bytes() == raw


def test_cache_clean_skips_manga_cache_when_library_index_is_unreadable(
        tmp_path, monkeypatch):
    import server.state as state

    manga_cache = tmp_path / "manga-cache"
    protected_legacy = manga_cache / "copymanga" / "old-downloaded-comic" / "chapter"
    protected_legacy.mkdir(parents=True)
    image = protected_legacy / "0000.jpg"
    image.write_bytes(b"legacy user media")
    library = tmp_path / "_library.json"
    library.write_bytes(b"broken library index")
    data = tmp_path / "data"
    data.mkdir()

    monkeypatch.setattr(state, "MANGA_CACHE_DIR", str(manga_cache))
    monkeypatch.setattr(state, "MANGA_LIBRARY_FILE", str(library))
    monkeypatch.setattr(state, "SEARCH_CACHE_FILE", str(tmp_path / "search.json"))
    monkeypatch.setattr(state, "TOC_CACHE_FILE", str(tmp_path / "toc.json"))
    monkeypatch.setattr(state, "DATA_DIR", str(data))

    _freed, cleaned = state._clean_caches()

    assert image.read_bytes() == b"legacy user media"
    assert any("已安全跳过" in name and size == 0 for name, size in cleaned)


@pytest.mark.parametrize("raw", [b"{truncated", b"{}", b"[null]"])
def test_library_api_reports_corruption_instead_of_empty_shelf(
        tmp_path, monkeypatch, raw):
    import app
    import server.manga_api as mapi

    index = tmp_path / "_library.json"
    index.write_bytes(raw)
    monkeypatch.setattr(mapi, "MANGA_LIBRARY_FILE", str(index))

    response = app.app.test_client().get("/api/manga/library")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["recoverable"] is True
    assert "无法读取" in payload["error"]
    assert payload["comics"] == []
    assert index.read_bytes() == raw


@pytest.mark.parametrize("raw", [b"{truncated", b"[]", b"null"])
def test_history_api_reports_invalid_root_without_replacing_original(
        tmp_path, monkeypatch, raw):
    import app
    import server.manga_api as mapi

    history = tmp_path / "_history.json"
    history.write_bytes(raw)
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history))

    response = app.app.test_client().get("/api/manga/history")

    assert response.status_code == 503
    assert response.get_json()["recoverable"] is True
    assert history.read_bytes() == raw


@pytest.mark.parametrize("which,raw", [
    ("favorites", b"[]"), ("history", b"null")])
def test_favorites_api_refuses_to_report_unread_from_invalid_identity_store(
        tmp_path, monkeypatch, which, raw):
    import app
    import server.manga_api as mapi

    favorites = tmp_path / "_favorites.json"
    history = tmp_path / "_history.json"
    favorites.write_text("{}", encoding="utf-8")
    history.write_text("{}", encoding="utf-8")
    target = favorites if which == "favorites" else history
    target.write_bytes(raw)
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(favorites))
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history))

    response = app.app.test_client().get("/api/manga/favorites")

    assert response.status_code == 503
    assert response.get_json()["recoverable"] is True
    assert target.read_bytes() == raw


@pytest.mark.parametrize("raw", [b"{truncated", b"[]"])
def test_favorite_update_check_refuses_corrupt_store_without_rewriting(
        tmp_path, monkeypatch, raw):
    import app
    import server.manga_api as mapi

    favorites = tmp_path / "_favorites.json"
    favorites.write_bytes(raw)
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(favorites))

    response = app.app.test_client().post("/api/manga/favorites/check-updates")

    assert response.status_code == 503
    assert response.get_json()["recoverable"] is True
    assert favorites.read_bytes() == raw


def test_favorite_update_check_does_not_turn_corrupt_history_into_unread_baseline(
        tmp_path, monkeypatch):
    import app
    import server.manga_api as mapi

    favorites = tmp_path / "_favorites.json"
    original_favorites = json.dumps({"mangadex:comic": {
        "title": "损坏历史保护", "unread_count": 3,
    }}, ensure_ascii=False).encode("utf-8")
    favorites.write_bytes(original_favorites)
    history = tmp_path / "_history.json"
    original_history = b"{truncated history"
    history.write_bytes(original_history)
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(favorites))
    monkeypatch.setattr(mapi, "MANGA_HISTORY_FILE", str(history))
    monkeypatch.setattr(mapi, "MANGA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(mapi, "_manga_favorites_check_state", {
        "running": False, "total": 0, "checked": 0, "succeeded": 0,
        "failed": 0, "started_at": 0.0, "finished_at": 0.0,
    })
    requested_sources = []
    monkeypatch.setattr(mapi, "_manga_check_one", lambda *args, **kwargs:
                        requested_sources.append(args) or {"ok": True})

    response = app.app.test_client().post("/api/manga/favorites/check-updates")
    assert response.status_code == 200
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = app.app.test_client().get(
            "/api/manga/favorites/check-updates/status").get_json()
        if not status["running"]:
            break
        time.sleep(0.01)

    assert not requested_sources, "corrupt reading history must stop before source requests"
    assert status["failed"] == 1 and status["succeeded"] == 0
    assert favorites.read_bytes() == original_favorites
    assert history.read_bytes() == original_history


def test_empty_favorites_update_check_completes_without_source_requests(
        tmp_path, monkeypatch):
    import app
    import server.manga_api as mapi

    favorites = tmp_path / "_favorites.json"
    favorites.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_FAV_FILE", str(favorites))
    requested_sources = []
    monkeypatch.setattr(mapi, "_manga_check_one", lambda *args, **kwargs:
                        requested_sources.append(args) or {"ok": True})

    response = app.app.test_client().post("/api/manga/favorites/check-updates")

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True, "started": 0, "already_running": False,
    }
    assert not requested_sources, "empty favorites must never schedule a source request"


def test_shelf_removal_updates_recovery_generation_without_resurrection(
        tmp_path, monkeypatch):
    import app
    import server.manga_api as mapi

    removed_id = str(uuid.uuid4())
    primary = tmp_path / "_library.json"
    backup = tmp_path / "_library.json.bak"
    records = [
        {"source": "mangadex", "comic_id": removed_id, "title": "移除作品"},
        {"source": "mangadex", "comic_id": "preserve-me", "title": "保留作品"},
    ]
    encoded = json.dumps(records, ensure_ascii=False)
    primary.write_text(encoded, encoding="utf-8")
    backup.write_text(encoded, encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_LIBRARY_FILE", str(primary))
    monkeypatch.setattr(mapi, "_manga_stats_note_change", lambda *args, **kwargs: None)

    response = app.app.test_client().delete(
        f"/api/manga/library/mangadex/{removed_id}")

    assert response.status_code == 200
    expected = [records[1]]
    assert json.loads(primary.read_text(encoding="utf-8")) == expected
    assert json.loads(backup.read_text(encoding="utf-8")) == expected

    # A later writer may need the backup if the primary becomes unreadable.
    primary.write_bytes(b"truncated library")
    writer = _writer(primary)
    writer._write_library(SimpleNamespace(name="新源"), "mangadex:new", "新作品",
                          "", total=1, images=1)
    recovered = json.loads(primary.read_text(encoding="utf-8"))
    identities = {(row["source"], row["comic_id"]) for row in recovered}
    assert ("mangadex", removed_id) not in identities
    assert ("mangadex", "preserve-me") in identities


def test_shelf_removal_refuses_to_overwrite_corrupt_library(tmp_path, monkeypatch):
    import app
    import server.manga_api as mapi

    comic_id = str(uuid.uuid4())
    primary = tmp_path / "_library.json"
    backup = tmp_path / "_library.json.bak"
    original = b"{broken user index"
    valid_backup = [{"source": "mangadex", "comic_id": comic_id, "title": "旧作品"}]
    primary.write_bytes(original)
    backup.write_text(json.dumps(valid_backup), encoding="utf-8")
    monkeypatch.setattr(mapi, "MANGA_LIBRARY_FILE", str(primary))

    response = app.app.test_client().delete(
        f"/api/manga/library/mangadex/{comic_id}")

    assert response.status_code == 503
    assert response.get_json()["recoverable"] is True
    assert primary.read_bytes() == original
    assert json.loads(backup.read_text(encoding="utf-8")) == valid_backup
