"""Management inventories must not weaken the complete-only local reader."""
import json

import pytest


@pytest.fixture
def media_env(tmp_path, monkeypatch):
    import app
    import server.manga_api as api
    import server.state as state
    downloads, cache = tmp_path / "downloads", tmp_path / "cache"
    for module in (api, state):
        monkeypatch.setattr(module, "MANGA_DOWNLOADS_DIR", str(downloads))
        monkeypatch.setattr(module, "MANGA_CACHE_DIR", str(cache))
    def no_adapter(*args, **kwargs):
        raise AssertionError("local media management must not call adapters")
    monkeypatch.setattr(api, "_manga_adapter", no_adapter)
    return app.app.test_client(), downloads, cache


def page(path):
    path.mkdir(parents=True)
    (path / "0000.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 2048)


def test_inventory_includes_partial_volume_without_promoting_it(media_env):
    client, downloads, _ = media_env
    base = downloads / "mangadex" / "local-inventory"
    page(base / "partial-volume")
    page(base / "complete-chapter")
    manifest = base / "_info.json"
    manifest.write_text(json.dumps({"chapters": [
        {"id": "complete-chapter", "name": "第02话", "download_page_count": 1},
        {"id": "partial-volume", "name": "第01卷", "download_page_count": 3},
        {"id": "not-downloaded", "name": "第03话"},
    ]}), encoding="utf-8")
    before = {p: p.read_bytes() for p in base.rglob("*") if p.is_file()}
    response = client.get("/api/manga/mangadex/local-inventory/local-media")
    assert response.status_code == 200
    result = response.get_json()
    assert result["ok"] and result["local_only"] and result["count"] == 2
    assert result["media"] == [
        {"id": "partial-volume", "name": "第01卷", "group": "", "downloaded": False, "partial": True},
        {"id": "complete-chapter", "name": "第02话", "group": "", "downloaded": True, "partial": False},
    ]
    assert {p: p.read_bytes() for p in base.rglob("*") if p.is_file()} == before
    import server.state as state
    assert "partial-volume" not in state._scan_downloaded_chapters("mangadex", "local-inventory")


def test_inventory_excludes_read_only_cache_and_preserves_same_name_ids(media_env):
    client, downloads, cache = media_env
    base = downloads / "copymanga" / "same-name"
    for chapter_id in ("old-id", "new-id"):
        page(base / chapter_id)
    (base / "_info.json").write_text(json.dumps({"chapters": [
        {"id": "old-id", "name": "第41話", "local_archive": True},
        {"id": "new-id", "name": "第41話"},
    ]}), encoding="utf-8")
    page(cache / "copymanga" / "same-name" / "read-only-cache")
    result = client.get("/api/manga/copymanga/same-name/local-media").get_json()
    assert {row["id"] for row in result["media"]} == {"old-id", "new-id"}
    assert all(row["downloaded"] and not row["partial"] for row in result["media"])


def test_empty_inventory_stays_empty_without_network(media_env):
    client, _, _ = media_env
    result = client.get("/api/manga/mangadex/no-media/local-media")
    assert result.status_code == 200
    assert result.get_json()["media"] == []
    assert result.get_json()["count"] == 0


def test_legacy_alias_media_is_visible_with_exact_disk_identity(media_env):
    client, downloads, _ = media_env
    base = downloads / "copymanga_web" / "legacy-local"
    page(base / "legacy-uuid")
    (base / "_info.json").write_text(json.dumps({"chapters": [
        {"id": "legacy-uuid", "name": "第09话"},
    ]}), encoding="utf-8")
    result = client.get("/api/manga/copymanga/legacy-local/local-media").get_json()
    assert result["media"] == [
        {"id": "legacy-uuid", "name": "第09话", "group": "", "downloaded": True, "partial": False},
    ]


def test_broken_manifest_does_not_hide_proven_download_files(media_env):
    client, downloads, _ = media_env
    base = downloads / "mangadex" / "broken-manifest"
    page(base / "known-disk-id")
    (base / "_info.json").write_text("{broken", encoding="utf-8")
    result = client.get("/api/manga/mangadex/broken-manifest/local-media").get_json()
    assert result["media"][0]["id"] == "known-disk-id"
    assert result["media"][0]["name"] == "known-disk-id"
    assert result["media"][0]["downloaded"]
    assert "path" not in result["media"][0]
